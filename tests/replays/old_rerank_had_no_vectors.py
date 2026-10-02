"""Restore vector reranking with nothing to rerank by.

No call site passed ``get_vector_fn`` and the candidates carry no vector, so
``exact`` returned the input order untouched and ``mmr`` returned nothing at
all.
"""

from vectrixdb.core.advanced_search import Reranker

_fixed = Reranker.rerank
_fixed_mmr = Reranker._rerank_mmr


def _old_rerank(self, query_vector, candidates, limit=10, query_text=None, get_vector_fn=None):
    return _fixed(self, query_vector, candidates, limit, query_text, None)


def _old_rerank_mmr(self, query_vector, candidates, limit, get_vector_fn):
    # The old body had no all-None fallback: with no vectors the selection
    # loop ended immediately and the result was empty.
    if all(c.get("vector") is None for c in candidates):
        return []
    return _fixed_mmr(self, query_vector, candidates, limit, get_vector_fn)


def pytest_configure(config):
    Reranker.rerank = _old_rerank
    Reranker._rerank_mmr = _old_rerank_mmr
