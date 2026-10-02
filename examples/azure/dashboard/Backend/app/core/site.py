"""The built pages, served at /.

Two cache rules, because the build has two kinds of file. Vite's own output
under assets/ carries a hash in its name, so it can be kept for a year and
never asked for again. Everything else, index.html and the pages copied into
public/, keeps its name from one build to the next, so it is never cached: a
deploy has to be seen without anybody clearing a browser.

The page is built with VectrixDB's own brand in it, as data. The company's
name, logo, colours and line are set once, on the retrieval service, and this
service asks it for them (at most once a minute) and writes them into the page
as it goes out, so both dashboards look the same. When the service cannot be
asked, the page goes out as it was built.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

#: A year, which is what "forever" means in a cache header.
FOREVER = "public, max-age=31536000, immutable"
NEVER = "no-store"
#: The block the page carries its brand in: data, never a script that runs.
BRAND_BLOCK = re.compile(r'(<script id="vx-brand-data" type="application/json">)(.*?)(</script>)', re.S)
#: How long the retrieval service's answer about the brand is kept.
BRAND_SECONDS = 60.0


async def brand_of(app: Any) -> Optional[dict]:
    """The company's brand as the retrieval service says it, or None when it cannot be asked."""
    settings = app.state.settings
    client = getattr(app.state, "client", None)
    if not settings.ready or client is None:
        return None
    held = getattr(app.state, "brand", None)
    if held is not None and time.monotonic() - held[0] < BRAND_SECONDS:
        return held[1]
    try:
        answer = await client.get(settings.upstream_path("/brand.json"), timeout=5.0)
        brand = answer.json() if answer.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        brand = None
    brand = brand if isinstance(brand, dict) and isinstance(brand.get("name"), str) else None
    app.state.brand = (time.monotonic(), brand)
    return brand


class Site(StaticFiles):
    """The build, with a cache rule a name can be trusted for, and the page dressed in the company's brand."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        hashed = path.startswith("assets/") or path.startswith("assets\\")
        response.headers["Cache-Control"] = FOREVER if hashed else NEVER
        if response.status_code == 200 and path in ("", ".", "index.html"):
            page = (Path(str(self.directory)) / "index.html").read_text(encoding="utf-8")
            if BRAND_BLOCK.search(page):
                brand = await brand_of(scope["app"])
                if brand is not None:
                    # A "<" is written escaped, so no name can end the block.
                    data = json.dumps(brand).replace("<", "\\u003c")
                    page = BRAND_BLOCK.sub(lambda m: m.group(1) + data + m.group(3), page, count=1)
                    return HTMLResponse(page, headers={"Cache-Control": NEVER})
        return response


def mount(app: FastAPI, site: Path) -> None:
    """Serve the build at /, or say how to make one.

    Mounted last, after every route, so a path this service answers itself is
    never taken by a file that happens to share its name.
    """
    if not (site / "index.html").exists():

        @app.get("/", include_in_schema=False)
        async def not_built() -> JSONResponse:
            return JSONResponse(
                {"detail": f"The pages are not built. Run: cd Frontend && python run_prod.py (looked in {site})"},
                status_code=503,
            )

        return

    app.mount("/", Site(directory=str(site), html=True), name="site")
