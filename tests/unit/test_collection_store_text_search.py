"""Keyword and hybrid search on a collection another process filled.

Found on the live Azure query app on 2026-10-02: the query instance opens the
collections the ingest instance wrote, so its local index and text index are
empty, and only the store holds anything. Vector search already went to the
store in that case. Keyword search answered 200 with no results at all, and
hybrid search asked the store for k=0 and failed. Both now go to the store,
on the same rule.
"""

from __future__ import annotations

import contextlib

import numpy as np
import pytest

from vectrixdb.core.collection import Collection

DIM = 4


class Store:
    """A store holding three chunks, answering the way the Azure backend does."""

    ROWS = [
        (
            "a",
            {"text_content": "TD total revenue was 64 billion", "_vx_doc": "td.pdf", "page": 3},
            3.1,
        ),
        ("b", {"text_content": "office icons and a download arrow", "_vx_doc": "office.png"}, 1.2),
        ("c", {"text_content": "net income rose", "_vx_doc": "td.pdf", "page": 9}, 0.4),
    ]

    def __init__(self):
        self.asked = []

    def insert_batch(self, collection, documents):
        self.asked.append(("insert", len(documents)))

    @contextlib.contextmanager
    def session_role(self, role):
        yield

    def vector_search(self, collection, query_vector, limit=10, filter=None):
        self.asked.append(("vector", limit))
        return [(i, dict(d), 0.1) for i, d, _ in self.ROWS][:limit]

    def text_search(self, collection, query_text, limit=10, filter=None):
        self.asked.append(("text", query_text, limit))
        return [
            (i, dict(d), s)
            for i, d, s in self.ROWS
            if any(w in d["text_content"] for w in query_text.split())
        ][:limit]

    def hybrid_search(self, collection, query_vector, query_text, limit=10, filter=None):
        self.asked.append(("hybrid", query_text, limit))
        if limit < 1:
            raise AssertionError("k must be between 1 and 10000")
        return [(i, dict(d), s) for i, d, s in self.ROWS][:limit]


@pytest.fixture
def filled_elsewhere():
    store = Store()
    return Collection("financial", DIM, enable_text_index=True, storage_backend=store), store


class TestAQueryInstanceSearchesTheStore:
    def test_keyword_search_answers_from_the_store_not_an_empty_local_index(self, filled_elsewhere):
        collection, store = filled_elsewhere
        found = collection.keyword_search("revenue", limit=5)
        assert [r.id for r in found.results] == ["a"], "it answered nothing with a 200 before"
        hit = found.results[0]
        assert hit.text == "TD total revenue was 64 billion" and hit.metadata["page"] == 3
        assert "text_content" not in hit.metadata, "the store's own columns are not metadata"
        assert hit.matched_by == ["keywords"] and hit.relevance == 1.0
        assert store.asked[0][0] == "text"

    def test_hybrid_search_is_fused_by_the_store_and_never_asks_for_none(self, filled_elsewhere):
        collection, store = filled_elsewhere
        found = collection.hybrid_search(np.ones(DIM, dtype=np.float32), "revenue", limit=2)
        assert [r.id for r in found.results] == ["a", "b"]
        assert store.asked == [("hybrid", "revenue", 2)]

    def test_a_filter_is_applied_to_what_the_store_returns(self, filled_elsewhere):
        collection, _ = filled_elsewhere
        found = collection.keyword_search("revenue income", limit=5, filter={"_vx_doc": "td.pdf"})
        assert {r.id for r in found.results} == {"a", "c"}

    def test_a_collection_with_its_own_text_still_searches_it(self):
        store = Store()
        collection = Collection("docs", DIM, enable_text_index=True, storage_backend=store)
        collection.add(
            vectors=np.eye(DIM, dtype=np.float32)[:2],
            ids=["x", "y"],
            texts=["local revenue line", "something else"],
        )
        found = collection.keyword_search("revenue", limit=5)
        assert [r.id for r in found.results] == ["x"] and not any(
            a[0] == "text" for a in store.asked
        )

    def test_the_local_hybrid_prefetch_is_never_zero(self):
        collection = Collection("docs", DIM, enable_text_index=True)
        collection.add(vectors=np.eye(DIM, dtype=np.float32)[:1], ids=["x"], texts=["revenue"])
        found = collection.hybrid_search(np.eye(DIM, dtype=np.float32)[0], "revenue", limit=3)
        assert [r.id for r in found.results] == ["x"]
