"""The built pages, and the two cache rules they are served under.

A hashed name can be kept for a year; every other name is never cached, or a
deploy is invisible until somebody clears a browser. And a build that is not
there says how to make one, rather than answering 404 to the whole dashboard.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[5] / "Backend"
sys.path.insert(0, str(BACKEND))

from app.core.settings import Settings  # noqa: E402
from app.main import build  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A build as Vite leaves one: the page, a hashed asset, and a copied page script."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!DOCTYPE html><title>Dashboard</title>", encoding="utf-8")
    (tmp_path / "assets" / "main-a1b2c3.js").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "app.js").write_text("function go() {}\n", encoding="utf-8")
    return tmp_path


def reader(site: Path) -> TestClient:
    return TestClient(build(Settings(upstream="https://retrieval.example.net", site=site)))


class TestServingTheBuild:
    def test_the_page_is_served_and_never_cached(self, built):
        with reader(built) as client:
            got = client.get("/")
        assert got.status_code == 200 and "Dashboard" in got.text
        assert got.headers["cache-control"] == "no-store"

    def test_a_hashed_asset_is_kept_for_a_year(self, built):
        with reader(built) as client:
            got = client.get("/assets/main-a1b2c3.js")
        assert got.status_code == 200 and got.headers["cache-control"] == "public, max-age=31536000, immutable"

    def test_a_page_script_keeps_its_name_so_it_is_not_cached(self, built):
        """The copies under pages/ are not hashed: cached, an old one would outlive a deploy."""
        with reader(built) as client:
            got = client.get("/pages/app.js")
        assert got.status_code == 200 and got.headers["cache-control"] == "no-store"

    def test_nothing_outside_the_build_is_served(self, built, tmp_path):
        (tmp_path.parent / "secret.txt").write_text("not yours", encoding="utf-8")
        with reader(built) as client:
            for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
                assert client.get(path).status_code in (404, 400), path


class TestThePageGoesOutInTheCompanysBrand:
    """Set once on the retrieval service, and written into the page here, so both dashboards look the same."""

    PAGE = '<!DOCTYPE html><title>Dashboard</title><script id="vx-brand-data" type="application/json">{"name": "VectrixDB", "custom": false}</script>'
    BRAND = {"name": "BMO", "custom": True, "logo": "/brand/logo", "logo_dark": None, "accent": "#1155cc", "wordmark": True, "copyright": "© 2026 BMO"}

    def serve(self, built, answer):
        import httpx

        asked = []

        def handle(request):
            asked.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer

        (built / "index.html").write_text(self.PAGE, encoding="utf-8")
        app = build(Settings(upstream="https://retrieval.example.net", site=built), transport=httpx.MockTransport(handle))
        return TestClient(app), asked

    def told(self, page: str) -> dict:
        import json
        import re

        return json.loads(re.search(r'id="vx-brand-data" type="application/json">(.*?)</script>', page).group(1))

    def test_the_services_brand_is_written_into_the_page(self, built):
        import httpx

        client, asked = self.serve(built, httpx.Response(200, json=self.BRAND))
        with client:
            got = client.get("/")
        assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
        assert self.told(got.text) == self.BRAND
        assert [str(r.url) for r in asked] == ["https://retrieval.example.net/brand.json"]
        assert "api-key" not in asked[0].headers, "the brand is public: no key goes with the question"

    def test_a_name_cannot_end_the_block(self, built):
        import httpx

        client, _ = self.serve(built, httpx.Response(200, json={**self.BRAND, "name": "</script><script>alert(1)</script>"}))
        with client:
            page = client.get("/").text
        assert page.count("</script>") == 1 and self.told(page)["name"] == "</script><script>alert(1)</script>"

    def test_it_is_asked_once_a_minute_not_once_a_page(self, built):
        import httpx

        client, asked = self.serve(built, httpx.Response(200, json=self.BRAND))
        with client:
            for _ in range(3):
                assert self.told(client.get("/").text)["name"] == "BMO"
        assert len(asked) == 1

    @pytest.mark.parametrize("answer", ["down", "404", "not json", "not a brand"])
    def test_when_the_service_cannot_say_the_page_goes_out_as_built(self, built, answer):
        import httpx

        given = {
            "down": httpx.ConnectError("asleep"),
            "404": httpx.Response(404, json={"detail": "Not Found"}),
            "not json": httpx.Response(200, text="<html>"),
            "not a brand": httpx.Response(200, json=["BMO"]),
        }[answer]
        client, _ = self.serve(built, given)
        with client:
            got = client.get("/")
        assert got.status_code == 200 and got.text == self.PAGE

    def test_a_page_without_the_block_is_not_asked_about(self, built):
        import httpx

        (built / "index.html").write_text("<!DOCTYPE html><title>Dashboard</title>", encoding="utf-8")
        asked = []
        app = build(Settings(upstream="https://retrieval.example.net", site=built), transport=httpx.MockTransport(lambda r: asked.append(r)))
        with TestClient(app) as client:
            assert client.get("/").text == "<!DOCTYPE html><title>Dashboard</title>"
        assert asked == []


class TestWithoutABuild:
    def test_it_says_how_to_make_one(self, tmp_path):
        with reader(tmp_path / "never-built") as client:
            got = client.get("/")
        assert got.status_code == 503 and "run_prod.py" in got.json()["detail"]

    def test_the_forwarded_paths_still_work(self, tmp_path):
        """The pages and the API are independent: an unbuilt frontend does not stop the API."""
        with reader(tmp_path / "never-built") as client:
            assert client.get("/healthz").json()["pages_built"] is False
