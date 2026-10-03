"""Restore the sync that copied nothing and called it a success.

``VectrixSync.full`` guarded its copy with ``if source_coll and target_coll``.
A Collection is sized and the target has just been created, so the empty
target was falsy, the copy was skipped, and the result came back with no
rows, no collections and no errors.
"""

from vectrixdb.core.sync import VectrixSync

_fixed_read_points = VectrixSync._read_points


def _old_read_points(self, backend, collection):
    # The old guard skipped the whole copy for an empty target, which is the
    # same observable result as reading no points at all.
    return iter(())


def pytest_configure(config):
    VectrixSync._read_points = _old_read_points
