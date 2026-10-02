"""The forwarded calls: what the retrieval service sees, and what the browser gets back.

The service is a fake here, an httpx transport that records the request and
answers from the test, so nothing leaves this machine.

What is held to. The caller reaches the service as themselves: their cookie
and their key travel untouched, because the entitlement policy and the audit
record are about the person, not about this Backend. Our key is added only
when the caller sent none. Everything the service answers comes back as it
came, cookies included, and only the paths named in the settings are forwarded
at all.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[5] / "Backend"
sys.path.insert(0, str(BACKEND))

from app.core.settings import Settings  # noqa: E402
from app.main import build  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

UPSTREAM = "https://retrieval.example.net"


class Service:
    """The retrieval service, as a transport: it keeps what it was asked and answers what the test set."""

    def __init__(self, answer: httpx.Response | None = None) -> None:
        self.asked: list[httpx.Request] = []
        self.answer = answer
        self.raise_with: Exception | None = None

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.asked.append(request)
            if self.raise_with is not None:
                raise self.raise_with
            return self.answer or httpx.Response(200, json={"ok": True})

        return httpx.MockTransport(handle)

    @property
    def last(self) -> httpx.Request:
        assert self.asked, "the service was not called"
        return self.asked[-1]


def client(service: Service, **settings) -> TestClient:
    where = Settings(upstream=UPSTREAM, site=Path(__file__).parent / "no-build-here", **settings)
    return TestClient(build(where, transport=service.transport()))


class TestWhatTheServiceSees:
    def test_the_method_path_query_and_body_arrive_as_they_were(self):
        service = Service(httpx.Response(201, json={"added": 2}))
        with client(service) as reader:
            sent = reader.post(
                "/api/v1/collections/financial/text-upsert?wait=1", json={"points": [1, 2]}
            )
        assert sent.status_code == 201 and sent.json() == {"added": 2}
        asked = service.last
        assert asked.method == "POST"
        assert str(asked.url) == f"{UPSTREAM}/api/v1/collections/financial/text-upsert?wait=1"
        assert json.loads(asked.read()) == {"points": [1, 2]}, (
            "the body as it was sent, whitespace aside"
        )

    def test_the_caller_travels_as_themselves(self):
        """Their cookie and their key, untouched: the policy and the audit are about the person."""
        service = Service()
        with client(service, key="ours") as reader:
            reader.get(
                "/api/v1/collections",
                headers={"cookie": "vx_sid=abc", "api-key": "theirs", "x-csrf-token": "t"},
            )
        asked = service.last
        assert asked.headers["cookie"] == "vx_sid=abc"
        assert asked.headers["api-key"] == "theirs", "their key is scoped where ours may not be"
        assert asked.headers["x-csrf-token"] == "t"

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/info",
            "/auth/whoami",
            "/health",
            "/docs",
            "/openapi.json",
            "/brand.json",
            "/brand.css",
            "/brand/logo",
            "/brand/logo-dark",
            "/auth/break-glass",
        ],
    )
    def test_every_path_the_pages_ask_for_reaches_the_service(self, path):
        """The API reference the account menu links to is the service's own, so /docs travels too, and so does the brand."""
        service = Service()
        with client(service) as reader:
            reader.get(path)
        assert service.last.url.path == path

    def test_our_key_is_added_when_the_caller_sent_none(self):
        service = Service()
        with client(service, key="ours", key_header="x-api-key") as reader:
            reader.get("/api/v1/info")
        assert service.last.headers["x-api-key"] == "ours"

    def test_the_callers_headers_about_their_own_connection_do_not_travel(self):
        """Their hop ends here. Ours to the service is the client's business, and it sets its own."""
        service = Service()
        with client(service) as reader:
            reader.get("/api/v1/info", headers={"te": "trailers", "user-agent": "a browser"})
        asked = service.last
        assert "te" not in asked.headers, "a header about the browser's hop means nothing on ours"
        assert asked.headers["host"] == "retrieval.example.net", "the service's host, not ours"
        assert asked.headers["user-agent"] == "a browser", (
            "who is calling still reaches the access log"
        )


class TestWhatTheBrowserGets:
    def test_every_cookie_a_sign_in_set_comes_back(self):
        answer = httpx.Response(
            204, headers=[("set-cookie", "vx_sid=s; Path=/"), ("set-cookie", "vx_csrf=c; Path=/")]
        )
        service = Service(answer)
        with client(service) as reader:
            got = reader.post("/auth/email/verify", json={"code": "123456"})
        assert got.status_code == 204
        assert [value for name, value in got.headers.multi_items() if name == "set-cookie"] == [
            "vx_sid=s; Path=/",
            "vx_csrf=c; Path=/",
        ]

    def test_a_refusal_is_passed_on_as_it_came(self):
        service = Service(
            httpx.Response(
                403, json={"detail": "collection 'financial' carries an entitlement policy"}
            )
        )
        with client(service) as reader:
            got = reader.get("/api/v1/collections/financial/builds")
        assert got.status_code == 403 and "entitlement policy" in got.json()["detail"]

    def test_a_service_that_does_not_answer_is_a_bad_gateway(self):
        service = Service()
        service.raise_with = httpx.ConnectError("no route")
        with client(service) as reader:
            got = reader.get("/api/v1/info")
        assert got.status_code == 502 and "did not answer" in got.json()["detail"]


class TestWhatIsNotForwarded:
    @pytest.mark.parametrize("path", ["/nope", "/healthz", "/pages/app.js"])
    def test_only_the_named_paths_go_upstream(self, path):
        service = Service()
        with client(service) as reader:
            reader.get(path)
        assert not service.asked, f"{path} reached the service"

    def test_without_an_upstream_it_says_so_rather_than_failing_oddly(self):
        service = Service()
        where = Settings(upstream="", site=Path(__file__).parent / "no-build-here")
        with TestClient(build(where, transport=service.transport())) as reader:
            got = reader.get("/api/v1/info")
        assert got.status_code == 503 and "UPSTREAM" in got.json()["detail"]
        assert not service.asked


class TestItsOwnHealth:
    def test_it_answers_without_calling_the_service(self):
        service = Service()
        with client(service, key="ours") as reader:
            said = reader.get("/healthz").json()
        assert said == {
            "ok": True,
            "upstream": "retrieval.example.net",
            "keyed": True,
            "pages_built": False,
        }
        assert not service.asked, (
            "a liveness check that waits on another service reports its cold start as ours"
        )


class TestACallFromAnotherSite:
    """Our key goes on every call that carries none, so a page on another site
    that the operator has open could post here and act with it."""

    def test_a_write_from_another_sites_page_is_not_forwarded(self):
        service = Service()
        with client(service, key="ours") as reader:
            sent = reader.delete(
                "/api/v1/collections/media", headers={"Origin": "https://evil.example"}
            )
        assert sent.status_code == 403 and not service.asked

    def test_a_write_from_our_own_page_is(self):
        service = Service()
        with client(service, key="ours") as reader:
            sent = reader.delete(
                "/api/v1/collections/media", headers={"Origin": "http://testserver"}
            )
        assert sent.status_code == 200 and service.last.method == "DELETE"

    def test_behind_a_proxy_the_forwarded_host_is_ours(self):
        service = Service()
        with client(service, key="ours") as reader:
            sent = reader.post(
                "/api/v1/collections/media/search",
                json={},
                headers={
                    "Origin": "https://dash.example.net",
                    "X-Forwarded-Host": "dash.example.net",
                },
            )
        assert sent.status_code == 200

    def test_a_call_with_no_origin_is_not_a_browsers_and_passes(self):
        service = Service()
        with client(service, key="ours") as reader:
            assert reader.post("/api/v1/collections/media/search", json={}).status_code == 200

    def test_a_read_is_forwarded_whatever_its_origin(self):
        service = Service()
        with client(service, key="ours") as reader:
            assert (
                reader.get(
                    "/api/v1/collections", headers={"Origin": "https://evil.example"}
                ).status_code
                == 200
            )
