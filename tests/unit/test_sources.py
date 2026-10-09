"""Sources that stay current: feeds and pages read again, only what changed written.

The network is a fake: a resolver and a connection answering from a dict, and
a writer that records what it was asked to write. The collection, the REST
routes and the command line are in test_sources_api.py.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import timedelta

import pytest

from vectrixdb._fetch import MAX_DEFER, Fetcher
from vectrixdb.exceptions import ConfigurationError, ExtractionError
from vectrixdb.signin.records import Record, SqlRecords
from vectrixdb.sources import (
    ITEM,
    LEASE,
    SOURCE,
    Feed,
    Item,
    Page,
    Source,
    Sources,
    _expand,
    _kept_address,
    _now_text,
    _scrub,
    every_text,
    forget_collection,
    kept_sources,
    parse_every,
    public,
    register_source,
    restore_sources,
)

feedparser = pytest.importorskip("feedparser")

PUBLIC = "93.184.216.34"
NOW = 1_800_000_000.0


# ============================================================================
# THE FAKES
# ============================================================================


class FakeWeb:
    """Every host resolves to a public address; every address answers from a dict, robots.txt 404 unless given."""

    def __init__(self, pages=None, dns=None):
        self.pages = dict(pages or {})
        self.dns = dict(dns or {})
        self.requests = []

    def resolve(self, host, port):
        return self.dns.get(host, [PUBLIC])

    def open(self, request):
        self.requests.append(request)
        answer = self.pages.get(request.url)
        if answer is None:
            return 404, {"content-type": "text/plain"}, b"not here"
        if callable(answer):
            return answer(request)
        return answer

    @property
    def urls(self):
        return [r.url for r in self.requests if not r.url.endswith("/robots.txt")]


class FakeWriter:
    """Writes into a dict, and remembers every write and delete."""

    def __init__(self, extractors=None):
        self.extractors = extractors
        self.docs = {}
        self.writes = []
        self.deletes = []

    def batch(self):
        return contextlib.nullcontext()

    def write(self, doc, doc_id, metadata, version):
        self.docs[doc_id] = {
            "text": doc.text,
            "metadata": dict(metadata),
            "doc": dict(doc.metadata),
            "version": version,
        }
        self.writes.append(doc_id)
        return 1

    def delete(self, doc_id):
        self.deletes.append(doc_id)
        return 1 if self.docs.pop(doc_id, None) else 0


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def make(pages=None, *, writer=None, env=None, clock=None, dns=None, store=None, **given):
    web = FakeWeb(pages, dns)
    writer = writer or FakeWriter()
    fetcher = Fetcher(
        resolver=web.resolve,
        opener=web.open,
        proxies={},
        sleep=lambda seconds: None,
        **given,
    )
    sources = Sources(
        "news",
        store or SqlRecords.sqlite(":memory:"),
        writer,
        fetcher=fetcher,
        env=env if env is not None else {},
        clock=clock or Clock(),
    )
    return sources, web, writer


# ============================================================================
# FIXTURE FEEDS AND PAGES
# ============================================================================

FEED = "https://news.example.com/feed.xml"
RSS_TYPE = {"content-type": "application/rss+xml; charset=utf-8"}

RATES = {
    "guid": "rates-1",
    "title": "Rates rise",
    "link": "https://news.example.com/rates?utm_source=rss",
    "date": "Tue, 06 Oct 2026 10:00:00 GMT",
    "author": "Ama Mensah",
    "html": "<p>The central bank raised its rate by a quarter point.</p><p>Inflation is the reason given.</p>",
}
BRIDGE = {
    "guid": "bridge-1",
    "title": "Bridge opens",
    "link": "https://news.example.com/bridge",
    "date": "Mon, 05 Oct 2026 08:00:00 GMT",
    "html": "<p>The new bridge across the river opened to traffic on Monday.</p>",
}


def rss(*items, title="Example News"):
    parts = []
    for item in items:
        parts.append(
            "<item>"
            f"<title>{item['title']}</title>"
            f"<link>{item['link'].replace('&', '&amp;')}</link>"
            f'<guid isPermaLink="false">{item["guid"]}</guid>'
            + (f"<pubDate>{item['date']}</pubDate>" if item.get("date") else "")
            + (f"<dc:creator>{item['author']}</dc:creator>" if item.get("author") else "")
            + f"<content:encoded><![CDATA[{item['html']}]]></content:encoded>"
            + item.get("extra", "")
            + "</item>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        f"<channel><title>{title}</title><link>https://news.example.com/</link>"
        + "".join(parts)
        + "</channel></rss>"
    ).encode()


ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Engineering</title>
 <entry><id>tag:eng.example.com,2026:1</id><title>Shipping 2.2</title>
  <link rel="alternate" href="https://eng.example.com/2-2"/>
  <author><name>Kofi Boateng</name></author>
  <published>2026-10-01T09:00:00Z</published><updated>2026-10-02T09:30:00Z</updated>
  <content type="html">&lt;p&gt;What changed in version 2.2, and why.&lt;/p&gt;</content></entry>
</feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
 <channel rdf:about="https://old.example.org/"><title>Old Site</title><link>https://old.example.org/</link></channel>
 <item rdf:about="https://old.example.org/1"><title>One</title><link>https://old.example.org/1</link>
  <description>The first item of an RSS 1.0 feed.</description><dc:date>2026-10-01T09:00:00Z</dc:date></item>
</rdf:RDF>"""

JSON_FEED = json.dumps(
    {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "JSON News",
        "items": [
            {
                "id": "j1",
                "url": "https://json.example.net/j1",
                "title": "Hello JSON",
                "content_html": "<p>A JSON Feed entry about harbours.</p>",
                "date_published": "2026-10-03T08:00:00Z",
                "authors": [{"name": "Esi Owusu"}],
            }
        ],
    }
).encode()

YOUTUBE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>A Channel</title>
 <entry>
  <id>yt:video:abc123XYZ00</id><yt:videoId>abc123XYZ00</yt:videoId><yt:channelId>UCxyz</yt:channelId>
  <title>How tides work</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=abc123XYZ00"/>
  <author><name>A Channel</name><uri>https://www.youtube.com/channel/UCxyz</uri></author>
  <published>2026-10-01T12:00:00+00:00</published><updated>2026-10-02T12:00:00+00:00</updated>
  <media:group><media:title>How tides work</media:title>
   <media:description>The moon, the sun and the shape of the coast.</media:description></media:group>
 </entry>
</feed>"""

EPISODE = {
    "guid": "ep-1",
    "title": "Episode 1",
    "link": "https://pod.example.com/ep1",
    "html": "Show notes: we talk about interest rates.",
    "extra": '<enclosure url="https://cdn.example.com/ep1.mp3?token=s3cr3t-cdn-token&amp;expires=999" '
    'length="1234" type="audio/mpeg"/>',
}

CLOUDFLARE = (
    b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
    b"<script>window._cf_chl_opt={cvId:'3'};</script></body></html>"
)


def page(price="$10", nav="News"):
    return (
        "<html><head><title>Pricing</title></head><body>"
        f"<nav><a href='/'>Home</a> <a href='/news'>{nav}</a></nav>"
        f"<main><h1>Pricing</h1><p>The standard plan costs {price} a month, billed yearly.</p></main>"
        "<footer>Example Ltd</footer></body></html>"
    ).encode()


# ============================================================================
# HOW OFTEN, AND WHICH ADDRESSES ARE KEPT
# ============================================================================


class TestHowOften:
    @pytest.mark.parametrize(
        "given, seconds",
        [
            ("6h", 21600),
            ("1d", 86400),
            ("1w", 604800),
            ("30m", 1800),
            ("1h30m", 5400),
            ("2H", 7200),
            (600, 600),
            ("900", 900),
            (timedelta(hours=2), 7200),
        ],
    )
    def test_lengths(self, given, seconds):
        assert parse_every(given) == seconds

    @pytest.mark.parametrize("given", ["1m", 60, "soon", "6 hours", True, None, "h6"])
    def test_too_often_or_not_a_length_is_refused(self, given):
        with pytest.raises(ConfigurationError):
            parse_every(given)

    @pytest.mark.parametrize("given", [float("inf"), float("nan"), 1e12, "53w", "366d"])
    def test_less_often_than_once_a_year_is_refused(self, given):
        # 1e309 over JSON is infinity: it was kept, and every listing after it failed.
        with pytest.raises(ConfigurationError, match="once a year"):
            parse_every(given)

    def test_a_year_is_the_least_often(self):
        assert parse_every("52w") == 52 * 604800 and parse_every("365d") == 365 * 86400

    def test_a_date_past_what_a_date_holds_is_written_as_the_last_one(self):
        assert _now_text(1e300) == "9999-12-31T23:59:59Z"
        assert _now_text(NOW) == "2027-01-15T08:00:00Z"

    def test_written_back_the_short_way(self):
        assert (
            every_text(21600) == "6h"
            and every_text(5400) == "1h30m"
            and every_text(86400 * 8) == "1w1d"
        )


class TestAddressesKept:
    @pytest.mark.parametrize(
        "address, named",
        [
            ("https://feeds.example.com/rss?token=abc123secret", "token="),
            (
                "https://acct.blob.core.windows.net/c/prices.pdf?sv=2024-01-01&sig=abc123secret",
                "sig=",
            ),
            ("https://feeds.example.com/rss?api_key=abc123secret&page=2", "api_key="),
            # Akamai's signed links, a Java session, a single-page app's
            # sign-in, and API keys by other names.
            ("https://cdn.example.com/v.m3u8?__token__=exp=1~hmac=abc123secret", "__token__="),
            ("https://cdn.example.com/v.m3u8?hdnts=exp=1~hmac=abc123secret", "hdnts="),
            ("https://shop.example.com/rss;jsessionid=abc123secret?x=1", "jsessionid="),
            ("https://app.example.com/#/feed?code=abc123secret&state=s", "code="),
            ("https://api.example.com/feed?user_key=abc123secret", "user_key="),
            ("https://cdn.example.com/feed?auth_key=abc123secret&x=1", "auth_key="),
        ],
    )
    def test_an_address_with_a_secret_written_into_it_is_refused_without_repeating_it(
        self, address, named
    ):
        with pytest.raises(ConfigurationError) as caught:
            _kept_address(address, web=True)
        assert named in str(caught.value) and "abc123secret" not in str(caught.value)
        assert "${" in str(caught.value)

    def test_a_name_and_password_in_the_address_is_refused(self):
        with pytest.raises(ConfigurationError) as caught:
            _kept_address("https://reader:hunter2@feeds.example.com/rss", web=True)
        assert "hunter2" not in str(caught.value)

    @pytest.mark.parametrize(
        "address",
        [
            "https://www.patreon.com/rss/creator?auth=${PATREON_TOKEN}",
            "https://acct.blob.core.windows.net/c/prices.pdf?${PRICES_SAS}",
            "https://feeds.example.com/${FEED_KEY}/rss",
            "https://news.example.com/feed.xml?category=rates",
        ],
    )
    def test_secrets_written_as_names_are_kept(self, address):
        assert _kept_address(address, web=True) == address

    @pytest.mark.parametrize(
        "address", ["ftp://feeds.example.com/rss", "feeds.example.com/rss", ""]
    )
    def test_feeds_and_pages_are_http_or_https(self, address):
        with pytest.raises(ConfigurationError):
            _kept_address(address, web=True)

    def test_what_is_shown_leaves_the_names_out(self):
        assert public("https://www.patreon.com/rss/creator?auth=${PATREON_TOKEN}") == (
            "https://www.patreon.com/rss/creator"
        )
        assert public("https://acct.blob.core.windows.net/c/prices.pdf?${SAS}&v=2") == (
            "https://acct.blob.core.windows.net/c/prices.pdf?v=2"
        )

    def test_the_value_is_read_from_the_environment_and_taken_back_out_of_messages(self):
        env = {"PATREON_TOKEN": "tok-9f8e7d6c"}
        address = "https://www.patreon.com/rss/creator?auth=${PATREON_TOKEN}"
        assert _expand(address, env).endswith("auth=tok-9f8e7d6c")
        said = _scrub(
            "https://www.patreon.com/rss/creator?auth=tok-9f8e7d6c answered 500", address, env
        )
        assert "tok-9f8e7d6c" not in said and "${PATREON_TOKEN}" in said
        with pytest.raises(ConfigurationError, match="PATREON_TOKEN is not set"):
            _expand(address, {})


# ============================================================================
# FEEDS
# ============================================================================


class TestFeeds:
    def test_each_entry_is_a_document_with_its_metadata(self):
        sources, web, writer = make({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))})
        info = sources.add(FEED, every="6h")
        assert info.kind == "feed" and info.every == 21600
        report = sources.refresh()
        assert sorted(report.added) == [
            "https://news.example.com/bridge",
            "https://news.example.com/rates",
        ]
        rates = writer.docs["https://news.example.com/rates"]
        assert rates["metadata"]["title"] == "Rates rise"
        assert rates["metadata"]["link"] == "https://news.example.com/rates?utm_source=rss"
        assert rates["metadata"]["author"] == "Ama Mensah"
        assert rates["metadata"]["published"] == "2026-10-06T10:00:00Z"
        assert rates["metadata"]["feed"] == "Example News"
        assert rates["metadata"]["source_id"] == info.id
        assert rates["metadata"]["source_kind"] == "feed"
        assert rates["metadata"]["source_address"] == FEED
        assert rates["text"].startswith("Rates rise\n\nThe central bank raised its rate")
        assert rates["doc"]["filename"] == "Rates rise"
        assert report.ok and str(report).startswith("1 source: 2 added, 0 updated, 0 unchanged")

    def test_the_kind_is_found_from_what_the_address_answers(self):
        sources, _web, _writer = make(
            {
                FEED: (200, {"content-type": "text/xml"}, rss(RATES)),
                "https://news.example.com/pricing": (200, {"content-type": "text/html"}, page()),
            }
        )
        assert sources.add(FEED).kind == "feed"
        assert sources.add("https://news.example.com/pricing").kind == "page"

    @pytest.mark.parametrize(
        "body, kind, doc_id, title, author, published",
        [
            (
                ATOM,
                "application/atom+xml",
                "https://eng.example.com/2-2",
                "Shipping 2.2",
                "Kofi Boateng",
                "2026-10-01T09:00:00Z",
            ),
            (RDF, "application/rdf+xml", "https://old.example.org/1", "One", None, None),
            (
                JSON_FEED,
                "application/feed+json",
                "https://json.example.net/j1",
                "Hello JSON",
                "Esi Owusu",
                "2026-10-03T08:00:00Z",
            ),
        ],
    )
    def test_atom_rss_1_and_json_feed(self, body, kind, doc_id, title, author, published):
        sources, _web, writer = make({FEED: (200, {"content-type": kind}, body)})
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert report.added == [doc_id], report.to_dict()
        meta = writer.docs[doc_id]["metadata"]
        assert meta["title"] == title
        assert meta.get("author") == author and meta.get("published") == published

    def test_a_youtube_channel_is_its_videos_titles_and_descriptions(self):
        sources, _web, writer = make(
            {FEED: (200, {"content-type": "application/atom+xml"}, YOUTUBE)}
        )
        sources.add(FEED, kind="feed")
        sources.refresh()
        doc = writer.docs["https://www.youtube.com/watch?v=abc123XYZ00"]
        assert doc["metadata"]["video_id"] == "abc123XYZ00"
        assert "The moon, the sun and the shape of the coast." in doc["text"]

    def test_a_podcast_without_an_audio_engine_is_its_show_notes_and_its_signed_link_is_kept_secret(
        self,
    ):
        sources, web, writer = make({FEED: (200, RSS_TYPE, rss(EPISODE, title="The Show"))})
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        doc = writer.docs["https://pod.example.com/ep1"]
        assert "Show notes: we talk about interest rates." in doc["text"]
        assert doc["metadata"]["audio"] == "https://cdn.example.com/ep1.mp3?expires=999"
        assert not any("cdn.example.com" in url for url in web.urls)
        assert "s3cr3t" not in json.dumps(writer.docs) + json.dumps(report.to_dict())

    def test_a_podcast_with_an_audio_engine_is_transcribed_from_the_whole_link(self):
        heard = []

        def engine(data, name):
            heard.append((data, name))
            return "Transcript. Today we talk about interest rates and the bank."

        audio = "https://cdn.example.com/ep1.mp3?token=s3cr3t-cdn-token&expires=999"
        sources, web, writer = make(
            {
                FEED: (200, RSS_TYPE, rss(EPISODE, title="The Show")),
                audio: (200, {"content-type": "audio/mpeg"}, b"ID3 sound"),
            },
            writer=FakeWriter(extractors={".mp3": engine}),
        )
        sources.add(FEED, kind="feed")
        sources.refresh()
        assert heard == [(b"ID3 sound", "ep1.mp3")]
        assert audio in web.urls
        doc = writer.docs["https://pod.example.com/ep1"]
        assert doc["text"].startswith("Transcript.")
        assert doc["doc"]["source"] == "https://cdn.example.com/ep1.mp3?expires=999"
        assert doc["doc"]["filename"] == "Episode 1"
        assert doc["doc"]["show_notes"].startswith("Show notes")
        assert "s3cr3t" not in json.dumps(writer.docs)

    def test_transcribe_false_keeps_to_the_show_notes(self):
        sources, web, writer = make(
            {FEED: (200, RSS_TYPE, rss(EPISODE))},
            writer=FakeWriter(extractors={".mp3": lambda d, n: "x"}),
        )
        sources.add(Feed(FEED, transcribe=False))
        sources.refresh()
        assert "Show notes" in writer.docs["https://pod.example.com/ep1"]["text"]
        assert not any("cdn.example.com" in url for url in web.urls)

    def test_signed_links_in_entries_are_kept_without_their_secrets(self):
        signed = dict(RATES, link="https://news.example.com/rates?sig=abcdef0123456789&id=7")
        sources, _web, writer = make({FEED: (200, RSS_TYPE, rss(signed))})
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert report.added == ["https://news.example.com/rates?id=7"]
        assert "abcdef0123456789" not in json.dumps(writer.docs) + json.dumps(report.to_dict())

    def test_something_that_is_not_a_feed_fails_with_the_reason(self):
        sources, _web, writer = make(
            {FEED: (200, {"content-type": "application/xml"}, b"<html><p>hi</p></html>")}
        )
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert report.sources[0].status == "failed" and "is not a feed" in report.sources[0].reason
        assert writer.writes == []
        assert sources.list()[0].last_status == "failed"

    def test_a_feed_that_is_gone_fails_and_says_so(self):
        sources, _web, _writer = make({FEED: (410, {}, b"")})
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert report.failed[0]["source"] == FEED and "410" in report.failed[0]["reason"]

    def test_a_bot_check_in_place_of_the_feed_is_refused(self):
        sources, _web, writer = make({FEED: (403, {"server": "cloudflare"}, CLOUDFLARE)})
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert "Cloudflare" in report.failed[0]["reason"] and writer.writes == []


# ============================================================================
# ONLY WHAT CHANGED
# ============================================================================


class TestOnlyWhatChanged:
    def test_a_feed_that_answers_304_costs_one_request_and_writes_nothing(self):
        def feed(request):
            if request.headers.get("If-None-Match") == '"v1"':
                return 304, {}, b""
            return (
                200,
                {**RSS_TYPE, "etag": '"v1"', "last-modified": "Tue, 06 Oct 2026 10:00:00 GMT"},
                rss(RATES, BRIDGE),
            )

        sources, web, writer = make({FEED: feed})
        sources.add(FEED, kind="feed")
        sources.refresh()
        again = sources.refresh(force=True)
        assert again.sources[0].status == "not modified" and again.unchanged == 2
        assert web.requests[-1].headers["If-Modified-Since"] == "Tue, 06 Oct 2026 10:00:00 GMT"
        assert len(writer.writes) == 2

    def test_the_same_entries_again_are_unchanged(self):
        sources, _web, writer = make({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))})
        sources.add(FEED, kind="feed")
        sources.refresh()
        again = sources.refresh(force=True)
        assert again.unchanged == 2 and not again.added and not again.updated
        assert len(writer.writes) == 2

    def test_a_changed_entry_replaces_its_document_under_the_same_id(self):
        sources, web, writer = make({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))})
        sources.add(FEED, kind="feed")
        sources.refresh()
        changed = dict(RATES, html="<p>The central bank raised its rate by half a point.</p>")
        web.pages[FEED] = (200, RSS_TYPE, rss(changed, BRIDGE))
        again = sources.refresh(force=True)
        assert again.updated == ["https://news.example.com/rates"] and again.unchanged == 1
        assert writer.writes.count("https://news.example.com/rates") == 2
        assert "half a point" in writer.docs["https://news.example.com/rates"]["text"]
        assert len(writer.docs) == 2

    def test_an_entry_that_drops_out_of_the_feed_keeps_its_document(self):
        sources, web, writer = make({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))})
        sources.add(FEED, kind="feed")
        sources.refresh()
        web.pages[FEED] = (200, RSS_TYPE, rss(BRIDGE))
        again = sources.refresh(force=True)
        assert not again.removed and writer.deletes == []
        assert "https://news.example.com/rates" in writer.docs
        assert sources.list()[0].documents == 2

    def test_entries_past_the_limit_wait_for_the_next_refresh(self):
        items = [
            dict(BRIDGE, guid=f"b{n}", link=f"https://news.example.com/b{n}") for n in range(5)
        ]
        sources, _web, writer = make({FEED: (200, RSS_TYPE, rss(*items))})
        sources.add(FEED, kind="feed")
        first = sources.refresh(max_items=2)
        assert len(first.added) == 2 and first.waiting == 3
        assert "3 waiting for the next refresh" in str(first)
        second = sources.refresh(force=True, max_items=2)
        assert len(second.added) == 2 and second.unchanged == 2 and second.waiting == 1
        third = sources.refresh(force=True, max_items=2)
        assert len(third.added) == 1 and len(writer.docs) == 5

    def test_a_source_is_read_again_only_when_it_is_due(self):
        clock = Clock()
        sources, web, _writer = make({FEED: (200, RSS_TYPE, rss(RATES))}, clock=clock)
        sources.add(FEED, kind="feed", every="6h")
        assert len(sources.refresh().sources) == 1
        clock.now += 3600
        assert sources.refresh().sources == []
        assert len(sources.refresh(force=True).sources) == 1
        clock.now += 6 * 3600 + 1
        assert len(sources.refresh().sources) == 1
        assert sources.list()[0].next_due is not None

    def test_reset_writes_everything_again(self):
        sources, _web, writer = make({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))})
        sources.add(FEED, kind="feed")
        sources.refresh()
        assert sources.reset() == 1
        again = sources.refresh()
        assert sorted(again.added) == sorted(writer.docs) and len(writer.writes) == 4

    def test_entries_waiting_behind_an_etag_are_all_written_before_it_is_sent(self):
        items = [
            dict(BRIDGE, guid=f"b{n}", link=f"https://news.example.com/b{n}") for n in range(5)
        ]

        def feed(request):
            if request.headers.get("If-None-Match") == '"v1"':
                return 304, {}, b""
            return 200, {**RSS_TYPE, "etag": '"v1"'}, rss(*items)

        sources, _web, writer = make({FEED: feed})
        sources.add(FEED, kind="feed")
        assert len(sources.refresh(max_items=2).added) == 2
        # Asked for whole while entries wait, rather than told nothing changed.
        assert len(sources.refresh(force=True, max_items=2).added) == 2
        assert len(sources.refresh(force=True, max_items=2).added) == 1
        assert len(writer.docs) == 5
        last = sources.refresh(force=True, max_items=2)
        assert last.sources[0].status == "not modified", "and its ETag sent once all are in"

    def test_an_entry_that_failed_behind_an_etag_is_tried_again(self):
        class Flaky(FakeWriter):
            failed = False

            def write(self, doc, doc_id, metadata, version):
                if doc_id.endswith("/bridge") and not self.failed:
                    self.failed = True
                    raise RuntimeError("the index is away for a moment")
                return super().write(doc, doc_id, metadata, version)

        def feed(request):
            if request.headers.get("If-None-Match") == '"v1"':
                return 304, {}, b""
            return 200, {**RSS_TYPE, "etag": '"v1"'}, rss(RATES, BRIDGE)

        sources, _web, writer = make({FEED: feed}, writer=Flaky())
        sources.add(FEED, kind="feed")
        first = sources.refresh()
        assert first.added == ["https://news.example.com/rates"] and len(first.failed) == 1
        assert sources.refresh(force=True).added == ["https://news.example.com/bridge"]
        assert sources.refresh(force=True).sources[0].status == "not modified"
        assert len(writer.docs) == 2


class TestOneDocumentAnEntry:
    @pytest.mark.parametrize(
        "links",
        [
            # A podcast whose every episode links the show.
            ["https://pod.example.com/"] * 3,
            # A changelog whose entries link one page's anchors.
            [f"https://news.example.com/changelog#v1-{n}" for n in range(3)],
            # Links that differ only in a parameter kept off every id, code= here.
            [f"https://uni.example.edu/course?code=CS10{n}" for n in range(3)],
        ],
    )
    def test_entries_that_share_a_link_are_each_a_document_of_their_own(self, links):
        items = [
            dict(BRIDGE, guid=f"e{n}", title=f"Entry {n}", link=link, html=f"<p>Entry {n}.</p>")
            for n, link in enumerate(links)
        ]
        sources, _web, writer = make({FEED: (200, RSS_TYPE, rss(*items))})
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert len(set(report.added)) == 3 and len(writer.docs) == 3
        titles = sorted(d["text"].splitlines()[0] for d in writer.docs.values())
        assert titles == ["Entry 0", "Entry 1", "Entry 2"]
        again = sources.refresh(force=True)
        assert again.unchanged == 3 and not again.added and len(writer.writes) == 3

    def test_the_first_keeps_the_link_as_its_id_and_the_rest_add_their_key(self):
        items = [dict(BRIDGE, guid=f"ep-{n}", link="https://pod.example.com/") for n in (1, 2)]
        sources, _web, _writer = make({FEED: (200, RSS_TYPE, rss(*items))})
        sources.add(FEED, kind="feed")
        assert sources.refresh().added == [
            "https://pod.example.com/",
            "https://pod.example.com/#ep-2",
        ]

    def test_an_entry_whose_link_an_earlier_one_holds_does_not_replace_it(self):
        one = dict(BRIDGE, guid="ep-1", link="https://pod.example.com/", html="<p>One.</p>")
        two = dict(BRIDGE, guid="ep-2", link="https://pod.example.com/", html="<p>Two.</p>")
        now = {"feed": rss(one)}
        sources, _web, writer = make({FEED: lambda request: (200, RSS_TYPE, now["feed"])})
        sources.add(FEED, kind="feed")
        sources.refresh()
        now["feed"] = rss(two, one)  # newest first, as a feed lists them
        report = sources.refresh(force=True)
        assert report.added == ["https://pod.example.com/#ep-2"] and report.unchanged == 1
        assert "One." in writer.docs["https://pod.example.com/"]["text"]


# ============================================================================
# PAGES
# ============================================================================

PRICING = "https://news.example.com/pricing"


class TestPages:
    def test_a_page_is_its_main_text(self):
        sources, _web, writer = make({PRICING: (200, {"content-type": "text/html"}, page())})
        sources.add(PRICING, kind="page")
        report = sources.refresh()
        assert report.added == [PRICING]
        doc = writer.docs[PRICING]
        assert "The standard plan costs $10 a month" in doc["text"]
        assert "Home" not in doc["text"] and "Example Ltd" not in doc["text"]
        assert doc["metadata"]["title"] == "Pricing" and doc["metadata"]["link"] == PRICING

    def test_a_change_around_the_main_text_is_not_a_change(self):
        sources, web, writer = make({PRICING: (200, {"content-type": "text/html"}, page())})
        sources.add(PRICING, kind="page")
        sources.refresh()
        web.pages[PRICING] = (200, {"content-type": "text/html"}, page(nav="Blog"))
        assert sources.refresh(force=True).unchanged == 1 and len(writer.writes) == 1
        web.pages[PRICING] = (200, {"content-type": "text/html"}, page(price="$12"))
        assert sources.refresh(force=True).updated == [PRICING]
        assert "$12" in writer.docs[PRICING]["text"]

    def test_a_conditional_request_that_answers_304_is_not_modified(self):
        def answer(request):
            if request.headers.get("If-None-Match") == '"p1"':
                return 304, {}, b""
            return 200, {"content-type": "text/html", "etag": '"p1"'}, page()

        sources, _web, writer = make({PRICING: answer})
        sources.add(PRICING, kind="page")
        sources.refresh()
        assert sources.refresh(force=True).sources[0].status == "not modified"
        assert len(writer.writes) == 1

    def test_a_page_that_is_gone_keeps_its_chunks_unless_asked(self):
        sources, web, writer = make({PRICING: (200, {"content-type": "text/html"}, page())})
        sources.add(PRICING, kind="page")
        sources.refresh()
        web.pages[PRICING] = (410, {}, b"")
        report = sources.refresh(force=True)
        assert report.gone == [PRICING] and not report.removed and writer.deletes == []
        assert sources.list()[0].documents == 0

    def test_delete_when_gone_removes_its_chunks(self):
        sources, web, writer = make({PRICING: (200, {"content-type": "text/html"}, page())})
        sources.add(PRICING, kind="page", delete_when_gone=True)
        sources.refresh()
        web.pages[PRICING] = (404, {}, b"")
        report = sources.refresh(force=True)
        assert report.removed == [PRICING] and writer.deletes == [PRICING]

    def test_a_page_that_was_never_there_fails(self):
        sources, _web, writer = make({})
        sources.add(PRICING, kind="page")
        report = sources.refresh()
        assert "answered 404" in report.failed[0]["reason"] and writer.writes == []

    def test_a_pdf_at_an_address_is_read_as_a_pdf_would_be(self):
        read = []

        def pdf_reader(data, name):
            read.append(name)
            return "The price list for 2027."

        sources, _web, writer = make(
            {
                "https://news.example.com/prices.pdf": (
                    200,
                    {"content-type": "application/pdf"},
                    b"%PDF-1.7",
                )
            },
            writer=FakeWriter(extractors={".pdf": pdf_reader}),
        )
        sources.add("https://news.example.com/prices.pdf", kind="page")
        sources.refresh()
        assert read == ["prices.pdf"]
        assert "price list" in writer.docs["https://news.example.com/prices.pdf"]["text"]


# ============================================================================
# ARTICLES, ROBOTS AND THE ADDRESS GUARD
# ============================================================================


class TestFetchingSafely:
    def test_articles_reads_the_linked_page(self):
        article = (
            "<html><body><nav>Menu</nav><article><h1>Rates rise</h1>"
            "<p>Full story: the committee voted seven to two.</p></article></body></html>"
        ).encode()
        sources, _web, writer = make(
            {
                FEED: (200, RSS_TYPE, rss(RATES)),
                RATES["link"]: (200, {"content-type": "text/html"}, article),
            }
        )
        sources.add(FEED, kind="feed", articles=True)
        sources.refresh()
        assert "seven to two" in writer.docs["https://news.example.com/rates"]["text"]

    def test_an_article_robots_txt_refuses_falls_back_to_the_feed_text_with_a_note(self):
        sources, web, writer = make(
            {
                FEED: (200, RSS_TYPE, rss(RATES)),
                "https://news.example.com/robots.txt": (
                    200,
                    {},
                    b"User-agent: *\nDisallow: /rates\n",
                ),
            }
        )
        sources.add(FEED, kind="feed", articles=True)
        report = sources.refresh()
        assert "central bank raised" in writer.docs["https://news.example.com/rates"]["text"]
        assert "robots.txt" in report.sources[0].notes[0]
        assert RATES["link"] not in web.urls

    def test_a_feed_robots_txt_disallows_is_a_failure_that_names_it(self):
        sources, web, writer = make(
            {
                FEED: (200, RSS_TYPE, rss(RATES)),
                "https://news.example.com/robots.txt": (
                    200,
                    {},
                    b"User-agent: VectrixDB\nDisallow: /\n",
                ),
            }
        )
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert "robots.txt" in report.failed[0]["reason"] and web.urls == []

    def test_a_robots_txt_that_cannot_be_read_puts_the_source_off(self):
        sources, _web, _writer = make({"https://news.example.com/robots.txt": (503, {}, b"")})
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert report.sources[0].status == "deferred" and report.deferred and report.ok
        assert sources.list()[0].last_status == "deferred"

    @pytest.mark.parametrize("status", [429, 503])
    def test_a_busy_site_puts_the_source_off_until_it_said(self, status, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: NOW)
        sources, _web, writer = make(
            {FEED: (status, {"retry-after": "7200", "content-type": "text/plain"}, b"busy")}
        )
        sources.add(FEED, kind="feed", every="1h")
        report = sources.refresh()
        (outcome,) = report.sources
        assert outcome.status == "deferred" and report.ok and writer.writes == []
        assert f"answered {status}" in outcome.reason and "7200 seconds" in outcome.reason
        assert sources.list()[0].next_due == "2027-01-15T10:00:00Z", "two hours on, as it asked"

    @pytest.mark.parametrize(
        "address",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8080/admin",
            "http://[::1]/",
            "http://10.0.0.5/feed",
        ],
    )
    def test_a_private_or_metadata_address_is_refused_when_it_is_added(self, address):
        sources, web, _writer = make({})
        with pytest.raises(ExtractionError):
            sources.add(address, kind="feed")
        assert web.requests == [] and sources.list() == []

    def test_a_name_that_resolves_to_a_private_address_is_refused(self):
        sources, web, _writer = make({}, dns={"intranet.example.com": ["10.1.2.3"]})
        with pytest.raises(ExtractionError, match="10.1.2.3"):
            sources.add("https://intranet.example.com/feed", kind="feed")
        assert web.requests == []

    def test_a_name_that_later_resolves_to_a_private_address_fails_at_the_refresh(self):
        sources, web, writer = make({FEED: (200, RSS_TYPE, rss(RATES))})
        sources.add(FEED, kind="feed")
        web.dns["news.example.com"] = ["169.254.169.254"]
        report = sources.refresh()
        assert "169.254.169.254" in report.failed[0]["reason"] and writer.writes == []

    def test_an_article_link_to_a_private_address_is_never_fetched(self):
        inside = dict(RATES, link="http://10.0.0.9/admin")
        sources, web, writer = make({FEED: (200, RSS_TYPE, rss(inside))})
        sources.add(FEED, kind="feed", articles=True)
        report = sources.refresh()
        assert web.urls == [FEED] and report.added == ["http://10.0.0.9/admin"]
        assert "10.0.0.9" in report.sources[0].notes[0]

    def test_a_name_in_a_link_a_feed_gives_is_never_read_from_the_environment(self):
        sneaky = dict(RATES, link="https://evil.example.org/collect?x=${SERVER_SECRET}")
        env = {"SERVER_SECRET": "top-secret-value"}
        sources, web, _writer = make({FEED: (200, RSS_TYPE, rss(sneaky))}, env=env)
        sources.add(FEED, kind="feed", articles=True)
        sources.refresh()
        assert all("top-secret-value" not in url for url in [r.url for r in web.requests])

    def test_an_address_read_from_the_environment_is_fetched_whole_and_never_shown(self):
        address = "https://news.example.com/feed.xml?auth=${FEED_TOKEN}"
        env = {"FEED_TOKEN": "tok-31415926"}
        sources, web, writer = make(
            {"https://news.example.com/feed.xml?auth=tok-31415926": (200, RSS_TYPE, rss(RATES))},
            env=env,
        )
        info = sources.add(address, kind="feed")
        assert info.address == FEED
        report = sources.refresh()
        assert report.added == ["https://news.example.com/rates"]
        assert "https://news.example.com/feed.xml?auth=tok-31415926" in web.urls
        kept = json.dumps([r.data for r in sources._records.query(SOURCE)])
        assert "tok-31415926" not in kept + json.dumps(writer.docs) + json.dumps(report.to_dict())

    def test_a_failure_never_repeats_a_value_read_from_the_environment(self):
        address = "https://news.example.com/${FEED_KEY}/feed.xml"
        env = {"FEED_KEY": "key-27182818"}
        sources, _web, _writer = make({}, env=env)
        sources.add(address, kind="feed")
        report = sources.refresh()
        reason = report.failed[0]["reason"]
        assert "key-27182818" not in reason and "${FEED_KEY}" in reason
        assert "key-27182818" not in json.dumps([s.to_dict() for s in sources.list()])

    def test_a_missing_environment_value_fails_the_source_and_names_it(self):
        sources, _web, _writer = make({})
        sources.add("https://news.example.com/feed.xml?auth=${FEED_TOKEN}", kind="feed")
        report = sources.refresh()
        assert "FEED_TOKEN is not set" in report.failed[0]["reason"]

    def test_a_page_whose_address_ends_in_a_value_from_the_environment_is_not_named_by_it(self):
        sources, _web, writer = make(
            {
                "https://intranet.example.com/share/tok-16180339": (
                    200,
                    {"content-type": "text/html"},
                    page(),
                )
            },
            env={"SHARE_TOKEN": "tok-16180339"},
        )
        sources.add("https://intranet.example.com/share/${SHARE_TOKEN}", kind="page")
        assert sources.refresh().added
        # Its name is on every chunk and in every citation.
        (stored,) = writer.docs.values()
        assert stored["doc"]["filename"] == "intranet.example.com.html"
        assert "tok-16180339" not in json.dumps(writer.docs)

    def test_a_redirect_that_repeats_the_value_has_it_taken_back_out(self):
        shared = "https://intranet.example.com/share/tok-16180339"
        sources, _web, writer = make(
            {
                shared: (301, {"location": "/share/tok-16180339/"}, b""),
                shared + "/": (200, {"content-type": "text/html"}, page()),
            },
            env={"SHARE_TOKEN": "tok-16180339"},
        )
        sources.add("https://intranet.example.com/share/${SHARE_TOKEN}", kind="page")
        report = sources.refresh()
        assert report.added and "tok-16180339" not in json.dumps(writer.docs)
        (stored,) = writer.docs.values()
        assert stored["doc"]["filename"] == "${SHARE_TOKEN}.html"

    def test_relative_links_in_a_feed_that_reads_the_environment_never_carry_its_value(self):
        sources, _web, writer = make(
            {
                "https://news.example.com/private.xml?u=tok-27182818": (
                    200,
                    RSS_TYPE,
                    rss(dict(RATES, link="#q3")),
                )
            },
            env={"FEED_TOKEN": "tok-27182818"},
        )
        sources.add("https://news.example.com/private.xml?u=${FEED_TOKEN}", kind="feed")
        report = sources.refresh()
        assert report.added == ["https://news.example.com/private.xml"]
        assert "tok-27182818" not in json.dumps(writer.docs) + json.dumps(report.to_dict())

    def test_a_site_that_asks_for_years_is_asked_again_in_a_week(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: NOW)
        busy = (503, {"retry-after": "9" * 400, "content-type": "text/plain"}, b"busy")
        sources, _web, _writer = make({FEED: busy})
        sources.add(FEED, kind="feed")
        (outcome,) = sources.refresh().sources
        assert outcome.status == "deferred" and "more than a week" in outcome.reason
        (info,) = sources.list()
        assert info.next_due == _now_text(NOW + MAX_DEFER)

    def test_a_crawl_delay_of_years_is_a_week_and_the_list_still_reads(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: NOW)
        robots = b"User-agent: *\nCrawl-delay: 99999999999999\nAllow: /\n"
        sources, _web, writer = make(
            {
                FEED: (200, RSS_TYPE, rss(RATES)),
                "https://news.example.com/robots.txt": (
                    200,
                    {"content-type": "text/plain"},
                    robots,
                ),
            }
        )
        sources.add(FEED, kind="feed")
        assert sources.refresh().sources[0].status == "deferred" and not writer.writes
        (info,) = sources.list()
        assert _now_text(NOW) < info.next_due <= _now_text(NOW + MAX_DEFER)

    def test_force_does_not_ask_a_site_sooner_than_it_said(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: NOW)
        answer = {"now": (503, {"retry-after": "7200", "content-type": "text/plain"}, b"busy")}
        web = FakeWeb({FEED: lambda request: answer["now"]})
        tick = {"now": 1000.0}
        fetcher = Fetcher(
            resolver=web.resolve,
            opener=web.open,
            proxies={},
            sleep=lambda seconds: None,
            clock=lambda: tick["now"],
        )
        clock = Clock()
        sources = Sources(
            "news",
            SqlRecords.sqlite(":memory:"),
            FakeWriter(),
            fetcher=fetcher,
            env={},
            clock=clock,
        )
        sources.add(FEED, kind="feed", every="1h")
        sources.refresh()
        asked = len(web.urls)
        answer["now"] = (200, RSS_TYPE, rss(RATES))
        for report in (sources.refresh(force=True), sources.refresh(only=FEED)):
            (outcome,) = report.sources
            assert outcome.status == "deferred"
            assert "asked to be left alone until 2027-01-15T10:00:00Z" in outcome.reason
        assert len(web.urls) == asked, "nothing was sent"
        clock.now += 7201
        tick["now"] += 7201
        assert sources.refresh().added == ["https://news.example.com/rates"]

    def test_one_fetcher_keeps_the_pace_and_robots_txt_across_refreshes(self):
        def fetcher_for(env):
            return Sources(
                "news", SqlRecords.sqlite(":memory:"), FakeWriter(), env=env
            )._new_fetcher()

        first, again = fetcher_for({}), fetcher_for({})
        narrowed = fetcher_for({"VECTRIXDB_SOURCES_HOSTS": "news.example.com"})
        assert first is again and narrowed is not first
        assert narrowed.allowed_hosts == ["news.example.com"]

    def test_offline_a_source_is_added_without_looking_its_host_up(self, monkeypatch):
        import socket

        looked_up = []
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda host, *a, **k: looked_up.append(host) or []
        )
        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        sources = Sources(
            "news", SqlRecords.sqlite(":memory:"), FakeWriter(), fetcher=Fetcher(proxies={}), env={}
        )
        assert sources.add(FEED, kind="feed").kind == "feed"
        (outcome,) = sources.refresh().sources
        assert outcome.status == "deferred" and "VECTRIXDB_OFFLINE" in outcome.reason
        assert looked_up == []


# ============================================================================
# KEPT, REMOVED, LEASED, AND YOUR OWN KIND
# ============================================================================


class Wire(Source):
    """A licensed wire service, in miniature: stories by desk, each with a revision."""

    kind = "test-wire"
    stories: dict = {}
    reads = 0

    def read(self, context):
        for story in Wire.stories.get(self.address, []):
            yield Item(
                key=story["id"],
                fingerprint=story["rev"],
                title=story["headline"],
                link=story["url"],
                published=story["time"],
                read=lambda s=story: Wire._body(s),
            )

    @staticmethod
    def _body(story):
        Wire.reads += 1
        if story["body"] is None:
            raise ValueError("the wire sent half a story")
        return story["body"]


@pytest.fixture
def wire():
    forget = register_source("test-wire", Wire)
    Wire.reads = 0
    Wire.stories = {
        "energy": [
            {
                "id": "s1",
                "rev": "1",
                "headline": "Oil steady",
                "url": "https://wire.example.com/s1",
                "time": "2026-10-07T06:00:00Z",
                "body": "Oil prices held steady in early trading.",
            }
        ]
    }
    yield Wire
    forget()


class TestKeptAndRemoved:
    def test_adding_an_address_again_changes_how_often_and_keeps_what_it_wrote(self):
        sources, _web, _writer = make({FEED: (200, RSS_TYPE, rss(RATES))})
        first = sources.add(FEED, kind="feed", every="6h")
        sources.refresh()
        again = sources.add(FEED, kind="feed", every="1d", articles=True)
        assert again.id == first.id and again.every == 86400 and again.settings["articles"] is True
        assert len(sources.list()) == 1 and sources.list()[0].documents == 1

    def test_remove_keeps_documents_unless_asked(self):
        sources, _web, writer = make({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))})
        info = sources.add(FEED, kind="feed")
        sources.refresh()
        assert sources.remove(info.id) is True and sources.list() == []
        assert writer.deletes == [] and len(writer.docs) == 2
        assert sources.remove(info.id) is False
        assert sources._records.query(ITEM) == []

    def test_remove_with_delete_documents_spares_what_another_source_wrote(self):
        other = "https://news.example.com/other.xml"
        sources, _web, writer = make(
            {FEED: (200, RSS_TYPE, rss(RATES, BRIDGE)), other: (200, RSS_TYPE, rss(BRIDGE))}
        )
        sources.add(FEED, kind="feed")
        sources.add(other, kind="feed")
        report = sources.refresh()
        # The same article in two feeds is one document, written once.
        assert report.added.count("https://news.example.com/bridge") == 1
        assert sources.remove(FEED, delete_documents=True)
        assert writer.deletes == ["https://news.example.com/rates"]
        assert "https://news.example.com/bridge" in writer.docs

    def test_a_source_another_refresh_holds_is_left_alone(self):
        sources, web, writer = make({FEED: (200, RSS_TYPE, rss(RATES))})
        info = sources.add(FEED, kind="feed")
        sources._records.create(
            Record(LEASE, f"news:{info.id}", {"by": "elsewhere"}, expires=time.time() + 600)
        )
        report = sources.refresh()
        assert report.sources[0].status == "busy" and web.urls == [] and writer.writes == []

    def test_a_lease_that_ran_out_is_taken_over(self):
        sources, _web, writer = make({FEED: (200, RSS_TYPE, rss(RATES))})
        info = sources.add(FEED, kind="feed")
        sources._records.put(
            Record(LEASE, f"news:{info.id}", {"by": "died"}, expires=time.time() - 1)
        )
        assert sources.refresh().added == ["https://news.example.com/rates"]
        assert sources._records.get(LEASE, f"news:{info.id}") is None

    def test_a_long_refresh_keeps_its_lease_and_one_that_lost_it_stops(self, monkeypatch):
        """Each write takes twenty minutes: the lease is made longer as it goes, and a refresh that
        lost it to another writes nothing more."""
        now = [time.time()]
        monkeypatch.setattr(time, "time", lambda: now[0])
        slow = FakeWriter()
        written = slow.write

        def write(doc, doc_id, metadata, version):
            now[0] += 20 * 60
            return written(doc, doc_id, metadata, version)

        slow.write = write
        sources, _web, _ = make({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))}, writer=slow)
        sources.add(FEED, kind="feed")
        report = sources.refresh()
        assert len(report.added) == 2 and report.waiting == 0, report.to_dict()

        taken_over = FakeWriter()
        again, _web, _ = make({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))}, writer=taken_over)
        written_too = taken_over.write

        def write_then_lose(doc, doc_id, metadata, version):
            now[0] += 20 * 60
            (lease,) = again._records.query(LEASE)
            again._records.put(Record(LEASE, lease.key, {"by": "elsewhere"}, expires=now[0] + 600))
            return written_too(doc, doc_id, metadata, version)

        taken_over.write = write_then_lose
        again.add(FEED, kind="feed")
        report = again.refresh()
        assert len(report.added) == 1 and report.waiting == 1
        assert "another may have taken it" in report.sources[0].notes[-1]

    def test_a_kind_this_process_does_not_know_fails_and_names_it(self):
        store = SqlRecords.sqlite(":memory:")
        store.put(
            Record(
                SOURCE,
                "news:abc",
                {
                    "id": "abc",
                    "collection": "news",
                    "type": "nowhere",
                    "address": "x",
                    "every": 3600,
                },
                ix1="news",
            )
        )
        sources, _web, _writer = make({}, store=store)
        report = sources.refresh()
        assert "no kind of source called 'nowhere'" in report.failed[0]["reason"]

    def test_forget_and_restore_for_a_collection(self):
        store = SqlRecords.sqlite(":memory:")
        sources, _web, _writer = make({FEED: (200, RSS_TYPE, rss(RATES))}, store=store)
        sources.add(FEED, kind="feed")
        sources.refresh()
        kept = kept_sources(store, "news")
        assert [k["address"] for k in kept] == [FEED] and "state" not in kept[0]
        assert forget_collection(store, "news") == 1
        assert store.query(SOURCE) == [] and store.query(ITEM) == []
        restore_sources(store, "news", kept)
        assert sources.list()[0].address == FEED and sources.list()[0].documents == 0

    def test_settings_hold_no_secret_and_the_list_shows_none(self):
        sources, _web, _writer = make({})
        sources.add("https://www.patreon.com/rss/creator?auth=${PATREON_TOKEN}", kind="feed")
        assert sources.list()[0].address == "https://www.patreon.com/rss/creator"


class TestYourOwnKind:
    def test_a_registered_source_is_kept_built_again_and_read_only_when_it_changed(self, wire):
        sources, _web, writer = make({})
        info = sources.add(Wire("energy"), every="1h")
        assert info.kind == "test-wire" and info.address == "energy"
        first = sources.refresh()
        assert first.added == ["https://wire.example.com/s1"] and Wire.reads == 1
        assert (
            writer.docs["https://wire.example.com/s1"]["metadata"]["published"]
            == "2026-10-07T06:00:00Z"
        )
        assert sources.refresh(force=True).unchanged == 1 and Wire.reads == 1
        Wire.stories["energy"][0].update(rev="2", body="Oil rose after the report.")
        assert (
            sources.refresh(force=True).updated == ["https://wire.example.com/s1"]
            and Wire.reads == 2
        )

    def test_a_signed_link_a_source_hands_over_is_kept_without_its_token(self, wire):
        Wire.stories["energy"] = [
            {
                "id": "s9",
                "rev": "1",
                "headline": "Signed",
                "url": "https://wire.example.com/s9?id=9&token=wire-secret-123",
                "time": None,
                "body": "A story behind a signed link.",
            },
            {
                "id": "s10",
                "rev": "1",
                "headline": "Broken",
                "url": "https://wire.example.com/s10?token=wire-secret-456",
                "time": None,
                "body": None,
            },
        ]
        sources, _web, writer = make({})
        sources.add(Wire("energy"))
        report = sources.refresh()
        doc = writer.docs["https://wire.example.com/s9?id=9"]
        assert doc["metadata"]["link"] == "https://wire.example.com/s9?id=9"
        assert doc["doc"]["source"] == "https://wire.example.com/s9?id=9"
        assert report.failed[0]["item"] == "https://wire.example.com/s10"
        assert "wire-secret" not in json.dumps(writer.docs) + json.dumps(report.to_dict())

    def test_an_unregistered_class_is_refused_when_it_is_added(self):
        class Stray(Source):
            kind = "stray"

            def read(self, context):
                return []

        sources, _web, _writer = make({})
        with pytest.raises(ConfigurationError, match="stray"):
            sources.add(Stray("x"))

    def test_registering_needs_a_source_and_a_name_of_its_own(self):
        with pytest.raises(TypeError):
            register_source("thing", dict)
        with pytest.raises(ConfigurationError):
            register_source("feed", Wire)
        with pytest.raises(ConfigurationError):
            register_source("Bad Name", Wire)

    def test_one_item_that_fails_does_not_stop_the_rest(self, wire):
        Wire.stories["energy"].append(
            {
                "id": "s2",
                "rev": "1",
                "headline": "Broken",
                "url": "https://wire.example.com/s2",
                "time": None,
                "body": None,
            }
        )

        sources, _web, _writer = make({})
        sources.add(Wire("energy"))
        report = sources.refresh()
        assert report.added == ["https://wire.example.com/s1"]
        assert report.failed == [
            {
                "source": "energy",
                "item": "https://wire.example.com/s2",
                "reason": "ValueError: the wire sent half a story",
            }
        ]
