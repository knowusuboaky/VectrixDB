"""The retrieval service: the Function App that ingests documents and answers retrieval over them.

One client for the life of this process, so connections are reused rather than
made per request, which is what a page making eight calls notices.

Redirects are not followed. A sign-in answer redirects the browser, and
following it here would swallow the redirect and hand the browser a page it
did not ask for.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from app.core.settings import Settings

#: Enough for a dashboard and the uploads it sends, and few enough that a
#: slow service cannot use every connection this process has.
LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=8)


def open_client(settings: Settings, transport: Optional[Any] = None) -> httpx.AsyncClient:
    """The client this service calls the retrieval service with. ``transport`` is for tests."""
    return httpx.AsyncClient(
        base_url=settings.upstream or "http://upstream.invalid",
        timeout=httpx.Timeout(settings.timeout, connect=10.0),
        follow_redirects=False,
        limits=LIMITS,
        transport=transport,
    )


def target(path: str, query: str, settings: Optional[Settings] = None) -> str:
    """Where a call goes upstream: the same path, as the gateway in front publishes it, and the same query. Never a path from the caller's body."""
    where = settings.upstream_path(path) if settings is not None else path
    return f"{where}?{query}" if query else where
