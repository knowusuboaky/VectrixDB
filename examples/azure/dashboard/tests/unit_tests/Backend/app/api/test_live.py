"""The live socket's own decisions: where it opens, and what it opens with.

The socket itself is pumped against a real service in the integration tests.
What is held to here is the part with no network in it: an https service is
reached over wss and never over ws, the caller's cookie travels so the service
knows who is watching, and a service we have no address for is a close rather
than a socket that hangs open saying nothing.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[5] / "Backend"
sys.path.insert(0, str(BACKEND))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from app.api.live import CANNOT_REACH, POLICY_VIOLATION, handshake, ws_url  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.main import build  # noqa: E402


class TestWhereTheSocketOpens:
    @pytest.mark.parametrize(
        "upstream, expected",
        [
            ("https://retrieval.example.net", "wss://retrieval.example.net/ws"),
            ("http://127.0.0.1:7337", "ws://127.0.0.1:7337/ws"),
            ("https://retrieval.example.net/", "wss://retrieval.example.net/ws"),
            ("https://gateway.example.net/retrieval", "wss://gateway.example.net/retrieval/ws"),
        ],
    )
    def test_it_keeps_the_services_own_scheme_host_and_prefix(self, upstream, expected):
        assert ws_url(upstream) == expected

    def test_a_service_behind_tls_is_never_reached_in_the_clear(self):
        assert ws_url("https://retrieval.example.net").startswith("wss://")


class TestWhatTheSocketOpensWith:
    def test_the_caller_is_who_the_service_sees(self):
        headers = Headers(
            {"cookie": "vx_sid=abc", "user-agent": "a browser", "sec-websocket-key": "k"}
        )
        passing = handshake(headers)
        assert passing == {"cookie": "vx_sid=abc"}, (
            "only what says who this is, never the handshake's own headers"
        )

    def test_our_key_is_added_when_the_caller_sent_none(self):
        assert handshake(Headers({}), key="ours", key_header="x-api-key")["x-api-key"] == "ours"

    def test_their_key_is_left_as_it_is(self):
        passing = handshake(Headers({"api-key": "theirs"}), key="ours")
        assert passing["api-key"] == "theirs", "their key is scoped where ours may not be"


class TestWithNoServiceToOpenTo:
    def test_the_socket_is_closed_rather_than_held_open_saying_nothing(self):
        where = Settings(upstream="", site=Path(__file__).parent / "no-build-here")
        with TestClient(build(where)) as reader:
            with pytest.raises(WebSocketDisconnect) as closed:
                with reader.websocket_connect("/ws"):
                    pass
        assert closed.value.code == CANNOT_REACH


class TestASocketFromAnotherSite:
    def test_it_is_closed_before_our_key_goes_anywhere(self):
        """A socket is not covered by CORS: any page may open one, and ours would carry UPSTREAM_KEY."""
        where = Settings(
            upstream="https://retrieval.example.net",
            key="ours",
            site=Path(__file__).parent / "no-build-here",
        )
        with TestClient(build(where)) as reader:
            with pytest.raises(WebSocketDisconnect) as closed:
                with reader.websocket_connect("/ws", headers={"Origin": "https://evil.example"}):
                    pass
        assert closed.value.code == POLICY_VIOLATION
