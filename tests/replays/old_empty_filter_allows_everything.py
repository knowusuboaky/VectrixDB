"""Restore the filters that read an empty allow-list as no filter at all.

``SparseSearch.search``, ``BM25Scorer.score`` and ``ColBERTSearch.search``
each tested ``filter_ids`` for truthiness. A caller who passed an empty set,
meaning allow nothing, got an unfiltered search over the whole corpus. This
is the one in the family with teeth: the other eight returned a wrong number
or blocked, and this one returned documents the caller had excluded.
"""

from vectrixdb.core.search.colbert import ColBERTSearch
from vectrixdb.core.search.sparse import BM25Scorer, SparseSearch

_fixed_sparse = SparseSearch.search
_fixed_score = BM25Scorer.score
_fixed_colbert = ColBERTSearch.search


def _drop_empty(fixed):
    """An empty filter becomes no filter, which is what the old code did."""

    def call(self, *args, **kwargs):
        if "filter_ids" in kwargs and kwargs["filter_ids"] is not None:
            if len(kwargs["filter_ids"]) == 0:
                kwargs["filter_ids"] = None
        return fixed(self, *args, **kwargs)

    return call


def pytest_configure(config):
    SparseSearch.search = _drop_empty(_fixed_sparse)
    BM25Scorer.score = _drop_empty(_fixed_score)
    ColBERTSearch.search = _drop_empty(_fixed_colbert)
