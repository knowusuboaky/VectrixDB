"""Restore the collection that reported HNSW defaults it was not built with.

``Collection.__init__`` builds the index from ``m`` and ``ef_construction``
but stored a bare ``IndexConfig()``. ``info()`` reports that config, so a
collection created with ``m=8`` said 16, and the REST API's ``hnsw_m`` and
``hnsw_ef_construction`` looked like they did nothing.
"""

from vectrixdb.core.collection import Collection
from vectrixdb.core.types import IndexConfig

_fixed_init = Collection.__init__


def _old_init(self, *args, **kwargs):
    _fixed_init(self, *args, **kwargs)
    if kwargs.get("index_config") is None:
        self.index_config = IndexConfig()


def pytest_configure(config):
    Collection.__init__ = _old_init
