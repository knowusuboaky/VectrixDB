"""What the REST server was sent survives the server being killed.

The library's one-line API saves the ANN index after every write. The
server called the same ``Collection.add`` and never saved, so the index
lived only in memory until a clean shutdown: a process killed rather than
stopped, which is how a container dies and how this dashboard's own preview
server was stopped, reopened with every id and no vectors. Dense search
found nothing, and a point lookup crashed on the vector it did not have.

The test writes through the API and then copies the data directory while
the app is still up, which is what a kill leaves behind: the SQLite file
with its write-ahead log, and whatever index file had been saved.
"""

from __future__ import annotations

import shutil

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402


def test_a_killed_server_keeps_what_it_was_sent(tmp_path):
    from vectrixdb.api import server
    from vectrixdb.core.database import VectrixDB

    data = tmp_path / "db"
    with TestClient(server.create_app(db_path=str(data), enable_dashboard=False)) as client:
        created = client.post(
            "/api/v2/collections",
            json={"name": "notes", "dimension": 384, "metric": "cosine", "enable_text_index": True, "tags": ["hybrid"]},
        )
        assert created.status_code in (200, 201), created.text
        sent = client.post(
            "/api/collections/notes/text-upsert",
            json={"points": [{"id": "a", "text": "Basalt is a fine-grained volcanic rock.", "payload": {"kind": "rock"}}]},
        )
        assert sent.status_code == 200, sent.text

        # The process dies here: nothing closes, nothing flushes.
        copy = tmp_path / "after_kill"
        shutil.copytree(data, copy)

    db = VectrixDB(path=str(copy))
    try:
        notes = db.get_collection("notes")
        assert notes.count() == 1
        assert notes._index_size() == 1, "the ANN index was never written to disk"
        point = notes.get("a")
        assert point is not None and len(point.vector) == 384
        hits = notes.search(query=list(point.vector), limit=1)
        assert [h.id for h in hits.results] == ["a"]
    finally:
        db.close()


def test_a_point_without_a_vector_is_still_a_point(tmp_path):
    """Belt and braces: an index that lost a key must not turn a lookup into a 500."""
    from vectrixdb.core.database import VectrixDB

    db = VectrixDB(path=str(tmp_path / "db"))
    try:
        coll = db.create_collection("c", dimension=4)
        coll.add(ids=["x"], vectors=[[0.1, 0.2, 0.3, 0.4]], metadata=[{}], texts=["x"])
        coll._index.remove(coll._id_to_idx["x"])
        point = coll.get("x")
        assert point is not None and point.vector == []
    finally:
        db.close()
