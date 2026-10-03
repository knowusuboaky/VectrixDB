"""A write is not finished until the store and the cache know about it.

Three faults, found together while building named vectors, each older than
that work:

* Cache invalidation was an empty function, so a search repeated after an
  add or a delete returned the answer from before it, for an hour in memory
  and a day in Redis.
* A delete never reached the storage backend. The row stayed in the store
  for ever, which for a regulated index means a deleted document was not
  deleted, and once the local index was empty the store served it back.
* A metadata update never reached the store either, and under a policy the
  decision is made on the metadata the store returns. So a revocation did
  not revoke anything a store-served search returned.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from vectrixdb.core.cache import CacheConfig, MemoryCache, RedisCache, VectorCache


def embed(texts):
    return np.asarray(
        [[1, 0, 0, 0] if "alpha" in t else [0, 1, 0, 0] for t in texts], dtype=np.float32
    )


@pytest.fixture
def db(tmp_path):
    from vectrixdb import Vectrix

    handle = Vectrix("memos", path=str(tmp_path), embed_fn=embed, dimension=4)
    yield handle
    handle.close()


class TestARepeatedSearchSeesTheWrite:
    def test_after_an_add(self, db):
        db.add(["beta memo"], ids=["b"])
        assert [h.id for h in db.search("alpha", limit=3)] == ["b"]
        db.add(["alpha memo"], ids=["a"])
        assert [h.id for h in db.search("alpha", limit=3)] == ["a", "b"]

    def test_after_a_delete(self, db):
        db.add(["alpha memo", "beta memo"], ids=["a", "b"])
        assert [h.id for h in db.search("alpha", limit=3)] == ["a", "b"]
        db.delete("a")
        assert [h.id for h in db.search("alpha", limit=3)] == ["b"]

    def test_after_a_metadata_update(self, db):
        db.add(["alpha memo"], ids=["a"], metadata=[{"status": "draft"}])
        assert db.search("alpha", limit=1).top.metadata["status"] == "draft"
        db._collection.update_metadata("a", {"status": "final"})
        assert db.search("alpha", limit=1).top.metadata["status"] == "final"

    def test_deleting_everything_leaves_nothing_for_the_store_to_serve(self, db):
        db.add(["alpha memo", "beta memo"], ids=["a", "b"])
        db.delete(["a", "b"])
        assert db.count() == 0 and db.search("alpha", limit=3).items == []


class TestTheCache:
    def test_a_memory_cache_forgets_a_prefix_and_nothing_else(self):
        cache = MemoryCache(CacheConfig())
        for key in ("vec:q:memos:1", "vec:q:memos:2", "vec:q:memos_archive:1", "vec:v:memos:a"):
            cache.set(key, {"x": 1})
        assert cache.delete_prefix("vec:q:memos:") == 2
        assert cache.get("vec:q:memos:1") is None and cache.get("vec:q:memos_archive:1") == {"x": 1}

    def test_invalidating_a_collection_leaves_its_neighbour_alone(self):
        cache = VectorCache(MemoryCache(CacheConfig()), prefix="vectrix")
        cache.set_search_results("memos", [1.0, 0.0], [{"id": "a"}])
        cache.set_search_results("memos_archive", [1.0, 0.0], [{"id": "z"}])
        cache.invalidate_collection("memos")
        assert cache.get_search_results("memos", [1.0, 0.0]) is None
        assert cache.get_search_results("memos_archive", [1.0, 0.0]) == [{"id": "z"}]

    def test_redis_scans_for_the_prefix_and_never_asks_for_every_key(self):
        class Client:
            def __init__(self):
                self.keys_called = False
                self.data = {
                    b"vectrix:vec:q:memos:1": b"1",
                    b"vectrix:vec:q:memos:2": b"1",
                    b"vectrix:vec:q:other:1": b"1",
                }
                self.patterns = []

            def scan(self, cursor, match=None, count=None):
                import fnmatch

                self.patterns.append(match)
                return 0, [k for k in self.data if fnmatch.fnmatchcase(k.decode(), match)]

            def delete(self, *keys):
                for key in keys:
                    self.data.pop(key, None)
                return len(keys)

            def keys(self, *args):  # pragma: no cover - the point is that it is not called
                self.keys_called = True
                return []

        cache = RedisCache.__new__(RedisCache)
        cache.config = CacheConfig(redis_prefix="vectrix:")
        cache._client = Client()
        assert cache.delete_prefix("vec:q:memos:") == 2
        assert (
            list(cache._client.data) == [b"vectrix:vec:q:other:1"] and not cache._client.keys_called
        )
        assert cache._client.patterns == ["vectrix:vec:q:memos:*"]


# ---------------------------------------------------------------- a real store ---

pytest.importorskip("azure.search.documents")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_azure_search import FakeIndexClient  # noqa: E402

from vectrixdb.core.storage import StorageBackend, StorageConfig  # noqa: E402
from vectrixdb.core.storage_azure import AzureSearchStorage  # noqa: E402
from vectrixdb.policy import Overlap, Policy  # noqa: E402


@pytest.fixture
def azure(monkeypatch, tmp_path):
    from vectrixdb import Vectrix
    from vectrixdb.core.database import VectrixDB

    fake = FakeIndexClient()
    storage = AzureSearchStorage(
        StorageConfig(
            backend=StorageBackend.AZURE_SEARCH,
            azure_search_index_prefix="t",
            azure_search_filter_fields={"client_id": "string"},
        ),
        index_client=fake,
        client_factory=fake.get_search_client,
    )
    storage.connect()
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: storage)
    backend = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k")
    policy = Policy([Overlap("client_id", "clients", scope=True)], require_pushdown=True)
    docs = Vectrix(
        "memos",
        storage_backend=backend,
        path=str(tmp_path),
        embed_fn=embed,
        dimension=4,
        policy=policy,
    )
    docs.add(
        ["alpha memo for acme", "alpha memo for zeta"],
        ids=["acme-1", "zeta-1"],
        metadata=[{"client_id": "acme"}, {"client_id": "zeta"}],
    )
    yield docs, fake
    docs.close()


def rows(fake):
    return {d["doc_id"]: d for d in fake.get_index("t-memos").docs.values()}


class TestTheStore:
    def test_a_deleted_document_is_deleted_from_the_service(self, azure):
        docs, fake = azure
        assert set(rows(fake)) == {"acme-1", "zeta-1"}
        docs.delete("acme-1")
        assert set(rows(fake)) == {"zeta-1"}
        assert docs.as_principal({"clients": ["acme"]}).search("alpha", limit=5).items == []

    def test_a_revocation_reaches_the_service_and_takes_effect_there(self, azure):
        docs, fake = azure
        acme = docs.as_principal({"clients": ["acme"]})
        assert [h.id for h in acme.search("alpha", limit=5)] == ["acme-1"]

        docs._collection.update_metadata("acme-1", {"client_id": "quarantine"})

        assert rows(fake)["acme-1"]["f_client_id"] == "quarantine", (
            "the field the service filters on"
        )
        assert acme.search("alpha", limit=5).items == [], "and the search it serves"
        assert [
            h.id for h in docs.as_principal({"clients": ["quarantine"]}).search("alpha", limit=5)
        ] == ["acme-1"]

    def test_a_store_that_refuses_a_delete_leaves_the_document_where_it_was(
        self, azure, monkeypatch
    ):
        docs, fake = azure
        from vectrixdb.exceptions import StorageOperationError

        def refuse(collection, ids):
            raise StorageOperationError("delete_batch", "azure_search", "throttled")

        monkeypatch.setattr(docs._collection._storage_backend, "delete_batch", refuse)
        with pytest.raises(StorageOperationError):
            docs._collection.delete(["acme-1"])
        assert "acme-1" in rows(fake) and docs._collection._count_raw() == 2
