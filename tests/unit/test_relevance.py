"""One number from 0 to 1 that means the same thing whichever engine answered.

``score`` is whatever a mode ranks by. In a hybrid search that is a sum of
reciprocal ranks, where the best result there can be scores about 0.018, so
it orders a list and says nothing about how good anything on it is. It could
never carry a threshold, and a system that cannot say "the best thing I found
is not good enough" cannot decline to answer.

Held here: what ``relevance`` is in each mode, that it survives the trip
through the library and the REST API, and that each cloud store's conversion
is the one its vendor documents. The OpenSearch conversion is not trusted to a
table, because it changed between versions and differs by engine: the store
works it out from a hit, and the test runs it against both formulas.
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from vectrixdb import Vectrix  # noqa: E402
from vectrixdb.core import relevance as rel  # noqa: E402
from vectrixdb.core.database import VectrixDB  # noqa: E402

VECTORS = {
    "payment invoice terms": [1, 0, 0, 0],
    "payment is late": [0.9, 0.1, 0, 0],
    "zebra crossing rules": [0, 1, 0, 0],
    "when is payment due": [1, 0, 0, 0],
    "zebra": [0.1, 0.9, 0, 0],
}


def embed(texts):
    return np.array([VECTORS[t] for t in texts], dtype=np.float32)


class TestTheConversions:
    @pytest.mark.parametrize("cos", [1.0, 0.8, 0.5, 0.25, 0.0])
    def test_azure_there_and_back(self, cos):
        score = 1.0 / (1.0 + (1.0 - cos))  # what Microsoft documents @search.score to be
        assert rel.from_azure_cosine_score(score) == pytest.approx(cos, abs=1e-6)

    def test_the_bottom_of_azures_range_is_the_opposite_vector(self):
        assert 1.0 / (1.0 + 2.0) == pytest.approx(0.333, abs=1e-3), (
            "the documented range is 0.333 to 1.00"
        )
        assert rel.from_azure_cosine_score(1.0 / 3.0) == 0.0, (
            "opposite and unrelated are both no match"
        )

    @pytest.mark.parametrize("cos", [1.0, 0.8, 0.5, 0.0])
    def test_both_of_opensearchs_documented_formulas(self, cos):
        d = 1.0 - cos
        assert rel.from_opensearch_cosine_score((2.0 - d) / 2.0, "half") == pytest.approx(
            cos, abs=1e-6
        )
        assert rel.from_opensearch_cosine_score(1.0 / (1.0 + d), "reciprocal") == pytest.approx(
            cos, abs=1e-6
        )

    def test_no_formula_no_number(self):
        assert rel.from_opensearch_cosine_score(0.9, None) is None
        assert rel.from_opensearch_cosine_score(0.9, "guess") is None
        assert (
            rel.from_azure_cosine_score(0.0) is None and rel.from_azure_cosine_score(None) is None
        )

    def test_a_hit_says_which_formula_a_cluster_uses(self):
        cos = 0.6
        assert rel.pick_opensearch_formula((2.0 - (1 - cos)) / 2.0, cos) == "half"
        assert rel.pick_opensearch_formula(1.0 / (1.0 + (1 - cos)), cos) == "reciprocal"
        assert rel.pick_opensearch_formula(1.0, 1.0) is None, (
            "a perfect match fits both and settles nothing"
        )
        assert rel.pick_opensearch_formula(0.123, cos) is None, (
            "and a score neither explains is not explained"
        )

    def test_a_rerankers_logits_go_through_a_sigmoid_and_probabilities_do_not(self):
        assert rel.from_reranker([0.9, 0.2, 0.5]) == [0.9, 0.2, 0.5]
        logits = rel.from_reranker([4.0, 0.4, -6.0])
        assert logits[0] == pytest.approx(1 / (1 + math.exp(-4.0)), abs=1e-6)
        assert logits[1] == pytest.approx(1 / (1 + math.exp(-0.4)), abs=1e-6), (
            "0.4 beside a 4.0 is a logit, not a probability"
        )
        assert 0.0 < logits[2] < 0.01 and rel.from_reranker([]) == []
        assert rel.from_reranker([1000.0, -1000.0]) == [1.0, 0.0], (
            "and an absurd one does not overflow"
        )

    def test_a_score_with_no_ceiling_is_a_share_of_the_best(self):
        assert rel.relative([4.0, 2.0, 1.0]) == [1.0, 0.5, 0.25]
        assert rel.relative([]) == [] and rel.relative([0.0, None]) == [None, None]


@pytest.fixture
def collection(tmp_path):
    database = VectrixDB(str(tmp_path))
    coll = database.create_collection(
        name="c", dimension=4, metric="cosine", enable_text_index=True
    )
    coll.add(
        ids=["first", "a", "b"],
        vectors=np.array(
            [
                VECTORS["zebra crossing rules"],
                VECTORS["payment invoice terms"],
                VECTORS["payment is late"],
            ],
            dtype=np.float32,
        ),
        texts=["zebra crossing rules", "payment invoice terms", "payment is late"],
    )
    yield coll
    database.close()


class TestTheLocalIndex:
    def test_dense_is_the_similarity(self, collection):
        hits = collection.search(query=[1, 0, 0, 0], limit=3).results
        assert [(h.id, h.relevance_kind) for h in hits] == [
            ("a", "similarity"),
            ("b", "similarity"),
            ("first", "similarity"),
        ]
        assert (
            hits[0].relevance == 1.0
            and hits[1].relevance == pytest.approx(0.9939, abs=1e-3)
            and hits[2].relevance == 0.0
        )
        assert hits[0].matched_by == ["meaning"]
        assert hits[2].matched_by is None, (
            "a chunk with no likeness is on the list because the list had room"
        )

    def test_keyword_is_a_share_of_the_best_and_says_so(self, collection):
        hits = collection.keyword_search(query_text="payment", limit=3).results
        assert hits[0].relevance == 1.0 and 0.0 < hits[1].relevance < 1.0
        assert {h.relevance_kind for h in hits} == {"relative"} and hits[0].matched_by == [
            "keywords"
        ]

    def test_hybrid_orders_by_rank_and_reports_the_similarity(self, collection):
        hits = collection.hybrid_search(
            query=[1, 0, 0, 0], query_text="payment late", limit=3
        ).results
        assert all(h.score < 0.02 for h in hits), (
            "the rank score is tiny, which is the whole problem"
        )
        by_id = {h.id: h for h in hits}
        assert by_id["a"].relevance == 1.0 and by_id["b"].relevance == pytest.approx(
            0.9939, abs=1e-3
        )
        assert by_id["a"].matched_by == by_id["b"].matched_by == ["meaning", "keywords"]
        assert {h.relevance_kind for h in hits} == {"similarity"}

    def test_the_first_chunk_ever_added_can_be_found_by_keywords_alone(self, collection):
        """Index keys start at 0, and hybrid tested ``if idx:``, so the chunk with key 0 was never there."""
        hits = collection.hybrid_search(
            query=[1, 0, 0, 0], query_text="zebra", limit=1, prefetch_multiplier=1
        ).results
        found = collection.hybrid_search(query=[1, 0, 0, 0], query_text="zebra", limit=3).results
        assert "first" in {h.id for h in found}
        first = next(h for h in found if h.id == "first")
        assert first.text_score and "keywords" in first.matched_by
        assert first.relevance == 0.0, (
            "the keywords found it; its meaning is nowhere near, and the number says so"
        )
        assert hits

    def test_a_collection_that_is_not_cosine_says_distance(self, tmp_path):
        database = VectrixDB(str(tmp_path / "e"))
        coll = database.create_collection(name="e", dimension=4, metric="euclidean")
        coll.add(ids=["x"], vectors=np.array([[1, 0, 0, 0]], dtype=np.float32))
        hit = coll.search(query=[1, 0, 0, 0], limit=1).results[0]
        assert hit.relevance_kind == "distance" and 0.0 <= hit.relevance <= 1.0
        database.close()

    def test_the_reply_carries_it_and_a_result_without_one_is_unchanged(self, collection):
        shown = collection.search(query=[1, 0, 0, 0], limit=1).results[0].to_dict()
        assert (
            shown["relevance"] == 1.0
            and shown["relevance_kind"] == "similarity"
            and shown["matched_by"] == ["meaning"]
        )
        from vectrixdb.core.types import SearchResult

        assert set(SearchResult(id="x", score=0.5).to_dict()) == {"id", "score", "metadata"}


class TestThroughTheLibrary:
    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
        d = Vectrix(
            "t",
            path=str(tmp_path),
            dimension=4,
            tier="hybrid",
            embed_fn=embed,
            embedding_cache=False,
        )
        d.add(
            ["zebra crossing rules", "payment invoice terms", "payment is late"],
            ids=["z", "a", "b"],
        )
        yield d
        d.close()

    def test_dense(self, db):
        hits = list(db.search("when is payment due", mode="dense", limit=3))
        assert (
            hits[0].id == "a"
            and hits[0].relevance == 1.0
            and hits[0].relevance_kind == "similarity"
        )
        assert hits[0].similarity == 1.0 and hits[0].matched_by == ["meaning"]

    def test_hybrid_without_the_reranker_is_the_similarity(self, db):
        hits = list(db.search("when is payment due", mode="hybrid", limit=3, rerank=False))
        assert all(h.score < 0.03 for h in hits)
        best = max(hits, key=lambda h: h.relevance)
        assert (
            best.relevance == 1.0 and best.relevance_kind == "similarity" and best.similarity == 1.0
        )
        assert {"meaning", "keywords"} == set(next(h for h in hits if h.id == "b").matched_by)

    def test_sparse_is_relative(self, db):
        hit = list(db.search("zebra", mode="sparse", limit=1))[0]
        assert (
            hit.relevance == 1.0
            and hit.relevance_kind == "relative"
            and hit.similarity is None
            and hit.matched_by == ["keywords"]
        )

    def test_when_a_reranker_read_the_pair_its_verdict_leads_and_the_similarity_is_kept(self, db):
        class Judge:
            label = "judge"

            def rerank(self, query, docs, top):
                # logits: the zebra chunk answers a zebra question, the others do not
                scored = [(i, 5.0 if "zebra" in d else -5.0) for i, d in enumerate(docs)]
                return sorted(scored, key=lambda x: -x[1])[:top]

        db._external_reranker = Judge()
        hits = list(db.search("zebra", mode="hybrid", limit=3))
        assert hits[0].id == "z" and hits[0].relevance_kind == "reranker"
        assert hits[0].relevance == pytest.approx(1 / (1 + math.exp(-5.0)), abs=1e-4)
        assert hits[-1].relevance < 0.01, "on topic or not, a chunk that does not answer scores low"
        assert hits[0].similarity is not None and hits[0].similarity != hits[0].relevance, (
            "a threshold kept on the similarity still has something to hold"
        )
