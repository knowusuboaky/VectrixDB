"""A reopened collection still knows when it was last written to.

The time was kept in memory only, so after a restart the dashboard said
"nothing written yet" beside a collection with chunks in it, until the next
write. It is on the rows, and is read back from them.
"""

from __future__ import annotations

import numpy as np

from vectrixdb.core.database import VectrixDB


def test_the_time_of_the_last_write_is_read_back(tmp_path):
    database = VectrixDB(str(tmp_path))
    coll = database.create_collection(name="kept", dimension=4, metric="cosine")
    coll.add(ids=["a", "b"], vectors=np.eye(4, dtype=np.float32)[:2], texts=["one", "two"])
    written = coll._updated_at
    assert written is not None
    database.close()

    reopened = VectrixDB(str(tmp_path))
    try:
        again = reopened.get_collection("kept")
        assert again._updated_at is not None, "a collection with chunks in it was never written to"
        assert abs((again._updated_at - written).total_seconds()) < 1
    finally:
        reopened.close()


def test_an_empty_collection_has_no_last_write(tmp_path):
    database = VectrixDB(str(tmp_path))
    try:
        assert database.create_collection(name="empty", dimension=4)._updated_at is None
    finally:
        database.close()


def test_so_is_the_time_it_was_made(tmp_path):
    database = VectrixDB(str(tmp_path))
    coll = database.create_collection(name="kept", dimension=4, metric="cosine")
    made = coll._created_at
    coll.add(ids=["a"], vectors=np.eye(4, dtype=np.float32)[:1], texts=["one"])
    database.close()

    reopened = VectrixDB(str(tmp_path))
    try:
        again = reopened.get_collection("kept")
        assert again._created_at == made, "a reopened collection was made when the process started"
        assert again._created_at <= again._updated_at, "made after it was last written to"
    finally:
        reopened.close()
