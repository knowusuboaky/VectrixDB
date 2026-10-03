"""Unit tests for the sparse index and the search helper classes.

Covers vectrixdb.core.sparse_index (SparseIndex, HybridSparseIndex) and the
three search helpers under vectrixdb.core.search: sparse (SparseSearch,
BM25Scorer, QueryExpander), dense (DenseSearch, MultiQuerySearch,
PrefetchRescore), and colbert (MaxSimScorer, ColBERTSearch, ColBERTEncoder).

All fixtures are hand built: small dictionaries and 2 or 3 dimensional numpy
arrays, plus two tiny fake index classes for the dense search tests. Nothing
here loads a model, touches the network, or starts a service. The two
persistence tests use pytest's tmp_path fixture, which is local disk only.

A few genuine bugs turned up while writing these tests. Each one is pinned
with a strict xfail test so the suite starts failing the day the bug is
fixed, which is the signal to delete that test. The full list, with file and
line references:

  - vectrixdb/core/sparse_index.py:270 SparseIndex.search_cosine narrows to
    the top dot product candidates before computing cosine similarity, so
    the true highest cosine document can be dropped before it is ever
    scored. Its sibling in search/sparse.py (SparseSearch.search with
    metric="cosine") normalizes the whole candidate set first and does not
    have this problem, which the tests below show side by side.
  - vectrixdb/core/search/sparse.py:172 SparseSearch.remove leaves an empty
    posting list behind instead of deleting the now empty key, so
    get_stats overcounts num_posting_lists (and understates
    avg_posting_list_length) after removals.
  - vectrixdb/core/search/sparse.py:211 SparseSearch.search uses
    `if filter_ids` instead of `if filter_ids is not None`, so an explicit
    empty filter set silently searches the whole corpus instead of nothing.
  - vectrixdb/core/search/sparse.py:374 BM25Scorer.score has the same
    `filter_ids or ...` bug: an explicit empty filter set is treated as no
    filter at all.
  - vectrixdb/core/search/colbert.py:287 ColBERTSearch.search has the same
    truthiness bug for filter_ids (an empty list means "search everything").
  - vectrixdb/core/search/dense.py:348 MultiQuerySearch.search indexes
    weights[i] with no length check against queries, so a weights list
    shorter than queries raises an unhandled IndexError instead of a clear
    validation error.
  - vectrixdb/core/search/dense.py:257 DenseSearch (via _brute_force_search)
    does not validate k, so a negative k silently returns len(vectors) - 1
    results because of Python's [:-1] slicing, instead of rejecting the
    input or returning nothing.
  - vectrixdb/core/search/colbert.py:128 MaxSimScorer.score raises a numpy
    ValueError on a zero token document (similarities.max(axis=1) on an
    empty axis) instead of handling the edge case.
"""

import numpy as np
import pytest

from vectrixdb.core.sparse_index import HybridSparseIndex, SparseIndex
from vectrixdb.core.types import SparseVector as CoreSparseVector

from vectrixdb.core.search.sparse import (
    BM25Scorer,
    QueryExpander,
    SparseSearch,
    SparseVector,
)
from vectrixdb.core.search.dense import (
    DenseSearch,
    DenseSearchConfig,
    MultiQuerySearch,
    PrefetchRescore,
    SearchResult,
)
from vectrixdb.core.search.colbert import (
    ColBERTEncoder,
    ColBERTSearch,
    MaxSimScorer,
    TokenEmbeddings,
)


def tok_emb(rows, mask=None):
    """Build a TokenEmbeddings from plain nested lists, optionally with a mask."""
    embeddings = np.array(rows, dtype=np.float32)
    mask_arr = None if mask is None else np.array(mask, dtype=np.int32)
    return TokenEmbeddings(embeddings=embeddings, mask=mask_arr)


class FakeANNIndex:
    """Stand-in for an HNSW-like index.

    Returns exact Euclidean nearest neighbours in the batched
    (indices, distances) shape that DenseSearch and PrefetchRescore expect
    from a real index's search method.
    """

    def __init__(self, vectors):
        self.vectors = np.asarray(vectors, dtype=np.float32)

    def search(self, queries, k=10, ef=None):
        queries = np.atleast_2d(np.asarray(queries, dtype=np.float32))
        all_idx, all_dist = [], []
        for q in queries:
            dists = np.linalg.norm(self.vectors - q, axis=1)
            order = np.argsort(dists, kind="stable")[:k]
            all_idx.append(order.astype(np.int64))
            all_dist.append(dists[order].astype(np.float32))
        return np.array(all_idx), np.array(all_dist)


class FakeANNIndexWithGaps:
    """Like FakeANNIndex, but pads short result rows with -1, the sentinel a
    real ANN index uses when fewer than k neighbours exist."""

    def __init__(self, vectors):
        self.vectors = np.asarray(vectors, dtype=np.float32)

    def search(self, queries, k=10, ef=None):
        queries = np.atleast_2d(np.asarray(queries, dtype=np.float32))
        all_idx, all_dist = [], []
        for q in queries:
            dists = np.linalg.norm(self.vectors - q, axis=1)
            order = np.argsort(dists, kind="stable").tolist()
            idx = order[:k]
            dist = dists[order].tolist()[:k]
            while len(idx) < k:
                idx.append(-1)
                dist.append(0.0)
            all_idx.append(idx)
            all_dist.append(dist)
        return np.array(all_idx), np.array(all_dist)


class TestSparseIndexBasics:
    def test_add_and_get_roundtrip(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0, 2: 2.0})
        got = idx.get("a")
        assert list(got.indices) == [0, 2]
        assert list(got.values) == pytest.approx([1.0, 2.0])

    def test_get_missing_document_returns_none(self):
        idx = SparseIndex()
        assert idx.get("nope") is None

    def test_count_and_stats_on_empty_index(self):
        idx = SparseIndex()
        assert idx.count() == 0
        stats = idx.stats()
        assert stats["count"] == 0
        assert stats["total_nnz"] == 0
        assert stats["avg_nnz"] == 0
        assert stats["vocab_size"] == 0
        assert stats["memory_estimate_mb"] == 0.0

    def test_stats_reports_vocab_size_and_avg_nnz(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0, 1: 2.0})
        idx.add("b", {1: 3.0, 2: 4.0, 3: 5.0})
        stats = idx.stats()
        assert stats["count"] == 2
        assert stats["total_nnz"] == 5
        assert stats["avg_nnz"] == pytest.approx(2.5)
        assert stats["vocab_size"] == 4
        assert stats["memory_estimate_mb"] > 0

    def test_remove_then_search_no_longer_returns_removed_doc(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        idx.add("b", {0: 1.0})
        assert idx.remove("a") is True
        assert idx.get("a") is None
        assert idx.count() == 1
        assert [r.id for r in idx.search({0: 1.0}, limit=10)] == ["b"]

    def test_remove_nonexistent_returns_false(self):
        idx = SparseIndex()
        assert idx.remove("nope") is False

    def test_add_batch_length_mismatch_raises_value_error(self):
        idx = SparseIndex()
        with pytest.raises(ValueError):
            idx.add_batch(["a", "b"], [{0: 1.0}])

    def test_add_batch_returns_count_added(self):
        idx = SparseIndex()
        added = idx.add_batch(["a", "b"], [{0: 1.0}, {1: 1.0}])
        assert added == 2
        assert idx.count() == 2

    def test_normalize_constructor_flag_scales_stored_vectors_to_unit_norm(self):
        idx = SparseIndex(normalize=True)
        idx.add("a", {0: 3.0, 1: 4.0})
        stored = idx.get("a")
        assert stored.norm() == pytest.approx(1.0)
        assert list(stored.values) == pytest.approx([0.6, 0.8])

    def test_clear_resets_index_to_empty(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        idx.add("b", {1: 1.0})
        idx.clear()
        assert idx.count() == 0
        assert idx.stats()["vocab_size"] == 0
        assert idx.search({0: 1.0}, limit=10) == []

    def test_save_and_load_round_trip_preserves_documents(self, tmp_path):
        idx = SparseIndex(path=tmp_path / "idx")
        idx.add("a", {0: 1.0, 2: 2.0})
        idx.save()
        reloaded = SparseIndex(path=tmp_path / "idx")
        assert reloaded.count() == 1
        got = reloaded.get("a")
        assert list(got.indices) == [0, 2]
        assert list(got.values) == pytest.approx([1.0, 2.0])

    def test_save_writes_an_npz_and_never_a_pickle(self, tmp_path):
        """A pickle runs code when read, and a snapshot can bring one from anywhere."""
        idx = SparseIndex(path=tmp_path / "idx")
        idx.add("a", {0: 1.0, 2: 2.0})
        idx.add("b", {2: 0.5})
        idx.save()
        assert (tmp_path / "idx" / "sparse_index.npz").exists()
        assert not (tmp_path / "idx" / "sparse_index.pkl").exists()
        reloaded = SparseIndex(path=tmp_path / "idx")
        assert reloaded.count() == 2 and reloaded.stats()["total_nnz"] == 3
        assert [r.id for r in reloaded.search({2: 1.0}, limit=10)] == ["a", "b"]
        assert reloaded._norms["a"] == pytest.approx(5**0.5)
        empty = SparseIndex(path=tmp_path / "empty")
        empty.save()
        assert SparseIndex(path=tmp_path / "empty").count() == 0

    def test_a_pickle_from_before_is_read_once_and_replaced_on_save(self, tmp_path):
        import pickle

        folder = tmp_path / "idx"
        folder.mkdir()
        data = {
            "inverted_index": {0: [("a", 1.0)]},
            "docs": {"a": {"indices": [0], "values": [1.0]}},
            "norms": {"a": 1.0},
            "count": 1,
            "total_nnz": 1,
            "normalize": True,
        }
        with open(folder / "sparse_index.pkl", "wb") as f:
            pickle.dump(data, f)
        idx = SparseIndex(path=folder)
        assert idx.count() == 1 and idx.normalize is True
        assert [r.id for r in idx.search({0: 1.0}, limit=10)] == ["a"]
        idx.save()
        assert not (folder / "sparse_index.pkl").exists()
        assert (folder / "sparse_index.npz").exists()
        assert SparseIndex(path=folder).count() == 1

    def test_add_same_id_twice_replaces_the_vector(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        idx.add("a", {1: 9.0})
        assert list(idx.get("a").indices) == [1]
        assert idx.count() == 1

    def test_remove_cleans_up_now_empty_posting_lists(self):
        """Contrast with the documented bug in SparseSearch.remove
        (search/sparse.py:172): SparseIndex correctly deletes a posting
        list once it becomes empty, so vocab_size drops to 0."""
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        assert idx.stats()["vocab_size"] == 1
        idx.remove("a")
        assert idx.stats()["vocab_size"] == 0

    def test_save_with_no_path_configured_is_a_no_op(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        idx.save()
        assert idx.count() == 1

    def test_loading_from_a_path_with_no_saved_file_yet_starts_empty(self, tmp_path):
        empty_dir = tmp_path / "idx"
        empty_dir.mkdir()
        idx = SparseIndex(path=empty_dir)
        assert idx.count() == 0

    def test_remove_from_index_on_an_unknown_doc_is_a_no_op(self):
        # Defensive guard inside _remove_from_index: not reachable through
        # add()/remove() since both already check membership first.
        idx = SparseIndex()
        idx._remove_from_index("nope")

    def test_load_with_no_path_configured_is_a_no_op(self):
        # Defensive guard inside _load: __init__ only calls _load when
        # self.path is already set, so this is otherwise unreachable.
        idx = SparseIndex()
        idx._load()


class TestSparseIndexSearch:
    def test_search_ranking_order(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        idx.add("b", {0: 2.0})
        idx.add("c", {0: 0.5})
        results = idx.search({0: 1.0}, limit=10)
        assert [r.id for r in results] == ["b", "a", "c"]

    def test_search_tie_break_is_insertion_order_on_the_sorted_branch(self):
        idx = SparseIndex()
        for doc_id in ["a", "b", "c"]:
            idx.add(doc_id, {0: 1.0})
        results = idx.search({0: 1.0}, limit=10)  # len(scores)=3 <= limit: sorted() branch
        assert [r.id for r in results] == ["a", "b", "c"]

    def test_search_tie_break_is_insertion_order_on_the_heap_branch(self):
        idx = SparseIndex()
        for doc_id in ["a", "b", "c", "d", "e"]:
            idx.add(doc_id, {0: 1.0})
        results = idx.search({0: 1.0}, limit=3)  # len(scores)=5 > limit: heapq.nlargest branch
        assert [r.id for r in results] == ["a", "b", "c"]

    def test_search_respects_limit(self):
        idx = SparseIndex()
        for i, val in enumerate([5.0, 4.0, 3.0, 2.0, 1.0]):
            idx.add(f"d{i}", {0: val})
        results = idx.search({0: 1.0}, limit=2)
        assert [r.id for r in results] == ["d0", "d1"]

    def test_search_score_threshold_filters_low_scores(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        idx.add("b", {0: 2.0})
        idx.add("c", {0: 0.5})
        results = idx.search({0: 1.0}, limit=10, score_threshold=1.0)
        assert {r.id for r in results} == {"a", "b"}

    def test_search_doc_ids_filter_restricts_candidates(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        idx.add("b", {0: 2.0})
        idx.add("c", {0: 0.5})
        results = idx.search({0: 1.0}, limit=10, doc_ids={"a", "c"})
        assert [r.id for r in results] == ["a", "c"]

    def test_search_empty_corpus_returns_empty_list(self):
        idx = SparseIndex()
        assert idx.search({0: 1.0}, limit=10) == []

    def test_search_single_document_corpus(self):
        idx = SparseIndex()
        idx.add("only", {0: 1.0})
        results = idx.search({0: 1.0}, limit=10)
        assert [r.id for r in results] == ["only"]

    def test_search_with_no_overlapping_dimensions_returns_empty_list(self):
        idx = SparseIndex()
        idx.add("a", {0: 1.0})
        assert idx.search({}, limit=10) == []

    def test_search_normalizes_the_query_when_normalize_flag_is_set(self):
        idx = SparseIndex(normalize=True)
        idx.add("a", {0: 3.0, 1: 4.0})
        results = idx.search({0: 3.0, 1: 4.0}, limit=1)
        # The stored vector and the query both end up unit length, so their
        # dot product is exactly 1.0.
        assert results[0].score == pytest.approx(1.0)


class TestSparseIndexCosineSearch:
    """SparseIndex.search_cosine and its bug.

    The fixture below is shared by the correct-case test and the xfail bug
    test: doc "a" and "b" have a large dot product with the query but a
    large norm, so their cosine similarity is small; doc "c" has a tiny dot
    product but a tiny norm too, so its cosine similarity is the highest of
    the three.
    """

    def _corpus(self):
        idx = SparseIndex()
        idx.add("a", {0: 10.0, 1: 100.0})
        idx.add("b", {0: 9.0, 1: 100.0})
        idx.add("c", {0: 0.5})
        return idx

    def test_search_cosine_orders_by_cosine_when_the_corpus_fits_the_prefetch_window(self):
        idx = self._corpus()
        # limit=2 => internal dot-product prefetch is limit*2=4, which covers
        # the whole 3-document corpus, so the bug below does not trigger.
        results = idx.search_cosine({0: 1.0}, limit=2)
        assert [r.id for r in results] == ["c", "a"]

    def test_search_cosine_can_drop_the_true_top_hit(self):
        idx = self._corpus()
        # limit=1 => internal dot-product prefetch is limit*2=2, which only
        # covers "a" and "b" (the two largest dot products). "c" has the
        # true highest cosine similarity (1.0) but is dropped before cosine
        # is ever computed, so the wrong document wins.
        results = idx.search_cosine({0: 1.0}, limit=1)
        assert [r.id for r in results] == ["c"]

    def test_search_cosine_empty_query_norm_returns_empty_list(self):
        idx = self._corpus()
        assert idx.search_cosine({}, limit=5) == []

    def test_search_cosine_respects_doc_ids_filter(self):
        idx = self._corpus()
        results = idx.search_cosine({0: 1.0}, limit=10, doc_ids={"a", "b"})
        assert {r.id for r in results} == {"a", "b"}


class TestHybridSparseIndex:
    def test_hybrid_search_orders_by_sparse_rrf_score_when_dense_is_absent(self):
        sparse_idx = SparseIndex()
        sparse_idx.add("a", {0: 1.0})
        sparse_idx.add("b", {0: 0.5})
        hybrid = HybridSparseIndex(dense_index=None, sparse_index=sparse_idx)
        results = hybrid.search(sparse_query={0: 1.0}, limit=10, sparse_weight=1.0)
        assert [r[0] for r in results] == ["a", "b"]
        combined = {r[0]: r[1] for r in results}
        assert combined["a"] > combined["b"] > 0.0

    def test_hybrid_search_ignores_dense_query_because_dense_search_is_a_placeholder(self):
        """HybridSparseIndex._dense_search always returns [] (its own
        docstring calls it a placeholder), so a dense_query and a large
        dense_weight never change the ranking, no matter what the supplied
        dense_index could otherwise produce."""
        sparse_idx = SparseIndex()
        sparse_idx.add("a", {0: 1.0})
        sparse_idx.add("b", {0: 0.5})
        hybrid = HybridSparseIndex(dense_index=object(), sparse_index=sparse_idx)
        results = hybrid.search(
            dense_query=np.array([1.0, 0.0, 0.0], dtype=np.float32),
            sparse_query={0: 1.0},
            limit=10,
            dense_weight=0.9,
            sparse_weight=0.1,
        )
        assert [r[0] for r in results] == ["a", "b"]

    def test_hybrid_search_zero_weights_yield_zero_scores_for_all_candidates(self):
        sparse_idx = SparseIndex()
        sparse_idx.add("a", {0: 1.0})
        sparse_idx.add("b", {0: 0.5})
        hybrid = HybridSparseIndex(dense_index=None, sparse_index=sparse_idx)
        results = hybrid.search(
            sparse_query={0: 1.0}, limit=10, sparse_weight=0.0, dense_weight=0.0
        )
        assert {r[0] for r in results} == {"a", "b"}
        assert all(r[1] == 0.0 for r in results)

    def test_hybrid_search_a_literal_zero_sparse_score_gets_no_rrf_credit(self):
        """A document found by the sparse search with an exact dot product
        of 0.0 gets no rank credit at all: HybridSparseIndex.search only
        grants rrf credit when a score is strictly greater than zero."""
        sparse_idx = SparseIndex()
        sparse_idx.add("a", {0: 1.0})
        sparse_idx.add("b", {0: 0.0, 1: 5.0})
        hybrid = HybridSparseIndex(dense_index=None, sparse_index=sparse_idx)
        results = hybrid.search(
            sparse_query={0: 1.0}, limit=10, sparse_weight=1.0, dense_weight=0.0
        )
        combined = {r[0]: r[1] for r in results}
        assert combined["a"] > 0.0
        assert combined["b"] == 0.0

    def test_hybrid_search_empty_indexes_returns_empty_list(self):
        hybrid = HybridSparseIndex(dense_index=None, sparse_index=SparseIndex())
        assert hybrid.search(sparse_query={0: 1.0}, limit=10) == []

    def test_hybrid_search_blends_dense_and_sparse_scores_once_dense_search_is_implemented(self):
        """_dense_search's own docstring calls it an override point
        ("override for specific implementation"). Subclassing it here
        exercises the real rrf blending logic that the shipped placeholder
        can never reach."""

        class HybridWithFakeDense(HybridSparseIndex):
            def _dense_search(self, query, limit, filter_ids):
                return [("b", 0.9), ("a", 0.1)]

        sparse_idx = SparseIndex()
        sparse_idx.add("a", {0: 1.0})
        sparse_idx.add("b", {0: 0.5})
        hybrid = HybridWithFakeDense(dense_index=object(), sparse_index=sparse_idx)
        results = hybrid.search(
            dense_query=np.array([1.0, 0.0, 0.0], dtype=np.float32),
            sparse_query={0: 1.0},
            limit=10,
            dense_weight=1.0,
            sparse_weight=0.0,
        )
        # Dense now ranks "b" first, and with sparse_weight=0 that dense
        # ranking alone should decide the order.
        assert [r[0] for r in results] == ["b", "a"]

    def test_get_rank_returns_infinity_for_a_doc_id_absent_from_the_scores(self):
        hybrid = HybridSparseIndex(dense_index=None, sparse_index=SparseIndex())
        assert hybrid._get_rank("nope", {"other": 1.0}) == float("inf")


class TestSparseVectorHelper:
    """vectrixdb.core.search.sparse.SparseVector, a plain dataclass distinct
    from vectrixdb.core.types.SparseVector used by sparse_index.py."""

    def test_from_dict_sorts_by_index(self):
        sv = SparseVector.from_dict({5: 1.0, 1: 2.0, 3: 3.0})
        assert list(sv.indices) == [1, 3, 5]
        assert list(sv.values) == pytest.approx([2.0, 3.0, 1.0])

    def test_from_dict_empty_dict_gives_empty_arrays(self):
        sv = SparseVector.from_dict({})
        assert len(sv.indices) == 0
        assert len(sv.values) == 0

    def test_dot_product_only_sums_overlapping_indices(self):
        a = SparseVector.from_dict({0: 1.0, 2: 3.0})
        b = SparseVector.from_dict({0: 2.0, 1: 5.0, 2: 1.0})
        assert a.dot(b) == pytest.approx(1.0 * 2.0 + 3.0 * 1.0)

    def test_norm_is_euclidean_norm_of_values(self):
        sv = SparseVector.from_dict({0: 3.0, 1: 4.0})
        assert sv.norm() == pytest.approx(5.0)

    def test_to_dict_returns_index_value_mapping(self):
        sv = SparseVector.from_dict({0: 1.0, 2: 3.0})
        assert sv.to_dict() == {0: 1.0, 2: 3.0}

    def test_dot_product_advances_through_a_non_overlapping_prefix(self):
        # a has an index (0) that is smaller than anything left in b, which
        # exercises the "advance the left pointer" branch of the merge.
        a = SparseVector.from_dict({0: 1.0, 2: 2.0, 5: 3.0})
        b = SparseVector.from_dict({2: 10.0, 5: 10.0})
        assert a.dot(b) == pytest.approx(2.0 * 10.0 + 3.0 * 10.0)


class TestSparseSearch:
    def test_add_and_search_dot_metric_ranks_by_raw_dot_product(self):
        s = SparseSearch(dimension=10)
        s.add("a", SparseVector.from_dict({0: 10.0, 1: 100.0}))
        s.add("b", SparseVector.from_dict({0: 9.0, 1: 100.0}))
        s.add("c", SparseVector.from_dict({0: 0.5}))
        results = s.search(SparseVector.from_dict({0: 1.0}), k=10, metric="dot")
        assert [r.id for r in results] == ["a", "b", "c"]

    def test_search_cosine_metric_normalizes_over_the_full_candidate_set(self):
        """Contrast with SparseIndex.search_cosine in sparse_index.py: this
        sibling implementation normalizes every scored candidate before
        picking the top k, so it does not drop the true best match even
        when k is small."""
        s = SparseSearch(dimension=10)
        s.add("a", SparseVector.from_dict({0: 10.0, 1: 100.0}))
        s.add("b", SparseVector.from_dict({0: 9.0, 1: 100.0}))
        s.add("c", SparseVector.from_dict({0: 0.5}))
        query = SparseVector.from_dict({0: 1.0})
        assert [r.id for r in s.search(query, k=10, metric="cosine")] == ["c", "a", "b"]
        assert [r.id for r in s.search(query, k=1, metric="cosine")] == ["c"]

    def test_search_respects_k_limit(self):
        s = SparseSearch(dimension=5)
        for i, val in enumerate([5.0, 4.0, 3.0, 2.0, 1.0]):
            s.add(f"d{i}", SparseVector.from_dict({0: val}))
        results = s.search(SparseVector.from_dict({0: 1.0}), k=2)
        assert [r.id for r in results] == ["d0", "d1"]

    def test_matched_terms_counts_overlapping_query_terms(self):
        s = SparseSearch(dimension=5)
        s.add("a", SparseVector.from_dict({0: 1.0, 1: 1.0, 2: 1.0}))
        query = SparseVector.from_dict({0: 1.0, 1: 1.0, 3: 1.0})
        results = s.search(query, k=5)
        assert results[0].matched_terms == 2

    def test_remove_then_search_excludes_removed_doc(self):
        s = SparseSearch(dimension=5)
        s.add("a", SparseVector.from_dict({0: 1.0}))
        s.add("b", SparseVector.from_dict({0: 1.0}))
        assert s.remove("a") is True
        results = s.search(SparseVector.from_dict({0: 1.0}), k=5)
        assert [r.id for r in results] == ["b"]

    def test_remove_missing_document_returns_false(self):
        s = SparseSearch(dimension=5)
        assert s.remove("nope") is False

    def test_add_overwrites_existing_document(self):
        s = SparseSearch(dimension=5)
        s.add("a", SparseVector.from_dict({0: 1.0}))
        s.add("a", SparseVector.from_dict({1: 9.0}))
        assert s.search(SparseVector.from_dict({0: 1.0}), k=5) == []
        results = s.search(SparseVector.from_dict({1: 1.0}), k=5)
        assert [r.id for r in results] == ["a"]

    def test_search_empty_corpus_returns_empty_list(self):
        s = SparseSearch(dimension=5)
        assert s.search(SparseVector.from_dict({0: 1.0}), k=5) == []

    def test_get_stats_on_empty_search(self):
        s = SparseSearch(dimension=5)
        stats = s.get_stats()
        assert stats["num_documents"] == 0
        assert stats["num_posting_lists"] == 0
        assert stats["avg_posting_list_length"] == 0

    def test_get_stats_counts_documents_and_posting_lists(self):
        s = SparseSearch(dimension=5)
        s.add("a", SparseVector.from_dict({0: 1.0, 1: 1.0}))
        s.add("b", SparseVector.from_dict({1: 1.0}))
        stats = s.get_stats()
        assert stats["num_documents"] == 2
        assert stats["num_posting_lists"] == 2
        assert stats["avg_posting_list_length"] == pytest.approx((1 + 2) / 2)

    def test_add_batch_adds_every_item_and_returns_the_count(self):
        s = SparseSearch(dimension=5)
        added = s.add_batch(
            [("a", SparseVector.from_dict({0: 1.0})), ("b", SparseVector.from_dict({1: 1.0}))]
        )
        assert added == 2
        assert s.get_stats()["num_documents"] == 2

    def test_search_filter_ids_restricts_to_a_non_empty_subset(self):
        s = SparseSearch(dimension=5)
        s.add("a", SparseVector.from_dict({0: 1.0}))
        s.add("b", SparseVector.from_dict({0: 1.0}))
        results = s.search(SparseVector.from_dict({0: 1.0}), k=5, filter_ids={"a"})
        assert [r.id for r in results] == ["a"]

    def test_remove_should_clean_up_now_empty_posting_lists(self):
        s = SparseSearch(dimension=5)
        s.add("a", SparseVector.from_dict({0: 1.0, 1: 1.0}))
        s.remove("a")
        assert s.get_stats()["num_posting_lists"] == 0

    def test_search_empty_filter_ids_set_should_find_nothing(self):
        s = SparseSearch(dimension=5)
        s.add("a", SparseVector.from_dict({0: 1.0}))
        results = s.search(SparseVector.from_dict({0: 1.0}), k=5, filter_ids=set())
        assert results == []


class TestBM25Scorer:
    def test_idf_decreases_as_document_frequency_increases(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["common", "x1", "x2", "x3"])
        scorer.add_document("b", ["common", "x4", "x5", "x6"])
        scorer.add_document("c", ["common", "x7", "x8", "x9"])
        scorer.add_document("d", ["rare", "x10", "x11", "x12"])
        common_score = scorer.score(["common"], k=10)[0].score
        rare_score = scorer.score(["rare"], k=10)[0].score
        assert rare_score > common_score

    def test_k1_zero_makes_score_independent_of_term_frequency(self):
        scorer = BM25Scorer(k1=0.0, b=0.75)
        scorer.add_document("once", ["cat"])
        scorer.add_document("many", ["cat"] * 5)
        results = {r.id: r.score for r in scorer.score(["cat"], k=10)}
        assert results["once"] == pytest.approx(results["many"])

    def test_default_k1_rewards_extra_term_frequency(self):
        scorer = BM25Scorer(k1=1.5, b=0.75)
        scorer.add_document("once", ["cat"])
        scorer.add_document("many", ["cat"] * 5)
        results = {r.id: r.score for r in scorer.score(["cat"], k=10)}
        assert results["many"] > results["once"]

    def test_b_zero_makes_score_independent_of_document_length(self):
        scorer = BM25Scorer(k1=1.5, b=0.0)
        scorer.add_document("short", ["cat"])
        scorer.add_document("long", ["cat"] + ["filler"] * 9)
        results = {r.id: r.score for r in scorer.score(["cat"], k=10)}
        assert results["short"] == pytest.approx(results["long"])

    def test_higher_b_penalizes_longer_documents(self):
        scorer = BM25Scorer(k1=1.5, b=0.75)
        scorer.add_document("short", ["cat"])
        scorer.add_document("long", ["cat"] + ["filler"] * 9)
        results = {r.id: r.score for r in scorer.score(["cat"], k=10)}
        assert results["short"] > results["long"]

    def test_remove_document_updates_avg_length_and_df(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["cat", "dog"])
        scorer.add_document("b", ["cat"])
        assert scorer.remove_document("a") is True
        assert scorer.get_stats()["total_documents"] == 1
        assert scorer.get_stats()["avg_document_length"] == pytest.approx(1.0)
        assert scorer.score(["dog"], k=10) == []

    def test_remove_document_missing_returns_false(self):
        scorer = BM25Scorer()
        assert scorer.remove_document("nope") is False

    def test_avg_document_length_resets_to_zero_once_the_last_document_is_removed(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["cat", "dog"])
        scorer.remove_document("a")
        assert scorer.get_stats()["avg_document_length"] == 0.0

    def test_score_skips_filter_ids_not_present_in_the_corpus(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["cat"])
        results = scorer.score(["cat"], k=10, filter_ids={"a", "nonexistent"})
        assert [r.id for r in results] == ["a"]

    def test_score_on_empty_corpus_returns_empty_list(self):
        scorer = BM25Scorer()
        assert scorer.score(["anything"], k=10) == []

    def test_score_single_document_corpus(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["cat", "sat", "mat"])
        results = scorer.score(["cat"], k=10)
        assert [r.id for r in results] == ["a"]

    def test_score_ignores_query_terms_absent_from_the_corpus(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["cat"])
        results = scorer.score(["cat", "nonexistent"], k=10)
        assert [r.id for r in results] == ["a"]

    def test_get_stats_vocabulary_and_avg_length(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["cat", "dog"])
        scorer.add_document("b", ["cat", "bird", "fish"])
        stats = scorer.get_stats()
        assert stats["total_documents"] == 2
        assert stats["vocabulary_size"] == 4
        assert stats["avg_document_length"] == pytest.approx(2.5)

    def test_re_adding_same_document_id_replaces_rather_than_duplicates(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["x", "y"])
        scorer.add_document("b", ["x"])
        scorer.add_document("a", ["z"])
        assert scorer.get_stats()["total_documents"] == 2
        assert scorer.score(["y"], k=10) == []
        assert [r.id for r in scorer.score(["z"], k=10)] == ["a"]

    def test_score_empty_filter_ids_set_should_find_nothing(self):
        scorer = BM25Scorer()
        scorer.add_document("a", ["cat"])
        results = scorer.score(["cat"], k=10, filter_ids=set())
        assert results == []


class TestQueryExpander:
    def test_expand_always_includes_original_token_with_weight_one(self):
        expander = QueryExpander()
        assert expander.expand(["quick"])[0] == ("quick", 1.0)

    def test_expand_adds_synonyms_with_lower_weight(self):
        expander = QueryExpander()
        expander.add_synonyms("quick", ["fast", "rapid"])
        expanded = expander.expand(["quick"])
        assert expanded == [("quick", 1.0), ("fast", 0.7), ("rapid", 0.7)]

    def test_expand_respects_max_expansions_per_term_across_synonyms_and_learned(self):
        expander = QueryExpander()
        expander.add_synonyms("quick", ["fast", "rapid", "swift", "speedy"])
        expander.add_learned_expansion("quick", "brisk", weight=0.4)
        expanded = expander.expand(["quick"], max_expansions_per_term=3)
        # 3 synonyms already fill the cap, leaving no room for "brisk".
        assert expanded == [("quick", 1.0), ("fast", 0.7), ("rapid", 0.7), ("swift", 0.7)]

    def test_learned_expansions_fill_remaining_slots_after_synonyms(self):
        expander = QueryExpander()
        expander.add_synonyms("quick", ["fast"])
        expander.add_learned_expansion("quick", "brisk", weight=0.4)
        expander.add_learned_expansion("quick", "snappy", weight=0.3)
        expanded = expander.expand(["quick"], max_expansions_per_term=3)
        assert expanded == [
            ("quick", 1.0),
            ("fast", 0.7),
            ("brisk", 0.4),
            ("snappy", 0.3),
        ]

    def test_expand_can_disable_synonyms_or_learned_independently(self):
        expander = QueryExpander()
        expander.add_synonyms("quick", ["fast"])
        expander.add_learned_expansion("quick", "brisk", weight=0.4)
        assert expander.expand(["quick"], use_synonyms=False) == [("quick", 1.0), ("brisk", 0.4)]
        assert expander.expand(["quick"], use_learned=False) == [("quick", 1.0), ("fast", 0.7)]

    def test_expand_to_tokens_returns_flat_list_without_weights(self):
        expander = QueryExpander()
        expander.add_synonyms("quick", ["fast"])
        assert expander.expand_to_tokens(["quick"]) == ["quick", "fast"]

    def test_add_synonyms_is_case_insensitive_and_deduplicates(self):
        expander = QueryExpander()
        expander.add_synonyms("Quick", ["Fast"])
        expander.add_synonyms("quick", ["fast", "FAST", "Rapid"])
        expanded = expander.expand(["quick"])
        assert expanded == [("quick", 1.0), ("fast", 0.7), ("rapid", 0.7)]

    def test_expand_lowercases_query_tokens_before_lookup(self):
        expander = QueryExpander()
        expander.add_synonyms("quick", ["fast"])
        assert expander.expand(["QUICK"]) == [("quick", 1.0), ("fast", 0.7)]


class TestDenseSearchConfig:
    def test_defaults(self):
        config = DenseSearchConfig()
        assert config.metric == "cosine"
        assert config.ef_search == 100
        assert config.use_quantization is False
        assert config.rescore_multiplier == 4


class TestDenseSearchCore:
    """Tests that use a plain object() as the index, so DenseSearch always
    falls back to its own brute force computation. This exercises
    _compute_distances, _distance_to_score and _brute_force_search directly
    for every metric."""

    VECTORS = np.array([[1.0, 0.0, 0.0], [0.5, 0.5, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    IDS = ["A", "C", "B"]
    QUERY = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    @pytest.mark.parametrize(
        "metric, expected_scores",
        [
            ("cosine", [1.0, 0.70710678, 0.0]),
            ("euclidean", [1.0, 0.58578644, 0.41421356]),
            ("dot", [1.0, 0.5, 0.0]),
        ],
    )
    def test_search_ranks_consistently_across_metrics(self, metric, expected_scores):
        ds = DenseSearch(
            index=object(),
            vectors=self.VECTORS,
            ids=self.IDS,
            config=DenseSearchConfig(metric=metric),
        )
        results = ds.search(self.QUERY, k=3)
        assert [r.id for r in results] == ["A", "C", "B"]
        assert [r.score for r in results] == pytest.approx(expected_scores, abs=1e-4)
        assert isinstance(results[0], SearchResult)

    def test_unknown_metric_raises_value_error(self):
        ds = DenseSearch(
            index=object(),
            vectors=self.VECTORS,
            ids=self.IDS,
            config=DenseSearchConfig(metric="bogus"),
        )
        with pytest.raises(ValueError):
            ds.search(self.QUERY, k=1)

    def test_search_populates_the_stored_vector_on_each_result(self):
        ds = DenseSearch(index=object(), vectors=self.VECTORS, ids=self.IDS)
        results = ds.search(self.QUERY, k=1)
        assert np.array_equal(results[0].vector, self.VECTORS[0])

    def test_filter_ids_restricts_to_subset_and_preserves_ranking(self):
        ds = DenseSearch(index=object(), vectors=self.VECTORS, ids=self.IDS)
        results = ds.search(self.QUERY, k=10, filter_ids=["B", "C"])
        assert [r.id for r in results] == ["C", "B"]

    def test_filter_ids_with_unknown_id_is_ignored(self):
        vectors = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        ds = DenseSearch(index=object(), vectors=vectors, ids=["A", "B"])
        results = ds.search(self.QUERY, k=10, filter_ids=["A", "nope"])
        assert [r.id for r in results] == ["A"]

    def test_filter_ids_all_unknown_returns_empty_list(self):
        vectors = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        ds = DenseSearch(index=object(), vectors=vectors, ids=["A", "B"])
        assert ds.search(self.QUERY, k=10, filter_ids=["nope"]) == []

    def test_ids_default_to_string_indices_when_not_given(self):
        ds = DenseSearch(index=object(), vectors=self.VECTORS)
        results = ds.search(self.QUERY, k=1)
        assert results[0].id == "0"

    def test_distance_to_score_default_fallback_for_an_unrecognised_metric(self):
        """Not reachable through search(): _compute_distances already
        raises for an unknown metric before _distance_to_score would ever
        run. Calling it directly covers the defensive final-line
        fallback (dense.py:292)."""
        ds = DenseSearch(index=object(), vectors=self.VECTORS, ids=self.IDS)
        ds.config.metric = "bogus"
        assert ds._distance_to_score(1.0) == pytest.approx(0.5)

    def test_negative_k_should_not_silently_return_almost_all_vectors(self):
        vectors = np.array(
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 1.0, 0.0]], dtype=np.float32
        )
        ds = DenseSearch(index=object(), vectors=vectors, ids=["a", "b", "c", "d"])
        results = ds.search(self.QUERY, k=-1)
        assert results == []


class TestDenseSearchWithRealIndex:
    """Tests that use FakeANNIndex, so DenseSearch delegates to the index's
    own search method instead of falling back to brute force."""

    VECTORS = np.array([[1.0, 0.0, 0.0], [0.5, 0.5, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    IDS = ["A", "C", "B"]
    QUERY = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    def test_search_delegates_to_the_index_search_method(self):
        ds = DenseSearch(
            index=FakeANNIndex(self.VECTORS),
            vectors=self.VECTORS,
            ids=self.IDS,
            config=DenseSearchConfig(metric="euclidean"),
        )
        results = ds.search(self.QUERY, k=2)
        assert [r.id for r in results] == ["A", "C"]
        assert results[0].score == pytest.approx(1.0)
        assert results[1].score == pytest.approx(0.58578644, abs=1e-4)

    def test_search_batch_delegates_when_index_supports_batch_search(self):
        ds = DenseSearch(
            index=FakeANNIndex(self.VECTORS),
            vectors=self.VECTORS,
            ids=self.IDS,
            config=DenseSearchConfig(metric="euclidean"),
        )
        queries = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        batch_results = ds.search_batch(queries, k=2)
        assert len(batch_results) == 2
        assert [r.id for r in batch_results[0]] == ["A", "C"]
        assert [r.id for r in batch_results[1]] == ["B", "C"]
        assert batch_results[0][0].vector is None

    def test_search_skips_invalid_negative_indices_from_index(self):
        vectors = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        ds = DenseSearch(
            index=FakeANNIndexWithGaps(vectors),
            vectors=vectors,
            ids=["A", "B"],
            config=DenseSearchConfig(metric="euclidean"),
        )
        results = ds.search(self.QUERY, k=5)
        assert [r.id for r in results] == ["A", "B"]

    def test_search_batch_skips_invalid_negative_indices_from_index(self):
        vectors = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        ds = DenseSearch(
            index=FakeANNIndexWithGaps(vectors),
            vectors=vectors,
            ids=["A", "B"],
            config=DenseSearchConfig(metric="euclidean"),
        )
        batch_results = ds.search_batch(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), k=5)
        assert [r.id for r in batch_results[0]] == ["A", "B"]


class TestSearchWithNegatives:
    def test_empty_negatives_behaves_like_plain_search(self):
        vectors = np.array([[1.0, 0.0, 0.0], [0.5, 0.5, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        ds = DenseSearch(index=object(), vectors=vectors, ids=["A", "C", "B"])
        plain = ds.search(np.array([1.0, 0.0, 0.0], dtype=np.float32), k=3)
        with_empty_negatives = ds.search_with_negatives(
            positive=np.array([1.0, 0.0, 0.0], dtype=np.float32), negative=[], k=3
        )
        assert [r.id for r in with_empty_negatives] == [r.id for r in plain]

    def test_negative_equal_to_the_query_collapses_it_to_a_zero_vector(self):
        """positive - 1.0 * negative_centroid becomes the zero vector here,
        and the code only renormalizes when the norm is greater than zero,
        so every candidate ties at a cosine similarity of 0."""
        vectors = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        ds = DenseSearch(index=object(), vectors=vectors, ids=["A", "B"])
        results = ds.search_with_negatives(
            positive=np.array([1.0, 0.0, 0.0], dtype=np.float32),
            negative=[np.array([1.0, 0.0, 0.0], dtype=np.float32)],
            negative_weight=1.0,
            k=2,
        )
        assert {r.id for r in results} == {"A", "B"}
        for r in results:
            assert r.score == pytest.approx(0.0, abs=1e-6)

    def test_negative_orthogonal_to_the_query_shifts_the_ranking(self):
        vectors = np.array([[1.0, 0.0, 0.0], [0.5, 0.5, 0.0]], dtype=np.float32)
        ds = DenseSearch(index=object(), vectors=vectors, ids=["A", "C"])
        results = ds.search_with_negatives(
            positive=np.array([1.0, 0.0, 0.0], dtype=np.float32),
            negative=[np.array([0.0, 1.0, 0.0], dtype=np.float32)],
            negative_weight=1.0,
            k=2,
        )
        # This negative does not cancel the query, so the adjusted query is
        # renormalized (dense.py:170) instead of being left as a zero vector.
        assert results[0].id == "A"

    def test_search_batch_sequential_fallback_matches_individual_searches(self):
        vectors = np.array([[1.0, 0.0, 0.0], [0.5, 0.5, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        ds = DenseSearch(index=object(), vectors=vectors, ids=["A", "C", "B"])
        queries = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        batch_results = ds.search_batch(queries, k=2)
        assert [r.id for r in batch_results[0]] == [r.id for r in ds.search(queries[0], k=2)]
        assert [r.id for r in batch_results[1]] == [r.id for r in ds.search(queries[1], k=2)]


class TestMultiQuerySearch:
    def _dense_search(self):
        vectors = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)
        return DenseSearch(index=object(), vectors=vectors, ids=["x", "y", "z"])

    def test_max_strategy_takes_the_best_score_across_queries(self):
        mq = MultiQuerySearch(self._dense_search())
        queries = [
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            np.array([0.0, 0.0, 1.0], dtype=np.float32),
        ]
        results = {r.id: r.score for r in mq.search(queries, k=3, strategy="max")}
        assert results["x"] == pytest.approx(1.0)
        assert results["z"] == pytest.approx(1.0)

    def test_mean_strategy_averages_while_max_strategy_takes_the_best(self):
        mq = MultiQuerySearch(self._dense_search())
        queries = [
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            np.array([0.0, 1.0, 0.0], dtype=np.float32),
        ]
        mean_scores = {r.id: r.score for r in mq.search(queries, k=3, strategy="mean")}
        max_scores = {r.id: r.score for r in mq.search(queries, k=3, strategy="max")}
        assert mean_scores["x"] == pytest.approx(0.5)
        assert max_scores["x"] == pytest.approx(1.0)

    def test_weighted_strategy_applies_per_query_weights(self):
        mq = MultiQuerySearch(self._dense_search())
        queries = [
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            np.array([0.0, 1.0, 0.0], dtype=np.float32),
        ]
        results = {
            r.id: r.score for r in mq.search(queries, k=3, strategy="weighted", weights=[3.0, 1.0])
        }
        # x is favoured by the heavily weighted first query, y only by the
        # lightly weighted second query, so x should come out ahead.
        assert results["x"] > results["y"]

    def test_empty_queries_list_returns_empty_list(self):
        mq = MultiQuerySearch(self._dense_search())
        assert mq.search([], k=3) == []

    def test_unknown_strategy_falls_back_to_max(self):
        mq = MultiQuerySearch(self._dense_search())
        results = {
            r.id: r.score
            for r in mq.search([np.array([1.0, 0.0, 0.0], dtype=np.float32)], k=3, strategy="bogus")
        }
        assert results["x"] == pytest.approx(1.0)

    def test_weights_shorter_than_queries_should_raise_a_clear_error(self):
        mq = MultiQuerySearch(self._dense_search())
        queries = [
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            np.array([0.0, 1.0, 0.0], dtype=np.float32),
        ]
        with pytest.raises(ValueError):
            mq.search(queries, k=1, strategy="weighted", weights=[1.0])


class TestPrefetchRescore:
    """Fixture geometry: A is farther from the query in raw Euclidean terms
    but perfectly aligned with it (cosine 1.0); E is closer in Euclidean
    terms but slightly off axis (cosine below 1.0); F and G are filler, far
    on every metric. This lets the approximate (Euclidean) prefetch stage
    and the exact (cosine) rescore stage disagree on the winner."""

    VECTORS = np.array(
        [
            [2.0, 0.0, 0.0],
            [1.0, 0.2, 0.0],
            [0.0, 5.0, 0.0],
            [0.0, 0.0, 5.0],
        ],
        dtype=np.float32,
    )
    IDS = ["A", "E", "F", "G"]
    QUERY = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    def test_search_uses_brute_force_when_index_has_no_search_method(self):
        pr = PrefetchRescore(index=object(), vectors=self.VECTORS, ids=self.IDS, metric="cosine")
        results = pr.search(self.QUERY, k=1)
        assert results[0].id == "A"

    def test_brute_force_prefetch_and_exact_score_cover_the_euclidean_metric(self):
        pr = PrefetchRescore(index=object(), vectors=self.VECTORS, ids=self.IDS, metric="euclidean")
        results = pr.search(self.QUERY, k=1)
        assert results[0].id == "E"  # closest in raw Euclidean distance

    def test_brute_force_prefetch_and_exact_score_cover_the_dot_metric(self):
        pr = PrefetchRescore(index=object(), vectors=self.VECTORS, ids=self.IDS, metric="dot")
        results = pr.search(self.QUERY, k=1)
        assert results[0].id == "A"  # highest raw dot product

    def test_rescore_can_reorder_relative_to_the_approximate_prefetch_order(self):
        pr = PrefetchRescore(
            index=FakeANNIndex(self.VECTORS), vectors=self.VECTORS, ids=self.IDS, metric="cosine"
        )
        results = pr.search(self.QUERY, k=2, prefetch_k=2)
        assert [r.id for r in results] == ["A", "E"]
        assert results[0].score == pytest.approx(1.0, abs=1e-4)
        assert results[1].score == pytest.approx(0.98058, abs=1e-4)

    def test_custom_rescore_fn_overrides_default_metric_scoring(self):
        pr = PrefetchRescore(
            index=FakeANNIndex(self.VECTORS), vectors=self.VECTORS, ids=self.IDS, metric="cosine"
        )
        results = pr.search(
            self.QUERY,
            k=2,
            prefetch_k=2,
            rescore_fn=lambda q, v: -float(np.linalg.norm(q - v)),
        )
        # A Euclidean-based rescore_fn instead of the configured cosine
        # metric hands the top spot back to E, which is closer in raw
        # distance.
        assert [r.id for r in results] == ["E", "A"]

    def test_small_prefetch_k_can_exclude_the_true_best_match(self):
        """Not a bug: prefetch_k is a caller-controlled trade-off between
        speed and recall, and the docstring never promises exactness for an
        arbitrarily small value. A bigger prefetch_k (see the default
        multiplier test below) finds the true best match."""
        pr = PrefetchRescore(
            index=FakeANNIndex(self.VECTORS), vectors=self.VECTORS, ids=self.IDS, metric="cosine"
        )
        results = pr.search(self.QUERY, k=1, prefetch_k=1)
        assert [r.id for r in results] == ["E"]

    def test_prefetch_k_defaults_to_four_times_k(self):
        pr = PrefetchRescore(
            index=FakeANNIndex(self.VECTORS), vectors=self.VECTORS, ids=self.IDS, metric="cosine"
        )
        results = pr.search(self.QUERY, k=1)  # prefetch_k defaults to 1 * 4 = 4, the whole corpus
        assert [r.id for r in results] == ["A"]

    def test_returns_empty_list_when_no_candidates_are_valid(self):
        class AllInvalidIndex:
            def search(self, queries, k=10):
                n = len(np.atleast_2d(queries))
                return np.full((n, k), -1), np.zeros((n, k))

        pr = PrefetchRescore(index=AllInvalidIndex(), vectors=self.VECTORS, ids=self.IDS)
        assert pr.search(self.QUERY, k=2) == []


class TestTokenEmbeddings:
    def test_n_tokens_and_dimension_properties(self):
        emb = tok_emb([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])
        assert emb.n_tokens == 3
        assert emb.dimension == 2

    def test_normalize_returns_unit_norm_rows(self):
        emb = tok_emb([[3.0, 4.0], [1.0, 0.0]])
        normalized = emb.normalize()
        norms = np.linalg.norm(normalized.embeddings, axis=1)
        assert norms == pytest.approx([1.0, 1.0], abs=1e-4)

    def test_dimension_is_zero_for_a_flat_empty_array(self):
        emb = TokenEmbeddings(embeddings=np.array([], dtype=np.float32))
        assert emb.dimension == 0


class TestMaxSimScorer:
    def test_score_sums_max_similarity_per_query_token(self):
        query = tok_emb([[1.0, 0.0], [0.0, 1.0]])
        doc = tok_emb([[1.0, 0.0], [0.70710678, 0.70710678], [0.0, 1.0]])
        scorer = MaxSimScorer(normalize=True)
        score, token_scores = scorer.score(query, doc, return_token_scores=True)
        assert token_scores == pytest.approx([1.0, 1.0], abs=1e-4)
        assert score == pytest.approx(2.0, abs=1e-4)

    def test_score_without_normalize_uses_raw_dot_product(self):
        query = tok_emb([[2.0, 0.0]])
        doc = tok_emb([[3.0, 0.0]])
        raw_score, _ = MaxSimScorer(normalize=False).score(query, doc)
        assert raw_score == pytest.approx(6.0)
        cosine_score, _ = MaxSimScorer(normalize=True).score(query, doc)
        assert cosine_score == pytest.approx(1.0, abs=1e-3)

    def test_document_mask_excludes_padding_tokens_from_maxsim(self):
        query = tok_emb([[1.0, 0.0], [0.0, 1.0]])
        doc = tok_emb([[0.0, 1.0], [0.70710678, 0.70710678], [1.0, 0.0]], mask=[1, 1, 0])
        masked_score, _ = MaxSimScorer(normalize=True, use_mask=True).score(query, doc)
        assert masked_score == pytest.approx(1.70710678, abs=1e-4)
        unmasked_score, _ = MaxSimScorer(normalize=True, use_mask=False).score(query, doc)
        assert unmasked_score == pytest.approx(2.0, abs=1e-4)

    def test_query_mask_zeroes_out_padded_query_token_scores(self):
        query = tok_emb([[1.0, 0.0], [0.0, 1.0]], mask=[1, 0])
        doc = tok_emb([[1.0, 0.0], [0.0, 1.0]])
        score, token_scores = MaxSimScorer(normalize=True, use_mask=True).score(
            query, doc, return_token_scores=True
        )
        assert token_scores == pytest.approx([1.0, 0.0], abs=1e-4)
        assert score == pytest.approx(1.0, abs=1e-4)

    def test_score_batch_matches_individual_scores(self):
        query = tok_emb([[1.0, 0.0]])
        doc1 = tok_emb([[1.0, 0.0]])
        doc2 = tok_emb([[0.0, 1.0]])
        scorer = MaxSimScorer(normalize=True)
        batch_scores = scorer.score_batch(query, [doc1, doc2])
        assert batch_scores == pytest.approx(
            [scorer.score(query, doc1)[0], scorer.score(query, doc2)[0]]
        )

    def test_get_token_matches_filters_by_threshold_and_sorts_descending(self):
        query = tok_emb([[1.0, 0.0], [0.0, 1.0]])
        doc = tok_emb([[1.0, 0.0], [0.70710678, 0.70710678], [0.0, 1.0]])
        scorer = MaxSimScorer(normalize=True)
        matches = scorer.get_token_matches(query, doc, threshold=0.5)
        pairs = [(m[0], m[1]) for m in matches]
        assert pairs == [(0, 0), (1, 2), (0, 1), (1, 1)]
        sims = [m[2] for m in matches]
        assert sims == pytest.approx([1.0, 1.0, 0.70710678, 0.70710678], abs=1e-4)

    def test_fully_masked_document_yields_negative_infinity_score(self):
        query = tok_emb([[1.0, 0.0]])
        doc = tok_emb([[1.0, 0.0], [0.0, 1.0]], mask=[0, 0])
        score, _ = MaxSimScorer(normalize=True, use_mask=True).score(query, doc)
        assert score == float("-inf")

    def test_score_on_zero_token_document_should_not_crash(self):
        query = tok_emb([[1.0, 0.0, 0.0]])
        empty_doc = TokenEmbeddings(embeddings=np.zeros((0, 3), dtype=np.float32))
        score, _ = MaxSimScorer().score(query, empty_doc)
        assert score == 0.0


class TestColBERTSearch:
    def _ranked_corpus(self):
        cb = ColBERTSearch(dimension=2, normalize=False)
        cb.add("mid", tok_emb([[0.5, 0.0]]))
        cb.add("low", tok_emb([[0.0, 1.0]]))
        cb.add("high", tok_emb([[1.0, 0.0]]))
        return cb

    def test_add_rejects_dimension_mismatch(self):
        cb = ColBERTSearch(dimension=3)
        with pytest.raises(ValueError):
            cb.add("a", tok_emb([[1.0, 0.0]]))

    def test_add_batch_returns_number_added(self):
        cb = ColBERTSearch(dimension=2)
        added = cb.add_batch([("a", tok_emb([[1.0, 0.0]])), ("b", tok_emb([[0.0, 1.0]]))])
        assert added == 2

    def test_remove_existing_and_missing_document(self):
        cb = ColBERTSearch(dimension=2)
        cb.add("a", tok_emb([[1.0, 0.0]]))
        assert cb.remove("a") is True
        assert cb.remove("a") is False
        assert cb.remove("nope") is False

    def test_search_ranks_by_maxsim_descending(self):
        cb = self._ranked_corpus()
        query = tok_emb([[1.0, 0.0]])
        results = cb.search(query, k=10)
        assert [r.id for r in results] == ["high", "mid", "low"]
        assert results[0].score == pytest.approx(1.0)
        assert results[1].score == pytest.approx(0.5)
        assert results[2].score == pytest.approx(0.0)

    def test_search_respects_k_limit(self):
        cb = self._ranked_corpus()
        results = cb.search(tok_emb([[1.0, 0.0]]), k=2)
        assert [r.id for r in results] == ["high", "mid"]

    def test_search_empty_corpus_returns_empty_list(self):
        cb = ColBERTSearch(dimension=2)
        assert cb.search(tok_emb([[1.0, 0.0]]), k=5) == []

    def test_search_single_document_corpus(self):
        cb = ColBERTSearch(dimension=2, normalize=False)
        cb.add("only", tok_emb([[1.0, 0.0]]))
        results = cb.search(tok_emb([[1.0, 0.0]]), k=5)
        assert [r.id for r in results] == ["only"]

    def test_search_filter_ids_restricts_candidates(self):
        cb = self._ranked_corpus()
        results = cb.search(tok_emb([[1.0, 0.0]]), k=10, filter_ids=["mid", "low"])
        assert [r.id for r in results] == ["mid", "low"]

    def test_search_filter_ids_with_unknown_id_is_ignored(self):
        cb = self._ranked_corpus()
        results = cb.search(tok_emb([[1.0, 0.0]]), k=10, filter_ids=["high", "nonexistent"])
        assert [r.id for r in results] == ["high"]

    def test_search_empty_filter_ids_list_should_find_nothing(self):
        cb = self._ranked_corpus()
        results = cb.search(tok_emb([[1.0, 0.0]]), k=10, filter_ids=[])
        assert results == []

    def test_search_with_matches_populates_matched_tokens_above_threshold(self):
        cb = ColBERTSearch(dimension=2, normalize=True)
        cb.add("a", tok_emb([[1.0, 0.0], [0.70710678, 0.70710678], [0.0, 1.0]]))
        query = tok_emb([[1.0, 0.0], [0.0, 1.0]])
        results = cb.search_with_matches(query, k=1, match_threshold=0.5)
        pairs = [(m[0], m[1]) for m in results[0].matched_tokens]
        assert pairs == [(0, 0), (1, 2), (0, 1), (1, 1)]

    def test_compute_centroids_are_unit_normalized_means_and_cached(self):
        cb = ColBERTSearch(dimension=2, normalize=True)
        cb.add("x", tok_emb([[1.0, 0.0], [0.0, 1.0]]))
        cb.add("y", tok_emb([[3.0, 0.0]]))
        centroids = cb.compute_centroids()
        assert centroids.shape == (2, 2)
        assert centroids[0] == pytest.approx([0.70710678, 0.70710678], abs=1e-4)
        assert centroids[1] == pytest.approx([1.0, 0.0], abs=1e-4)
        assert cb.compute_centroids() is centroids
        cb.add("z", tok_emb([[0.0, 5.0]]))
        new_centroids = cb.compute_centroids()
        assert new_centroids is not centroids
        assert new_centroids.shape == (3, 2)

    def test_search_with_prefetch_bypasses_prefetch_for_small_collections(self):
        cb = ColBERTSearch(dimension=2, normalize=True)
        cb.add("d0", tok_emb([[1.0, 0.0]]))
        cb.add("d90", tok_emb([[0.0, 1.0]]))
        query = tok_emb([[1.0, 0.0]])
        assert [r.id for r in cb.search_with_prefetch(query, k=2, prefetch_k=100)] == [
            r.id for r in cb.search(query, k=2)
        ]

    def test_search_with_prefetch_narrows_via_centroid_similarity(self):
        """With one token per document, a centroid is just that token, so
        centroid-based prefetch ranking matches exact MaxSim ranking here
        and the prefetched top k equals the true top k."""
        cb = ColBERTSearch(dimension=2, normalize=True)
        cb.add("d0", tok_emb([[1.0, 0.0]]))
        cb.add("d30", tok_emb([[0.8660254, 0.5]]))
        cb.add("d60", tok_emb([[0.5, 0.8660254]]))
        cb.add("d90", tok_emb([[0.0, 1.0]]))
        cb.add("d180", tok_emb([[-1.0, 0.0]]))
        query = tok_emb([[1.0, 0.0]])
        prefetched = cb.search_with_prefetch(query, k=2, prefetch_k=2)
        assert [r.id for r in prefetched] == ["d0", "d30"]
        assert [r.id for r in prefetched] == [r.id for r in cb.search(query, k=2)]

    def test_get_stats_reports_totals_and_average_tokens(self):
        cb = ColBERTSearch(dimension=2, normalize=False)
        cb.add("a", tok_emb([[1.0, 0.0], [0.0, 1.0]]))
        cb.add("b", tok_emb([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]))
        stats = cb.get_stats()
        assert stats["num_documents"] == 2
        assert stats["total_tokens"] == 5
        assert stats["avg_tokens_per_doc"] == pytest.approx(2.5)
        assert stats["dimension"] == 2


class TestColBERTEncoder:
    """It never encoded anything: token embeddings came from a random
    generator seeded by the hash of each token, which is deterministic and
    meaningless. These tests used to pin that behaviour, which is how it read
    as a working component. Constructing it raises now."""

    def test_constructing_it_raises_and_names_the_replacement(self):
        with pytest.raises(NotImplementedError, match="LateInteractionEmbedder"):
            ColBERTEncoder(dimension=8)
