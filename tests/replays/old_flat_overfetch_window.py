"""Restore the fixed ``limit * 10`` window a filtered search had to live in.

The filter is applied to whatever the index already chose. With the window
pinned at ten times the limit, a filter that only one document in a hundred
passes left nothing in it, and the search returned an empty list while plenty
of documents matched.

The widening loop stops as soon as the window covers the whole index, so
telling the collection it is exactly one window long reproduces the single
pass the old code made.
"""

from vectrixdb.core.collection import Collection

_fixed = Collection.search


def _old_search(self, query, limit=10, **kwargs):
    if not kwargs.get("filter"):
        return _fixed(self, query, limit=limit, **kwargs)

    window = limit * 10
    real_count = self._count
    real_size = self._index_size
    self._count = min(real_count, window)
    self._index_size = lambda: min(real_size(), window)
    try:
        return _fixed(self, query, limit=limit, **kwargs)
    finally:
        self._count = real_count
        self._index_size = real_size


def pytest_configure(config):
    Collection.search = _old_search
