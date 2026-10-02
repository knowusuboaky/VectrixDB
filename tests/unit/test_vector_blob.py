"""Vectors in the local document store are float32 blobs, not JSON text.

Measured before the change: 8.8 KB per 384-d vector in SQLite, on top of the
copy the ANN index holds. A float32 blob is 1.5 KB. Rows written by earlier
versions keep their JSON form and still read.
"""

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from vectrixdb.core.storage import SQLiteStorage, StorageBackend, StorageConfig


@pytest.fixture
def storage(tmp_path):
    s = SQLiteStorage(StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=str(tmp_path)))
    s.connect()
    s.create_collection("c", {"mode": "dense"})
    return s


def _row(tmp_path, id_):
    con = sqlite3.connect(str(Path(tmp_path) / "c.db"))
    con.row_factory = sqlite3.Row
    return con.execute("SELECT * FROM documents WHERE id = ?", (id_,)).fetchone()


class TestBlobs:
    def test_vector_leaves_the_json(self, storage, tmp_path):
        vec = np.random.default_rng(0).random(384, dtype=np.float32)
        storage.insert("c", "a", {"_embedding": vec.tolist(), "k": 1, "text_content": "hi"})
        row = _row(tmp_path, "a")
        assert "_embedding" not in json.loads(row["data"])
        assert len(row["vector"]) == 384 * 4

    def test_round_trip_is_exact(self, storage):
        vec = np.random.default_rng(1).random(384, dtype=np.float32)
        storage.insert("c", "a", {"_embedding": vec.tolist()})
        back = np.asarray(storage.get("c", "a")["_embedding"], dtype=np.float32)
        assert np.array_equal(back, vec)

    def test_late_interaction_matrix_round_trips(self, storage):
        mat = np.random.default_rng(2).random((7, 96), dtype=np.float32)
        storage.insert(
            "c", "a", {"_embedding": [0.0] * 4, "late_interaction_embedding": mat.tolist()}
        )
        back = np.asarray(storage.get("c", "a")["late_interaction_embedding"], dtype=np.float32)
        assert back.shape == (7, 96) and np.array_equal(back, mat)

    def test_batch_insert_get_and_scan(self, storage):
        docs = [(f"d{i}", {"_embedding": [float(i)] * 4, "i": i}) for i in range(5)]
        storage.insert_batch("c", docs)
        got = storage.get_batch("c", ["d3", "d1", "nope"])
        assert got[0]["_embedding"] == [3.0] * 4 and got[1]["i"] == 1 and got[2] is None
        scanned = dict(storage.scan("c", limit=10))
        assert scanned["d4"]["_embedding"] == [4.0] * 4

    def test_update_keeps_the_vector_in_the_blob(self, storage, tmp_path):
        storage.insert("c", "a", {"_embedding": [1.0, 2.0], "k": 1})
        storage.update("c", "a", {"k": 2})
        assert storage.get("c", "a") == {"k": 2, "_embedding": [1.0, 2.0]}
        assert "_embedding" not in json.loads(_row(tmp_path, "a")["data"])

    def test_disk_cost_is_the_float32_size(self, storage, tmp_path):
        """Measured after close, so the WAL is checkpointed and the file is final."""
        n = 1000
        vecs = np.random.default_rng(3).random((n, 384), dtype=np.float32)
        storage.insert_batch("c", [(f"d{i}", {"_embedding": vecs[i].tolist()}) for i in range(n)])
        storage.close()
        size = (Path(tmp_path) / "c.db").stat().st_size
        per_vector = size / n
        assert per_vector < 2.0 * 384 * 4, f"{per_vector:.0f} bytes per vector (JSON was ~8,800)"


class TestLegacyRows:
    def test_json_vectors_from_earlier_versions_still_read(self, storage, tmp_path):
        con = sqlite3.connect(str(Path(tmp_path) / "c.db"))
        con.execute(
            "INSERT INTO documents (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("old", json.dumps({"_embedding": [0.5, 0.25], "k": 9}), "t", "t"),
        )
        con.commit()
        assert storage.get("c", "old") == {"_embedding": [0.5, 0.25], "k": 9}
        hits = storage.vector_search("c", [0.5, 0.25], limit=1)
        assert hits and hits[0][0] == "old"

    def test_compact_moves_them_into_blobs(self, storage, tmp_path):
        con = sqlite3.connect(str(Path(tmp_path) / "c.db"))
        con.execute(
            "INSERT INTO documents (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("old", json.dumps({"_embedding": [0.5, 0.25]}), "t", "t"),
        )
        con.commit()
        assert storage.compact_vectors("c") == 1
        assert storage.compact_vectors("c") == 0
        row = _row(tmp_path, "old")
        assert "_embedding" not in json.loads(row["data"]) and row["vector"] is not None
        assert storage.get("c", "old")["_embedding"] == [0.5, 0.25]

    def test_columns_are_added_to_an_old_table(self, tmp_path):
        con = sqlite3.connect(str(Path(tmp_path) / "c.db"))
        con.execute(
            "CREATE TABLE documents (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT, updated_at TEXT)"
        )
        con.commit()
        con.close()
        s = SQLiteStorage(StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=str(tmp_path)))
        s.connect()
        s.insert("c", "a", {"_embedding": [1.0]})
        assert s.get("c", "a")["_embedding"] == [1.0]


class TestVectorSearchFallback:
    def test_ranks_by_cosine(self, storage):
        storage.insert_batch(
            "c",
            [
                ("x", {"_embedding": [1.0, 0.0]}),
                ("y", {"_embedding": [0.0, 1.0]}),
                ("d", {"_embedding": [0.7, 0.7]}),
            ],
        )
        hits = storage.vector_search("c", [1.0, 0.1], limit=3)
        assert [h[0] for h in hits] == ["x", "d", "y"]
        assert hits[0][2] == pytest.approx(1 - 1.0 / np.sqrt(1.01), abs=1e-5)
        assert "_embedding" not in hits[0][1]

    def test_zero_vectors_are_skipped(self, storage):
        storage.insert_batch(
            "c", [("z", {"_embedding": [0.0, 0.0]}), ("x", {"_embedding": [1.0, 0.0]})]
        )
        assert [h[0] for h in storage.vector_search("c", [1.0, 0.0])] == ["x"]
