"""Pure tests for vectrixdb.core.search.fusion.

Everything here runs on hand-built (id, score) lists and tiny numpy arrays.
No models, no network, no database.
"""

import numpy as np
import pytest

from vectrixdb.core.search.fusion import (
    CondorcetFusion,
    FusedResult,
    FusionStrategy,
    HybridSearcher,
    LinearFusion,
    Reranker,
    RRFFusion,
)

# Two sources that disagree about the top document. "a" leads dense, "b" leads sparse,
# and "c" is only known to one of them.
DENSE = [("a", 0.9), ("b", 0.8), ("c", 0.1)]
SPARSE = [("b", 5.5), ("a", 4.2)]
LISTS = {"dense": DENSE, "sparse": SPARSE}


def ids(results):
    return [r.id for r in results]


# ---------------------------------------------------------------------------
# FusedResult and the abstract base
# ---------------------------------------------------------------------------


class TestFusedResult:
    def test_defaults_are_empty_and_independent(self):
        one = FusedResult(id="x", score=1.0, rank=1)
        two = FusedResult(id="y", score=0.5, rank=2)
        assert one.source_scores == {} and one.source_ranks == {} and one.payload is None
        one.source_scores["dense"] = 1.0
        assert two.source_scores == {}, "default dicts must not be shared between instances"

    def test_base_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            FusionStrategy()

    def test_subclass_must_implement_fuse(self):
        class Half(FusionStrategy):
            pass

        with pytest.raises(TypeError):
            Half()


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------


class TestRRFFusion:
    def test_empty_input_gives_empty_output(self):
        assert RRFFusion().fuse({}) == []
        assert RRFFusion().fuse({"dense": [], "sparse": []}) == []

    def test_scores_follow_the_rrf_formula(self):
        rrf = RRFFusion(k=60)
        results = rrf.fuse({"dense": [("a", 0.9), ("b", 0.8)]})
        assert results[0].id == "a"
        assert results[0].score == pytest.approx(1 / 61)
        assert results[1].score == pytest.approx(1 / 62)

    def test_document_in_both_sources_beats_a_single_source_document(self):
        results = RRFFusion().fuse(LISTS)
        # a and b each appear in both lists with ranks {1, 2}, so they tie and both
        # beat c, which only appears once at rank 3.
        assert set(ids(results[:2])) == {"a", "b"}
        assert results[2].id == "c"
        assert results[0].score == pytest.approx(results[1].score)

    def test_ranks_are_sequential_from_one(self):
        results = RRFFusion().fuse(LISTS)
        assert [r.rank for r in results] == [1, 2, 3]

    def test_k_limits_the_number_of_results(self):
        results = RRFFusion().fuse(LISTS, k=2)
        assert len(results) == 2
        assert [r.rank for r in results] == [1, 2]

    def test_source_bookkeeping_uses_sentinels_for_missing_sources(self):
        results = {r.id: r for r in RRFFusion().fuse(LISTS)}
        c = results["c"]
        assert c.source_scores == {"dense": 0.1, "sparse": 0.0}
        assert c.source_ranks == {"dense": 3, "sparse": -1}
        b = results["b"]
        assert b.source_scores == {"dense": 0.8, "sparse": 5.5}
        assert b.source_ranks == {"dense": 2, "sparse": 1}

    def test_larger_k_constant_flattens_the_gap_between_ranks(self):
        one = RRFFusion(k=1).fuse({"dense": [("a", 1.0), ("b", 0.5)]})
        big = RRFFusion(k=1000).fuse({"dense": [("a", 1.0), ("b", 0.5)]})
        gap_small_k = one[0].score - one[1].score
        gap_big_k = big[0].score - big[1].score
        assert gap_small_k > gap_big_k

    def test_raw_scores_do_not_matter_only_rank_does(self):
        by_rank = RRFFusion().fuse({"s": [("a", 1.0), ("b", 0.5)]})
        wild = RRFFusion().fuse({"s": [("a", 1e-9), ("b", -500.0)]})
        assert [(r.id, r.score) for r in by_rank] == [(r.id, r.score) for r in wild]


# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------


class TestLinearFusion:
    def test_empty_input_gives_empty_output(self):
        assert LinearFusion().fuse({}) == []

    def test_empty_source_is_skipped_not_counted(self):
        results = LinearFusion().fuse({"dense": DENSE, "sparse": []})
        assert ids(results) == ["a", "b", "c"]
        assert "sparse" not in results[0].source_scores

    def test_minmax_maps_each_source_onto_zero_one(self):
        results = LinearFusion(normalize="minmax").fuse({"dense": DENSE})
        by_id = {r.id: r for r in results}
        assert by_id["a"].source_scores["dense"] == pytest.approx(1.0)
        assert by_id["c"].source_scores["dense"] == pytest.approx(0.0)
        assert 0.0 < by_id["b"].source_scores["dense"] < 1.0

    def test_minmax_with_constant_scores_does_not_divide_by_zero(self):
        results = LinearFusion(normalize="minmax").fuse({"s": [("a", 2.0), ("b", 2.0)]})
        assert all(r.score == pytest.approx(0.0) for r in results)

    def test_zscore_normalisation_centres_the_scores(self):
        results = LinearFusion(normalize="zscore").fuse({"s": [("a", 3.0), ("b", 1.0)]})
        scores = sorted(r.score for r in results)
        assert scores == pytest.approx([-1.0, 1.0])

    def test_zscore_with_constant_scores_does_not_divide_by_zero(self):
        results = LinearFusion(normalize="zscore").fuse({"s": [("a", 2.0), ("b", 2.0)]})
        assert all(r.score == pytest.approx(0.0) for r in results)

    def test_rank_normalisation_ignores_raw_scores(self):
        results = LinearFusion(normalize="rank").fuse({"s": [("a", 0.01), ("b", 0.009)]})
        assert results[0].id == "a"
        assert results[0].score == pytest.approx(1.0)
        assert results[1].score == pytest.approx(0.5)

    def test_unknown_normalisation_uses_raw_scores(self):
        results = LinearFusion(normalize="none").fuse({"s": [("a", 4.0), ("b", 2.0)]})
        assert [(r.id, r.score) for r in results] == [("a", 4.0), ("b", 2.0)]

    def test_equal_weights_give_a_tie_between_symmetric_sources(self):
        results = LinearFusion().fuse(
            {"dense": [("a", 1.0), ("b", 0.5)], "sparse": [("b", 1.0), ("a", 0.5)]}
        )
        by_id = {r.id: r.score for r in results}
        assert by_id["a"] == pytest.approx(by_id["b"])

    def test_weights_decide_the_winner(self):
        lists = {"dense": [("a", 1.0), ("b", 0.5)], "sparse": [("b", 1.0), ("a", 0.5)]}
        dense_heavy = LinearFusion(weights={"dense": 0.9, "sparse": 0.1}).fuse(lists)
        sparse_heavy = LinearFusion(weights={"dense": 0.1, "sparse": 0.9}).fuse(lists)
        assert dense_heavy[0].id == "a"
        assert sparse_heavy[0].id == "b"

    def test_weights_are_normalised_so_scale_does_not_matter(self):
        small = LinearFusion(weights={"dense": 1, "sparse": 3}).fuse(LISTS)
        big = LinearFusion(weights={"dense": 100, "sparse": 300}).fuse(LISTS)
        assert [(r.id, pytest.approx(r.score)) for r in small] == [(r.id, r.score) for r in big]

    def test_missing_weight_defaults_to_one(self):
        explicit = LinearFusion(weights={"dense": 1.0, "sparse": 1.0}).fuse(LISTS)
        implicit = LinearFusion(weights={}).fuse(LISTS)
        assert [(r.id, pytest.approx(r.score)) for r in explicit] == [
            (r.id, r.score) for r in implicit
        ]

    def test_k_limits_and_ranks_are_sequential(self):
        results = LinearFusion().fuse(LISTS, k=2)
        assert len(results) == 2
        assert [r.rank for r in results] == [1, 2]

    def test_source_ranks_use_minus_one_for_absent_source(self):
        results = {r.id: r for r in LinearFusion().fuse(LISTS)}
        assert results["c"].source_ranks == {"dense": 3, "sparse": -1}
        assert results["c"].source_scores["sparse"] == 0.0


# ---------------------------------------------------------------------------
# Condorcet
# ---------------------------------------------------------------------------


class TestCondorcetFusion:
    def test_empty_input_gives_empty_output(self):
        assert CondorcetFusion().fuse({}) == []
        assert CondorcetFusion().fuse({"s": []}) == []

    def test_unanimous_winner_ranks_first(self):
        lists = {
            "one": [("a", 1.0), ("b", 0.5), ("c", 0.1)],
            "two": [("a", 9.0), ("c", 8.0), ("b", 7.0)],
        }
        results = CondorcetFusion().fuse(lists)
        assert results[0].id == "a"
        # a beats both others in both sources: 4 wins, 0 losses.
        assert results[0].score == pytest.approx(4.0)
        assert [r.rank for r in results] == [1, 2, 3]

    def test_single_source_reproduces_its_order(self):
        results = CondorcetFusion().fuse({"s": [("x", 1.0), ("y", 0.9), ("z", 0.8)]})
        assert ids(results) == ["x", "y", "z"]

    def test_document_absent_from_a_source_loses_to_everything_ranked_there(self):
        # c is unranked in "two", so it loses every pairwise comparison there.
        lists = {"one": [("c", 1.0), ("a", 0.5)], "two": [("a", 1.0), ("b", 0.5)]}
        results = {r.id: r for r in CondorcetFusion().fuse(lists)}
        assert results["a"].score > results["c"].score

    def test_weights_can_flip_a_split_decision(self):
        lists = {"dense": [("a", 1.0), ("b", 0.5)], "sparse": [("b", 1.0), ("a", 0.5)]}
        assert CondorcetFusion(weights={"dense": 3.0}).fuse(lists)[0].id == "a"
        assert CondorcetFusion(weights={"sparse": 3.0}).fuse(lists)[0].id == "b"

    def test_symmetric_sources_tie(self):
        lists = {"dense": [("a", 1.0), ("b", 0.5)], "sparse": [("b", 1.0), ("a", 0.5)]}
        results = CondorcetFusion().fuse(lists)
        assert results[0].score == pytest.approx(results[1].score) == pytest.approx(0.0)

    def test_k_limits_results(self):
        results = CondorcetFusion().fuse(LISTS, k=1)
        assert len(results) == 1 and results[0].rank == 1

    def test_source_bookkeeping(self):
        results = {r.id: r for r in CondorcetFusion().fuse(LISTS)}
        assert results["c"].source_scores == {"dense": 0.1, "sparse": 0.0}
        assert results["c"].source_ranks == {"dense": 3, "sparse": -1}
        assert results["b"].source_ranks == {"dense": 2, "sparse": 1}

    def test_score_is_a_plain_float(self):
        results = CondorcetFusion().fuse(LISTS)
        assert all(type(r.score) is float for r in results)


# ---------------------------------------------------------------------------
# HybridSearcher
# ---------------------------------------------------------------------------


class _Hit:
    """A result object with id and score attributes, as a search backend might return."""

    def __init__(self, id, score):
        self.id = id
        self.score = score


def dense_fn(query, k):
    return DENSE[:k]


def sparse_fn(query, k):
    return SPARSE[:k]


class TestHybridSearcher:
    def test_defaults_to_rrf(self):
        assert isinstance(HybridSearcher().fusion, RRFFusion)
        assert HybridSearcher().default_k == 100

    def test_no_sources_returns_empty(self):
        assert HybridSearcher().search("q") == []

    def test_add_list_and_remove_sources(self):
        s = HybridSearcher()
        s.add_source("dense", dense_fn, weight=0.7)
        s.add_source("sparse", sparse_fn)
        assert s.list_sources() == [
            {"name": "dense", "weight": 0.7},
            {"name": "sparse", "weight": 1.0},
        ]
        assert s.remove_source("dense") is True
        assert s.remove_source("dense") is False
        assert s.list_sources() == [{"name": "sparse", "weight": 1.0}]

    def test_search_fuses_all_sources(self):
        s = HybridSearcher()
        s.add_source("dense", dense_fn)
        s.add_source("sparse", sparse_fn)
        results = s.search("q", k=10)
        assert set(ids(results)) == {"a", "b", "c"}
        assert results[0].source_ranks.keys() == {"dense", "sparse"}

    def test_search_passes_source_k_to_each_source(self):
        seen = {}

        def spy(query, k):
            seen["query"], seen["k"] = query, k
            return DENSE

        s = HybridSearcher()
        s.add_source("dense", spy)
        s.search("hello", k=3)
        assert seen == {"query": "hello", "k": 30}, "source_k defaults to 10x k"
        s.search("hello", k=3, source_k=7)
        assert seen["k"] == 7

    def test_search_can_restrict_to_named_sources(self):
        s = HybridSearcher()
        s.add_source("dense", dense_fn)
        s.add_source("sparse", sparse_fn)
        results = s.search("q", sources=["dense"])
        assert ids(results) == ["a", "b", "c"]
        assert list(results[0].source_ranks) == ["dense"]

    def test_unknown_source_names_are_ignored_and_none_left_means_empty(self):
        s = HybridSearcher()
        s.add_source("dense", dense_fn)
        assert s.search("q", sources=["nope"]) == []

    def test_accepts_objects_with_id_and_score(self):
        s = HybridSearcher()
        s.add_source("obj", lambda q, k: [_Hit("a", 1), _Hit("b", 0.5)])
        results = s.search("q")
        assert ids(results) == ["a", "b"]
        assert results[0].source_scores["obj"] == 1.0

    def test_malformed_entries_are_dropped(self):
        s = HybridSearcher()
        s.add_source(
            "messy", lambda q, k: [("a", 1.0), ("lonely",), "junk", 42, ("b", 0.5, "extra")]
        )
        results = s.search("q")
        assert ids(results) == ["a", "b"]

    def test_failing_source_is_skipped_with_a_warning(self, caplog):
        def boom(query, k):
            raise RuntimeError("down")

        s = HybridSearcher()
        s.add_source("bad", boom)
        s.add_source("dense", dense_fn)
        with caplog.at_level("WARNING", logger="vectrixdb.core.search.fusion"):
            results = s.search("q")
        assert ids(results) == ["a", "b", "c"]
        # This printed to stdout, which an embedded library has no business
        # writing to; it is a log record the host can route or silence.
        assert "fusion source 'bad' failed" in caplog.text
        assert "down" in caplog.text

    def test_all_sources_failing_returns_empty(self):
        def boom(query, k):
            raise RuntimeError("down")

        s = HybridSearcher()
        s.add_source("bad", boom)
        assert s.search("q") == []

    def test_search_with_weights_uses_linear_fusion_then_restores(self):
        s = HybridSearcher()
        s.add_source("dense", lambda q, k: [("a", 1.0), ("b", 0.5)])
        s.add_source("sparse", lambda q, k: [("b", 1.0), ("a", 0.5)])
        original = s.fusion
        assert s.search_with_weights("q", {"dense": 0.9, "sparse": 0.1})[0].id == "a"
        assert s.search_with_weights("q", {"dense": 0.1, "sparse": 0.9})[0].id == "b"
        assert s.fusion is original

    def test_search_with_weights_restores_fusion_even_on_error(self):
        s = HybridSearcher()
        original = s.fusion
        # The source ignores k, so the failure happens inside the fusion step itself
        # when it slices the sorted list with a non-integer k.
        s.add_source("dense", lambda q, k: DENSE)
        with pytest.raises(TypeError):
            s.search_with_weights("q", {"dense": 1.0}, k="not-a-number")
        assert s.fusion is original

    def test_explain_result_reports_each_source(self):
        s = HybridSearcher()
        s.add_source("dense", dense_fn, weight=0.7)
        s.add_source("sparse", sparse_fn, weight=0.3)
        top = s.search("q")[0]
        explanation = s.explain_result(top)
        assert explanation["id"] == top.id
        assert explanation["final_rank"] == 1
        assert explanation["final_score"] == top.score
        by_source = {c["source"]: c for c in explanation["source_contributions"]}
        assert by_source["dense"]["weight"] == 0.7
        assert by_source["sparse"]["weight"] == 0.3
        assert by_source["dense"]["rank"] == top.source_ranks["dense"]

    def test_explain_result_for_a_source_no_longer_registered(self):
        s = HybridSearcher()
        s.add_source("dense", dense_fn)
        top = s.search("q")[0]
        s.remove_source("dense")
        contribution = s.explain_result(top)["source_contributions"][0]
        assert contribution == {"source": "dense", "score": 0.9, "rank": 1, "weight": 0.0}


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------


def fused(*pairs):
    return [FusedResult(id=i, score=s, rank=n) for n, (i, s) in enumerate(pairs, 1)]


VECTORS = {
    "a": np.array([1.0, 0.0]),
    "a2": np.array([1.0, 0.0]),  # identical direction to a
    "c": np.array([0.0, 1.0]),  # orthogonal to a
}


class TestMMRRerank:
    def test_empty_results_or_no_vectors_passes_through(self):
        assert Reranker.mmr_rerank([], VECTORS) == []
        results = fused(("a", 0.9), ("c", 0.1))
        assert Reranker.mmr_rerank(results, {}, k=1) == results[:1]

    def test_no_result_has_a_vector_passes_through(self):
        results = fused(("x", 0.9), ("y", 0.1))
        assert Reranker.mmr_rerank(results, VECTORS, k=5) == results

    def test_pure_relevance_keeps_the_score_order(self):
        results = fused(("a", 0.9), ("a2", 0.8), ("c", 0.1))
        out = Reranker.mmr_rerank(results, VECTORS, lambda_param=1.0)
        assert ids(out) == ["a", "a2", "c"]

    def test_diversity_promotes_the_orthogonal_document(self):
        results = fused(("a", 0.9), ("a2", 0.8), ("c", 0.1))
        out = Reranker.mmr_rerank(results, VECTORS, lambda_param=0.5)
        assert ids(out) == ["a", "c", "a2"]

    def test_ranks_are_rewritten_but_scores_and_payload_kept(self):
        results = fused(("a", 0.9), ("a2", 0.8), ("c", 0.1))
        results[2].payload = {"title": "c"}
        out = Reranker.mmr_rerank(results, VECTORS, lambda_param=0.5)
        assert [r.rank for r in out] == [1, 2, 3]
        by_id = {r.id: r for r in out}
        assert by_id["c"].score == 0.1 and by_id["c"].payload == {"title": "c"}

    def test_k_limits_the_output(self):
        results = fused(("a", 0.9), ("a2", 0.8), ("c", 0.1))
        assert len(Reranker.mmr_rerank(results, VECTORS, k=2)) == 2

    def test_results_without_vectors_are_dropped_when_others_have_them(self):
        results = fused(("a", 0.9), ("ghost", 0.8), ("c", 0.1))
        out = Reranker.mmr_rerank(results, VECTORS)
        assert "ghost" not in ids(out)
        assert set(ids(out)) == {"a", "c"}

    def test_accepts_plain_lists_as_vectors(self):
        results = fused(("a", 0.9), ("c", 0.1))
        out = Reranker.mmr_rerank(results, {"a": [1.0, 0.0], "c": [0.0, 1.0]})
        assert ids(out) == ["a", "c"]


class TestScoreThreshold:
    def test_keeps_scores_at_or_above_threshold(self):
        results = fused(("a", 0.9), ("b", 0.5), ("c", 0.1))
        assert ids(Reranker.score_threshold(results, 0.5)) == ["a", "b"]

    def test_empty_input(self):
        assert Reranker.score_threshold([], 0.0) == []


class TestDeduplicate:
    def test_no_vectors_or_single_result_passes_through(self):
        results = fused(("a", 0.9), ("a2", 0.8))
        assert Reranker.deduplicate(results) is results
        one = fused(("a", 0.9))
        assert Reranker.deduplicate(one, vectors=VECTORS) is one

    def test_removes_the_later_of_two_near_identical_results(self):
        results = fused(("a", 0.9), ("a2", 0.8), ("c", 0.1))
        out = Reranker.deduplicate(results, vectors=VECTORS)
        assert ids(out) == ["a", "c"]
        assert [r.rank for r in out] == [1, 2]

    def test_threshold_controls_what_counts_as_a_duplicate(self):
        vectors = {"a": np.array([1.0, 0.0]), "b": np.array([1.0, 0.2])}
        results = fused(("a", 0.9), ("b", 0.8))
        assert ids(Reranker.deduplicate(results, similarity_threshold=0.9, vectors=vectors)) == [
            "a"
        ]
        assert ids(Reranker.deduplicate(results, similarity_threshold=0.999, vectors=vectors)) == [
            "a",
            "b",
        ]

    def test_results_without_vectors_are_always_kept(self):
        results = fused(("a", 0.9), ("ghost", 0.8), ("a2", 0.7))
        out = Reranker.deduplicate(results, vectors=VECTORS)
        assert ids(out) == ["a", "ghost"]

    def test_a_kept_result_without_a_vector_is_skipped_when_comparing(self):
        results = fused(("ghost", 0.9), ("a", 0.8), ("a2", 0.7))
        out = Reranker.deduplicate(results, vectors=VECTORS)
        assert ids(out) == ["ghost", "a"]
        assert [r.rank for r in out] == [1, 2]

    def test_zero_vector_does_not_raise(self):
        vectors = {"a": np.zeros(2), "b": np.zeros(2)}
        out = Reranker.deduplicate(fused(("a", 1.0), ("b", 0.5)), vectors=vectors)
        assert ids(out) == ["a", "b"]
