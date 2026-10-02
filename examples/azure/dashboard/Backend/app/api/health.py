"""Whether this service is up, and whether it has what it needs.

It calls nothing: a liveness check that depends on another service reports
that one's cold start as this one being unwell. The retrieval service's own
health is at /health, which is forwarded.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> dict:
    """Up, where it forwards to, and whether the pages are built."""
    settings = request.app.state.settings
    return {
        "ok": True,
        "upstream": urlsplit(settings.upstream).netloc or None,
        "keyed": bool(settings.key),
        "pages_built": settings.built,
    }
