"""The dashboard as a browser gets it: the built pages served by the Backend, in one process.

This is the whole thing, both folders at once, which is why it sits outside
either mirror. It needs a build, so it is skipped until there is one:

    cd Frontend && python run_prod.py

What is held to. The page a browser is handed is the built one, with every
file it names served beside it. A name that carries a hash is cached for a
year; a name that does not is never cached, so a deploy is seen without
anybody clearing a browser. The pages mounted at / do not stand in front of
the forwarded paths or of this service's own health. And the page holds no
demo data: a server with nothing in it says so instead of showing figures
nobody put there.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
import pytest

DASHBOARD = Path(__file__).resolve().parents[2]
BACKEND = DASHBOARD / "Backend"
DIST = DASHBOARD / "Frontend" / "dist"
sys.path.insert(0, str(BACKEND))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.settings import Settings  # noqa: E402
from app.core.site import FOREVER, NEVER  # noqa: E402
from app.main import build  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").exists(),
    reason=f"no build in {DIST}. Run: cd Frontend && python run_prod.py",
)

#: Every page in the sidebar, so a build that lost one is noticed here.
#: Learn is the library's teaching page, and not a page of a company's own dashboard.
PAGES = ("overview", "collections", "search", "evaluate", "ingest", "audit", "access", "console")

#: The library's page scripts, copied in as they are.
SCRIPTS = ("app.js", "trends.js", "evaluate.js", "chunking.js", "theme.js", "sso-boot.js")


@pytest.fixture(scope="module")
def browser():
    """The Backend serving the real build, with nothing upstream to call."""
    served = Settings(upstream="https://retrieval.example.net", site=DIST)
    answers = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"reached": request.url.path})
    )
    with TestClient(build(served, transport=answers)) as client:
        yield client


@pytest.fixture(scope="module")
def page(browser) -> str:
    return browser.get("/").text


class TestThePageABrowserIsHanded:
    def test_it_is_the_built_page(self, browser, page):
        got = browser.get("/")
        assert got.status_code == 200
        assert got.headers["content-type"].startswith("text/html")
        assert "<title>VectrixDB</title>" in page
        assert got.content == (DIST / "index.html").read_bytes(), "served as built, byte for byte"

    def test_every_page_in_the_sidebar_is_there(self, page):
        for name in PAGES:
            assert f'data-page="{name}"' in page, f"the {name} page is missing from the build"

    def test_the_brand_is_data_the_page_reads(self, page):
        found = re.search(
            r'<script id="vx-brand-data" type="application/json">(.*?)</script>', page, re.S
        )
        assert found, "the brand block is gone, so the page has no name to show"
        assert json.loads(found.group(1))["name"], "a brand with no name leaves the sidebar blank"

    def test_it_names_our_own_module_as_well_as_the_copied_pages(self, page):
        for script in SCRIPTS:
            assert f'src="/pages/{script}"' in page
        assert re.search(r'src="/assets/index-[A-Za-z0-9_-]+\.js"', page), (
            "the Vite build's own module is not linked"
        )

    def test_nothing_it_names_of_its_own_is_missing(self, browser, page):
        named = set(re.findall(r'(?:src|href)="(/[^"]+)"', page))
        for path in sorted(named):
            assert browser.get(path).status_code == 200, (
                f"{path} is named by the page and not served"
            )
        assert len(named) >= len(SCRIPTS) + 2, "the page names fewer files than the build has"

    @pytest.mark.parametrize("path", ["/", "/pages/app.js"])
    def test_nothing_offers_figures_nobody_put_there(self, browser, path):
        low = browser.get(path).text.lower()
        for word in ("demo", "sample data", "lorem"):
            assert word not in low, f"{path} mentions {word!r}"

    def test_a_server_holding_nothing_says_so(self, browser, page):
        """Where the library's pages offered to load a demo collection, ours say what to do instead."""
        assert (
            "Create collection" in page
            and "Open the guide" not in page
            and 'data-page="learn"' not in page
        )
        served = browser.get("/pages/app.js").text
        assert "Nothing is indexed yet" in served, (
            "the Overview has nothing to say on an empty server"
        )
        assert "No collections yet" in served, (
            "the Collections page has nothing to say on an empty server"
        )


class TestWhatIsCachedAndWhatIsNot:
    def test_a_name_that_carries_a_hash_is_kept_for_a_year(self, browser, page):
        hashed = re.search(r'src="(/assets/index-[A-Za-z0-9_-]+\.js)"', page).group(1)
        assert browser.get(hashed).headers["cache-control"] == FOREVER

    @pytest.mark.parametrize("path", ["/", "/pages/app.js", "/pages/app.css", "/favicon.svg"])
    def test_a_name_that_stays_the_same_is_never_cached(self, browser, path):
        got = browser.get(path)
        assert got.status_code == 200
        assert got.headers["cache-control"] == NEVER, (
            f"{path} keeps its name, so a deploy must be seen"
        )


class TestWhatTheMountDoesNotTakeOver:
    def test_the_forwarded_paths_still_forward(self, browser):
        got = browser.get("/api/v1/collections")
        assert got.status_code == 200 and got.json() == {"reached": "/api/v1/collections"}

    def test_this_services_own_health_is_still_its_own(self, browser):
        said = browser.get("/healthz").json()
        assert said["ok"] is True and said["pages_built"] is True

    def test_a_path_in_no_build_is_a_plain_not_found(self, browser):
        assert browser.get("/pages/nothing-here.js").status_code == 404
