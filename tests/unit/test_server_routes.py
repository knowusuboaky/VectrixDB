"""Route tests for the FastAPI app in vectrixdb.api.server.

Every test builds its own app with create_app(db_path=<tmp>, enable_dashboard=False)
and drives it through fastapi.testclient.TestClient. No uvicorn, no network.
The text routes use the bundled embedding model, which is 384-dimensional.

What is deliberately left to other files: the dashboard mount and the page's
load-time fetches (test_dashboard_smoke.py), the route table surviving a second
create_app() call (test_create_app.py), and fuzzed bodies (test_fuzz.py).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

fastapi = pytest.importorskip("fastapi", reason="the API extra is not installed")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.api import server  # noqa: E402
from vectrixdb.api.server import create_app  # noqa: E402

DIM = 3
TEXT_DIM = 384

POINTS = [
    {"id": "p1", "vector": [1.0, 0.0, 0.0], "metadata": {"category": "a", "n": 1}},
    {"id": "p2", "vector": [0.0, 1.0, 0.0], "metadata": {"category": "b", "n": 2}},
    {"id": "p3", "vector": [0.9, 0.1, 0.0], "metadata": {"category": "a", "n": 3}},
]



def own(metadata: dict) -> dict:
    """The caller's metadata without the build stamp every write adds."""
    return {k: v for k, v in metadata.items() if not k.startswith("_vx_")}

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No auth, default storage and cache, and no inherited VECTRIXDB_PATH."""
    for name in (
        "VECTRIXDB_API_KEY",
        "VECTRIXDB_READ_ONLY_API_KEY",
        "VECTRIXDB_STORAGE_BACKEND",
        "VECTRIXDB_CACHE_BACKEND",
        "VECTRIXDB_PATH",
        "VECTRIXDB_SCALING_STRATEGY",
    ):
        monkeypatch.delenv(name, raising=False)


def make_client(tmp_path, name="db"):
    app = create_app(db_path=str(tmp_path / name), enable_dashboard=False)
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def create(client, name="c", dimension=DIM, **extra):
    r = client.post("/api/v1/collections", json={"name": name, "dimension": dimension, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def seed(client, name="c"):
    create(client, name)
    r = client.post(f"/api/v1/collections/{name}/points", json={"points": POINTS})
    assert r.status_code == 200, r.text
    return r.json()


def ids_of(response):
    return [hit["id"] for hit in response.json()["data"]["results"]]


# =============================================================================
# Isolation
# =============================================================================


class TestIsolation:
    def test_a_fresh_app_has_no_collections(self, client):
        r = client.get("/api/v1/collections")
        assert r.status_code == 200
        assert r.json() == {"collections": [], "total": 0}

    def test_collections_do_not_leak_between_apps(self, tmp_path):
        with make_client(tmp_path, "one") as first:
            create(first, "leak")
            assert first.get("/api/v1/collections").json()["total"] == 1
        with make_client(tmp_path, "two") as second:
            assert second.get("/api/v1/collections").json()["total"] == 0
        with make_client(tmp_path, "one") as reopened:
            names = [c["name"] for c in reopened.get("/api/v1/collections").json()["collections"]]
            assert names == ["leak"]

    def test_db_path_is_honoured_on_disk(self, tmp_path, client):
        create(client, "ondisk")
        assert (tmp_path / "db").exists()

    def test_env_path_is_used_when_no_db_path_is_given(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_PATH", str(tmp_path / "from-env"))
        with TestClient(create_app(enable_dashboard=False)) as c:
            create(c, "envy")
        assert (tmp_path / "from-env").exists()


# =============================================================================
# Info, health and auth status
# =============================================================================


class TestInfoRoutes:
    def test_root(self, client):
        body = client.get("/").json()
        assert body["name"] == "VectrixDB"
        assert body["version"] == server.__version__
        assert body["docs"] == "/docs"
        assert body["auth_enabled"] is False

    def test_health(self, client):
        body = client.get("/health").json()
        assert body["status"] == "healthy"
        assert "timestamp" in body

    def test_api_v1_root_with_and_without_slash(self, client):
        for path in ("/api/v1", "/api/v1/"):
            body = client.get(path).json()
            assert body["version"] == "v1"
            assert body["endpoints"]["collections"] == "/api/v1/collections"

    def test_info_counts_collections(self, client):
        before = client.get("/api/v1/info").json()
        assert before["collections_count"] == 0
        assert before["storage_backend"] == "sqlite"
        assert before["documents_count"] == 0
        assert before["total_vectors"] == 0
        seed(client)
        after = client.get("/api/info").json()
        assert after["collections_count"] == 1
        assert after["total_vectors"] == 3

    def test_documents_are_counted_across_the_collections(self, tmp_path):
        """The Overview said "0 indexed documents" for a server whose documents all went into collections."""
        from vectrixdb import Vectrix

        lib = Vectrix("lib", path=str(tmp_path / "db"), mode="dense")
        lib.add_document("# Refunds\n\nRefunds go back to the card that paid.", doc_id="refunds")
        lib.add_document("# Travel\n\nEconomy fares for trips under six hours.", doc_id="travel")
        lib.add(["A point with no document."], ids=["loose"])
        lib.close()
        with make_client(tmp_path) as c:
            assert c.get("/api/v1/info").json()["documents_count"] == 2, "two documents; a loose chunk belongs to none"

    def test_extended_info_and_resources(self, client):
        ext = client.get("/api/v1/info/extended")
        assert ext.status_code == 200 and ext.json()["ok"] is True
        assert isinstance(ext.json()["data"], dict)
        res = client.get("/api/v1/resources")
        assert res.status_code == 200 and isinstance(res.json()["data"], dict)

    def test_cache_stats_and_clear(self, client):
        stats = client.get("/api/v1/cache/stats").json()
        assert stats["ok"] is True
        assert {"hits", "misses", "hit_rate", "size"} <= set(stats["data"])
        cleared = client.delete("/api/v1/cache")
        assert cleared.status_code == 200
        assert cleared.json()["message"] == "Cache cleared"

    def test_ws_status_with_no_clients(self, client):
        body = client.get("/api/v1/ws/status").json()
        assert body == {"active_connections": 0, "endpoint": "/ws"}


class TestAuthStatus:
    def test_without_a_key(self, client):
        body = client.get("/auth/status").json()
        assert body["ok"] is True
        assert body["data"] == {"auth_enabled": False, "read_only_key_enabled": False}

    def test_with_a_full_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", "secret")
        with make_client(tmp_path) as c:
            body = c.get("/auth/status").json()
            assert body["data"] == {"auth_enabled": True, "read_only_key_enabled": False}
            assert c.get("/").json()["auth_enabled"] is True

    def test_with_both_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", "secret")
        monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY", "ro")
        with make_client(tmp_path) as c:
            body = c.get("/auth/status").json()
            assert body["data"] == {"auth_enabled": True, "read_only_key_enabled": True}

    def test_empty_key_counts_as_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", "")
        with make_client(tmp_path) as c:
            assert c.get("/auth/status").json()["data"]["auth_enabled"] is False


# =============================================================================
# API key middleware
# =============================================================================


class TestApiKeyMiddleware:
    @pytest.fixture
    def secured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", "secret")
        monkeypatch.setenv("VECTRIXDB_READ_ONLY_API_KEY", "ro")
        with make_client(tmp_path) as c:
            yield c

    BODY = {"name": "c", "dimension": DIM}

    def test_reads_are_open_without_a_key(self, secured):
        assert secured.get("/api/v1/collections").status_code == 200

    def test_writes_without_a_key_are_401(self, secured):
        r = secured.post("/api/v1/collections", json=self.BODY)
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == "ApiKey"
        body = r.json()
        assert body["ok"] is False and body["data"] is None
        assert "api-key" in body["message"]
        assert secured.delete("/api/v1/collections/c").status_code == 401

    def test_wrong_key_is_401_even_for_reads(self, secured):
        r = secured.get("/api/v1/collections", headers={"api-key": "nope"})
        assert r.status_code == 401
        assert r.json()["message"] == "Invalid API key"
        w = secured.post("/api/v1/collections", json=self.BODY, headers={"api-key": "nope"})
        assert w.status_code == 401

    def test_full_key_can_write(self, secured):
        r = secured.post("/api/v1/collections", json=self.BODY, headers={"api-key": "secret"})
        assert r.status_code == 200
        assert (
            secured.get("/api/v1/collections/c", headers={"api-key": "secret"}).status_code == 200
        )

    def test_read_only_key_reads_but_cannot_write(self, secured):
        assert secured.get("/api/v1/collections", headers={"api-key": "ro"}).status_code == 200
        r = secured.post("/api/v1/collections", json=self.BODY, headers={"api-key": "ro"})
        assert r.status_code == 403
        assert "Read-only" in r.json()["message"]

    def test_public_paths_skip_auth(self, secured):
        for path in ("/", "/health", "/auth/status", "/openapi.json"):
            assert secured.get(path).status_code == 200, path

    def test_dashboard_prefix_skips_auth(self, secured):
        # The dashboard is off in this app, so the path is a plain 404, not a 401.
        assert secured.get("/dashboard/index.html", headers={"api-key": "nope"}).status_code == 404

    def test_read_only_key_alone_is_not_a_full_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_API_KEY", "secret")
        with make_client(tmp_path) as c:
            r = c.post("/api/v1/collections", json=self.BODY, headers={"api-key": "ro"})
            assert r.status_code == 401


# =============================================================================
# Collections
# =============================================================================


class TestCollections:
    def test_create_returns_the_collection_info(self, client):
        body = create(client, "docs", description="d")
        assert body["ok"] is True
        assert body["message"] == "Collection 'docs' created"
        data = body["data"]
        assert data["name"] == "docs" and data["dimension"] == DIM
        assert data["metric"] == "cosine" and data["count"] == 0
        assert data["description"] == "d"
        assert data["tags"] == ["dense"]
        assert data["has_text_index"] is False

    def test_v1_auto_tags_dense_unless_a_tier_is_given(self, client):
        assert create(client, "a", tags=["Custom"])["data"]["tags"] == ["Custom", "dense"]
        assert create(client, "b", tags=["hybrid"])["data"]["tags"] == ["hybrid"]
        assert create(client, "c", tags=["demo"])["data"]["tags"] == ["demo"]

    def test_other_metrics(self, client):
        assert create(client, "e", metric="euclidean")["data"]["metric"] == "euclidean"
        assert create(client, "d", metric="dot")["data"]["metric"] == "dot"

    def test_invalid_metric_is_400(self, client):
        r = client.post(
            "/api/v1/collections", json={"name": "x", "dimension": 3, "metric": "hamming"}
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "Invalid metric: hamming"

    def test_duplicate_create_is_409(self, client):
        create(client, "dup")
        r = client.post("/api/v1/collections", json={"name": "dup", "dimension": DIM})
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "x", "dimension": 0},
            {"name": "x", "dimension": 70000},
            {"name": "", "dimension": 3},
            {"dimension": 3},
            {"name": "x"},
            {"name": "x", "dimension": "three"},
        ],
    )
    def test_invalid_bodies_are_422(self, client, body):
        r = client.post("/api/v1/collections", json=body)
        assert r.status_code == 422
        assert "detail" in r.json()

    def test_get_and_missing(self, client):
        create(client, "g")
        r = client.get("/api/v1/collections/g")
        assert r.status_code == 200
        assert r.json()["data"]["name"] == "g"
        missing = client.get("/api/v1/collections/nope")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Collection 'nope' not found"

    def test_list(self, client):
        create(client, "one")
        create(client, "two")
        body = client.get("/api/v1/collections").json()
        assert body["total"] == 2
        assert sorted(c["name"] for c in body["collections"]) == ["one", "two"]
        assert client.get("/api/collections").json()["total"] == 2

    def test_delete_then_404(self, client):
        create(client, "gone")
        r = client.delete("/api/v1/collections/gone")
        assert r.status_code == 200
        assert r.json()["message"] == "Collection 'gone' deleted"
        assert client.get("/api/v1/collections/gone").status_code == 404
        again = client.delete("/api/v1/collections/gone")
        assert again.status_code == 404
        assert again.json() == {"ok": False, "message": "Collection 'gone' not found", "data": None, "detail": "Collection 'gone' not found"}

    def test_legacy_aliases(self, client):
        r = client.post("/api/collections", json={"name": "old", "dimension": DIM})
        assert r.status_code == 200
        assert client.delete("/api/collections/old").status_code == 200


class TestCollectionsV2:
    def post(self, client, **body):
        return client.post("/api/v2/collections", json=body)

    def test_default_tier_is_dense_with_a_text_index(self, client):
        r = self.post(client, name="v2", dimension=DIM)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "dense tier" in body["message"]
        assert body["data"]["tags"] == ["dense"]
        assert body["data"]["has_text_index"] is True

    def test_text_index_can_be_switched_off_for_dense(self, client):
        r = self.post(client, name="plain", dimension=DIM, enable_text_index=False)
        assert r.json()["data"]["has_text_index"] is False

    def test_hybrid_tier_forces_a_text_index(self, client):
        r = self.post(client, name="h", dimension=DIM, enable_text_index=False, tags=["Hybrid"])
        assert r.json()["data"]["tags"] == ["hybrid"]
        assert r.json()["data"]["has_text_index"] is True
        assert "BM25" in r.json()["message"]

    def test_highest_tier_wins_and_language_tags_survive(self, client):
        r = self.post(client, name="g", dimension=DIM, tags=["dense", "en", "Graph", "ml"])
        assert r.json()["data"]["tags"] == ["graph", "EN", "ML"]
        assert "knowledge graph" in r.json()["message"]

    def test_demo_tag_keeps_tags_as_given(self, client):
        r = self.post(client, name="demo", dimension=DIM, tags=["demo", "Hybrid"])
        assert r.json()["data"]["tags"] == ["demo", "Hybrid"]

    def test_invalid_metric_and_duplicate(self, client):
        assert self.post(client, name="m", dimension=DIM, metric="bad").status_code == 400
        create(client, "taken")
        assert self.post(client, name="taken", dimension=DIM).status_code == 409

    def test_hnsw_parameters_are_accepted(self, client):
        r = self.post(client, name="tuned", dimension=DIM, hnsw_m=8, hnsw_ef_construction=50)
        assert r.status_code == 200
        assert r.json()["data"]["index_config"]["hnsw_m"] == 8


# =============================================================================
# Points
# =============================================================================


class TestPoints:
    def test_upsert(self, client):
        body = seed(client)
        assert body["message"] == "Added 3 points"
        assert body["data"] == {"added": 3, "total": 3}

    def test_upsert_into_missing_collection(self, client):
        r = client.post("/api/v1/collections/nope/points", json={"points": POINTS})
        assert r.status_code == 404

    def test_dimension_mismatch_is_400(self, client):
        create(client)
        r = client.post(
            "/api/v1/collections/c/points", json={"points": [{"id": "x", "vector": [1.0, 2.0]}]}
        )
        assert r.status_code == 400
        assert "dimension" in r.json()["detail"]

    @pytest.mark.parametrize(
        "body",
        [{"points": [{"id": "x"}]}, {"points": "x"}, {}, {"points": [{"vector": [1, 2, 3]}]}],
    )
    def test_bad_point_bodies_are_422(self, client, body):
        create(client)
        assert client.post("/api/v1/collections/c/points", json=body).status_code == 422

    def test_get_point(self, client):
        seed(client)
        r = client.get("/api/v1/collections/c/points/p2")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["id"] == "p2"
        assert data["vector"] == pytest.approx([0.0, 1.0, 0.0])
        assert own(data["metadata"]) == {"category": "b", "n": 2}
        assert "created_at" in data

    def test_get_missing_point_and_collection(self, client):
        create(client)
        r = client.get("/api/v1/collections/c/points/ghost")
        assert r.status_code == 404
        assert r.json()["detail"] == "Point 'ghost' not found"
        assert client.get("/api/v1/collections/none/points/p1").status_code == 404

    def test_list_with_pagination(self, client):
        seed(client)
        page1 = client.get("/api/v1/collections/c/points", params={"limit": 2}).json()["data"]
        assert page1 == {"ids": ["p1", "p2"], "limit": 2, "offset": 0, "total": 3}
        page2 = client.get("/api/v1/collections/c/points", params={"limit": 2, "offset": 2})
        assert page2.json()["data"]["ids"] == ["p3"]
        default = client.get("/api/v1/collections/c/points").json()["data"]
        assert default["limit"] == 100 and default["ids"] == ["p1", "p2", "p3"]

    @pytest.mark.parametrize(
        "params", [{"limit": 0}, {"limit": 1001}, {"offset": -1}, {"limit": "x"}]
    )
    def test_bad_pagination_is_422(self, client, params):
        create(client)
        assert client.get("/api/v1/collections/c/points", params=params).status_code == 422

    def test_list_missing_collection(self, client):
        assert client.get("/api/v1/collections/none/points").status_code == 404

    def test_delete_points(self, client):
        seed(client)
        r = client.request("DELETE", "/api/v1/collections/c/points", json={"ids": ["p1", "ghost"]})
        assert r.status_code == 200
        assert r.json()["data"] == {"deleted": 1, "total": 2}
        assert r.json()["message"] == "Deleted 1 points"
        assert client.get("/api/v1/collections/c/points/p1").status_code == 404
        assert client.get("/api/v1/collections/c/points").json()["data"]["ids"] == ["p2", "p3"]

    def test_delete_points_errors(self, client):
        create(client)
        assert client.request("DELETE", "/api/v1/collections/c/points", json={}).status_code == 422
        r = client.request("DELETE", "/api/v1/collections/none/points", json={"ids": ["p1"]})
        assert r.status_code == 404

    def test_upsert_overwrites_existing_ids(self, client):
        seed(client)
        r = client.post(
            "/api/v1/collections/c/points",
            json={"points": [{"id": "p1", "vector": [0.0, 0.0, 1.0], "metadata": {"n": 9}}]},
        )
        assert r.status_code == 200
        assert client.get("/api/v1/collections/c/points").json()["data"]["total"] == 3
        got = client.get("/api/v1/collections/c/points/p1").json()["data"]
        assert own(got["metadata"]) == {"n": 9}


class TestPointsV2:
    def test_upsert_with_text(self, client):
        create(client)
        r = client.post(
            "/api/v2/collections/c/points",
            json={
                "points": [
                    {"id": "t1", "vector": [1.0, 0.0, 0.0], "text": "hello world"},
                    {"id": "t2", "vector": [0.0, 1.0, 0.0], "metadata": {"k": 1}},
                ]
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["message"] == "Added 2 points with text"
        assert r.json()["data"] == {"added": 2, "total": 2}
        got = client.get("/api/v1/collections/c/points/t1").json()["data"]
        assert got["text"] == "hello world"
        assert own(client.get("/api/v1/collections/c/points/t2").json()["data"]["metadata"]) == {"k": 1}

    def test_missing_vector_key_is_400(self, client):
        create(client)
        r = client.post("/api/v2/collections/c/points", json={"points": [{"id": "x"}]})
        assert r.status_code == 400
        assert "Missing required field" in r.json()["detail"]

    def test_dimension_mismatch_and_missing_collection(self, client):
        create(client)
        r = client.post(
            "/api/v2/collections/c/points", json={"points": [{"id": "x", "vector": [1.0]}]}
        )
        assert r.status_code == 400
        assert (
            client.post("/api/v2/collections/none/points", json={"points": []}).status_code == 404
        )

    def test_sparse_points_and_sparse_search(self, client):
        create(client)
        r = client.post(
            "/api/v2/collections/c/points/sparse",
            json={
                "points": [
                    {
                        "id": "s1",
                        "vector": [1.0, 0.0, 0.0],
                        "sparse_vector": {"indices": [0, 42], "values": [0.5, 1.2]},
                    },
                    {"id": "s2", "vector": [0.0, 1.0, 0.0], "metadata": {"k": 1}},
                ]
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["message"] == "Added 2 points with sparse vectors"
        search = client.post(
            "/api/v1/collections/c/sparse-search",
            json={"query": {"indices": [42], "values": [1.0]}, "limit": 5},
        )
        assert search.status_code == 200, search.text
        assert ids_of(search) == ["s1"]
        both = client.post(
            "/api/v1/collections/c/dense-sparse-search",
            json={
                "dense_query": [1.0, 0.0, 0.0],
                "sparse_query": {"indices": [42], "values": [1.0]},
                "limit": 5,
            },
        )
        assert both.status_code == 200, both.text
        assert "s1" in ids_of(both)

    def test_sparse_errors(self, client):
        create(client)
        bad = client.post(
            "/api/v2/collections/c/points/sparse",
            json={
                "points": [
                    {"id": "x", "vector": [1.0, 0.0, 0.0], "sparse_vector": {"indices": [1]}}
                ]
            },
        )
        assert bad.status_code == 400
        assert "Missing required field" in bad.json()["detail"]
        mismatch = client.post(
            "/api/v1/collections/c/sparse-search",
            json={"query": {"indices": [1, 2], "values": [1.0]}},
        )
        assert mismatch.status_code == 400
        assert (
            client.post(
                "/api/v1/collections/none/sparse-search",
                json={"query": {"indices": [], "values": []}},
            ).status_code
            == 404
        )
        assert (
            client.post("/api/v2/collections/none/points/sparse", json={"points": []}).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/collections/none/dense-sparse-search",
                json={"dense_query": [1.0], "sparse_query": {"indices": [], "values": []}},
            ).status_code
            == 404
        )
        wrong_dim = client.post(
            "/api/v1/collections/c/dense-sparse-search",
            json={"dense_query": [1.0], "sparse_query": {"indices": [], "values": []}},
        )
        assert wrong_dim.status_code == 400


# =============================================================================
# Vector search
# =============================================================================


class TestSearch:
    def test_nearest_first(self, client):
        seed(client)
        r = client.post("/api/v1/collections/c/search", json={"query": [1.0, 0.0, 0.0], "limit": 2})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert [hit["id"] for hit in data["results"]] == ["p1", "p3"]
        assert data["search_mode"] == "vector"
        assert data["total_searched"] >= 2
        assert "query_time_ms" in data
        assert "vector" not in data["results"][0]
        assert own(data["results"][0]["metadata"]) == {"category": "a", "n": 1}

    def test_legacy_alias(self, client):
        seed(client)
        r = client.post("/api/collections/c/search", json={"query": [0.0, 1.0, 0.0], "limit": 1})
        assert ids_of(r) == ["p2"]

    def test_filter_eq_and_range(self, client):
        seed(client)
        eq = client.post(
            "/api/v1/collections/c/search",
            json={"query": [1.0, 0.0, 0.0], "filter": {"category": "b"}},
        )
        assert ids_of(eq) == ["p2"]
        rng = client.post(
            "/api/v1/collections/c/search",
            json={"query": [1.0, 0.0, 0.0], "filter": {"n": {"$lt": 3}}},
        )
        assert set(ids_of(rng)) == {"p1", "p2"}
        none = client.post(
            "/api/v1/collections/c/search",
            json={"query": [1.0, 0.0, 0.0], "filter": {"category": "zzz"}},
        )
        assert ids_of(none) == []

    def test_include_vectors_and_threshold(self, client):
        seed(client)
        r = client.post(
            "/api/v1/collections/c/search",
            json={"query": [1.0, 0.0, 0.0], "include_vectors": True, "limit": 1},
        )
        assert r.json()["data"]["results"][0]["vector"] == pytest.approx([1.0, 0.0, 0.0])
        strict = client.post(
            "/api/v1/collections/c/search",
            json={"query": [1.0, 0.0, 0.0], "score_threshold": 0.999},
        )
        assert ids_of(strict) == ["p1"]

    def test_wrong_dimension_is_400(self, client):
        seed(client)
        r = client.post("/api/v1/collections/c/search", json={"query": [1.0, 0.0]})
        assert r.status_code == 400
        assert "dimension" in r.json()["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            {"query": [1.0, 0.0, 0.0], "limit": 0},
            {"query": [1.0, 0.0, 0.0], "limit": 1001},
            {"query": "not a vector"},
            {"limit": 3},
            {"query": [1.0, 0.0, 0.0], "filter": "category=a"},
            {"query": [1.0, 0.0, 0.0], "include_vectors": "maybe"},
        ],
    )
    def test_bad_search_bodies_are_422(self, client, body):
        create(client)
        assert client.post("/api/v1/collections/c/search", json=body).status_code == 422

    def test_missing_collection_is_404(self, client):
        r = client.post("/api/v1/collections/none/search", json={"query": [1.0, 0.0, 0.0]})
        assert r.status_code == 404
        assert r.json()["detail"] == "Collection 'none' not found"

    def test_repeat_search_is_served_the_same_with_and_without_cache(self, client):
        seed(client)
        body = {"query": [0.9, 0.1, 0.0], "limit": 3}
        first = client.post("/api/v1/collections/c/search", json=body).json()["data"]
        second = client.post("/api/v1/collections/c/search", json=body).json()["data"]
        uncached = client.post(
            "/api/v1/collections/c/search", json={**body, "use_cache": False}
        ).json()["data"]
        ids = [hit["id"] for hit in first["results"]]
        assert ids == [hit["id"] for hit in second["results"]]
        assert ids == [hit["id"] for hit in uncached["results"]]
        stats = client.get("/api/v1/cache/stats").json()["data"]
        assert isinstance(stats["hits"], int) and isinstance(stats["misses"], int)

    def test_empty_collection_returns_no_results(self, client):
        create(client)
        r = client.post("/api/v1/collections/c/search", json={"query": [1.0, 0.0, 0.0]})
        assert r.status_code == 200
        assert r.json()["data"]["results"] == []


# =============================================================================
# Keyword, hybrid and enterprise search
# =============================================================================


TEXTS = [
    {
        "id": "k1",
        "vector": [1.0, 0.0, 0.0],
        "text": "red apple pie",
        "metadata": {"category": "food"},
    },
    {
        "id": "k2",
        "vector": [0.0, 1.0, 0.0],
        "text": "blue ocean water",
        "metadata": {"category": "sea"},
    },
    {
        "id": "k3",
        "vector": [0.0, 0.0, 1.0],
        "text": "apple orchard",
        "metadata": {"category": "food"},
    },
]


def seed_hybrid(client, name="h"):
    r = client.post(
        "/api/v2/collections", json={"name": name, "dimension": DIM, "tags": ["hybrid"]}
    )
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v2/collections/{name}/points", json={"points": TEXTS})
    assert r.status_code == 200, r.text


class TestKeywordAndHybridSearch:
    def test_keyword_search(self, client):
        seed_hybrid(client)
        r = client.post("/api/v1/collections/h/keyword-search", json={"query_text": "apple"})
        assert r.status_code == 200, r.text
        assert set(ids_of(r)) == {"k1", "k3"}
        assert r.json()["data"]["search_mode"] == "keyword"
        filtered = client.post(
            "/api/v1/collections/h/keyword-search",
            json={
                "query_text": "apple",
                "filter": {"category": "sea"},
                "include_highlights": False,
            },
        )
        assert ids_of(filtered) == []

    def test_hybrid_search(self, client):
        seed_hybrid(client)
        r = client.post(
            "/api/v1/collections/h/hybrid-search",
            json={"query": [0.0, 0.0, 1.0], "query_text": "apple", "limit": 2},
        )
        assert r.status_code == 200, r.text
        assert ids_of(r)[0] == "k3"
        assert r.json()["data"]["search_mode"] == "hybrid"

    def test_keyword_needs_a_text_index_and_hybrid_says_it_fell_back(self, client):
        """The two routes answer a collection with no text index differently,
        and both are honest about it: keyword refuses, hybrid runs the dense
        half and reports the mode it actually used."""
        create(client, "dense")
        kw = client.post("/api/v1/collections/dense/keyword-search", json={"query_text": "x"})
        assert kw.status_code == 400
        assert "enable_text_index" in kw.json()["detail"]
        hy = client.post(
            "/api/v1/collections/dense/hybrid-search",
            json={"query": [1.0, 0.0, 0.0], "query_text": "x"},
        )
        assert hy.status_code == 200, hy.text
        assert hy.json()["data"]["search_mode"] == "vector"

    def test_errors(self, client):
        seed_hybrid(client)
        assert (
            client.post(
                "/api/v1/collections/none/keyword-search", json={"query_text": "x"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/collections/none/hybrid-search", json={"query": [1.0], "query_text": "x"}
            ).status_code
            == 404
        )
        assert client.post("/api/v1/collections/h/keyword-search", json={}).status_code == 422
        assert (
            client.post(
                "/api/v1/collections/h/hybrid-search", json={"query": [1.0, 0.0, 0.0]}
            ).status_code
            == 422
        )
        bad_dim = client.post(
            "/api/v1/collections/h/hybrid-search", json={"query": [1.0], "query_text": "apple"}
        )
        assert bad_dim.status_code == 400
        bad_weight = client.post(
            "/api/v1/collections/h/hybrid-search",
            json={"query": [1.0, 0.0, 0.0], "query_text": "x", "vector_weight": 1.5},
        )
        assert bad_weight.status_code == 422


ACL_POINTS = [
    {"id": "a1", "vector": [1.0, 0.0, 0.0], "metadata": {"_acl": ["user:alice"], "category": "x"}},
    {"id": "a2", "vector": [0.9, 0.1, 0.0], "metadata": {"_acl": ["group:eng"], "category": "y"}},
    {"id": "a3", "vector": [0.8, 0.2, 0.0], "metadata": {"category": "x"}},
]


class TestEnterpriseSearch:
    @pytest.fixture
    def seeded(self, client):
        create(client, "e")
        r = client.post("/api/v1/collections/e/points", json={"points": ACL_POINTS})
        assert r.status_code == 200, r.text
        return client

    @pytest.mark.parametrize("method", ["exact", "mmr", "weighted"])
    def test_rerank_methods(self, seeded, method):
        r = seeded.post(
            "/api/v1/collections/e/search/rerank",
            json={"query": [1.0, 0.0, 0.0], "limit": 2, "rerank_method": method, "rerank_limit": 3},
        )
        assert r.status_code == 200, r.text
        assert len(ids_of(r)) == 2

    def test_facets(self, seeded):
        r = seeded.post(
            "/api/v1/collections/e/search/facets",
            json={"query": [1.0, 0.0, 0.0], "facets": ["category"], "facet_limit": 5},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert "results" in data
        assert "facets" in data
        assert len(data["results"]) == 3

    def test_acl(self, seeded):
        alice = seeded.post(
            "/api/v1/collections/e/search/acl",
            json={"query": [1.0, 0.0, 0.0], "user_principals": ["user:alice"]},
        )
        assert alice.status_code == 200, alice.text
        assert ids_of(alice) == ["a1"]
        eng = seeded.post(
            "/api/v1/collections/e/search/acl",
            json={
                "query": [1.0, 0.0, 0.0],
                "user_principals": ["group:eng"],
                "default_allow": True,
            },
        )
        assert set(ids_of(eng)) == {"a2", "a3"}
        nobody = seeded.post(
            "/api/v1/collections/e/search/acl",
            json={"query": [1.0, 0.0, 0.0], "user_principals": ["user:bob"]},
        )
        assert ids_of(nobody) == []

    def test_enterprise(self, seeded):
        r = seeded.post(
            "/api/v1/collections/e/search/enterprise",
            json={
                "query": [1.0, 0.0, 0.0],
                "user_principals": ["user:alice", "group:eng"],
                "facets": ["category"],
                "rerank": True,
                "rerank_method": "mmr",
                "rerank_limit": 3,
            },
        )
        assert r.status_code == 200, r.text
        assert set(ids_of(r)) == {"a1", "a2"}
        plain = seeded.post(
            "/api/v1/collections/e/search/enterprise", json={"query": [1.0, 0.0, 0.0]}
        )
        assert plain.status_code == 200 and len(ids_of(plain)) == 3

    def test_errors(self, seeded):
        q = {"query": [1.0, 0.0, 0.0]}
        for route in ("rerank", "facets", "enterprise"):
            assert (
                seeded.post(f"/api/v1/collections/none/search/{route}", json=q).status_code == 404
            )
            bad = seeded.post(f"/api/v1/collections/e/search/{route}", json={"query": [1.0]})
            assert bad.status_code == 400, route
        assert (
            seeded.post(
                "/api/v1/collections/none/search/acl", json={**q, "user_principals": []}
            ).status_code
            == 404
        )
        assert seeded.post("/api/v1/collections/e/search/acl", json=q).status_code == 422
        assert (
            seeded.post(
                "/api/v1/collections/e/search/acl", json={"query": [1.0], "user_principals": []}
            ).status_code
            == 400
        )
        assert (
            seeded.post(
                "/api/v1/collections/e/search/rerank", json={**q, "diversity_lambda": 2}
            ).status_code
            == 422
        )


# =============================================================================
# Text routes (bundled embedder, 384 dimensions)
# =============================================================================


SENTENCES = [
    {"id": "basalt", "text": "Basalt forms when lava cools quickly."},
    {"id": "bread", "text": "Sourdough is leavened by wild yeast.", "payload": {"kind": "food"}},
]


class TestTextRoutes:
    def test_a_collection_written_here_opens_in_the_library_without_a_warning(
        self, client, tmp_path
    ):
        """The server recorded the embedder's key, bge_small_en, where the
        library records the model's name. Opening the collection from Python
        then warned that it was built with another model and that scores
        would be wrong until a reembed(), about the same model."""
        import warnings

        from vectrixdb import Vectrix
        from vectrixdb.exceptions import ModelMismatchWarning

        create(client, "t", dimension=TEXT_DIM)
        assert client.post("/api/v1/collections/t/text-upsert", json={"points": SENTENCES}).status_code == 200
        client.close()
        with warnings.catch_warnings():
            warnings.simplefilter("error", ModelMismatchWarning)
            db = Vectrix("t", path=str(tmp_path / "db"))
        try:
            assert db.embedding_model == Vectrix._default_model
            assert db.search("volcanic rock", limit=1).top.id == "basalt"
        finally:
            db.close()

    def test_text_upsert_merges_text_into_payload(self, client):
        create(client, "t", dimension=TEXT_DIM)
        r = client.post("/api/v1/collections/t/text-upsert", json={"points": SENTENCES})
        assert r.status_code == 200, r.text
        assert r.json()["message"] == "Added 2 points with auto-embedded text"
        assert r.json()["data"] == {"added": 2, "total": 2}
        basalt = client.get("/api/v1/collections/t/points/basalt").json()["data"]
        assert own(basalt["metadata"]) == {"text": SENTENCES[0]["text"]}
        assert basalt["text"] == SENTENCES[0]["text"]
        assert len(basalt["vector"]) == TEXT_DIM
        bread = client.get("/api/v1/collections/t/points/bread").json()["data"]
        assert own(bread["metadata"]) == {"kind": "food", "text": SENTENCES[1]["text"]}

    def test_text_upsert_keeps_an_explicit_text_field(self, client):
        create(client, "t", dimension=TEXT_DIM)
        r = client.post(
            "/api/v1/collections/t/text-upsert",
            json={"points": [{"id": "x", "text": "embedded body", "payload": {"text": "custom"}}]},
        )
        assert r.status_code == 200
        got = client.get("/api/v1/collections/t/points/x").json()["data"]
        assert got["metadata"]["text"] == "custom"
        assert got["text"] == "embedded body"

    def test_text_search_finds_the_right_sentence(self, client):
        create(client, "t", dimension=TEXT_DIM)
        client.post("/api/v1/collections/t/text-upsert", json={"points": SENTENCES})
        r = client.post(
            "/api/v1/collections/t/text-search", json={"query_text": "volcanic rock", "limit": 1}
        )
        assert r.status_code == 200, r.text
        hits = r.json()["data"]["results"]
        assert hits[0]["id"] == "basalt"
        assert "Basalt" in hits[0]["text"]
        assert "vector" not in hits[0]
        filtered = client.post(
            "/api/v1/collections/t/text-search",
            json={
                "query_text": "volcanic rock",
                "filter": {"kind": "food"},
                "include_vectors": True,
            },
        )
        assert ids_of(filtered) == ["bread"]
        assert len(filtered.json()["data"]["results"][0]["vector"]) == TEXT_DIM

    def test_text_hybrid_search(self, client):
        r = client.post(
            "/api/v2/collections", json={"name": "th", "dimension": TEXT_DIM, "tags": ["hybrid"]}
        )
        assert r.status_code == 200, r.text
        client.post("/api/v1/collections/th/text-upsert", json={"points": SENTENCES})
        hy = client.post(
            "/api/v1/collections/th/text-hybrid-search",
            json={"query_text": "yeast bread", "limit": 1},
        )
        assert hy.status_code == 200, hy.text
        assert ids_of(hy) == ["bread"]
        assert hy.json()["data"]["search_mode"] == "hybrid"

    def test_every_search_of_a_collection_the_library_filled_carries_the_text(self, tmp_path):
        """The library keeps a chunk's text beside its vector, not in its metadata.
        The keyword, hybrid and reranked searches came back without it, and the
        dashboard showed the metadata where the text should have been."""
        from vectrixdb import Vectrix
        from vectrixdb.core.database import VectrixDB

        texts = ["Basalt forms when lava cools quickly at the surface.", "Sourdough is leavened by wild yeast and a long ferment."]
        lib = Vectrix("lib", path=str(tmp_path / "db"), mode="hybrid")
        lib.add(texts, ids=["basalt", "bread"])
        lib.close()
        with make_client(tmp_path) as client:
            for path, body in (
                ("/api/v1/collections/lib/keyword-search", {"query_text": "yeast"}),
                ("/api/v1/collections/lib/text-hybrid-search", {"query_text": "wild yeast"}),
                ("/api/v1/collections/lib/text-search", {"query_text": "wild yeast", "rerank": True}),
            ):
                r = client.post(path, json=body)
                assert r.status_code == 200, (path, r.text)
                hits = r.json()["data"]["results"]
                assert hits and all(h.get("text") in texts for h in hits), (path, hits)
                assert "text" not in own(hits[0]["metadata"]), "the text is the result's, not copied into the metadata"
                if body.get("rerank"):
                    # Handed empty strings, the cross-encoder gave every candidate one score.
                    assert hits[0]["id"] == "bread" and hits[0]["score"] > hits[1]["score"], hits
        core = VectrixDB(str(tmp_path / "db"))
        try:
            collection = core.get_collection("lib")
            assert collection.keyword_search("yeast").results[0].text == texts[1]
        finally:
            core.close()

    def test_text_hybrid_search_needs_a_text_index(self, client):
        create(client, "t", dimension=TEXT_DIM)
        r = client.post("/api/v1/collections/t/text-hybrid-search", json={"query_text": "x"})
        assert r.status_code == 400
        assert "requires text index" in r.json()["detail"]

    def test_text_upsert_into_a_small_collection_is_400(self, client):
        create(client, "small", dimension=DIM)
        r = client.post("/api/v1/collections/small/text-upsert", json={"points": SENTENCES[:1]})
        assert r.status_code == 400
        assert "dimension" in r.json()["detail"]

    def test_missing_collection_and_bad_bodies(self, client):
        assert (
            client.post(
                "/api/v1/collections/none/text-upsert", json={"points": SENTENCES}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/collections/none/text-search", json={"query_text": "x"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/collections/none/text-hybrid-search", json={"query_text": "x"}
            ).status_code
            == 404
        )
        create(client, "t", dimension=TEXT_DIM)
        assert (
            client.post(
                "/api/v1/collections/t/text-upsert", json={"points": [{"id": "x"}]}
            ).status_code
            == 422
        )
        assert client.post("/api/v1/collections/t/text-search", json={}).status_code == 422
        assert (
            client.post(
                "/api/v1/collections/t/text-search", json={"query_text": "x", "limit": 0}
            ).status_code
            == 422
        )

    def test_embedder_failures_surface_as_500(self, client, monkeypatch):
        create(client, "t", dimension=TEXT_DIM)
        hybrid = client.post(
            "/api/v2/collections", json={"name": "th", "dimension": TEXT_DIM, "tags": ["hybrid"]}
        )
        assert hybrid.status_code == 200

        def broken(model=None):
            raise RuntimeError("no model on this box")

        monkeypatch.setattr(server, "get_text_embedder", broken)
        search = client.post("/api/v1/collections/t/text-search", json={"query_text": "x"})
        assert search.status_code == 500
        assert search.json()["detail"].startswith("Failed to embed query")
        upsert = client.post("/api/v1/collections/t/text-upsert", json={"points": SENTENCES[:1]})
        assert upsert.status_code == 500
        assert upsert.json()["detail"].startswith("Text embedder not available")
        th = client.post("/api/v1/collections/th/text-hybrid-search", json={"query_text": "x"})
        assert th.status_code == 500

    def test_embedding_that_raises_during_insert_is_500(self, client, monkeypatch):
        create(client, "t", dimension=TEXT_DIM)

        class Embedder:
            def embed(self, texts):
                raise RuntimeError("boom")

        monkeypatch.setattr(server, "get_text_embedder", lambda model=None: Embedder())
        r = client.post("/api/v1/collections/t/text-upsert", json={"points": SENTENCES[:1]})
        assert r.status_code == 500
        assert "Failed to embed and insert" in r.json()["detail"]


class TestGetTextEmbedder:
    """The cached embedder, tested without loading a model."""

    def test_default_query_model_is_derived(self):
        assert isinstance(server.DEFAULT_QUERY_MODEL, str) and server.DEFAULT_QUERY_MODEL
        assert server._default_query_model() == server.DEFAULT_QUERY_MODEL

    def test_cached_instance_is_reused_for_the_same_model(self, monkeypatch):
        stub = types.SimpleNamespace(_vectrix_model="m1")
        monkeypatch.setattr(server, "_text_embedder", stub)
        assert server.get_text_embedder("m1") is stub

    def test_a_different_model_rebuilds(self, monkeypatch):
        import vectrixdb.models as models

        built = []

        class FakeEmbedder:
            def __init__(self, model=None):
                self.model = model
                built.append(model)

        monkeypatch.setattr(models, "DenseEmbedder", FakeEmbedder)
        monkeypatch.setattr(server, "_text_embedder", types.SimpleNamespace(_vectrix_model="old"))
        embedder = server.get_text_embedder("new")
        assert built == ["new"]
        assert embedder._vectrix_model == "new"
        assert server.get_text_embedder("new") is embedder
        assert server.get_text_embedder()._vectrix_model == server.DEFAULT_QUERY_MODEL

    def test_construction_failure_is_a_503(self, monkeypatch):
        import vectrixdb.models as models

        class Broken:
            def __init__(self, model=None):
                raise OSError("model files missing")

        monkeypatch.setattr(models, "DenseEmbedder", Broken)
        monkeypatch.setattr(server, "_text_embedder", None)
        with pytest.raises(HTTPException) as info:
            server.get_text_embedder()
        assert info.value.status_code == 503
        assert "download-models" in info.value.detail


# =============================================================================
# Graph routes (no extraction pipeline is run)
# =============================================================================


class TestGraphRoutes:
    def make_graph_collection(self, client, name="g"):
        r = client.post(
            "/api/v2/collections", json={"name": name, "dimension": DIM, "tags": ["graph"]}
        )
        assert r.status_code == 200, r.text

    def test_get_graph_needs_the_graph_tag(self, client):
        create(client, "dense")
        r = client.get("/api/v1/collections/dense/graph")
        assert r.status_code == 400
        said = r.json()["detail"]
        assert said.startswith("dense was not made for graph search, so it has no knowledge graph.")
        assert 'mode="graph"' in said and "--mode graph" in said and "GraphRAG" not in said, "in words a person can act on"
        assert client.get("/api/v1/collections/none/graph").status_code == 404

    def test_health_says_whether_there_is_a_graph_so_the_page_need_not_ask(self, client):
        create(client, "dense")
        self.make_graph_collection(client)
        reply = client.get("/api/v1/collections/dense/health").json()
        assert reply["data"]["capabilities"]["graph"] is False, reply
        assert client.get("/api/v1/collections/g/health").json()["data"]["capabilities"]["graph"] is True

    def test_get_graph_on_an_empty_graph_collection(self, client):
        self.make_graph_collection(client)
        r = client.get("/api/v1/collections/g/graph", params={"limit": 10})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["nodes"] == [] and data["edges"] == []
        assert data["stats"] == {"total_entities": 0, "total_relationships": 0}
        assert "No graph data" in data["message"]

    def test_extract_needs_the_graph_tag(self, client):
        create(client, "dense")
        r = client.post("/api/v1/collections/dense/graph/extract")
        assert r.status_code == 400 and "was not made for graph search" in r.json()["detail"], "the same words as the read route"
        assert client.post("/api/v1/collections/none/graph/extract").status_code == 404

    def test_a_kept_graph_is_read_from_the_store_and_says_when_the_index_moved_on(self, client, tmp_path, monkeypatch):
        from vectrixdb.api import server
        from vectrixdb.graph_store import graph_store

        monkeypatch.setenv("VECTRIXDB_GRAPH_STORE", str(tmp_path / "graphs"))
        server._GRAPH_STORES.clear()
        self.make_graph_collection(client)
        graph_store(str(tmp_path / "graphs")).put("g", {
            "collection": "g", "extracted_at": "2026-09-18T10:00:00+00:00", "build": "old-build", "model": "spaCy",
            "entities": [{"id": "e1", "name": "Meridian", "type": "Organization", "description": "", "importance": 0.8}, {"id": "e2", "name": "Q3 test", "type": "event", "description": "", "importance": 0.5}],
            "relationships": [{"id": "r1", "source_id": "e1", "target_id": "e2", "type": "underwent", "description": "", "strength": 0.7}],
            "communities": {"e1": 0, "e2": 0},
        })
        data = client.get("/api/v1/collections/g/graph").json()["data"]
        assert [n["data"]["label"] for n in data["nodes"]] == ["Meridian", "Q3 test"] and data["nodes"][0]["data"]["type"] == "organization"
        assert data["edges"][0]["data"]["label"] == "underwent" and data["nodes"][0]["data"]["community"] == 0
        assert data["build"] == "old-build" and data["extracted_at"].startswith("2026-09-18") and data["kept_at"] == str(tmp_path / "graphs")
        assert data["stale"] is False and data["since"] is None, "nothing has been written, so the index has not moved on"
        client.post("/api/v1/collections/g/points", json={"points": POINTS})
        data = client.get("/api/v1/collections/g/graph").json()["data"]
        assert data["stale"] is True and data["current_build"] and data["current_build"] != "old-build"
        assert data["since"] == {"builds": 1, "chunks": len(POINTS)}, "one build wrote every point after the graph was read"
        assert data["stats"]["total_entities"] == 2 and data["stats"]["total_communities"] == 1

    def test_deleting_a_collection_deletes_its_graph(self, client, tmp_path, monkeypatch):
        from vectrixdb.api import server
        from vectrixdb.graph_store import graph_store

        monkeypatch.setenv("VECTRIXDB_GRAPH_STORE", str(tmp_path / "graphs"))
        server._GRAPH_STORES.clear()
        self.make_graph_collection(client)
        store = graph_store(str(tmp_path / "graphs"))
        store.put("g", {"collection": "g", "extracted_at": "2026-09-18T10:00:00+00:00", "build": None, "model": "spaCy", "entities": [], "relationships": [], "communities": {}})
        assert client.delete("/api/v1/collections/g").status_code == 200
        assert store.get("g") is None and not (tmp_path / "graphs" / "g" / "graph.json").exists()

    def test_extract_with_no_documents(self, client):
        self.make_graph_collection(client)
        r = client.post("/api/v1/collections/g/graph/extract")
        assert r.status_code == 200
        assert r.json() == {"ok": False, "message": "No documents in collection", "data": None}

    def test_extract_with_points_that_have_no_text(self, client):
        self.make_graph_collection(client)
        client.post("/api/v1/collections/g/points", json={"points": POINTS})
        r = client.post("/api/v1/collections/g/graph/extract")
        assert r.status_code == 200
        assert r.json()["message"] == "No text content found in documents"


# =============================================================================
# Document index
# =============================================================================


DOC = """# Title

Intro paragraph about vectors.

## Section

More text about indexes and search.
"""


class TestDocumentRoutes:
    def test_empty_index(self, client):
        assert client.get("/api/v1/documents").json() == {"documents": [], "total": 0}
        assert client.get("/api/v1/documents/nope").status_code == 404
        assert client.get("/api/v1/documents/nope/chunks").status_code == 404
        assert client.delete("/api/v1/documents/nope").status_code == 404

    def test_index_get_chunks_delete(self, client):
        r = client.post(
            "/api/v1/documents", json={"text": DOC, "title": "Doc", "metadata": {"k": 1}}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["document"]["title"] == "Doc"
        doc_id = body["document"]["doc_id"]
        assert body["document"]["node_count"] >= 1

        listed = client.get("/api/v1/documents").json()
        assert listed["total"] == 1
        assert listed["documents"][0]["doc_id"] == doc_id
        assert listed["documents"][0]["metadata"] == {"k": 1}
        assert client.get("/api/v1/info").json()["documents_count"] == 1

        got = client.get(f"/api/v1/documents/{doc_id}").json()
        assert got["document"]["doc_id"] == doc_id
        assert got["nodes"] and {"node_id", "level", "title", "text"} <= set(got["nodes"][0])

        chunks = client.get(f"/api/v1/documents/{doc_id}/chunks", params={"chunk_size": 200}).json()
        assert chunks["doc_id"] == doc_id and chunks["total"] >= 1
        assert "text" in chunks["chunks"][0]
        assert (
            client.get(f"/api/v1/documents/{doc_id}/chunks", params={"chunk_size": 10}).status_code
            == 422
        )

        deleted = client.delete(f"/api/v1/documents/{doc_id}")
        assert deleted.json() == {"ok": True, "deleted": doc_id}
        assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404
        assert client.get("/api/v1/documents").json()["total"] == 0

    def test_empty_text_is_422(self, client):
        assert client.post("/api/v1/documents", json={"text": ""}).status_code == 422
        assert client.post("/api/v1/documents", json={}).status_code == 422


# =============================================================================
# WebSocket and the connection manager
# =============================================================================


class TestWebSocket:
    def test_connect_ping_and_status(self, client):
        with client.websocket_connect("/ws") as ws:
            hello = json.loads(ws.receive_text())
            assert hello["event"] == "connected"
            assert "timestamp" in hello
            assert client.get("/api/v1/ws/status").json()["active_connections"] == 1
            ws.send_text("ping")
            pong = json.loads(ws.receive_text())
            assert pong["event"] == "pong"
        assert client.get("/api/v1/ws/status").json()["active_connections"] == 0

    def test_events_are_broadcast_to_connected_clients(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
            create(client, "live")
            event = json.loads(ws.receive_text())
            assert event["event"] == "collection_created"
            assert event["data"]["name"] == "live"
            assert event["data"]["collection"]["dimension"] == DIM

    def test_broadcast_drops_dead_connections(self):
        manager = server.ConnectionManager()

        class Live:
            def __init__(self):
                self.sent = []

            async def send_text(self, text):
                self.sent.append(json.loads(text))

        class Dead:
            async def send_text(self, text):
                raise ConnectionError("gone")

        live, dead = Live(), Dead()
        manager.active_connections.update({live, dead})
        asyncio.run(manager.broadcast("evt", {"k": 1}))
        assert manager.connection_count == 1
        assert live.sent[0]["event"] == "evt" and live.sent[0]["data"] == {"k": 1}
        asyncio.run(manager.broadcast("bare"))
        assert live.sent[1]["data"] == {}
        manager.disconnect(live)
        manager.disconnect(live)  # discarding twice is fine
        assert manager.connection_count == 0


# =============================================================================
# Helpers, lifespan branches and run_server
# =============================================================================


class TestHelpers:
    def test_get_db_before_startup(self, monkeypatch):
        monkeypatch.setattr(server, "_db", None)
        with pytest.raises(RuntimeError, match="not initialized"):
            server.get_db()

    def test_redact_search_results(self):
        payload = {
            "results": [
                {"id": "abcdefghij", "vector": [1.0, 2.0, 3.0]},
                {"id": "short", "vector": "already hidden"},
                {"id": "", "score": 1.0},
            ]
        }
        out = server.redact_search_results(payload)
        assert out["_redacted"] is True
        assert out["results"][0]["id"] == "abcd***ghij"
        assert out["results"][0]["vector"] == "[3 dimensions - hidden]"
        assert out["results"][1]["id"] == "short"
        assert out["results"][1]["vector"] == "already hidden"
        assert out["results"][2]["id"] == ""
        assert server.redact_search_results({"other": 1}) == {"other": 1, "_redacted": True}

    def test_is_authenticated(self, monkeypatch):
        class Req:
            def __init__(self, key):
                self.headers = {"api-key": key} if key else {}

        assert server.is_authenticated(Req(None)) is True
        monkeypatch.setenv("VECTRIXDB_API_KEY", "secret")
        assert server.is_authenticated(Req("secret")) is True
        assert server.is_authenticated(Req("other")) is False
        assert server.is_authenticated(Req(None)) is False

    def test_memory_storage_and_no_cache_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_STORAGE_BACKEND", "memory")
        monkeypatch.setenv("VECTRIXDB_CACHE_BACKEND", "none")
        with make_client(tmp_path) as c:
            assert c.get("/api/v1/info").json()["storage_backend"] == "memory"
            seed(c)
            r = c.post("/api/v1/collections/c/search", json={"query": [1.0, 0.0, 0.0], "limit": 1})
            assert ids_of(r) == ["p1"]

    def test_run_server_configures_env_and_calls_uvicorn(self, monkeypatch):
        calls = []
        fake_uvicorn = types.SimpleNamespace(run=lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        for name in ("VECTRIXDB_PATH", "VECTRIXDB_API_KEY", "VECTRIXDB_READ_ONLY_API_KEY"):
            monkeypatch.setenv(name, "placeholder")  # so monkeypatch restores them
        server.run_server(
            host="127.0.0.1", port=1234, db_path="/tmp/x", api_key="k", read_only_key="r"
        )
        assert calls == [
            (("vectrixdb.api.server:app",), {"host": "127.0.0.1", "port": 1234, "reload": False, "server_header": False})
        ]
        import os

        assert os.environ["VECTRIXDB_PATH"] == "/tmp/x"
        assert os.environ["VECTRIXDB_API_KEY"] == "k"
        assert os.environ["VECTRIXDB_READ_ONLY_API_KEY"] == "r"

    def test_run_server_leaves_keys_alone_when_not_given(self, monkeypatch):
        fake_uvicorn = types.SimpleNamespace(run=lambda *a, **kw: None)
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        monkeypatch.setenv("VECTRIXDB_PATH", "placeholder")
        monkeypatch.setenv("VECTRIXDB_API_KEY", "keep")
        server.run_server(db_path="/tmp/y")
        import os

        assert os.environ["VECTRIXDB_API_KEY"] == "keep"
        assert os.environ["VECTRIXDB_PATH"] == "/tmp/y"
