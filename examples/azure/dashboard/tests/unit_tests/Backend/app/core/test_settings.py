"""The retrieval service behind a gateway: UPSTREAM_PREFIX and UPSTREAM_GATEWAY_PATHS.

What is held to. The two settings are read the way the retrieval service reads
its own VECTRIXDB_PREFIX and VECTRIXDB_GATEWAY_PATHS, and a mistake stops the
start with the setting named; each forwarded call, the brand this service asks
for and the live socket go where the gateway publishes their route, the
longest name that fits deciding; and with neither set, every path goes as it
came, as it always has.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[5] / "Backend"
sys.path.insert(0, str(BACKEND))

from app.api.live import ws_url  # noqa: E402
from app.core.settings import Settings, gateway_paths  # noqa: E402
from app.main import build  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

GATEWAY = "https://gateway.example.com"
PATHS = "api/v1=/files/search, api/v1/collections/handbook=/files/handbook, auth=/files/auth"


class TestReadingTheTwoSettings:
    def test_as_the_gateway_team_hands_them_over(self):
        assert gateway_paths(" api/v1 = files/search/ ,auth=/files/auth ,") == {"api/v1": "/files/search", "auth": "/files/auth"}
        got = Settings.from_env({"UPSTREAM": GATEWAY + "/", "UPSTREAM_PREFIX": "/acme/", "UPSTREAM_GATEWAY_PATHS": PATHS})
        assert got.upstream == GATEWAY and got.prefix == "/acme" and got.paths["auth"] == "/files/auth"

    @pytest.mark.parametrize("given, says", [
        ("api/v1", "route=path with both given"),
        ("api/v1=", "route=path with both given"),
        ("api/v1=/a, api/v1=/b", "given two gateway paths"),
        ("api/v1=/../admin", "a path of names"),
    ])
    def test_a_mistake_stops_the_start_and_names_the_setting(self, given, says):
        with pytest.raises(ValueError, match=says):
            Settings.from_env({"UPSTREAM": GATEWAY, "UPSTREAM_GATEWAY_PATHS": given})

    def test_nothing_set_is_every_path_as_it_came(self):
        plain = Settings.from_env({"UPSTREAM": GATEWAY})
        assert plain.prefix == "" and dict(plain.paths) == {} and plain.upstream_path("/api/v1/collections") == "/api/v1/collections"


class TestWhereEachCallGoes:
    SETTINGS = Settings(upstream=GATEWAY, prefix="/acme", paths=gateway_paths(PATHS))

    def test_its_gateway_path_then_the_prefix_then_the_route(self):
        assert self.SETTINGS.upstream_path("/api/v1/collections") == "/files/search/acme/api/v1/collections"
        assert self.SETTINGS.upstream_path("/auth/me") == "/files/auth/acme/auth/me"

    def test_the_longest_name_that_fits_decides(self):
        assert self.SETTINGS.upstream_path("/api/v1/collections/handbook/text-search") == "/files/handbook/acme/api/v1/collections/handbook/text-search"
        assert self.SETTINGS.upstream_path("/api/v1/collections/handbookish") == "/files/search/acme/api/v1/collections/handbookish"

    def test_a_route_with_no_gateway_path_is_under_the_prefix_alone(self):
        assert self.SETTINGS.upstream_path("/health") == "/acme/health"

    def test_a_forwarded_call_reaches_the_service_through_the_gateway(self, tmp_path):
        asked = []

        def handle(request):
            asked.append(request)
            return httpx.Response(200, json={"ok": True})

        app = build(Settings(upstream=GATEWAY, prefix="/acme", paths=gateway_paths(PATHS), site=tmp_path / "none"), transport=httpx.MockTransport(handle))
        with TestClient(app) as reader:
            reader.get("/api/v1/collections?limit=10")
            reader.post("/auth/break-glass", json={"username": "x"})
        assert [str(r.url) for r in asked] == [
            f"{GATEWAY}/files/search/acme/api/v1/collections?limit=10",
            f"{GATEWAY}/files/auth/acme/auth/break-glass",
        ]

    def test_the_brand_is_asked_for_where_it_is_published(self, tmp_path):
        asked = []

        def handle(request):
            asked.append(request)
            return httpx.Response(200, json={"name": "Northwind", "custom": True})

        site = tmp_path / "dist"
        site.mkdir()
        (site / "index.html").write_text('<!DOCTYPE html><script id="vx-brand-data" type="application/json">{"name": "VectrixDB"}</script>', encoding="utf-8")
        app = build(Settings(upstream=GATEWAY, prefix="/acme", site=site), transport=httpx.MockTransport(handle))
        with TestClient(app) as reader:
            assert "Northwind" in reader.get("/").text
        assert [str(r.url) for r in asked] == [f"{GATEWAY}/acme/brand.json"]

    def test_the_live_socket_opens_where_its_route_is_published(self):
        settings = Settings(upstream=GATEWAY, prefix="/acme", paths={"ws": "/files/live"})
        assert ws_url(settings.upstream, settings.upstream_path("/ws")) == "wss://gateway.example.com/files/live/acme/ws"
