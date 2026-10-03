"""The text search routes when the store is behind the collection.

Found on the live Azure query app on 2026-10-02. Every text search failed: the
index's second vector is searched with the question's words, and the routes
handed the collection only a vector. The failure was a StorageOperationError,
which no handler took, so under the Functions host the reply was an empty 500
and nothing reached the log.
"""

from __future__ import annotations

import contextlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.api import server  # noqa: E402
from vectrixdb.core.collection import Collection  # noqa: E402
from vectrixdb.exceptions import StorageOperationError  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTRIXDB_PATH", str(tmp_path))
    for name in ("VECTRIXDB_API_KEY", "VECTRIXDB_SIGNIN"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(server.create_app()) as c:
        assert (
            c.post("/api/v1/collections", json={"name": "t", "dimension": 384}).status_code == 200
        )
        yield c


class TestAFailingStoreSaysWhy:
    def test_a_store_error_is_a_502_with_the_stores_words_not_an_empty_500(
        self, client, monkeypatch
    ):
        def fails(self, *args, **kwargs):
            raise StorageOperationError(
                "vector_search", "AzureSearch", "k must be between 1 and 10000"
            )

        monkeypatch.setattr(Collection, "search", fails)
        reply = client.post("/api/v1/collections/t/text-search", json={"query_text": "revenue"})
        assert reply.status_code == 502
        assert (
            reply.json()["ok"] is False
            and "k must be between 1 and 10000" in reply.json()["message"]
        )


class TestTheWordsReachTheStore:
    def test_a_store_that_searches_with_words_is_told_them(self):
        heard = []

        class Store:
            @contextlib.contextmanager
            def using_vectors(self, vectors, query_text=None):
                heard.append((vectors, query_text))
                yield self

            @staticmethod
            def scoped():
                return False

        class Held:
            _storage_backend = Store()

        with server._in_words(Held(), "total revenue"):
            pass
        assert heard == [(None, "total revenue")]

    def test_a_store_without_the_idea_or_a_search_already_scoped_is_left_alone(self):
        class Plain:
            _storage_backend = object()

        with server._in_words(Plain(), "revenue"):
            pass

        class Scoped:
            class _storage_backend:  # noqa: N801 - stands in for an instance
                @staticmethod
                def using_vectors(vectors, query_text=None):
                    raise AssertionError("not asked twice")

                @staticmethod
                def scoped():
                    return True

        with server._in_words(Scoped(), "revenue"):
            pass
