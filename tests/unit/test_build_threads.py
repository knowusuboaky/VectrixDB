"""VECTRIXDB_BUILD_THREADS=1: two builds of the same vectors are the same index.

usearch inserts on every core at once, so the graph differs a little from one
build to the next and the tail of an approximate search moves with it. On one
thread the inserts happen in one order. What is held to here is that the
setting reaches the index, that two one-thread builds answer every query the
same, and that a value that is not a number of threads stops the write rather
than being read as "every core".
"""

from __future__ import annotations

import numpy as np
import pytest

from vectrixdb import Vectrix
from vectrixdb.core import collection as collection_module
from vectrixdb.exceptions import ConfigurationError

pytestmark = pytest.mark.skipif(not collection_module.USEARCH_AVAILABLE, reason="usearch is not installed")


def build(root, vectors):
    db = Vectrix("c", path=str(root), dimension=vectors.shape[1], embedding_cache=False)
    db._collection.add(ids=[f"v{n}" for n in range(len(vectors))], vectors=vectors.tolist())
    return db


def answers(db, queries):
    return [[hit.id for hit in db._collection.search(q.tolist(), limit=10).results] for q in queries]


class TestTheSetting:
    @pytest.mark.parametrize("given, expected", [("", {}), ("  ", {}), ("0", {}), ("1", {"threads": 1}), (" 4 ", {"threads": 4})])
    def test_what_it_asks_usearch_for(self, monkeypatch, given, expected):
        monkeypatch.setenv("VECTRIXDB_BUILD_THREADS", given)
        assert collection_module._build_threads() == expected

    def test_unset_is_usearchs_own_choice(self, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_BUILD_THREADS", raising=False)
        assert collection_module._build_threads() == {}

    @pytest.mark.parametrize("given", ["one", "1.5", "-1"])
    def test_a_value_that_is_not_a_number_of_threads_is_refused(self, monkeypatch, given):
        monkeypatch.setenv("VECTRIXDB_BUILD_THREADS", given)
        with pytest.raises(ConfigurationError, match="VECTRIXDB_BUILD_THREADS"):
            collection_module._build_threads()


def test_two_one_thread_builds_answer_every_query_the_same(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTRIXDB_BUILD_THREADS", "1")
    rng = np.random.default_rng(7)
    vectors = rng.standard_normal((3000, 32)).astype(np.float32)
    queries = rng.standard_normal((60, 32)).astype(np.float32)
    first, second = build(tmp_path / "a", vectors), build(tmp_path / "b", vectors)
    try:
        assert answers(first, queries) == answers(second, queries)
    finally:
        first.close()
        second.close()


def test_the_setting_reaches_a_rebuild_too(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTRIXDB_BUILD_THREADS", "1")
    rng = np.random.default_rng(11)
    vectors = rng.standard_normal((500, 16)).astype(np.float32)
    seen = []
    real = collection_module._build_threads
    monkeypatch.setattr(collection_module, "_build_threads", lambda: seen.append(1) or real())
    db = build(tmp_path / "a", vectors)
    try:
        before = len(seen)
        db._collection.rebuild_index()
        assert len(seen) > before, "a rebuild inserts on as many threads as a first build does"
    finally:
        db.close()
