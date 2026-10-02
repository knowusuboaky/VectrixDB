"""A deleted or cleared collection leaves nothing behind in the store.

The store holds a collection's vector rows. Deleting the collection closed
it, removed its row from the catalogue and removed its folder, and never told
the store. An empty local index defers to the store, so a collection cleared,
or recreated under the same name, answered a search with the ids of documents
that were gone, and with no text, while count() said zero. It only showed on
a collection that had been closed and reopened, which is why no test had it.
"""

from __future__ import annotations

import numpy as np


def embed(texts):
    return np.asarray([[1, 0, 0, 0] if "alpha" in t else [0, 1, 0, 0] for t in texts], dtype=np.float32)


def reopened(tmp_path):
    from vectrixdb import Vectrix

    first = Vectrix("memos", path=str(tmp_path), embed_fn=embed, dimension=4)
    first.add(["alpha memo", "beta memo"], ids=["a", "b"])
    first.close()
    return Vectrix("memos", path=str(tmp_path), embed_fn=embed, dimension=4)


def test_clear_on_a_reopened_collection_clears_the_store_too(tmp_path):
    db = reopened(tmp_path)
    try:
        assert [h.id for h in db.search("alpha", limit=3)] == ["a", "b"]
        db.clear()
        assert db.count() == 0
        assert db.search("alpha", limit=3).items == []
        assert db.search("something not asked before", limit=3).items == []
        db.add(["alpha again"], ids=["a2"])
        assert [h.id for h in db.search("alpha", limit=3)] == ["a2"]
    finally:
        db.close()


def test_a_collection_recreated_under_the_same_name_starts_empty(tmp_path):
    from vectrixdb.core.database import VectrixDB

    reopened(tmp_path).close()
    database = VectrixDB(str(tmp_path))
    try:
        assert database.delete_collection("memos") is True
        fresh = database.create_collection(name="memos", dimension=4, metric="cosine")
        found = fresh.search(np.asarray([1, 0, 0, 0], dtype=np.float32), limit=3)
        assert [r.id for r in found.results] == []
    finally:
        database.close()
