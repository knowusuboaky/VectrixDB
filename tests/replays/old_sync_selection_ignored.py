"""Restore a collection selection of [] meaning "sync everything".

``full`` guarded with ``if collections and name not in collections``, which
never skips when the list is empty.
"""

from vectrixdb.core.sync import VectrixSync

_fixed = VectrixSync.full


def _old_full(self, collections=None):
    return _fixed(self, collections=collections if collections else None)


def pytest_configure(config):
    VectrixSync.full = _old_full
