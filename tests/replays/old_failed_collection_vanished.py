"""Restore a collection that would not load being dropped in silence.

The loader printed a warning and carried on, so the collection was simply
absent and listing it showed nothing.
"""

from vectrixdb.core.database import VectrixDB

_fixed = VectrixDB._load_collections


def _old_load_collections(self):
    _fixed(self)
    # The old code kept no record and issued no warning.
    self._failed_collections.clear()


def pytest_configure(config):
    VectrixDB._load_collections = _old_load_collections
