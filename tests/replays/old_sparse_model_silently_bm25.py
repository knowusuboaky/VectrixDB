"""Restore an unreachable sparse model being swapped for BM25 in silence.

Hybrid search on a local collection is the bundled BM25 index whatever
``sparse_model`` says, and nothing said so.
"""

from vectrixdb.easy import Vectrix


def _old_warn(self):
    return None


def pytest_configure(config):
    Vectrix._warn_if_sparse_model_is_unreachable = _old_warn
