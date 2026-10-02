"""Several processes reading one collection while another writes to it.

docs/explanation/concurrency.md promises this works: readers open their own
Vectrix, the writer saves the index atomically, and a reader that opens
mid-save sees either the old index or the new one, never a torn file.
"""

import multiprocessing as mp
import time

import pytest

from vectrixdb import Vectrix


def _reader(path: str, rounds: int):
    """Open, search repeatedly, report the first error or None."""
    try:
        for _ in range(rounds):
            db = Vectrix("shared", path=path)
            db.search("topic 1", limit=3)
            db.count()
            db.close()
            time.sleep(0.05)
        return None
    except Exception as exc:  # noqa: BLE001 - the error is the finding
        return f"{type(exc).__name__}: {exc}"


@pytest.mark.slow
def test_readers_survive_a_concurrent_writer(tmp_path):
    path = str(tmp_path)
    writer = Vectrix("shared", path=path)
    writer.add(["seed document about topic 1"])

    ctx = mp.get_context("spawn")
    with ctx.Pool(3) as pool:
        pending = [pool.apply_async(_reader, (path, 12)) for _ in range(3)]
        for i in range(40):
            writer.add(f"document number {i} about topic {i % 3}")
        errors = [r.get(timeout=300) for r in pending]

    assert errors == [None, None, None], errors
    assert writer.count() == 41
    assert Vectrix("shared", path=path).count() == 41
