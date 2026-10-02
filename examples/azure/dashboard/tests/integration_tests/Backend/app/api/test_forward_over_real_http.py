"""The forwarded calls over a real socket, against a retrieval service that is really running.

The unit tests answer from a transport in this process, which proves what we
send and what we return but never that a body is streamed, that an encoding
survives, or that a timeout is a timeout. So here the upstream is a second
ASGI app under a real uvicorn on a free loopback port. Nothing leaves this
machine: no Azure, no Function App, no network.

What is held to. A large answer arrives whole, byte for byte, and so does a
large upload. An encoded body is passed on still encoded, so the browser
decodes it and this service never spends memory doing so. Every cookie a
sign-in sets survives the hop. A service slower than our ceiling is a bad
gateway, not a five hundred from here.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import socket
import sys
import threading
from pathlib import Path
from typing import Iterator

import pytest

BACKEND = Path(__file__).resolve().parents[5] / "Backend"
sys.path.insert(0, str(BACKEND))

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.settings import Settings  # noqa: E402
from app.main import build  # noqa: E402

#: Big enough that it cannot arrive in one TCP segment, small enough to stay quick.
CHUNK = b"x" * 64 * 1024
CHUNKS = 48
WHOLE = CHUNK * CHUNKS


def service() -> FastAPI:
    """The retrieval service, as little of it as these tests need."""
    app = FastAPI()

    @app.get("/api/v1/big")
    async def big() -> StreamingResponse:
        async def body():
            for _ in range(CHUNKS):
                yield CHUNK

        return StreamingResponse(body(), media_type="application/octet-stream")

    @app.post("/api/v1/echo")
    async def echo(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse(
            {
                "length": len(body),
                "digest": hashlib.sha256(body).hexdigest(),
                "key": request.headers.get("api-key"),
                "agent": request.headers.get("user-agent"),
                "te": request.headers.get("te"),
            }
        )

    @app.get("/api/v1/gzipped")
    async def gzipped() -> Response:
        packed = gzip.compress(b'{"collections": []}')
        return Response(packed, media_type="application/json", headers={"content-encoding": "gzip"})

    @app.post("/auth/email/verify")
    async def verify() -> Response:
        answer = Response(status_code=204)
        answer.headers.append("set-cookie", "vx_sid=s; Path=/; HttpOnly; SameSite=Lax")
        answer.headers.append("set-cookie", "vx_csrf=c; Path=/; SameSite=Lax")
        return answer

    @app.websocket("/ws")
    async def live(socket: WebSocket) -> None:
        await socket.accept()
        # What the handshake carried, so the test can see who the service was
        # told is watching, then an event of the kind the pages refresh on.
        await socket.send_json(
            {"cookie": socket.headers.get("cookie"), "key": socket.headers.get("api-key")}
        )
        await socket.send_json({"event": "collection_created", "data": {"collection": "financial"}})
        while True:
            try:
                said = await socket.receive_text()
            except WebSocketDisconnect:
                return
            await socket.send_text(f"heard {said}")

    @app.get("/api/v1/slow")
    async def slow() -> JSONResponse:
        await asyncio.sleep(5)
        return JSONResponse({"late": True})

    return app


def free_port() -> int:
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        return int(held.getsockname()[1])


class Running:
    """A uvicorn on loopback, started and stopped with the fixture."""

    def __init__(self, app: FastAPI) -> None:
        self.port = free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Running":
        self.thread.start()
        for _ in range(200):  # ten seconds, in case a cold import is slow
            if self.server.started:
                return self
            threading.Event().wait(0.05)
        raise RuntimeError("the fake retrieval service did not start")

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(scope="module")
def upstream() -> Iterator[Running]:
    with Running(service()) as running:
        yield running


def reader(upstream: Running, **settings) -> TestClient:
    where = Settings(
        upstream=upstream.url, site=Path(__file__).parent / "no-build-here", **settings
    )
    return TestClient(build(where))


class TestABodyTooBigToHold:
    def test_a_large_answer_arrives_whole(self, upstream):
        with reader(upstream) as browser:
            got = browser.get("/api/v1/big")
        assert got.status_code == 200
        assert len(got.content) == len(WHOLE)
        assert hashlib.sha256(got.content).hexdigest() == hashlib.sha256(WHOLE).hexdigest()

    def test_a_large_upload_arrives_whole(self, upstream):
        with reader(upstream) as browser:
            got = browser.post("/api/v1/echo", content=WHOLE)
        said = got.json()
        assert said["length"] == len(WHOLE)
        assert said["digest"] == hashlib.sha256(WHOLE).hexdigest()


class TestWhatTheHopDoesNotChange:
    def test_an_encoded_body_is_passed_on_still_encoded(self, upstream):
        """Ours is not the hop that decodes: the browser asked for gzip and gets gzip."""
        with reader(upstream) as browser:
            got = browser.get("/api/v1/gzipped", headers={"accept-encoding": "gzip"})
        assert got.headers["content-encoding"] == "gzip"
        assert got.json() == {"collections": []}, "the client decoded it, so the bytes were sound"

    def test_every_cookie_a_sign_in_set_survives_the_hop(self, upstream):
        with reader(upstream) as browser:
            got = browser.post("/auth/email/verify", json={"code": "123456"})
        assert got.status_code == 204
        cookies = [value for name, value in got.headers.multi_items() if name == "set-cookie"]
        assert len(cookies) == 2 and cookies[0].startswith("vx_sid=") and "HttpOnly" in cookies[0]

    def test_the_caller_is_who_the_service_sees(self, upstream):
        with reader(upstream, key="ours") as browser:
            mine = browser.post(
                "/api/v1/echo", content=b"", headers={"user-agent": "a browser"}
            ).json()
            theirs = browser.post("/api/v1/echo", content=b"", headers={"api-key": "theirs"}).json()
        assert mine["key"] == "ours", "our key when they sent none"
        assert mine["agent"] == "a browser", "who is calling reaches the service's access log"
        assert mine["te"] is None, "a header about the browser's hop stopped here"
        assert theirs["key"] == "theirs", (
            "their key when they have one, because the policy is about them"
        )


class TestAServiceThatIsTooSlow:
    def test_a_service_slower_than_our_ceiling_is_a_bad_gateway(self, upstream):
        with reader(upstream, timeout=0.5) as browser:
            got = browser.get("/api/v1/slow")
        assert got.status_code == 502, (
            "not a five hundred: this service is up, the other one is late"
        )
        assert "did not answer" in got.json()["detail"]


class TestTheLiveSocket:
    """The one call the pages make that is not HTTP: without it the pill reads Reconnecting forever."""

    def test_the_service_pushes_and_the_browser_answers(self, upstream):
        with reader(upstream) as browser:
            with browser.websocket_connect("/ws") as socket:
                socket.receive_json()  # the handshake's own report, checked below
                assert socket.receive_json() == {
                    "event": "collection_created",
                    "data": {"collection": "financial"},
                }
                socket.send_text("still here")
                assert socket.receive_text() == "heard still here"

    def test_the_service_is_told_who_is_watching(self, upstream):
        with reader(upstream, key="ours") as browser:
            with browser.websocket_connect("/ws", headers={"cookie": "vx_sid=abc"}) as socket:
                seen = socket.receive_json()
        assert seen["cookie"] == "vx_sid=abc", (
            "the socket is the caller's, so the service reads their session"
        )
        assert seen["key"] == "ours", "our key travels on the handshake as it does on a call"
