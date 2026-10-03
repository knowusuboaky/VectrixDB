"""One Vectrix shared across threads has to behave, because the docs say it does.

docs/explanation/concurrency.md promises per-thread SQLite connections, a lock
around the index, and thread-safe embedding. This exercises all three at once.
"""

import threading

from vectrixdb import Vectrix

THREADS = 8
PER_THREAD = 5


def test_concurrent_adds_and_searches_on_one_instance(tmp_path):
    db = Vectrix("shared", path=str(tmp_path))
    errors = []
    start = threading.Barrier(THREADS)

    def worker(n):
        try:
            start.wait()
            for i in range(PER_THREAD):
                db.add(f"thread {n} wrote note number {i} about topic {n % 3}")
                db.search(f"topic {n % 3}", limit=3)
        except Exception as exc:  # noqa: BLE001 - any failure is the finding
            errors.append((n, repr(exc)))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert db.count() == THREADS * PER_THREAD
    db.close()

    reopened = Vectrix("shared", path=str(tmp_path))
    assert reopened.count() == THREADS * PER_THREAD
    assert reopened.search("topic 1", limit=3)
