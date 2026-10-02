"""Restore the server that never saved the ANN index after a write.

`Vectrix.add` calls `Collection.save()` when it is done; the five write
routes in the REST server called `Collection.add` and `Collection.delete`
and stopped there. A clean shutdown saved everything at once, so nothing
showed in a test that exits its client context. A killed process, which is
how containers die and how the dashboard's preview server was stopped,
reopened with every id in SQLite and an empty index: dense search returned
nothing, and a point lookup crashed on the vector it did not have.

The replay makes `save()` do nothing, which is exactly what the server did
between writes. Only the API test exercises it, so the library's own path is
untouched by the comparison.

    python scripts/replay.py tests/replays/old_rest_writes_never_saved_the_index.py \\
        tests/unit/test_api_durability.py
"""

from vectrixdb.core.collection import Collection


def _old_save(self) -> None:
    return None


def pytest_configure(config):
    Collection.save = _old_save
