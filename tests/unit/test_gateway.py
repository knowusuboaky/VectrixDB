"""Behind APIM, an API gateway or any reverse proxy.

The path the app is served under, the address it holds callers to, and the
promise that what the dashboard calls is what the OpenAPI document a gateway
team imports describes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.api.server import create_app, root_path_from_env  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402

DASHBOARD = Path(__file__).resolve().parents[2] / "vectrixdb" / "dashboard"


@pytest.fixture(autouse=True)
def _no_inherited_settings(monkeypatch):
    for name in ("VECTRIXDB_ROOT_PATH", "VECTRIXDB_PUBLIC_URL", "VECTRIXDB_TRUSTED_PROXIES", "VECTRIXDB_PATH"):
        monkeypatch.delenv(name, raising=False)


class TestTheRootPath:
    def test_nothing_set_is_the_root(self):
        assert root_path_from_env({}) == ""

    def test_the_public_url_carries_it(self):
        assert root_path_from_env({"VECTRIXDB_PUBLIC_URL": "https://apim.company.com/vectrixdb"}) == "/vectrixdb"

    def test_a_public_url_with_no_path_is_the_root(self):
        assert root_path_from_env({"VECTRIXDB_PUBLIC_URL": "https://vectors.company.com"}) == ""
        assert root_path_from_env({"VECTRIXDB_PUBLIC_URL": "https://vectors.company.com/"}) == ""

    def test_the_setting_wins_and_is_tidied(self):
        env = {"VECTRIXDB_ROOT_PATH": "vx/", "VECTRIXDB_PUBLIC_URL": "https://apim.company.com/vectrixdb"}
        assert root_path_from_env(env) == "/vx"

    def test_a_server_under_a_path_answers_both_shapes(self, tmp_path, monkeypatch):
        """A gateway either forwards the prefix or strips it before the
        backend sees it, and a deployment should not have to know which."""
        monkeypatch.setenv("VECTRIXDB_ROOT_PATH", "/vectrixdb")
        with TestClient(create_app(db_path=str(tmp_path), enable_dashboard=False)) as client:
            assert client.get("/vectrixdb/health").status_code == 200
            assert client.get("/health").status_code == 200

    def test_the_document_a_gateway_imports_carries_the_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_PUBLIC_URL", "https://apim.company.com/vectrixdb")
        with TestClient(create_app(db_path=str(tmp_path), enable_dashboard=False)) as client:
            spec = client.get("/vectrixdb/openapi.json").json()
        assert spec["servers"] == [{"url": "/vectrixdb"}]
        assert "/api/v1/collections" in spec["paths"]

    def test_without_a_path_the_document_names_no_server(self, tmp_path):
        with TestClient(create_app(db_path=str(tmp_path), enable_dashboard=False)) as client:
            assert "servers" not in client.get("/openapi.json").json()

    @pytest.mark.parametrize("path", ["/vectrixdb/dashboard/", "/dashboard/"])
    def test_the_dashboard_is_served_in_both_shapes(self, tmp_path, monkeypatch, path):
        """A mount is matched against the path as ASGI shapes it, prefix and
        all, so a gateway that strips the prefix left the dashboard a 404
        until a stripped request got its prefix back at the edge."""
        monkeypatch.setenv("VECTRIXDB_ROOT_PATH", "/vectrixdb")
        with TestClient(create_app(db_path=str(tmp_path), enable_dashboard=True)) as client:
            assert client.get(path).status_code == 200

    def test_the_trailing_slash_redirect_keeps_the_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_ROOT_PATH", "/vectrixdb")
        with TestClient(create_app(db_path=str(tmp_path), enable_dashboard=True)) as client:
            reply = client.get("/vectrixdb/dashboard", follow_redirects=False)
        assert reply.status_code in (301, 307) and reply.headers["location"].endswith("/vectrixdb/dashboard/")


class TestTheRoutePath:
    """What the layers around the routes read, so a prefix cannot make the
    role table, the guest rules or the masking layer miss."""

    def test_the_prefix_comes_off(self):
        from vectrixdb.api.rootpath import route_path

        assert route_path({"path": "/vectrixdb/api/v1/collections", "root_path": "/vectrixdb"}) == "/api/v1/collections"

    def test_a_path_that_never_had_it_is_left_alone(self):
        from vectrixdb.api.rootpath import route_path

        assert route_path({"path": "/api/v1/collections", "root_path": "/vectrixdb"}) == "/api/v1/collections"

    def test_no_root_path_at_all(self):
        from vectrixdb.api.rootpath import route_path

        assert route_path({"path": "/api/v1/collections", "root_path": ""}) == "/api/v1/collections"


class TestWhoIsCalling:
    def test_the_proxy_list_is_read_once_at_start_up(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_TRUSTED_PROXIES", "10.0.0.0/8, 192.168.1.5")
        app = create_app(db_path=str(tmp_path), enable_dashboard=False)
        assert [str(n) for n in app.state.trusted_proxies] == ["10.0.0.0/8", "192.168.1.5/32"]

    def test_a_list_nobody_can_parse_stops_the_start(self, tmp_path, monkeypatch):
        """Read as "trust nothing" it would leave the lockout counting the
        gateway, which is the failure this setting exists to prevent."""
        monkeypatch.setenv("VECTRIXDB_TRUSTED_PROXIES", "apim.company.com")
        with pytest.raises(ConfigurationError) as info:
            create_app(db_path=str(tmp_path), enable_dashboard=False)
        assert "apim.company.com" in str(info.value)

    def test_the_address_the_lockout_and_the_log_use(self, tmp_path, monkeypatch):
        """_address is what the sign-in lockout, the guest limit and the
        access log all key on."""
        from vectrixdb.api.signin import _address

        monkeypatch.setenv("VECTRIXDB_TRUSTED_PROXIES", "10.0.0.0/8")
        app = create_app(db_path=str(tmp_path), enable_dashboard=False)

        class Request:
            def __init__(self, peer, forwarded):
                self.app = app
                self.client = type("C", (), {"host": peer})()
                self.headers = {"x-forwarded-for": forwarded}

        assert _address(Request("10.0.0.9", "203.0.113.7")) == "203.0.113.7"
        assert _address(Request("198.51.100.4", "203.0.113.7")) == "198.51.100.4"


class TestTheDashboardCallsDocumentedRoutes:
    def test_the_api_base_comes_from_the_page_not_the_origin(self):
        """Taking the origin sent every call to the gateway's root, outside
        the route the app is published under."""
        app_js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
        assert "const API = location.origin + location.pathname" in app_js
        assert "window.location.origin" not in app_js

    @pytest.mark.parametrize("name", ["app.js", "evaluate.js", "chunking.js", "trends.js", "demo-data.js", "sso-boot.js"])
    def test_every_api_path_is_a_documented_one(self, name):
        """The unversioned /api/... aliases are left out of the OpenAPI
        document, so a gateway team publishing what the document lists would
        not publish them, and the dashboard would break."""
        source = (DASHBOARD / name).read_text(encoding="utf-8")
        paths = set(re.findall(r"[`'\"](/api/[a-zA-Z0-9_${}./-]*)", source))
        undocumented = {p for p in paths if not p.startswith(("/api/v1/", "/api/v2/"))}
        assert not undocumented, f"{name} calls paths the OpenAPI document does not list: {sorted(undocumented)}"


class TestTheDocumentThatShips:
    def test_it_is_the_route_table_the_server_serves(self, tmp_path):
        shipped = json.loads((Path(__file__).resolve().parents[2] / "docs" / "reference" / "openapi.json").read_text(encoding="utf-8"))
        live = create_app(db_path=str(tmp_path), enable_dashboard=False).openapi()
        assert set(shipped["paths"]) == set(live["paths"])
        assert shipped["info"]["version"] == live["info"]["version"]
