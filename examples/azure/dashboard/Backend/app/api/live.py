"""The live socket, forwarded: the one call the pages make that is not HTTP.

The dashboard opens a socket at /ws and keeps it open. The retrieval service
sends a line down it whenever a collection is created, points are added or an
index is rebuilt, and the page refreshes what it is showing. Without this the
pages still work, and the pill in the corner reads "Reconnecting" forever
while the browser retries a socket nothing answers.

So the socket is forwarded the way the calls are: accepted here, opened to the
retrieval service with the caller's own cookie, and pumped both ways until
either end goes. Nothing in the messages is read on the way through.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import asyncio
from typing import Any, Set
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketState

router = APIRouter()

#: Close codes. The page retries on any close, with a backoff of its own, so a
#: service that is not there is not an error to shout about.
GOING_AWAY = 1001
CANNOT_REACH = 1011

#: What travels up with the socket handshake. The service decides who this is
#: from the same cookie and the same key it would read on a call.
PASSED = ("cookie", "api-key", "x-api-key", "authorization")


def ws_url(upstream: str, path: str = "/ws") -> str:
    """The socket's address on the retrieval service: its own scheme, its own host, our path."""
    parts = urlsplit(upstream)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + path, "", ""))


def handshake(headers: Any, key: str = "", key_header: str = "api-key") -> dict:
    """The headers the socket opens with: the caller's own, and our key only when they sent none."""
    passing = {name.lower(): value for name, value in headers.items() if name.lower() in PASSED}
    if key and key_header.lower() not in passing:
        passing[key_header] = key
    return passing


async def _to_service(browser: WebSocket, service: Any) -> None:
    while True:
        message = await browser.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("text") is not None:
            await service.send(message["text"])
        elif message.get("bytes") is not None:
            await service.send(message["bytes"])


async def _to_browser(browser: WebSocket, service: Any) -> None:
    async for message in service:
        if isinstance(message, str):
            await browser.send_text(message)
        else:
            await browser.send_bytes(message)


async def pump(browser: WebSocket, service: Any) -> None:
    """Both directions at once, until whichever end goes first takes the other with it."""
    both = [asyncio.create_task(_to_service(browser, service)), asyncio.create_task(_to_browser(browser, service))]
    done, pending = await asyncio.wait(both, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()  # a failure in the pump is this service's, not the caller's


@router.websocket("/ws")
async def live(browser: WebSocket) -> None:
    """The dashboard's live socket, held open against the retrieval service's."""
    settings = browser.app.state.settings
    if not settings.ready:
        await browser.close(code=CANNOT_REACH)
        return
    try:
        from websockets.asyncio.client import connect
    except ImportError:  # uvicorn[standard] brings it; a bare uvicorn does not
        await browser.close(code=CANNOT_REACH)
        return

    headers = handshake(browser.headers, key=settings.key, key_header=settings.key_header)
    await browser.accept()
    try:
        async with connect(ws_url(settings.upstream, settings.upstream_path("/ws")), additional_headers=headers, open_timeout=10) as service:
            await pump(browser, service)
    except Exception:
        # The service is asleep, or it refused the socket. The page reads a
        # close as "try again shortly", which is the truth of it.
        pass
    finally:
        if browser.client_state is WebSocketState.CONNECTED:
            await browser.close(code=GOING_AWAY)


#: Named here rather than in the settings, because the settings' list is about
#: what the HTTP forwarder passes and this is the one path that is not HTTP.
SOCKETS: Set[str] = {"/ws"}
