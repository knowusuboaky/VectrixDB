"""Restore the neighbour count that ignored tombstones.

``Collection.search`` asked the index for ``min(search_limit, self._count)``
neighbours, where ``_count`` is the live document count. A deleted vector
stays in the graph as a tombstone, so the index holds more keys than that,
and a query whose nearest neighbours were all deleted came back empty while
live documents sat in the index.
"""

from vectrixdb.core.collection import Collection

_fixed_index_size = Collection._index_size


def _old_index_size(self):
    return self._count


def pytest_configure(config):
    Collection._index_size = _old_index_size
