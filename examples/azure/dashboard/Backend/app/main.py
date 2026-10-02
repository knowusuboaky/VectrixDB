"""The dashboard's Backend: it serves the pages and forwards their calls.

    the browser  ->  this service  ->  the retrieval service

One origin, so the sign-in cookie the retrieval service sets works without a
cross-site exception, and no CORS rule is needed anywhere. The live socket the
pages keep open goes the same way, at /ws. The upstream's key
lives here and never reaches a browser. Everything else is passed through:
this service decides nothing about who a caller is or what they may see.

It imports no part of the library. What it knows about the retrieval service
is its address, its key, and which paths to forward.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI

from app.api import forward, health, live
from app.core import site
from app.core.settings import Settings
from app.integrations.retrieval_integration import open_client


# ============================================================================
# BUILDING THE APP
# ============================================================================
#
# INPUT   settings and a transport, which is how the tests hand it a fake
#         service
# OUTPUT  the application: the pages served, the calls forwarded, the
#         upstream's key kept here and never sent to a browser
#
# One origin, so the sign-in cookie the retrieval service sets works without a
# cross-site exception and no CORS rule is needed anywhere. This service
# decides nothing about who a caller is or what they may see.


def build(settings: Optional[Settings] = None, transport: Optional[Any] = None) -> FastAPI:
    """The application. ``settings`` and ``transport`` are how the tests hand it a fake service."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = open_client(settings, transport)
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(
        title="Retrieval dashboard",
        description="Serves the dashboard and forwards its calls to the retrieval service.",
        version="0.1.0",
        lifespan=lifespan,
        # This service publishes no API of its own: what it forwards is
        # documented by the service it forwards to, at /openapi.json.
        openapi_url=None,
    )
    app.state.settings = settings

    app.include_router(health.router)
    app.include_router(live.router)
    app.include_router(forward.router)
    # Last, so no file in the build can stand in front of a route above.
    site.mount(app, settings.site)
    return app


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   the environment
# OUTPUT  the app uvicorn serves
#
# Built once, at import.

app = build()
