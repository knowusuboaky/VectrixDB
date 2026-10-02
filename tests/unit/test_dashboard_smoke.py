"""The dashboard page is served, is the real page, and every endpoint it calls
on load answers.

No browser. The page is static HTML plus fetches; a browser test would
prove the JavaScript runs, which this does not. What it does prove is the
half that has broken before: the mount is present in a fresh app, the file
served is the dashboard and not a 404 page, and the three endpoints the
page hits before a user does anything all answer with the shapes the page
reads. A refactor that renames a route or drops the mount fails here.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="the API extra is not installed")


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from vectrixdb.api.server import create_app

    app = create_app(db_path=str(tmp_path / "db"), enable_dashboard=True)
    with TestClient(app) as c:
        yield c


class TestDashboardIsServed:
    def test_the_page_is_the_dashboard(self, client):
        page = client.get("/dashboard/")
        assert page.status_code == 200, page.text[:200]
        assert "text/html" in page.headers["content-type"]
        html = page.text
        # Not any HTML: the tabs the page is built around. The graph library is not on the page: the
        # script fetches it the first time the Graph tab is opened, and the tab is hidden until a collection has a graph.
        for marker in ('id="tab-graph"', '"switchTab"'):
            assert marker in html, f"dashboard page is missing {marker!r}"
        assert "cytoscape" not in html and "function ensureCytoscape()" in client.get("/dashboard/app.js").text
        # The page is three files now; the script and the stylesheet must be served beside it.
        for asset in ("app.js", "app.css", "demo-data.js", "evaluate.js", "chunking.js", "trends.js"):
            assert f'"{asset}"' in html, f"the page no longer loads {asset}"
            served = client.get(f"/dashboard/{asset}")
            assert served.status_code == 200, f"{asset} is not served"

    def test_the_mount_survives_a_fresh_app(self, tmp_path):
        """create_app() used to come back with an empty route table on the
        second call; the dashboard mount is the easiest place to see it."""
        from fastapi.testclient import TestClient

        from vectrixdb.api.server import create_app

        for i in range(2):
            app = create_app(db_path=str(tmp_path / f"db{i}"), enable_dashboard=True)
            with TestClient(app) as c:
                assert c.get("/dashboard/").status_code == 200

    def test_the_dashboard_can_be_switched_off(self, tmp_path):
        from fastapi.testclient import TestClient

        from vectrixdb.api.server import create_app

        app = create_app(db_path=str(tmp_path / "db"), enable_dashboard=False)
        with TestClient(app) as c:
            assert c.get("/dashboard/").status_code == 404


class TestWhatThePageCallsOnLoad:
    """The fetches index.html makes before any user action, and the fields it reads."""

    def test_info(self, client):
        r = client.get("/api/v1/info")
        assert r.status_code == 200, r.text
        body = r.json()
        data = body.get("data", body)
        # The page reads the counts; version is not part of this payload.
        assert "collections_count" in data and "documents_count" in data

    def test_collections_list(self, client):
        r = client.get("/api/collections")
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body.get("data", body), (list, dict))

    def test_auth_status(self, client):
        r = client.get("/auth/status")
        assert r.status_code == 200, r.text
        assert "auth_enabled" in r.json() or "enabled" in r.json() or "data" in r.json()

    def test_a_collection_round_trip_through_the_page_endpoints(self, client):
        """Create, upsert text, list points, search by text: the sequence the
        page performs, on the routes that exist."""
        created = client.post(
            "/api/v2/collections", json={"name": "smoke", "mode": "dense", "dimension": 384}
        )
        assert created.status_code in (200, 201), created.text
        added = client.post(
            "/api/collections/smoke/text-upsert",
            json={
                "points": [
                    {"id": "a", "text": "Basalt forms when lava cools quickly."},
                    {"id": "b", "text": "Sourdough is leavened by wild yeast."},
                ]
            },
        )
        assert added.status_code == 200, added.text
        points = client.get("/api/v1/collections/smoke/points?limit=10&offset=0")
        assert points.status_code == 200, points.text
        searched = client.post(
            "/api/collections/smoke/text-search", json={"query_text": "volcanic rock", "limit": 1}
        )
        assert searched.status_code == 200, searched.text
        results = searched.json().get("data", searched.json())
        hits = results.get("results", results) if isinstance(results, dict) else results
        assert hits and "Basalt" in str(hits[0])

    def test_the_upload_handler_calls_a_route_that_exists(self, client):
        """The page posted uploaded markdown to
        ``/api/v2/collections/{name}/add``, which the server has never
        served, so every drag and drop ended in the error snackbar. It calls
        text-upsert now. This drives the page's own payload shape through the
        real route, so the two cannot drift apart again silently."""
        page = client.get("/dashboard/app.js").text
        assert "/add`" not in page, (
            "the upload handler is back on a route the server does not serve"
        )
        assert "text-upsert" in page, "the upload handler no longer calls text-upsert"

        client.post("/api/v2/collections", json={"name": "up", "mode": "dense", "dimension": 384})
        # The body the page builds, with its generated ids and payload.
        sent = client.post(
            "/api/collections/up/text-upsert",
            json={
                "points": [
                    {
                        "id": "upload_1_0",
                        "text": "Basalt forms when lava cools quickly.",
                        "payload": {"source": "Rocks", "type": "markdown"},
                    }
                ]
            },
        )
        assert sent.status_code == 200, sent.text
        found = client.post(
            "/api/collections/up/text-search", json={"query_text": "volcanic rock", "limit": 1}
        )
        assert found.status_code == 200, found.text
        assert "Basalt" in found.text


class TestDocumentsInThePage:
    def _script(self, client):
        return client.get("/dashboard/app.js").text

    def test_the_drop_zone_asks_the_server_what_it_reads_and_sends_files_to_it(self, client):
        script, page = self._script(client), client.get("/dashboard/").text
        assert "/api/v1/extractors" in script and "/documents?" in script and "X-Filename" in script
        assert 'value="server"' in page and 'id="dlg-doc"' in page and 'id="ing-readers"' in page
        readers = client.get("/api/v1/extractors").json()
        assert ".md" in readers["accepted"] and ".pdf" in readers["accepted"]

    def test_json_in_a_click_handler_is_escaped(self, client):
        # onclick="showPoint(${JSON.stringify(id)})" puts a double quote
        # inside a double-quoted attribute, and the attribute ends at
        # showPoint( . Clicking a point did nothing until this was found.
        import re

        unescaped = re.compile(r'on[a-z]+="[^"`]*\$\{JSON\.stringify\(')
        assert unescaped.search('<div onclick="showPoint(${JSON.stringify(id)})">'), "the pattern has to catch the bug"
        assert not unescaped.search('<div onclick="showPoint(${esc(JSON.stringify(id))})">')
        raw = unescaped.findall(self._script(client))
        assert raw == [], f"unescaped JSON inside an attribute: {raw}"
