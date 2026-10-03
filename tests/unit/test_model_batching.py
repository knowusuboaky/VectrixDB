"""Batched reranker and ColBERT inference: fast, and reproducible.

The INT8 exports compute activation scales per batch, so a score moves a
little with its batch-mates and with padding width; that cannot be removed
from outside the model. What a ranking needs is that the same candidate set
scores the same however it arrives, so batches are formed from a canonical
order (length bucket, then text) rather than input order. These pin that,
plus a sanity bound on how far batched scores may drift from single ones.
"""

import time

import numpy as np
import pytest

from vectrixdb.models import RerankerEmbedder

QUERY = "documents about topic 3"
DOCS = [f"document number {i} about topic {i % 9} and subject {i % 7}" for i in range(30)] + [
    "a much longer document that discusses topic 3 at length, across several clauses, "
    "with far more words than any of the others in this batch"
]


@pytest.fixture(scope="module")
def reranker():
    return RerankerEmbedder(model="reranker_en", language="en")


class TestReranker:
    def test_the_same_set_scores_the_same_in_any_order(self, reranker):
        forward = np.array(reranker.score(QUERY, DOCS))
        backward = np.array(reranker.score(QUERY, list(reversed(DOCS))))[::-1]
        assert np.array_equal(forward, backward)

    def test_batched_scores_stay_close_to_single(self, reranker):
        """A quality bound, not equality: the INT8 model drifts with its batch."""
        alone = np.array([reranker.score(QUERY, [d])[0] for d in DOCS])
        together = np.array(reranker.score(QUERY, DOCS, batch_size=16))
        assert float(np.abs(alone - together).max()) < 0.1
        rank_alone = np.argsort(np.argsort(-alone))
        rank_together = np.argsort(np.argsort(-together))
        correlation = np.corrcoef(rank_alone, rank_together)[0, 1]
        assert correlation > 0.95, correlation

    @pytest.mark.perf
    def test_thirty_pairs_are_fast(self, reranker):
        """~5 s before batching; allow 1.5 s so CI noise cannot fail it."""
        reranker.score(QUERY, DOCS[:2])  # warm
        started = time.perf_counter()
        reranker.score(QUERY, DOCS[:30])
        assert time.perf_counter() - started < 1.5

    def test_empty_input(self, reranker):
        assert reranker.score(QUERY, []).tolist() == []


class TestColbert:
    @pytest.fixture(scope="class")
    def colbert(self):
        from vectrixdb.models import LateInteractionEmbedder

        # The bundled English model. The multilingual default (bge-m3) is a
        # download, and its INT8 export does not load on every onnxruntime.
        return LateInteractionEmbedder(model="colbert")

    def test_batched_embeddings_match_single(self, colbert):
        single = [colbert.encode_document(d) for d in DOCS[:6]]
        batched = colbert.encode_documents(DOCS[:6])
        assert len(batched) == 6
        for s, b in zip(single, batched):
            assert s.shape == b.shape
            cosine = np.sum(s * b, axis=1) / (np.linalg.norm(s, axis=1) * np.linalg.norm(b, axis=1))
            assert cosine.min() > 0.98, float(cosine.min())

    def test_the_same_set_embeds_the_same_in_any_order(self, colbert):
        forward = colbert.encode_documents(DOCS[:8])
        backward = colbert.encode_documents(list(reversed(DOCS[:8])))[::-1]
        for f, b in zip(forward, backward):
            assert np.array_equal(f, b)

    @pytest.mark.perf
    def test_score_is_fast(self, colbert):
        colbert.score(QUERY, DOCS[:2])
        started = time.perf_counter()
        colbert.score(QUERY, DOCS[:30])
        assert time.perf_counter() - started < 3.0
