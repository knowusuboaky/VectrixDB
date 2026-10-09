"""The guarded fetcher sources use: which addresses it refuses, and how politely it asks.

Every test hands the fetcher a fake resolver, a fake connection and a fake
clock, so nothing here reaches the network; one test serves a page on
loopback to show the connection goes to the address that was checked.
"""

from __future__ import annotations

import gzip
import socket
import threading
import time as clock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http.client as http_client

import pytest

from vectrixdb._fetch import (
    MAX_DEFER,
    MAX_REDIRECTS,
    Deferred,
    Fetcher,
    Refused,
    Request,
    _bypassed,
    _http_open,
    _retry_after,
    _rules_from,
    host_matches,
    user_agent,
    why_not_public,
)
from vectrixdb.exceptions import ExtractionError

PUBLIC = "93.184.216.34"


class FakeNet:
    """A resolver and a connection, both answering from dictionaries, both keeping a log."""

    def __init__(self, dns=None, pages=None):
        self.dns = {"feeds.example.com": [PUBLIC], "other.example.org": ["93.184.216.35"]}
        self.dns.update(dns or {})
        self.pages = dict(pages or {})
        self.lookups = []
        self.requests = []

    def resolve(self, host, port):
        self.lookups.append(host)
        found = self.dns.get(host)
        if found is None:
            raise OSError("Name or service not known")
        return found() if callable(found) else list(found)

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
        return [r.url for r in self.requests]


class FakeTime:
    """A clock that only moves when the fetcher sleeps, or a test says so."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def fetcher(net, time=None, **given):
    time = time or FakeTime()
    given.setdefault("robots", False)
    return Fetcher(
        resolver=net.resolve,
        opener=net.open,
        proxies={},
        clock=time.clock,
        sleep=time.sleep,
        **given,
    )


# ============================================================================
# THE ADDRESS GUARD
# ============================================================================

REFUSED = [
    ("127.0.0.1", "loopback"),
    ("127.8.9.10", "loopback"),
    ("10.1.2.3", "private"),
    ("172.16.5.4", "private"),
    ("172.31.255.255", "private"),
    ("192.168.1.1", "private"),
    ("169.254.169.254", "metadata"),
    ("100.64.0.1", "carrier-grade"),
    ("0.0.0.0", "unspecified"),
    ("224.0.0.1", "multicast"),
    ("240.0.0.1", "reserved"),
    ("255.255.255.255", "reserved"),
    ("192.0.0.8", "reserved"),
    ("192.0.2.1", "documentation"),
    ("198.51.100.1", "documentation"),
    ("203.0.113.5", "documentation"),
    ("198.18.0.1", "benchmarking"),
    ("::1", "loopback"),
    ("::", "unspecified"),
    ("fe80::1", "link-local"),
    ("fe80::1%eth0", "link-local"),
    ("fc00::1", "unique local"),
    ("fd12:3456::1", "unique local"),
    ("fec0::1", "site-local"),
    ("ff02::1", "multicast"),
    ("::ffff:127.0.0.1", "IPv4 address written as IPv6"),
    ("::ffff:169.254.169.254", "IPv4 address written as IPv6"),
    ("::127.0.0.1", "IPv4 address written as IPv6"),
    ("64:ff9b::a9fe:a9fe", "inside an IPv6 address"),
    ("64:ff9b::7f00:1", "inside an IPv6 address"),
    ("2002:a9fe:a9fe::", "inside an IPv6 address"),
    ("2002:c0a8:0101::1", "inside an IPv6 address"),
    ("2001:db8::1", "documentation"),
    ("100::1", "discard"),
    ("2001::1", "Teredo"),
    ("::ffff:0:7f00:1", "reserved"),
    # Metadata services and the platform's own address, wherever they sit.
    ("100.100.100.200", "Alibaba Cloud's metadata service"),
    ("fd00:ec2::254", "AWS's metadata service"),
    ("168.63.129.16", "Azure's platform address"),
]


class TestTheAddressGuard:
    @pytest.mark.parametrize("address, why", REFUSED)
    def test_every_range_that_is_not_the_public_internet_is_refused(self, address, why):
        assert why in why_not_public(address)
        net = FakeNet(dns={"feeds.example.com": [address]})
        with pytest.raises(Refused) as caught:
            fetcher(net).get("https://feeds.example.com/rss")
        assert address in str(caught.value) and why in str(caught.value)
        assert net.requests == []

    @pytest.mark.parametrize(
        "address", [PUBLIC, "8.8.8.8", "2606:4700::6810:85e5", "64:ff9b::5db8:d822"]
    )
    def test_public_addresses_pass(self, address):
        assert why_not_public(address) is None
        net = FakeNet(
            dns={"feeds.example.com": [address]},
            pages={"https://feeds.example.com/rss": (200, {}, b"ok")},
        )
        assert fetcher(net).get("https://feeds.example.com/rss").body == b"ok"
        assert net.requests[0].addresses == [address]

    def test_every_address_a_name_resolves_to_is_judged_not_only_the_first(self):
        net = FakeNet(dns={"feeds.example.com": [PUBLIC, "10.0.0.5"]})
        with pytest.raises(Refused, match="10.0.0.5"):
            fetcher(net).get("https://feeds.example.com/rss")
        assert net.requests == []

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://[::1]:8080/",
            "http://[fd00:ec2::254]/latest/meta-data/",
            "http://[::ffff:a9fe:a9fe]/",
        ],
    )
    def test_an_address_written_as_a_number_is_judged_without_a_lookup(self, url):
        net = FakeNet()
        with pytest.raises(Refused):
            fetcher(net).get(url)
        assert net.lookups == [] and net.requests == []

    @pytest.mark.parametrize(
        "host", ["2130706433", "0x7f.1", "017700000001", "localhost", "metadata.google.internal"]
    )
    def test_names_are_judged_by_what_they_resolve_to(self, host):
        # Decimal, hex and octal spellings of 127.0.0.1 and well-known internal
        # names: whatever the system resolver makes of them, the answer is judged.
        answer = "169.254.169.254" if host.startswith("metadata") else "127.0.0.1"
        net = FakeNet(dns={host: [answer]})
        with pytest.raises(Refused, match=answer):
            fetcher(net).get(f"http://{host}/")
        assert net.requests == []

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://feeds.example.com/rss",
            "gopher://feeds.example.com/",
            "data:text/plain,hi",
        ],
    )
    def test_only_http_and_https_are_fetched(self, url):
        net = FakeNet()
        with pytest.raises(Refused, match="only http and https"):
            fetcher(net).get(url)
        assert net.lookups == []

    def test_an_address_with_a_name_and_password_in_it_is_refused_without_repeating_them(self):
        net = FakeNet()
        with pytest.raises(Refused) as caught:
            fetcher(net).get("https://reader:hunter2@feeds.example.com/rss")
        assert "hunter2" not in str(caught.value) and "reader" not in str(caught.value)
        assert net.lookups == []

    def test_a_host_that_cannot_be_found_is_an_extraction_error_not_a_refusal(self):
        net = FakeNet()
        with pytest.raises(ExtractionError, match="could not be found") as caught:
            fetcher(net).get("https://nowhere.example.net/rss")
        assert not isinstance(caught.value, Refused)

    def test_allowed_hosts_limit_every_fetch(self):
        net = FakeNet(pages={"https://feeds.example.com/rss": (200, {}, b"ok")})
        only = fetcher(net, allowed_hosts=["*.example.com"])
        assert only.get("https://feeds.example.com/rss").status == 200
        with pytest.raises(Refused, match="VECTRIXDB_SOURCES_HOSTS"):
            only.get("https://other.example.org/rss")

    def test_internal_hosts_may_resolve_to_private_addresses_and_no_others(self):
        net = FakeNet(
            dns={"wiki.corp.example": ["10.0.0.7"], "evil.example.net": ["10.0.0.7"]},
            pages={"https://wiki.corp.example/feed": (200, {}, b"ok")},
        )
        inside = fetcher(net, internal_hosts=["wiki.corp.example"])
        assert inside.get("https://wiki.corp.example/feed").status == 200
        with pytest.raises(Refused, match="VECTRIXDB_SOURCES_INTERNAL_HOSTS"):
            inside.get("https://evil.example.net/feed")

    @pytest.mark.parametrize("address", ["169.254.169.254", "100.100.100.200", "fd00:ec2::254"])
    def test_an_internal_host_still_never_reaches_a_metadata_service(self, address):
        # Alibaba's sits in carrier-grade NAT space and AWS's IPv6 one in the
        # unique-local range, both ranges an intranet host may have.
        net = FakeNet(dns={"wiki.corp.example": [address]})
        with pytest.raises(Refused) as caught:
            fetcher(net, internal_hosts=["wiki.corp.example"]).get("https://wiki.corp.example/feed")
        assert "metadata" in str(caught.value)
        assert "VECTRIXDB_SOURCES_INTERNAL_HOSTS" not in str(caught.value)

    def test_settings_come_from_the_environment(self):
        built = Fetcher.from_environment(
            {
                "VECTRIXDB_SOURCES_HOSTS": "*.example.com, news.example.org",
                "VECTRIXDB_SOURCES_INTERNAL_HOSTS": "wiki.corp",
            }
        )
        assert built.allowed_hosts == ["*.example.com", "news.example.org"]
        assert built.internal_hosts == ["wiki.corp"]
        assert Fetcher.from_environment({}).allowed_hosts is None

    @pytest.mark.parametrize(
        "host, patterns, expected",
        [
            ("feeds.example.com", ["feeds.example.com"], True),
            ("FEEDS.example.com.", ["feeds.example.com"], True),
            ("a.b.example.com", ["*.example.com"], True),
            ("example.com", ["*.example.com"], False),
            ("badexample.com", ["*.example.com"], False),
            ("anything.net", ["*"], True),
            ("feeds.example.com", [], False),
        ],
    )
    def test_host_patterns(self, host, patterns, expected):
        assert host_matches(host, patterns) is expected


# ============================================================================
# REDIRECTS, AND A NAME THAT CHANGES ITS ANSWER
# ============================================================================


class TestRedirects:
    def test_a_redirect_to_a_private_address_is_refused_before_it_is_followed(self):
        net = FakeNet(
            dns={"internal.example.com": ["10.0.0.9"]},
            pages={
                "https://feeds.example.com/rss": (
                    302,
                    {"Location": "https://internal.example.com/admin"},
                    b"",
                )
            },
        )
        with pytest.raises(Refused, match="10.0.0.9"):
            fetcher(net).get("https://feeds.example.com/rss")
        assert net.urls == ["https://feeds.example.com/rss"]

    def test_a_redirect_to_a_metadata_address_written_as_a_number_is_refused(self):
        net = FakeNet(
            pages={
                "https://feeds.example.com/rss": (
                    301,
                    {"location": "http://169.254.169.254/latest/meta-data/"},
                    b"",
                )
            }
        )
        with pytest.raises(Refused):
            fetcher(net).get("https://feeds.example.com/rss")
        assert len(net.requests) == 1

    def test_a_redirect_is_followed_and_the_reply_says_where_it_ended(self):
        net = FakeNet(
            pages={
                "https://feeds.example.com/rss": (301, {"location": "/feed.xml"}, b""),
                "https://feeds.example.com/feed.xml": (
                    200,
                    {"content-type": "application/rss+xml"},
                    b"<rss/>",
                ),
            }
        )
        reply = fetcher(net).get("https://feeds.example.com/rss")
        assert reply.url == "https://feeds.example.com/feed.xml" and reply.body == b"<rss/>"

    def test_a_key_or_a_cookie_goes_no_further_than_the_site_it_was_sent_to(self):
        net = FakeNet(
            pages={
                "https://feeds.example.com/latest": (302, {"location": "/v2/latest"}, b""),
                "https://feeds.example.com/v2/latest": (
                    302,
                    {"location": "https://other.example.org/x.txt"},
                    b"",
                ),
                "https://other.example.org/x.txt": (200, {}, b"story"),
            }
        )
        sent = {"Authorization": "Bearer key-1", "Cookie": "s=1", "Accept": "text/plain"}
        assert fetcher(net).get("https://feeds.example.com/latest", sent).body == b"story"
        same_site, other_site = net.requests[1].headers, net.requests[2].headers
        assert same_site["Authorization"] == "Bearer key-1" and same_site["Cookie"] == "s=1"
        assert "Authorization" not in other_site and "Cookie" not in other_site
        assert other_site["Accept"] == "text/plain" and other_site["User-Agent"] == user_agent()

    def test_a_redirect_from_https_to_plain_http_is_refused(self):
        net = FakeNet(
            pages={
                "https://feeds.example.com/rss": (
                    302,
                    {"location": "http://feeds.example.com/rss"},
                    b"",
                )
            }
        )
        with pytest.raises(Refused, match="plain http"):
            fetcher(net).get("https://feeds.example.com/rss")

    def test_a_redirect_out_of_the_allowed_hosts_is_refused(self):
        net = FakeNet(
            pages={
                "https://feeds.example.com/rss": (
                    302,
                    {"location": "https://other.example.org/rss"},
                    b"",
                )
            }
        )
        with pytest.raises(Refused, match="not one of the hosts"):
            fetcher(net, allowed_hosts=["feeds.example.com"]).get("https://feeds.example.com/rss")
        assert len(net.requests) == 1

    def test_five_redirects_are_the_most_followed(self):
        def loop(request):
            return 302, {"location": request.url + "x"}, b""

        net = FakeNet()
        net.pages = _Always(loop)
        with pytest.raises(Refused, match="more than 5 times"):
            fetcher(net).get("https://feeds.example.com/r")
        assert len(net.requests) == MAX_REDIRECTS + 1

    def test_dns_rebinding_gets_no_second_lookup_to_change_its_answer(self):
        # A name that answers public once and private after: the fetcher looks
        # it up once, checks that answer, and hands the connection exactly the
        # addresses it checked, so the second answer is never asked for.
        answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])
        net = FakeNet(
            dns={"rebind.example.com": lambda: next(answers)},
            pages={"https://rebind.example.com/rss": (200, {}, b"ok")},
        )
        reply = fetcher(net).get("https://rebind.example.com/rss")
        assert reply.body == b"ok"
        assert net.lookups == ["rebind.example.com"]
        assert net.requests[0].addresses == [PUBLIC]

    def test_each_request_after_a_rebinding_is_judged_by_its_own_lookup(self):
        answers = iter([[PUBLIC], ["127.0.0.1"]])
        net = FakeNet(
            dns={"rebind.example.com": lambda: next(answers)},
            pages={"https://rebind.example.com/rss": (200, {}, b"ok")},
        )
        guard = fetcher(net)
        guard.get("https://rebind.example.com/rss")
        with pytest.raises(Refused, match="127.0.0.1"):
            guard.get("https://rebind.example.com/rss")
        assert len(net.requests) == 1


class _Always(dict):
    """Pages that answer every address with one function."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def get(self, key, default=None):
        return self.answer


# ============================================================================
# SIZE, TIME, AND WHAT IS SENT
# ============================================================================


class TestCapsAndHeaders:
    def test_a_body_past_the_cap_is_refused(self):
        net = FakeNet(pages={"https://feeds.example.com/big": (200, {}, b"x" * 101)})
        with pytest.raises(Refused, match="larger than 100 bytes"):
            fetcher(net, max_bytes=100).get("https://feeds.example.com/big")

    def test_a_compressed_body_is_unpacked_and_a_bomb_is_refused_at_the_cap(self):
        small = gzip.compress(b"<rss>hello</rss>")
        bomb = gzip.compress(b"\0" * 5_000_000)
        assert len(bomb) < 10_000
        net = FakeNet(
            pages={
                "https://feeds.example.com/small": (200, {"content-encoding": "gzip"}, small),
                "https://feeds.example.com/bomb": (200, {"content-encoding": "gzip"}, bomb),
            }
        )
        guard = fetcher(net, max_bytes=1_000_000)
        assert guard.get("https://feeds.example.com/small").body == b"<rss>hello</rss>"
        with pytest.raises(Refused, match="once unpacked"):
            guard.get("https://feeds.example.com/bomb")

    def test_a_fetch_has_a_time_limit_for_the_whole_of_it_redirects_included(self):
        time = FakeTime()

        def slow(request):
            time.now += 11
            return 302, {"location": "https://feeds.example.com/next"}, b""

        net = FakeNet(pages={"https://feeds.example.com/rss": slow})
        with pytest.raises(Refused, match="took longer than 10 seconds"):
            fetcher(net, time, timeout=10).get("https://feeds.example.com/rss")

    def test_one_request_can_have_longer_and_more(self):
        """A podcast episode is larger and slower than a page: its own caps, for that request only."""
        time = FakeTime()

        def episode(request):
            time.now += 45
            return 200, {"content-type": "audio/mpeg"}, b"\xff" * 2_000

        net = FakeNet(pages={"https://feeds.example.com/ep1.mp3": episode})
        guard = fetcher(net, time, timeout=30, max_bytes=1_000)
        reply = guard.get("https://feeds.example.com/ep1.mp3", max_bytes=10_000, timeout=600)
        assert reply.status == 200 and len(reply.body) == 2_000
        assert net.requests[0].timeout == 600 and net.requests[0].max_bytes == 10_000
        with pytest.raises(Refused, match="larger than 1,000 bytes"):
            guard.get("https://feeds.example.com/ep1.mp3")
        assert net.requests[1].timeout == 30, "the next request has the fetcher's own caps"

    def test_polite_waits_are_not_held_against_the_time_limit(self):
        time = FakeTime()
        net = FakeNet(pages={"https://feeds.example.com/rss": (200, {}, b"ok")})
        guard = fetcher(net, time, timeout=5, min_interval=4)
        guard.get("https://feeds.example.com/rss")
        guard.get("https://feeds.example.com/rss")
        assert time.slept == [4]
        assert net.requests[1].timeout == 5

    def test_every_request_says_who_it_is(self):
        net = FakeNet(pages={"https://feeds.example.com/rss": (200, {}, b"ok")})
        fetcher(net).get("https://feeds.example.com/rss", {"If-None-Match": '"v1"'})
        sent = net.requests[0].headers
        assert sent["User-Agent"] == user_agent()
        assert (
            user_agent().startswith("VectrixDB/")
            and "(+https://github.com/knowusuboaky/VectrixDB)" in user_agent()
        )
        assert sent["If-None-Match"] == '"v1"'

    def test_a_bot_check_in_place_of_the_page_is_refused_with_the_site_named(self):
        challenge = (
            b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
            b"<script>window._cf_chl_opt={cvId:'3'};</script></body></html>"
        )
        net = FakeNet(
            pages={"https://feeds.example.com/rss": (403, {"server": "cloudflare"}, challenge)}
        )
        with pytest.raises(Refused) as caught:
            fetcher(net).get("https://feeds.example.com/rss")
        assert caught.value.status == 403
        assert "feeds.example.com" in str(caught.value) and "Cloudflare" in str(caught.value)

    def test_a_connection_that_fails_is_reported_without_the_address_secret(self):
        def broken(request):
            raise ConnectionResetError(f"reset while reading {request.url}")

        url = "https://feeds.example.com/rss?token=s3cr3t-value"
        net = FakeNet(pages={url: broken})
        with pytest.raises(ExtractionError) as caught:
            fetcher(net).get(url)
        assert "s3cr3t-value" not in str(caught.value) and "could not be reached" in str(
            caught.value
        )

    def test_nothing_is_fetched_while_offline(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        looked = []
        guard = Fetcher(resolver=lambda host, port: looked.append(host) or [PUBLIC], proxies={})
        with pytest.raises(Deferred, match="VECTRIXDB_OFFLINE"):
            guard.get("https://feeds.example.com/rss")
        assert looked == []

    def test_nothing_is_looked_up_while_offline(self, monkeypatch):
        looked = []
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: looked.append(host))
        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        with pytest.raises(Deferred, match="VECTRIXDB_OFFLINE"):
            Fetcher(proxies={}).check("https://feeds.example.com/rss")
        assert looked == []

    def test_a_link_with_a_space_in_it_is_sent_as_a_browser_sends_it(self):
        url = "https://feeds.example.com/ep 1.mp3?sv=2024-01-01&sig=s3cr3t-value"
        net = FakeNet(pages={url: (200, {}, b"sound")})
        assert fetcher(net).get(url).body == b"sound"
        assert net.requests[0].target == "/ep%201.mp3?sv=2024-01-01&sig=s3cr3t-value"

    def test_an_error_that_repeats_only_the_path_and_query_repeats_no_signature(self):
        def refused(request):
            # As http.client's InvalidURL does: the target, and not the address.
            raise http_client.InvalidURL(
                f"URL can't contain control characters. {request.target!r}"
            )

        url = "https://feeds.example.com/ep.mp3?sv=2024-01-01&sig=s3cr3t-value"
        net = FakeNet(pages={url: refused})
        with pytest.raises(ExtractionError) as caught:
            fetcher(net).get(url)
        assert "s3cr3t-value" not in str(caught.value) and "sig=***" in str(caught.value)


# ============================================================================
# THE PACE
# ============================================================================


class TestThePace:
    def test_requests_to_one_host_are_spaced_and_other_hosts_are_not_held_up(self):
        time = FakeTime()
        net = FakeNet(
            pages={
                "https://feeds.example.com/a": (200, {}, b"a"),
                "https://feeds.example.com/b": (200, {}, b"b"),
                "https://other.example.org/c": (200, {}, b"c"),
            }
        )
        guard = fetcher(net, time)
        guard.get("https://feeds.example.com/a")
        guard.get("https://other.example.org/c")
        guard.get("https://feeds.example.com/b")
        assert time.slept == [1.0]

    def test_retry_after_is_obeyed_and_a_long_one_is_left_for_the_next_refresh(self):
        time = FakeTime()
        net = FakeNet(
            pages={
                "https://feeds.example.com/busy": (429, {"retry-after": "30"}, b"slow down"),
                "https://other.example.org/busy": (503, {"retry-after": "7200"}, b"later"),
                "https://feeds.example.com/rss": (200, {}, b"ok"),
                "https://other.example.org/rss": (200, {}, b"ok"),
            }
        )
        guard = fetcher(net, time)
        reply = guard.get("https://feeds.example.com/busy")
        assert reply.status == 429 and reply.retry_after == 30
        guard.get("https://feeds.example.com/rss")
        assert time.slept == [30]
        guard.get("https://other.example.org/busy")
        with pytest.raises(Deferred) as caught:
            guard.get("https://other.example.org/rss")
        assert caught.value.until is not None

    @pytest.mark.parametrize(
        "value", ["99999999999999", "9" * 400, "Fri, 31 Dec 9999 23:59:59 GMT"]
    )
    def test_retry_after_is_a_week_at_the_most(self, value):
        # Years on, the source was due again in year three million, and every
        # listing of the collection's sources failed.
        assert _retry_after(value) == MAX_DEFER

    def test_retry_after_as_a_date(self):
        net = FakeNet(
            pages={
                "https://feeds.example.com/busy": (
                    503,
                    {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"},
                    b"",
                )
            }
        )
        assert fetcher(net).get("https://feeds.example.com/busy").retry_after == 0.0


# ============================================================================
# ROBOTS.TXT
# ============================================================================


class TestRobotsRules:
    def test_the_longest_match_wins_and_an_allow_wins_a_tie(self):
        rules = _rules_from("User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /x\nAllow: /x\n")
        assert rules.allows("/a/b/c") and not rules.allows("/a/c") and rules.allows("/x/1")
        assert rules.allows("/elsewhere")

    def test_wildcards_and_the_end_anchor(self):
        rules = _rules_from("User-agent: *\nDisallow: /*.pdf$\nDisallow: /*?sort=\n")
        assert not rules.allows("/files/report.pdf")
        assert rules.allows("/files/report.pdf%3Fpage%3D2")
        assert not rules.allows("/list%3Fsort%3Dasc")
        assert rules.allows("/list")

    def test_a_group_naming_vectrixdb_is_the_one_obeyed(self):
        text = "User-agent: VectrixDB\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
        assert not _rules_from(text).allows("/feed")
        assert _rules_from("User-agent: OtherBot\nDisallow: /\n").allows("/feed")
        assert not _rules_from("User-agent: *\nDisallow: /\n").allows("/feed")

    def test_an_empty_disallow_allows_everything(self):
        assert _rules_from("User-agent: *\nDisallow:\n").allows("/anything")

    @pytest.mark.parametrize("delay", ["99999999999999", "9" * 400])
    def test_a_crawl_delay_is_a_week_at_the_most(self, delay):
        assert _rules_from(f"User-agent: *\nCrawl-delay: {delay}\n").delay == MAX_DEFER

    def test_crawl_delay(self):
        assert _rules_from("User-agent: *\nCrawl-delay: 5\nDisallow: /private\n").delay == 5.0


class TestRobotsFetched:
    def test_a_disallowed_page_is_refused_and_never_asked_for(self):
        net = FakeNet(
            pages={
                "https://feeds.example.com/robots.txt": (
                    200,
                    {},
                    b"User-agent: *\nDisallow: /private\n",
                ),
                "https://feeds.example.com/private/feed": (200, {}, b"secret"),
            }
        )
        with pytest.raises(Refused, match="robots.txt"):
            fetcher(net, robots=True).get("https://feeds.example.com/private/feed")
        assert net.urls == ["https://feeds.example.com/robots.txt"]

    def test_robots_txt_is_read_once_per_site(self):
        net = FakeNet(
            pages={
                "https://feeds.example.com/robots.txt": (200, {}, b"User-agent: *\nAllow: /\n"),
                "https://feeds.example.com/a": (200, {}, b"a"),
                "https://feeds.example.com/b": (200, {}, b"b"),
            }
        )
        guard = fetcher(net, robots=True)
        guard.get("https://feeds.example.com/a")
        guard.get("https://feeds.example.com/b")
        assert net.urls.count("https://feeds.example.com/robots.txt") == 1

    def test_a_missing_robots_txt_allows_everything(self):
        net = FakeNet(pages={"https://feeds.example.com/feed": (200, {}, b"ok")})
        assert fetcher(net, robots=True).get("https://feeds.example.com/feed").body == b"ok"

    @pytest.mark.parametrize("answer", [(500, {}, b"down"), (503, {}, b"")])
    def test_a_robots_txt_that_cannot_be_read_puts_the_fetch_off(self, answer):
        net = FakeNet(pages={"https://feeds.example.com/robots.txt": answer})
        with pytest.raises(Deferred, match="robots.txt"):
            fetcher(net, robots=True).get("https://feeds.example.com/feed")
        assert "https://feeds.example.com/feed" not in net.urls

    def test_a_robots_txt_that_cannot_be_reached_puts_the_fetch_off(self):
        def down(request):
            raise ConnectionRefusedError("refused")

        net = FakeNet(pages={"https://feeds.example.com/robots.txt": down})
        with pytest.raises(Deferred):
            fetcher(net, robots=True).get("https://feeds.example.com/feed")

    def test_crawl_delay_spaces_the_requests(self):
        time = FakeTime()
        net = FakeNet(
            pages={
                "https://feeds.example.com/robots.txt": (
                    200,
                    {},
                    b"User-agent: *\nCrawl-delay: 5\n",
                ),
                "https://feeds.example.com/a": (200, {}, b"a"),
                "https://feeds.example.com/b": (200, {}, b"b"),
            }
        )
        guard = fetcher(net, time, robots=True)
        guard.get("https://feeds.example.com/a")
        guard.get("https://feeds.example.com/b")
        # robots.txt, then five seconds, then /a, then five seconds, then /b.
        assert time.slept == [5.0, 5.0]

    def test_a_large_robots_txt_is_read_to_its_cap_and_not_refused(self):
        text = b"User-agent: *\nDisallow: /private\n" + b"# padding\n" * 60_000
        net = FakeNet(
            pages={
                "https://feeds.example.com/robots.txt": (200, {}, text),
                "https://feeds.example.com/feed": (200, {}, b"ok"),
            }
        )
        guard = fetcher(net, robots=True)
        assert guard.get("https://feeds.example.com/feed").body == b"ok"
        with pytest.raises(Refused):
            guard.get("https://feeds.example.com/private/x")


# ============================================================================
# PROXIES
# ============================================================================


class TestProxies:
    def test_the_proxy_the_environment_names_is_used_unless_no_proxy_names_the_host(self):
        net = FakeNet(
            dns={"wiki.internal.example": [PUBLIC]},
            pages={
                "https://feeds.example.com/rss": (200, {}, b"ok"),
                "https://wiki.internal.example/rss": (200, {}, b"ok"),
            },
        )
        time = FakeTime()
        guard = Fetcher(
            resolver=net.resolve,
            opener=net.open,
            proxies={"https": "http://proxy.example.com:3128", "no": ".internal.example"},
            clock=time.clock,
            sleep=time.sleep,
            robots=False,
        )
        guard.get("https://feeds.example.com/rss")
        guard.get("https://wiki.internal.example/rss")
        assert net.requests[0].proxy == "http://proxy.example.com:3128"
        assert net.requests[1].proxy is None

    def test_plain_http_through_a_proxy_is_refused_and_https_or_no_proxy_is_not(self):
        # The proxy looks the name up again itself: over plain http, nothing
        # tells if that reached the metadata service instead.
        net = FakeNet(
            dns={"wiki.internal.example": [PUBLIC]},
            pages={
                "https://feeds.example.com/rss": (200, {}, b"ok"),
                "http://wiki.internal.example/rss": (200, {}, b"ok"),
            },
        )
        guard = fetcher(net)
        guard._proxies = {
            "http": "http://proxy.example.com:3128",
            "https": "http://proxy.example.com:3128",
            "no": ".internal.example",
        }
        with pytest.raises(Refused, match="plain http") as caught:
            guard.get("http://feeds.example.com/rss")
        assert "NO_PROXY" in str(caught.value)
        assert guard.get("https://feeds.example.com/rss").status == 200
        assert guard.get("http://wiki.internal.example/rss").status == 200
        assert [r.proxy for r in net.requests] == ["http://proxy.example.com:3128", None]

    def test_an_address_is_still_checked_before_the_proxy_is_asked(self):
        net = FakeNet(dns={"feeds.example.com": ["10.0.0.1"]})
        guard = fetcher(net)
        guard._proxies = {"https": "http://proxy.example.com:3128"}
        with pytest.raises(Refused):
            guard.get("https://feeds.example.com/rss")
        assert net.requests == []

    @pytest.mark.parametrize(
        "host, port, no_proxy, expected",
        [
            ("feeds.example.com", 443, "*", True),
            ("feeds.example.com", 443, "example.com", True),
            ("feeds.example.com", 443, ".example.com", True),
            ("feeds.example.com", 443, "*.example.com", True),
            ("feeds.example.com", 443, "example.com:443", True),
            ("feeds.example.com", 443, "example.com:8443", False),
            ("feeds.example.com", 443, "other.com, example.org", False),
            ("badexample.com", 443, "example.com", False),
            ("feeds.example.com", 443, None, False),
        ],
    )
    def test_no_proxy(self, host, port, no_proxy, expected):
        assert _bypassed(host, port, no_proxy) is expected


# ============================================================================
# THE REAL CONNECTION, ON LOOPBACK
# ============================================================================


class _Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append({"path": self.path, "host": self.headers.get("Host")})
        if self.path in ("/slow-headers", "/slow-body"):
            return self._trickle()
        if self.path == "/big":
            body = b"x" * 5000
        elif self.path == "/gz":
            body = gzip.compress(b"<rss>packed</rss>")
        else:
            body = b"<rss>served</rss>"
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        if self.path == "/gz":
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trickle(self):
        """A byte every tenth of a second: never slow enough for one read to time out."""
        try:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            if self.path == "/slow-headers":
                for _ in range(60):
                    self.wfile.write(b"X")
                    self.wfile.flush()
                    clock.sleep(0.1)
                return
            self.wfile.write(b"Content-Length: 60\r\n\r\n")
            for _ in range(60):
                self.wfile.write(b"x")
                self.wfile.flush()
                clock.sleep(0.1)
        except OSError:
            pass  # hung up on, as it should be


@pytest.fixture
def loopback():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    httpd.seen = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


class TestThePinnedConnection:
    def test_the_connection_goes_to_the_checked_address_with_the_name_kept(
        self, loopback, monkeypatch
    ):
        monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
        port = loopback.server_address[1]
        looked = []

        def resolver(host, wanted_port):
            looked.append(host)
            return ["127.0.0.1"]

        guard = Fetcher(
            internal_hosts=["feeds.example.com"], resolver=resolver, proxies={}, robots=False
        )
        reply = guard.get(f"http://feeds.example.com:{port}/rss")
        assert reply.body == b"<rss>served</rss>"
        assert reply.header("Content-Type") == "application/rss+xml"
        assert loopback.seen[0]["host"] == f"feeds.example.com:{port}"
        assert looked == ["feeds.example.com"]
        assert guard.get(f"http://feeds.example.com:{port}/gz").body == b"<rss>packed</rss>"

    def test_a_body_whose_length_is_past_the_cap_is_refused_before_it_is_read(
        self, loopback, monkeypatch
    ):
        monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
        port = loopback.server_address[1]
        guard = Fetcher(
            internal_hosts=["feeds.example.com"],
            resolver=lambda host, p: ["127.0.0.1"],
            proxies={},
            robots=False,
            max_bytes=1000,
        )
        with pytest.raises(Refused, match="larger than 1,000 bytes"):
            guard.get(f"http://feeds.example.com:{port}/big")

    @pytest.mark.parametrize("path", ["/slow-headers", "/slow-body"])
    def test_the_time_limit_holds_against_a_server_that_answers_a_byte_at_a_time(
        self, loopback, monkeypatch, path
    ):
        monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
        port = loopback.server_address[1]
        guard = Fetcher(
            internal_hosts=["feeds.example.com"],
            resolver=lambda host, p: ["127.0.0.1"],
            proxies={},
            robots=False,
            timeout=1.0,
        )
        began = clock.monotonic()
        with pytest.raises(Refused, match="took longer than 1 seconds"):
            guard.get(f"http://feeds.example.com:{port}{path}")
        # Six seconds of bytes, cut at the one second it was given.
        assert clock.monotonic() - began < 3.0

    def test_the_opener_reads_no_further_than_the_cap(self, loopback):
        port = loopback.server_address[1]
        request = Request(
            method="GET",
            url=f"http://feeds.example.com:{port}/big",
            scheme="http",
            host="feeds.example.com",
            port=port,
            addresses=["127.0.0.1"],
            target="/big",
            headers={},
            timeout=5,
            max_bytes=100,
            truncate=True,
        )
        status, headers, body = _http_open(request)
        assert status == 200 and len(body) == 101
