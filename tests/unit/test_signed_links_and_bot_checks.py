"""Signed links kept secret, and bot-check pages refused.

``load_url`` kept the whole address as a document's ``source``, so a SAS
link's signature was copied onto every chunk, every citation and every
answer that quoted one. And a site's "Just a moment..." page came back as
the document, to be indexed and cited as though it were the page. Both are
held here, on the pure functions and on every place a fetched address is
kept or repeated: ``load_url`` and the extraction service's address routes.
Every network is a stand-in.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vectrixdb._web import (
    bot_check,
    bot_check_message,
    redact_message,
    redact_url,
    site_of,
    status_message,
)
from vectrixdb.exceptions import ExtractionError
from vectrixdb.extract import load_url

SIGNED = "https://acct.blob.core.windows.net/raw/report.html?sv=2024-01-01&sig=SECRET&se=2026-12-31"


# ======================================================== the redaction ===


class TestWhatIsTakenOut:
    @pytest.mark.parametrize(
        "given, kept",
        [
            # The address the release was found with.
            (SIGNED, "https://acct.blob.core.windows.net/raw/report.html"),
            # A SAS takes every field it signs with it, and keeps what is not its.
            (
                "https://acct.blob.core.windows.net/c/q3.pdf?sp=r&st=2026-01-01T00:00:00Z"
                "&se=2026-01-02T00:00:00Z&spr=https&sv=2022-11-02&sr=b&sig=SECRET%3D&versionid=7",
                "https://acct.blob.core.windows.net/c/q3.pdf?versionid=7",
            ),
            # S3, signed: every X-Amz- field goes with its signature.
            (
                "https://bucket.s3.amazonaws.com/k.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256"
                "&X-Amz-Credential=AKIASECRET%2F20260101%2Fus-east-1%2Fs3%2Faws4_request"
                "&X-Amz-Date=20260101T000000Z&X-Amz-Expires=900&X-Amz-SignedHeaders=host"
                "&X-Amz-Security-Token=SECRET&X-Amz-Signature=SECRET&versionId=3",
                "https://bucket.s3.amazonaws.com/k.pdf?versionId=3",
            ),
            # Cloud Storage, signed.
            (
                "https://storage.googleapis.com/b/o.pdf?X-Goog-Algorithm=GOOG4-RSA-SHA256"
                "&X-Goog-Credential=svc%40p.iam.gserviceaccount.com%2F20260101"
                "&X-Goog-Date=20260101T000000Z&X-Goog-Expires=900&X-Goog-SignedHeaders=host"
                "&X-Goog-Signature=SECRET",
                "https://storage.googleapis.com/b/o.pdf",
            ),
            # The older S3 form and CloudFront's.
            (
                "https://b.s3.amazonaws.com/o.pdf?AWSAccessKeyId=AKIA&Expires=1700000000&Signature=SECRET",
                "https://b.s3.amazonaws.com/o.pdf",
            ),
            (
                "https://d111.cloudfront.net/v.mp3?Expires=1700000000&Signature=SECRET&Key-Pair-Id=K2",
                "https://d111.cloudfront.net/v.mp3",
            ),
            # A name and password before the host.
            ("https://ama:SECRET@feeds.example.com/rss", "https://feeds.example.com/rss"),
            ("https://ama@feeds.example.com:8443/rss", "https://feeds.example.com:8443/rss"),
            # Generic credentials, whatever their case.
            ("https://x.example/a?TOKEN=SECRET&page=2", "https://x.example/a?page=2"),
            ("https://x.example/a?access_token=SECRET", "https://x.example/a"),
            ("https://x.example/a?api_key=SECRET&q=rates", "https://x.example/a?q=rates"),
            ("https://x.example/a?apikey=SECRET", "https://x.example/a"),
            ("https://x.example/a?Key=SECRET&lang=en", "https://x.example/a?lang=en"),
            ("https://x.example/a?password=SECRET", "https://x.example/a"),
            ("https://x.example/a?client_secret=SECRET", "https://x.example/a"),
            ("https://x.example/a?signature=SECRET", "https://x.example/a"),
            ("https://x.example/cb?code=SECRET&state=abc", "https://x.example/cb?state=abc"),
            ("https://x.example/a?auth_token=SECRET", "https://x.example/a"),
            ("https://x.example/a?x-api-key=SECRET", "https://x.example/a"),
            # A sign-in hands its token back in the fragment.
            (
                "https://app.example/cb#access_token=SECRET&token_type=bearer&expires_in=3600",
                "https://app.example/cb#token_type=bearer&expires_in=3600",
            ),
            ("https://app.example/cb#id_token=SECRET", "https://app.example/cb"),
            # A JSON Web Token is a credential whatever it is called.
            (
                "https://x.example/a?state=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SECRET&q=1",
                "https://x.example/a?q=1",
            ),
            # A value with a semicolon in it reads as it did.
            ("https://x.example/a?q=a;b&token=SECRET", "https://x.example/a?q=a;b"),
            # Akamai's signed links: hdnts, and __token__ without its underscores.
            (
                "https://cdn.example/v.m3u8?__token__=exp=1~acl=/*~hmac=SECRET",
                "https://cdn.example/v.m3u8",
            ),
            (
                "https://cdn.example/v.m3u8?hdnts=exp=1~acl=/*~hmac=SECRET&q=hd",
                "https://cdn.example/v.m3u8?q=hd",
            ),
            # A Java session in a path parameter, and the others kept.
            (
                "https://shop.example/app/page;jsessionid=SECRET;lang=en?x=1",
                "https://shop.example/app/page;lang=en?x=1",
            ),
            # A single-page app's sign-in hands its code back in the route.
            (
                "https://app.example/#/callback?code=SECRET&state=s",
                "https://app.example/#/callback?state=s",
            ),
            (
                "https://app.example/#/route?next=/x?code=SECRET",
                "https://app.example/#/route?next=/x",
            ),
            # API keys by other names.
            ("https://api.example/feed?auth_key=SECRET&x=1", "https://api.example/feed?x=1"),
            ("https://api.example/feed?user_key=SECRET", "https://api.example/feed"),
        ],
    )
    def test_each_credential(self, given, kept):
        assert redact_url(given) == kept
        assert "SECRET" not in redact_url(given)

    @pytest.mark.parametrize(
        "plain",
        [
            "https://example.test/guide",
            "https://www.example.com/blog/2026/10/rates-rise?page=2&sort=new#comments",
            "https://example.com/search?q=sig+fig",
            "http://[2001:db8::1]:8080/feed.xml",
            "s3://bucket/key.pdf",
            "C:\\docs\\q3.pdf",
            "",
        ],
    )
    def test_an_address_with_nothing_to_take_out_comes_back_as_it_was(self, plain):
        assert redact_url(plain) == plain

    def test_a_sas_field_name_without_a_signature_is_somebody_else_s_parameter(self):
        # sp, se and sv are a SAS's only beside sig; on their own they are a site's own.
        assert (
            redact_url("https://x.example/a?sp=1&se=2&sv=3") == "https://x.example/a?sp=1&se=2&sv=3"
        )

    def test_it_is_the_same_address_however_often_the_link_is_signed_again(self):
        first = "https://a.blob.core.windows.net/c/q3.pdf?se=2026-01-01&sp=r&sig=ONE"
        again = "https://a.blob.core.windows.net/c/q3.pdf?se=2027-01-01&sp=r&sig=TWO"
        assert redact_url(first) == redact_url(again)

    def test_an_address_that_will_not_parse_still_loses_its_query(self):
        assert redact_url("http://[::1/x?token=SECRET") == "http://[::1/x"

    def test_the_site_is_named_without_the_address(self):
        assert site_of(SIGNED) == "acct.blob.core.windows.net"
        assert site_of("not an address") == "the site"


class TestAMessageThatRepeatsAnAddress:
    def test_the_address_whole_is_put_back_without_its_credentials(self):
        said = redact_message(f"reset while reading {SIGNED}", SIGNED)
        assert said == "reset while reading https://acct.blob.core.windows.net/raw/report.html"

    def test_its_path_and_query_alone_lose_their_values(self):
        # http.client's InvalidURL repeats the path and query, never the address.
        url = "https://cdn.example/ep 1.mp3?sv=2024-01-01&sig=SECRET%2Bvalue"
        said = redact_message(
            "can't contain control characters. '/ep 1.mp3?sv=2024-01-01&sig=SECRET%2Bvalue'", url
        )
        assert "SECRET" not in said and "sig=***" in said and "sv=***" in said

    def test_a_password_and_a_token_before_the_host_lose_theirs(self):
        assert "hunter22" not in redact_message(
            "auth hunter22 failed", "https://ama:hunter22@h.example/x"
        )
        assert redact_message("bad ghp_token1", "https://ghp_token1@github.example/x") == "bad ***"

    def test_a_message_about_an_address_with_nothing_to_take_out_is_as_it_was(self):
        assert redact_message("timed out", "https://h.example/x?q=1") == "timed out"


# ========================================================== bot checks ===

CLOUDFLARE_JS = b"""<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"><meta name="robots" content="noindex,nofollow">
<style>*{box-sizing:border-box;margin:0;padding:0}</style><meta http-equiv="refresh" content="390"></head>
<body class="no-js"><div class="main-wrapper" role="main"><div class="main-content"><noscript>
<div id="challenge-error-title"><div class="h2"><span id="challenge-error-text">Enable JavaScript and cookies to continue</span>
</div></div></noscript></div></div><script>(function(){window._cf_chl_opt={cvId: '3',cZone: "www.example.com",
cType: 'managed',cRay: '8a1b2c3d4e5f6a7b'};var cpo = document.createElement('script');
cpo.src = '/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1?ray=8a1b2c3d4e5f6a7b';
document.getElementsByTagName('head')[0].appendChild(cpo);}());</script></body></html>"""

# The page load_url read as a document, before this release: no Cloudflare header, two marks of its own.
CLOUDFLARE_VERIFYING = b"""<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head>
<body><div class="main-wrapper" role="main"><div class="main-content"><h1>www.example.com</h1>
<p class="h2">Performing security verification</p><p>This website uses a security service to protect against
malicious bots. This page is displayed while the website verifies you are not a bot.</p></div></div>
<div class="footer"><div>Ray ID: <code>8f0a1b2c3d4e5f60</code></div></div></body></html>"""

CLOUDFLARE_BLOCK = b"""<!DOCTYPE html><html class="no-js" lang="en-US"><head>
<title>Attention Required! | Cloudflare</title></head><body><div id="cf-wrapper">
<div id="cf-error-details" class="cf-error-details-wrapper"><h1>Sorry, you have been blocked</h1>
<h2 class="cf-subheadline">You are unable to access example.com</h2>
<p>Cloudflare Ray ID: <strong>8a1b2c3d4e5f6a7b</strong></p></div></div></body></html>"""

DATADOME = b"""<html><head><title>example.com</title><style>#cmsg{animation: A 1.5s;}</style></head>
<body style="margin:0"><p id="cmsg">Please enable JS and disable any ad blocker</p>
<script data-cfasync="false">var dd={'rt':'c','cid':'AHrlqAAAAAMA','hsh':'2211F522B61E269B869FA6EAFFB5E1',
't':'bv','s':17434,'e':'abc','host':'geo.captcha-delivery.com'}</script>
<script data-cfasync="false" src="https://ct.captcha-delivery.com/c.js"></script></body></html>"""

PERIMETERX = b"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Access to this page has been denied</title></head><body><div class="px-captcha-container">
<div class="px-captcha-header">Before we continue...</div>
<div class="px-captcha-message">Press &amp; Hold to confirm you are<br>a human (and not a bot).</div>
<div id="px-captcha"></div></div><script>window._pxAppId = 'PXabc123';</script>
<script src="https://captcha.px-cdn.net/PXabc123/captcha.js?a=c&m=0"></script></body></html>"""

INCAPSULA = b"""<html style="height:100%"><head><META NAME="ROBOTS" CONTENT="NOINDEX, NOFOLLOW"></head>
<body style="margin:0px;height:100%"><iframe id="main-iframe"
src="/_Incapsula_Resource?CWUDNSAI=24&xinfo=8-12345-0&incident_id=123-456&edet=12&cinfo=04000000"
frameborder=0 width="100%" height="100%">Request unsuccessful. Incapsula incident ID: 123000450012345678-901234567890123456</iframe>
</body></html>"""

PARDON = b"""<!DOCTYPE html><html><head><title>Pardon Our Interruption</title></head><body><div class="container">
<h1>Pardon Our Interruption</h1><p>As you were browsing something about your browser made us think you were a bot.
There are a few reasons this might happen:</p><ul><li>You've disabled JavaScript in your web browser.</li>
<li>You've disabled cookies in your web browser.</li></ul><p>To regain access, please make sure that cookies and
JavaScript are enabled before reloading the page.</p></div></body></html>"""

AKAMAI = b"""<HTML><HEAD>
<TITLE>Access Denied</TITLE>
</HEAD><BODY>
<H1>Access Denied</H1>

You don't have permission to access "http&#58;&#47;&#47;www&#46;example&#46;com&#47;rates" on this server.<P>
Reference&#32;&#35;18&#46;2b3c4d5e&#46;1700000000&#46;6f7a8b9c
<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;2b3c4d5e&#46;1700000000&#46;6f7a8b9c</P>
</BODY>
</HTML>"""

AWS_WAF = b"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title></title><script>
window.awsWafCookieDomainList = [];window.gokuProps = {"key":"AQIDAHjcYu","iv":"CgAHhDJ","context":"abc"};
</script><script src="https://abc123.edge.sdk.awswaf.com/abc123/def456/challenge.js"></script></head>
<body><div id="challenge-container"></div><noscript><h1>JavaScript is disabled</h1>In order to continue, we
need to verify that you're not a robot. This requires JavaScript.</noscript></body></html>"""

# A normal page behind Cloudflare: its bot-detection script is on every page, and is no challenge.
BEHIND_CLOUDFLARE = b"""<html><head><title>Rates</title></head><body><main><h1>Rates</h1>
<p>The savings rate is 4.5 percent.</p></main><script>(function(){var d=document.createElement('script');
d.innerHTML="window.__CF$cv$params={r:'8a1b',t:'MTcw'};var a=document.createElement('script');
a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';";})();</script></body></html>"""


def _article_about_cloudflare() -> bytes:
    """A long page that quotes every mark a challenge leaves, and is an article."""
    paragraph = (
        "<p>When a visitor looks like a bot, Cloudflare answers with a page titled "
        "&quot;Just a moment...&quot; that says Enable JavaScript and cookies to continue, or with "
        "&quot;Attention Required! | Cloudflare&quot; and a Ray ID. The challenge script loads from "
        "/cdn-cgi/challenge-platform/h/ and sets window._cf_chl_opt. Performing security "
        "verification is what the newest version says. None of this is a reason to refuse an "
        "article that explains it.</p>"
    )
    return (
        "<html><head><title>How Cloudflare's challenge pages work</title></head><body><article>"
        "<h1>How Cloudflare's challenge pages work</h1>"
        + paragraph * 6
        + "<pre>window._cf_chl_opt={cType: 'managed'}</pre></article></body></html>"
    ).encode()


class TestBotChecks:
    @pytest.mark.parametrize(
        "status, headers, body, vendor",
        [
            (403, {"cf-mitigated": "challenge"}, b"", "Cloudflare challenge"),
            (
                403,
                {"Server": "cloudflare", "CF-RAY": "8a1b"},
                CLOUDFLARE_JS,
                "Cloudflare challenge",
            ),
            (200, {"Content-Type": "text/html"}, CLOUDFLARE_VERIFYING, "Cloudflare challenge"),
            (503, {}, CLOUDFLARE_JS, "Cloudflare challenge"),
            (403, {"Server": "cloudflare"}, CLOUDFLARE_BLOCK, "Cloudflare block"),
            (403, {"X-DataDome": "protected"}, DATADOME, "DataDome"),
            (403, {}, PERIMETERX, "PerimeterX"),
            (200, {}, INCAPSULA, "Imperva (Incapsula)"),
            (405, {}, PARDON, "Pardon Our Interruption"),
            (403, {"Server": "AkamaiGHost"}, AKAMAI, "Akamai"),
            (202, {"x-amzn-waf-action": "challenge"}, b"", "AWS WAF challenge"),
            (405, {"x-amzn-waf-action": "captcha"}, b"", "AWS WAF CAPTCHA"),
            (202, {}, AWS_WAF, "AWS WAF challenge"),
            (
                200,
                {},
                b"<html><body><noscript>Enable JavaScript and cookies to continue</noscript></body></html>",
                "JavaScript and cookies",
            ),
        ],
    )
    def test_each_vendor_s_page_is_a_bot_check(self, status, headers, body, vendor):
        assert vendor in (bot_check(status, headers, body) or "")

    @pytest.mark.parametrize(
        "status, headers, body",
        [
            (200, {"Server": "cloudflare", "CF-RAY": "8a1b"}, _article_about_cloudflare()),
            (200, {"Server": "cloudflare", "CF-RAY": "8a1b"}, BEHIND_CLOUDFLARE),
            (
                404,
                {"Server": "cloudflare"},
                b"<html><title>Not found</title><p>No such page.</p></html>",
            ),
            (200, {"Content-Type": "application/json"}, json.dumps({"rate": 4.5}).encode()),
            (200, {}, b"<html><title>Just a moment...</title><p>A poem about waiting.</p></html>"),
            (200, {}, b""),
            # Larger than any bot check: a page, whatever it quotes.
            (200, {}, CLOUDFLARE_JS + b"<!--" + b"x" * (300 * 1024) + b"-->"),
        ],
    )
    def test_a_page_is_not_one(self, status, headers, body):
        assert bot_check(status, headers, body) is None

    def test_the_message_names_the_site_and_what_it_sent_and_never_the_address(self):
        said = bot_check_message(SIGNED, 403, "a Cloudflare challenge page")
        assert said.startswith(
            "acct.blob.core.windows.net answered 403 with a Cloudflare challenge page"
        )
        assert "SECRET" not in said and "sig=" not in said


class TestWhatAnAnswerMeans:
    @pytest.mark.parametrize(
        "status, headers, said",
        [
            (403, {}, "answered 403: the site refused the request. It may want a sign-in"),
            (401, {}, "answered 401: the site refused the request"),
            (
                429,
                {"Retry-After": "120"},
                "answered 429, too many requests, and asked for 120 seconds",
            ),
            (
                503,
                {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
                "answered 503, unavailable for now, and asked to be left until Wed, 21 Oct 2026 07:28:00 GMT",
            ),
            (503, {}, "answered 503, unavailable for now"),
            (404, {}, "answered 404: nothing is at this address"),
            (410, {}, "answered 410: what was at this address is gone"),
            (500, {}, "answered 500"),
        ],
    )
    def test_a_status_is_said_in_words(self, status, headers, said):
        message = status_message("https://www.example.com/rates", status, headers)
        assert message.startswith(f"https://www.example.com/rates {said}"), message

    def test_a_refusal_points_at_what_to_do(self):
        said = status_message("https://www.example.com/rates", 403)
        assert "ask the site for a feed or an API" in said


# ============================================================ load_url ===


def _answer(status, headers, body):
    return lambda method, url, sent, data, timeout: (status, headers, body)


class TestLoadUrl:
    def test_a_signed_link_is_kept_without_its_signature(self):
        doc = load_url(
            SIGNED,
            transport=_answer(200, {"Content-Type": "text/html"}, b"<h1>Report</h1><p>Q3.</p>"),
        )
        assert doc.metadata["source"] == "https://acct.blob.core.windows.net/raw/report.html"
        assert doc.metadata["filename"] == "report.html"
        assert "SECRET" not in json.dumps(doc.metadata)

    def test_the_whole_address_is_what_is_fetched(self):
        asked = []

        def transport(method, url, headers, body, timeout):
            asked.append(url)
            return 200, {"Content-Type": "text/html"}, b"<p>ok</p>"

        load_url(SIGNED, transport=transport)
        assert asked == [SIGNED]

    def test_an_error_never_repeats_the_signature(self):
        with pytest.raises(ExtractionError, match="answered 404") as gone:
            load_url(SIGNED, transport=_answer(404, {}, b""))
        assert "SECRET" not in str(gone.value)

        def refuses(method, url, headers, body, timeout):
            raise OSError(f"connection to {url} refused")

        with pytest.raises(ExtractionError, match="could not be reached") as down:
            load_url(SIGNED, transport=refuses)
        assert "SECRET" not in str(down.value)
        assert down.value.__cause__ is None and down.value.__suppress_context__

    def test_an_error_that_repeats_only_the_path_and_query_never_repeats_the_signature(self):
        from http.client import InvalidURL

        def refuses(method, url, headers, body, timeout):
            # As http.client words it: the request's path and query, not the address.
            target = url.split(".net", 1)[1]
            raise InvalidURL(
                f"URL can't contain control characters. {target!r} (found at least ' ')"
            )

        with pytest.raises(ExtractionError, match="could not be reached") as down:
            load_url(SIGNED, transport=refuses)
        assert "SECRET" not in str(down.value) and "sig=***" in str(down.value)

    def test_a_bot_check_is_refused_naming_the_site_and_the_reason(self):
        with pytest.raises(ExtractionError) as caught:
            load_url(
                "https://www.example.com/rates", transport=_answer(200, {}, CLOUDFLARE_VERIFYING)
            )
        said = str(caught.value)
        assert said.startswith("www.example.com answered 200 with a Cloudflare challenge page")
        assert caught.value.status == 200

    def test_a_403_says_more_than_its_number_when_it_was_a_challenge(self):
        with pytest.raises(ExtractionError) as caught:
            load_url(
                "https://www.example.com/rates",
                transport=_answer(403, {"Server": "cloudflare", "CF-RAY": "1"}, CLOUDFLARE_JS),
            )
        assert "answered 403 with a Cloudflare challenge page" in str(caught.value)
        assert caught.value.status == 403

    def test_a_busy_site_and_a_refusal_say_what_they_mean(self):
        with pytest.raises(ExtractionError) as busy:
            load_url(SIGNED, transport=_answer(429, {"Retry-After": "120"}, b"slow down"))
        assert "answered 429, too many requests, and asked for 120 seconds" in str(busy.value)
        assert busy.value.status == 429 and "SECRET" not in str(busy.value)
        with pytest.raises(ExtractionError, match="the site refused the request") as refused:
            load_url(
                "https://www.example.com/members",
                transport=_answer(403, {"Content-Type": "text/html"}, b"<h1>Forbidden</h1>"),
            )
        assert refused.value.status == 403

    def test_an_article_about_cloudflare_is_read(self):
        doc = load_url(
            "https://blog.example.com/cloudflare",
            transport=_answer(200, {"Content-Type": "text/html"}, _article_about_cloudflare()),
        )
        assert "How Cloudflare's challenge pages work" in doc.text

    def test_a_refused_scheme_does_not_repeat_the_signature(self):
        with pytest.raises(ValueError, match="http or https") as caught:
            load_url("ftp://files.example.com/q3.pdf?token=SECRET")
        assert "SECRET" not in str(caught.value)


# ============================================== the extraction service ===


class _Elsewhere(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://elsewhere.invalid/x.pdf?sig=OTHERSECRET")
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_a_redirect_off_the_allowed_hosts_is_refused_without_either_signature(monkeypatch):
    from vectrixdb.api.extraction import ExtractionService, _Refused

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Elsewhere)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/raw/q3.pdf?sv=2024-01-01&sig=SECRET"
        with pytest.raises(_Refused) as caught:
            ExtractionService(url_hosts=("127.0.0.1",), timeout=5.0).fetch(url)
    finally:
        httpd.shutdown()
        httpd.server_close()
    said = str(caught.value)
    assert caught.value.status == 403 and "elsewhere.invalid" in said
    assert "SECRET" not in said, "neither the address's signature nor the redirect's"


KEY = "a-test-key"
HEADERS = {"api-key": KEY}
SIGNED_PAGE = "https://www.td.com/about.html?sv=2024-01-01&sig=SECRET&se=2026-12-31"
SIGNED_CALL = "https://www.td.com/call.wav?token=SECRET"


def _speech():
    from vectrixdb.extract.engines import AzureSpeech

    def transport(method, url, headers, body, timeout):
        phrases = [{"offsetMilliseconds": 0, "durationMilliseconds": 4000, "text": "Rates rose."}]
        return 200, {}, json.dumps({"phrases": phrases}).encode()

    return AzureSpeech("https://s.cognitiveservices.azure.com", "k", transport=transport)


@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from vectrixdb.api.extraction import ExtractionService, create_extraction_app

    monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
    for name in (
        "VECTRIXDB_SIGNIN",
        "VECTRIXDB_ALLOW_OPEN",
        "VECTRIXDB_READ_ONLY_API_KEY",
        "VECTRIXDB_EXTRACT_PREFIX",
        "VECTRIXDB_EXTRACT_GATEWAY_PATHS",
        "VECTRIXDB_ROOT_PATH",
        "VECTRIXDB_PUBLIC_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    pages = {
        SIGNED_PAGE: (200, {"Content-Type": "text/html"}, b"<h1>About TD</h1><p>A bank.</p>"),
        SIGNED_CALL: (200, {"Content-Type": "audio/wav"}, b"RIFF" + bytes(64)),
        "https://www.td.com/rates": (403, {"Server": "cloudflare", "CF-RAY": "1"}, CLOUDFLARE_JS),
        "https://www.td.com/gone?token=SECRET": (404, {}, b"no"),
        "https://www.td.com/busy?token=SECRET": (429, {"Retry-After": "30"}, b"slow down"),
    }
    service = ExtractionService(
        audio=_speech(),
        url_hosts=("www.td.com",),
        transport=lambda method, url, headers, body, timeout: pages.get(url, (404, {}, b"no")),
    )
    return TestClient(create_extraction_app(service))


class TestTheExtractionService:
    def test_a_page_s_source_is_kept_without_its_signature(self, client):
        said = client.post(
            "/transcribe/webpage",
            json={"url": SIGNED_PAGE},
            headers={**HEADERS, "Accept": "application/json"},
        ).json()
        assert "About TD" in said["text"]
        assert said["metadata"]["source"] == "https://www.td.com/about.html"
        assert "SECRET" not in json.dumps(said)

    def test_a_recording_s_source_is_kept_without_its_token(self, client):
        for route in ("/transcribe/audio_url", "/transcribe/auto"):
            said = client.post(
                route, json={"url": SIGNED_CALL}, headers={**HEADERS, "Accept": "application/json"}
            ).json()
            assert said["metadata"]["source"] == "https://www.td.com/call.wav", route
            assert "SECRET" not in json.dumps(said), route

    def test_a_refusal_never_repeats_the_token(self, client):
        response = client.post(
            "/transcribe/webpage",
            json={"url": "https://www.td.com/gone?token=SECRET"},
            headers=HEADERS,
        )
        assert response.status_code == 502 and "SECRET" not in response.text

    def test_a_site_that_asks_for_time_is_a_503_to_come_back_to(self, client):
        response = client.post(
            "/transcribe/webpage",
            json={"url": "https://www.td.com/busy?token=SECRET"},
            headers=HEADERS,
        )
        assert response.status_code == 503 and "SECRET" not in response.text
        assert "too many requests, and asked for 30 seconds" in response.json()["message"]

    def test_a_bot_check_is_refused_with_the_site_and_the_reason(self, client):
        response = client.post(
            "/transcribe/webpage", json={"url": "https://www.td.com/rates"}, headers=HEADERS
        )
        assert response.status_code == 502
        assert (
            "www.td.com answered 403 with a Cloudflare challenge page" in response.json()["message"]
        )


# ===================================================== the ingest worker ===


class _Blobs:
    """A BlobServiceClient's one method the worker calls, over a dict."""

    def __init__(self, blobs):
        self.blobs = blobs

    def get_blob_client(self, container, blob):
        data = self.blobs[(container, blob)]

        class _Client:
            def download_blob(self):
                class _Stream:
                    def readall(self):
                        return data

                return _Stream()

        return _Client()


class TestTheIngestWorker:
    def test_a_signed_blob_address_is_its_id_and_source_without_the_signature(self, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.worker import BlobFetcher, IngestEvent, IngestWorker

        db = Vectrix("inbox", path=str(tmp_path / "db"), mode="dense")
        try:
            blobs = _Blobs(
                {("raw", "report.md"): b"# Report\n\nDeposits rose in the third quarter."}
            )
            worker = IngestWorker(db, BlobFetcher(blobs))
            uri = "https://acct.blob.core.windows.net/raw/report.md?sv=2024-01-01&sig=SECRET&se=2026-12-31"
            out = worker.handle(IngestEvent(kind="created", uri=uri))
            assert out.action == "created"
            assert out.doc_id == "https://acct.blob.core.windows.net/raw/report.md"
            kept = [m for _, _, m in db._collection._iter_documents_raw()]
            assert kept and all(m["source"] == out.doc_id for m in kept)
            assert "SECRET" not in json.dumps(kept)
            # The same object signed again is the same document, not a second one.
            again = worker.handle(IngestEvent(kind="created", uri=uri.replace("SECRET", "RENEWED")))
            assert again.action == "unchanged" and again.doc_id == out.doc_id
        finally:
            db.close()
