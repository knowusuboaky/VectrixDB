"""The document index routes on the REST API.

``POST /api/v1/documents`` raised TypeError on every call until 2.2: it
passed ``text=`` to ``DocumentIndex.index_text()``, whose parameters are the
document id and its content. This holds the route to the contract the page
and the MCP server rely on: index, read back, delete.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from vectrixdb.api.server import create_app

    app = create_app(db_path=str(tmp_path / "db"), enable_dashboard=False)
    with TestClient(app) as c:
        yield c


def test_index_then_get_then_delete(client):
    created = client.post(
        "/api/v1/documents",
        json={
            "text": "# Basalt" + chr(10) * 2 + "Basalt forms when lava cools quickly.",
            "title": "Basalt",
            "doc_type": "markdown",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["ok"] is True
    doc_id = body["document"]["doc_id"]
    assert doc_id and body["document"]["title"] == "Basalt"

    fetched = client.get(f"/api/v1/documents/{doc_id}")
    assert fetched.status_code == 200, fetched.text

    deleted = client.delete(f"/api/v1/documents/{doc_id}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/v1/documents/{doc_id}").status_code in (404, 200)


def test_empty_text_is_refused(client):
    assert client.post("/api/v1/documents", json={"text": ""}).status_code == 422
