"""Restore a delete that left index_build_id where it was.

Only add() and revoke() minted a build id. A delete, a clear, an index
rebuild and a re-embed all changed what a query could answer and left the id
untouched, and reproduction rests on one invariant: same build id, same
index. A reproduction run after a delete would have called itself exact
against an index that had lost documents.

Under this replay delete() goes back to not stamping.

    python scripts/replay.py tests/replays/old_delete_kept_the_build_id.py \\
        tests/unit/test_lineage.py
"""

from vectrixdb.easy import Vectrix


def _old_delete(self, ids):
    if isinstance(ids, str):
        ids = [ids]
    self._collection.delete(ids=ids)
    for id_ in ids:
        self._texts.pop(id_, None)
    return self


def pytest_configure(config):
    Vectrix.delete = _old_delete
