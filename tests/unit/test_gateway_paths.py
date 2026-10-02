"""A gateway that publishes each part of the server under a path of its own, in front of a prefix.

What is being held to: VECTRIXDB_PREFIX and VECTRIXDB_GATEWAY_PATHS are read
the way the gateway team writes them, and a mistake is refused by name; a
route's path is its gateway path, then the prefix, then the route, the
longest name that fits deciding; a name no route falls under stops the
start, while one collection's own route may be named; each listed route
answers with its gateway path or without it and with the prefix or without
it, a gateway path opens only its own routes, and nothing is redirected for
a slash, except the dashboard's own address, relatively; the dashboard's
page is told the map and its links point where they are published; the
OpenAPI document carries the prefix; the key and the token arrive in the
headers the settings name, and a token header of the settings' own leaves
Authorization to the gateway; single sign-on returns through the gateway,
its cookie is kept for the path the browser sees, and a sign-in email links
through it; the host's own path and the prefix are each put back when a
gateway takes one off; the extraction service reads its lists the same way;
and vectrixdb check --url asks each route where it is published.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import json
import os
import re
import sys

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

sys.path.insert(0, os.path.dirname(__file__))
from fake_idp import ISSUER, FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.api.gateway import Gateway, GatewayPathsMiddleware, declared_paths, read_gateway_paths, route_prefix  # noqa: E402
from vectrixdb.api.rootpath import RootPathMiddleware  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.signin import OidcConfig, SignInConfig  # noqa: E402

KEY = "the-full-api-key"
SECRET = "k" * 48
PUBLIC = "https://gateway.example.com"
PATHS = "api/v1=/files/search, auth=/files/auth, health=/files/health, dashboard=/files/dash"


def create_app(**kwargs):
    """The server as it is now; another test re-imports vectrixdb.api."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


@pytest.fixture(autouse=True)
def _no_inherited_settings(monkeypatch):
    for name in [n for n in os.environ if n.startswith("VECTRIXDB_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def behind(tmp_path, monkeypatch):
    """The server with a prefix and gateway paths, and a key. Call it with what the test changes."""
    made = []

    def build(prefix="acme", paths=PATHS, **env):
        monkeypatch.setenv("VECTRIXDB_API_KEY", KEY)
        if prefix is not None:
            monkeypatch.setenv("VECTRIXDB_PREFIX", prefix)
        if paths is not None:
            monkeypatch.setenv("VECTRIXDB_GATEWAY_PATHS", paths)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        client = TestClient(create_app(db_path=str(tmp_path / f"db{len(made)}")), follow_redirects=False)
        client.__enter__()
        made.append(client)
        return client

    yield build
    for client in made:
        client.__exit__(None, None, None)


def ours(reply) -> bool:
    """A reply in the server's own shape, not a gateway's."""
    try:
        return {"ok", "message", "data", "detail"} <= set(reply.json())
    except ValueError:
        return False


# ----------------------------------------------------------- the settings


class TestReadingTheSettings:
    @pytest.mark.parametrize("given, expected", [("acme", "/acme"), ("/acme/", "/acme"), (" a/b ", "/a/b"), ("", ""), (None, "")])
    def test_a_prefix_whatever_it_was_written_as(self, given, expected):
        assert route_prefix(given) == expected

    def test_a_prefix_is_a_path_of_names(self):
        with pytest.raises(ConfigurationError, match="a path of names"):
            route_prefix("acme/../admin")

    def test_the_gateway_paths_as_the_gateway_team_hands_them_over(self):
        assert read_gateway_paths(" api/v1 = /files/search , auth=files/auth/ ,") == {"api/v1": "/files/search", "auth": "/files/auth"}
        assert read_gateway_paths({"/health/": "files/health"}) == {"health": "/files/health"}

    @pytest.mark.parametrize("given, says", [
        ("api/v1", "route=path"),
        ("api/v1=", "both given"),
        ("=/files", "both given"),
        ("api/v1=/a,api/v1=/b", "given two gateway paths"),
        ("api/v1=/../admin", "a path of names"),
    ])
    def test_a_mistake_is_refused_and_the_setting_named(self, given, says):
        with pytest.raises(ConfigurationError, match=says) as stopped:
            Gateway.from_env({"VECTRIXDB_GATEWAY_PATHS": given})
        assert str(stopped.value).startswith("VECTRIXDB_GATEWAY_PATHS: ")

    def test_the_headers_are_read_as_names_and_a_wrong_one_refused(self):
        got = Gateway.from_env({"VECTRIXDB_KEY_HEADER": " X-Search-Key ", "VECTRIXDB_TOKEN_HEADER": "X-User-Token"})
        assert got.key_header == "x-search-key" and got.token_header == "x-user-token"
        assert Gateway.from_env({}).key_header == "api-key" and Gateway.from_env({}).token_header == "authorization"
        with pytest.raises(ConfigurationError, match="cannot be the name of an HTTP header"):
            Gateway.from_env({"VECTRIXDB_KEY_HEADER": "api key"})

    def test_the_host_is_never_a_setting_of_its_own(self):
        got = Gateway.from_env({"VECTRIXDB_PUBLIC_URL": "https://gateway.example.com/"})
        assert got.origin == "https://gateway.example.com" and got.root == ""


class TestTheAddressOfARoute:
    GATEWAY = Gateway(prefix="/acme", paths={"api/v1": "/files/search", "api/v1/collections/handbook": "/files/handbook"}, origin=PUBLIC)

    def test_its_gateway_path_then_the_prefix_then_the_route(self):
        assert self.GATEWAY.visible("/api/v1/collections") == "/files/search/acme/api/v1/collections"
        assert self.GATEWAY.address("/api/v1/collections") == "https://gateway.example.com/files/search/acme/api/v1/collections"

    def test_the_longest_name_that_fits_decides(self):
        assert self.GATEWAY.visible("/api/v1/collections/handbook/text-search") == "/files/handbook/acme/api/v1/collections/handbook/text-search"
        assert self.GATEWAY.visible("/api/v1/collections/handbookish") == "/files/search/acme/api/v1/collections/handbookish"

    def test_a_route_with_no_gateway_path_is_under_the_prefix_alone(self):
        assert self.GATEWAY.visible("/health") == "/acme/health"

    def test_the_host_path_goes_in_front_of_everything(self):
        assert Gateway(root="/edge", prefix="/acme", paths={"auth": "/files/auth"}).visible("/auth/me") == "/edge/files/auth/acme/auth/me"

    def test_with_nothing_set_a_route_is_itself(self):
        plain = Gateway.at("https://vectors.company.com")
        assert plain.visible("/auth/me") == "/auth/me" and not plain.shaped and not plain.dressed

    def test_under_a_path_the_page_is_told_so_its_links_carry_it(self):
        under = Gateway.at("https://apim.company.com/vectrixdb")
        assert under.visible("/auth/me") == "/vectrixdb/auth/me" and not under.shaped and under.dressed


class TestANameNoRouteFallsUnder:
    ROUTES = ["/api/v1/collections", "/api/v1/collections/{name}/text-search", "/auth/me", "/dashboard", "/files/{path:path}"]

    def test_a_family_or_one_route_is_fine(self):
        Gateway(paths={"api/v1": "/a", "auth/me": "/b", "dashboard": "/c"}).check(self.ROUTES)

    def test_one_collections_own_route_may_be_named(self):
        Gateway(paths={"api/v1/collections/handbook/text-search": "/files/handbook"}).check(self.ROUTES)
        Gateway(paths={"files/anything/at/all": "/f"}).check(self.ROUTES)

    def test_a_name_no_route_falls_under_stops_the_start(self):
        with pytest.raises(ConfigurationError, match="names api/v2, which no route here falls under"):
            Gateway(paths={"api/v2": "/a", "auth": "/b"}).check(self.ROUTES)

    def test_the_routes_of_included_routers_are_found_too(self, behind):
        found = declared_paths(behind(prefix=None, paths=None).app.routes)
        assert "/api/v1/collections" in found and "/auth/me" in found and len(found) > 60

    def test_the_server_will_not_start_with_one(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_GATEWAY_PATHS", "api/v1=/files/search, api/v9=/files/nine")
        with pytest.raises(ConfigurationError, match="api/v9"):
            create_app(db_path=str(tmp_path))


# ------------------------------------------------------ the server behind it


class TestTheServerAnswersEveryShape:
    @pytest.mark.parametrize("path", [
        "/files/search/acme/api/v1/collections",
        "/acme/api/v1/collections",
        "/files/search/api/v1/collections",
        "/api/v1/collections",
    ])
    def test_a_listed_route_with_or_without_its_gateway_path_and_the_prefix(self, behind, path):
        reply = behind().get(path, headers={"api-key": KEY})
        assert reply.status_code == 200, (path, reply.text)

    def test_a_gateway_path_opens_only_its_own_routes(self, behind):
        client = behind()
        for path in ("/files/search/acme/health", "/files/auth/acme/api/v1/collections", "/files/health/acme/auth/status"):
            reply = client.get(path, headers={"api-key": KEY})
            assert reply.status_code == 404 and ours(reply), path

    def test_nothing_is_redirected_for_a_slash(self, behind):
        reply = behind().get("/files/search/acme/api/v1/collections/", headers={"api-key": KEY})
        assert reply.status_code == 404

    def test_the_dashboards_own_address_redirects_relatively_so_it_holds_through_the_gateway(self, behind):
        reply = behind().get("/files/dash/acme/dashboard")
        assert reply.status_code == 307 and reply.headers["location"] == "dashboard/"

    def test_the_document_a_gateway_team_imports_carries_the_prefix(self, behind):
        spec = behind().get("/acme/openapi.json").json()
        assert spec["servers"] == [{"url": "/acme"}] and "/api/v1/collections" in spec["paths"]


class TestTheDashboardIsToldTheMap:
    def test_the_page_carries_the_map_as_data(self, behind):
        page = behind().get("/files/dash/acme/dashboard/").text
        found = re.search(r'<script type="application/json" id="vx-gateway-data">([^<]*)</script>', page)
        assert found, "data, not a script: the page's policy runs no inline script"
        told = json.loads(found.group(1))
        assert told == {
            "root": "", "prefix": "/acme", "key_header": "api-key",
            "paths": {"api/v1": "/files/search", "auth": "/files/auth", "health": "/files/health", "dashboard": "/files/dash"},
        }

    def test_the_link_it_writes_itself_points_where_it_is_published(self, behind):
        page = behind().get("/files/dash/acme/dashboard/").text
        assert 'href="/acme/docs"' in page and 'href="/docs"' not in page

    def test_a_brands_logo_is_linked_where_it_is_published(self, behind, tmp_path):
        logo = tmp_path / "logo.svg"
        logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8" fill="#135"/></svg>', encoding="utf-8")
        page = behind(VECTRIXDB_BRAND_NAME="Northwind", VECTRIXDB_BRAND_LOGO=str(logo)).get("/files/dash/acme/dashboard/").text
        assert 'src="/acme/brand/logo"' in page and '"/brand/logo"' not in page

    def test_with_no_gateway_the_page_is_left_as_it_was(self, behind):
        page = behind(prefix=None, paths=None).get("/dashboard/").text
        assert "vx-gateway-data" not in page and 'href="/docs"' in page

    def test_a_key_header_of_the_settings_own_is_told_too(self, behind):
        page = behind(prefix=None, paths=None, VECTRIXDB_KEY_HEADER="x-search-key").get("/dashboard/").text
        assert '"key_header": "x-search-key"' in page


class TestTheHeadersTheSettingsName:
    def test_a_key_arrives_in_the_header_named(self, behind):
        client = behind(prefix=None, paths=None, VECTRIXDB_KEY_HEADER="x-search-key")
        assert client.post("/api/v1/collections", json={"name": "c1", "dimension": 4}, headers={"x-search-key": KEY}).status_code in (200, 201)
        refused = client.post("/api/v1/collections", json={"name": "c2", "dimension": 4}, headers={"api-key": KEY})
        assert refused.status_code == 401, "the old header is not read once another is named"

    @pytest.mark.parametrize("value", [KEY, f"Bearer {KEY}"])
    def test_a_token_header_of_the_settings_own_takes_the_value_bare_or_after_bearer(self, behind, value):
        client = behind(prefix=None, paths=None, VECTRIXDB_TOKEN_HEADER="x-user-token")
        reply = client.post("/api/v1/collections", json={"name": "c1", "dimension": 4}, headers={"x-user-token": value})
        assert reply.status_code in (200, 201), reply.text

    def test_then_authorization_is_left_to_the_gateway(self, behind):
        client = behind(prefix=None, paths=None, VECTRIXDB_TOKEN_HEADER="x-user-token")
        reply = client.post("/api/v1/collections", json={"name": "c1", "dimension": 4}, headers={"Authorization": f"Bearer {KEY}"})
        assert reply.status_code == 401

    def test_the_default_still_reads_authorization(self, behind):
        client = behind(prefix=None, paths=None)
        assert client.post("/api/v1/collections", json={"name": "c1", "dimension": 4}, headers={"Authorization": f"Bearer {KEY}"}).status_code in (200, 201)


# ------------------------------------------------------ sign-in through it


class TestSignInThroughTheGateway:
    @pytest.fixture
    def sso(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_PREFIX", "acme")
        monkeypatch.setenv("VECTRIXDB_GATEWAY_PATHS", PATHS)
        monkeypatch.setenv("VECTRIXDB_PUBLIC_URL", PUBLIC)
        idp = FakeIdp()

        class Mail:
            sent: list = []

            def __call__(self, to, subject, text):
                self.sent.append(text)

        mail = Mail()
        config = SignInConfig(
            methods=("oidc", "email"), secrets=(SECRET,), public_url=PUBLIC, users=(("ada@example.com", "admin"), ("olu@example.com", "viewer")),
            store_path=tmp_path / "auth" / "signin.db", access_log=tmp_path / "auth" / "access.jsonl", sender=mail,
            oidc=OidcConfig(issuer=ISSUER, client_id="vectrixdb", client_secret="s3cret", role_map={"g-admins": "admin"}),
        )
        app = create_app(db_path=str(tmp_path / "db"), signin=config, oidc_transport=idp.transport)
        with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
            yield client, idp, mail

    def test_the_provider_returns_the_browser_through_the_gateway(self, sso):
        client, idp, _ = sso
        start = client.get("/files/auth/acme/auth/oidc/start")
        assert "redirect_uri=https%3A%2F%2Fgateway.example.com%2Ffiles%2Fauth%2Facme%2Fauth%2Foidc%2Fcallback" in start.headers["location"]
        cookie = start.headers["set-cookie"]
        assert "Path=/files/auth/acme/auth/oidc/callback" in cookie, "kept for the path the browser comes back to"
        code, state = idp.authorize(start.headers["location"])
        back = client.get("/files/auth/acme/auth/oidc/callback", params={"code": code, "state": state})
        assert back.status_code == 302 and back.headers["location"] == "/files/dash/acme/dashboard/"
        assert client.get("/files/auth/acme/auth/me").json()["data"]["person"]["email"] == "ada@example.com"

    def test_a_refusal_sends_the_browser_to_the_sign_in_page_through_the_gateway(self, sso):
        client, idp, _ = sso
        idp.person["groups"] = ["unrelated"]
        start = client.get("/files/auth/acme/auth/oidc/start")
        code, state = idp.authorize(start.headers["location"])
        back = client.get("/files/auth/acme/auth/oidc/callback", params={"code": code, "state": state})
        assert back.headers["location"] == "/files/dash/acme/dashboard/#/signin?error=no_role"

    def test_a_sign_in_email_links_through_the_gateway(self, sso):
        client, _, mail = sso
        client.post("/files/auth/acme/auth/email/begin", json={"email": "olu@example.com"})
        assert re.search(r"https://gateway\.example\.com/files/dash/acme/dashboard/#/enrol\?token=[\w-]+", mail.sent[-1]), mail.sent[-1]


# ----------------------------------------------------- the layers underneath


def _echo():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app, seen


class TestTheLayersUnderneath:
    @pytest.mark.parametrize("asked", ["/edge/acme/api/v1/x", "/acme/api/v1/x", "/edge/api/v1/x", "/api/v1/x"])
    def test_the_host_path_and_the_prefix_are_each_put_back(self, asked):
        import asyncio

        app, seen = _echo()
        layer = RootPathMiddleware(app, root_path="/edge", prefix="/acme")

        async def run():
            async def receive():
                return {"type": "http.request"}

            async def send(message):
                pass

            await layer({"type": "http", "path": asked, "raw_path": asked.encode()}, receive, send)

        asyncio.run(run())
        assert seen == ["/edge/acme/api/v1/x"]

    def test_a_websocket_through_the_wrong_gateway_path_is_closed(self):
        import asyncio

        app, seen = _echo()
        layer = GatewayPathsMiddleware(app, Gateway(prefix="/acme", paths={"api/v1": "/files/search", "ws": "/files/live"}))
        sent = []

        async def run():
            async def receive():
                return {"type": "websocket.connect"}

            async def send(message):
                sent.append(message)

            await layer({"type": "websocket", "path": "/files/search/acme/ws"}, receive, send)
            await layer({"type": "websocket", "path": "/files/live/acme/ws"}, receive, send)

        asyncio.run(run())
        assert sent[0] == {"type": "websocket.close", "code": 4404} and seen == ["/acme/ws"]

    def test_the_extraction_service_reads_its_lists_the_same_way(self):
        from vectrixdb.api import extraction

        assert extraction.read_gateway_paths is read_gateway_paths and extraction.route_prefix is route_prefix


# ------------------------------------------------------------ the probe


class TestCheckingItFromOutside:
    def test_each_route_is_asked_where_it_is_published(self, behind):
        from vectrixdb.probe import probe

        client = behind(VECTRIXDB_PUBLIC_URL=PUBLIC)
        asked = []

        def gateway(url, headers):
            path = url[len(PUBLIC):]
            asked.append(path)
            reply = client.get(path, headers=dict(headers))
            return reply.status_code, {k.lower(): v for k, v in reply.headers.items()}, reply.content

        findings = probe(PUBLIC, gateway, gateway=Gateway.from_env({"VECTRIXDB_PREFIX": "acme", "VECTRIXDB_GATEWAY_PATHS": PATHS}))
        errors = [f.text for f in findings if f.level == "error"]
        assert not errors, errors
        assert "/files/health/acme/health" in asked and "/files/search/acme/api/v1/collections" in asked and "/files/auth/acme/auth/status" in asked
        reached = " | ".join(f.text for f in findings if f.level == "ok")
        for where in ("/files/search", "/files/auth", "/files/health", "/files/dash"):
            assert f"the gateway path {where} reaches the server" in reached, where

    def test_a_gateway_path_that_is_not_published_is_said_so(self, behind):
        from vectrixdb.probe import probe

        client = behind(VECTRIXDB_PUBLIC_URL=PUBLIC)

        def gateway(url, headers):
            path = url[len(PUBLIC):]
            if path.startswith("/files/auth/"):
                return 404, {"content-type": "text/html"}, b"<html>Resource not found</html>"
            reply = client.get(path, headers=dict(headers))
            return reply.status_code, {k.lower(): v for k, v in reply.headers.items()}, reply.content

        findings = probe(PUBLIC, gateway, gateway=Gateway.from_env({"VECTRIXDB_PREFIX": "acme", "VECTRIXDB_GATEWAY_PATHS": PATHS}))
        assert any("the gateway path /files/auth did not reach the server" in f.text for f in findings if f.level == "error")
