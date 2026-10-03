"""The chunk store: a copy of every chunk that every process writing a collection shares, which the pages read.

Everything here runs against MemoryChunks, a second implementation written
from the protocol alone, so what passes here passes for any store that keeps
the protocol, not only for Cosmos. The Cosmos one is tested against a fake
container in test_chunk_store_cosmos.py.

What is held to. Every write to a collection reaches its store: an add is a
row a chunk with its text, metadata and time, a delete takes rows this
process never wrote, a metadata change is merged into the store's own copy.
A store that refuses is an error, and the collection and the index are left
whole. A database hands every collection it makes or opens a view of its
store, a deleted collection takes its rows, and Vectrix passes its store to
the database it was handed. Where the store lives is the caller's choice,
and an address the library does not know is refused without being repeated.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_chunk_store import MemoryChunks, RefusingChunks  # noqa: E402

from vectrixdb.api import chunk_source  # noqa: E402
from vectrixdb.chunk_store import CosmosChunks, open_chunk_store  # noqa: E402
from vectrixdb.core.collection import Collection  # noqa: E402
from vectrixdb.core.database import VectrixDB  # noqa: E402
from vectrixdb.exceptions import ConfigurationError, StorageOperationError  # noqa: E402

DIM = 4
AXES = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def collection(store=None, name="docs"):
    return Collection(name, DIM, chunk_store=store.collection(name) if store is not None else None)


class TestEveryWriteReachesTheStore:
    def test_an_add_is_a_row_a_chunk_with_its_text_metadata_and_time(self):
        store = MemoryChunks()
        docs = collection(store)
        docs.add(
            ids=["a", "b"],
            vectors=AXES[:2],
            metadata=[{"_vx_doc": "x.pdf", "_vx_quality": 0.9}, {}],
            texts=["alpha", None],
        )
        rows = store.of("docs")
        assert set(rows) == {"a", "b"}
        assert rows["a"]["text"] == "alpha" and rows["a"]["metadata"] == {
            "_vx_doc": "x.pdf",
            "_vx_quality": 0.9,
        }
        assert rows["b"]["text"] == ""
        # One write, one time, and one a page can read as a day in UTC.
        assert rows["a"]["written"] == rows["b"]["written"]
        assert datetime.fromisoformat(rows["a"]["written"]).utcoffset().total_seconds() == 0

    def test_a_delete_takes_rows_this_process_never_wrote(self):
        store = MemoryChunks()
        collection(store).add(ids=["a", "b"], vectors=AXES[:2], texts=["alpha", "beta"])
        # Another instance: the same collection, nothing beside it.
        elsewhere = collection(store)
        assert elsewhere.count() == 0
        elsewhere.delete(["a"])
        assert set(store.of("docs")) == {"b"}

    def test_metadata_changed_here_is_merged_into_the_store(self):
        store = MemoryChunks()
        docs = collection(store)
        docs.add(ids=["a"], vectors=AXES[:1], metadata=[{"k": 1}], texts=["alpha"])
        assert docs.update_metadata("a", {"j": 2}) is True
        assert store.of("docs")["a"]["metadata"] == {"k": 1, "j": 2}

    def test_metadata_changed_by_another_process_is_merged_into_the_stores_copy(self):
        store = MemoryChunks()
        collection(store).add(ids=["a"], vectors=AXES[:1], metadata=[{"k": 1}], texts=["alpha"])
        # This instance never wrote "a", and the store still has it to change.
        assert collection(store).update_metadata("a", {"j": 2}) is True
        assert store.of("docs")["a"]["metadata"] == {"k": 1, "j": 2}
        assert collection(store).update_metadata("absent", {"j": 2}) is False

    def test_replacing_metadata_replaces_it_there_too(self):
        store = MemoryChunks()
        docs = collection(store)
        docs.add(
            ids=["a"], vectors=AXES[:1], metadata=[{"k": 1, "client_id": "td"}], texts=["alpha"]
        )
        docs.update_metadata("a", {"client_id": "rbc"}, merge=False)
        assert store.of("docs")["a"]["metadata"] == {"client_id": "rbc"}

    def test_a_store_that_refuses_fails_the_write_and_leaves_the_collection_whole(self):
        docs = collection(RefusingChunks())
        with pytest.raises(StorageOperationError) as refused:
            docs.add(ids=["a", "b"], vectors=AXES[:2], texts=["alpha", "beta"])
        assert "chunk store" in str(refused.value) and "'docs'" in str(refused.value)
        # Written here and searchable, so writing it again is all a retry needs.
        assert docs.count() == 2
        assert docs.search(query=AXES[0], limit=1).results[0].id == "a"
        with pytest.raises(StorageOperationError):
            docs.delete(["a"])
        with pytest.raises(StorageOperationError):
            docs.update_metadata("b", {"j": 2})

    def test_without_a_store_nothing_is_different(self):
        docs = collection()
        assert docs._chunk_store is None
        docs.add(ids=["a"], vectors=AXES[:1], texts=["alpha"])
        assert docs.update_metadata("a", {"j": 2}) and docs.delete(["a"]) == 1


class TestTheDatabaseGivesEachCollectionItsView:
    def test_two_databases_on_two_paths_share_one_store(self, tmp_path):
        store = MemoryChunks()
        writer = VectrixDB(tmp_path / "writer", chunk_store=store)
        writer.create_collection("docs", DIM).add(
            ids=["a", "b"], vectors=AXES[:2], texts=["alpha", "beta"]
        )
        reader = VectrixDB(tmp_path / "reader", chunk_store=store)
        docs = reader.create_collection("docs", DIM)
        # The table beside the reader is empty. What the pages count is not.
        assert docs.count() == 0
        assert chunk_source.count(docs) == 2
        writer.close()
        reader.close()

    def test_a_collection_opened_again_gets_its_view(self, tmp_path):
        store = MemoryChunks()
        VectrixDB(tmp_path, chunk_store=store).create_collection("docs", DIM)
        reopened = VectrixDB(tmp_path, chunk_store=store).get_collection("docs")
        assert reopened._chunk_store is not None and reopened._chunk_store.name == "docs"
        reopened.add(ids=["a"], vectors=AXES[:1], texts=["alpha"])
        assert set(store.of("docs")) == {"a"}

    def test_a_deleted_collection_takes_its_rows_and_only_its_own(self, tmp_path):
        store = MemoryChunks()
        db = VectrixDB(tmp_path, chunk_store=store)
        db.create_collection("docs", DIM).add(ids=["a"], vectors=AXES[:1], texts=["alpha"])
        db.create_collection("notes", DIM).add(ids=["n"], vectors=AXES[:1], texts=["note"])
        assert db.delete_collection("docs") is True
        assert store.of("docs") == {} and set(store.of("notes")) == {"n"}

    def test_a_store_that_refuses_the_clear_leaves_the_collection_as_it_was(self, tmp_path):
        db = VectrixDB(tmp_path)
        db.create_collection("docs", DIM).add(ids=["a"], vectors=AXES[:1], texts=["alpha"])
        db.use_chunk_store(RefusingChunks())
        with pytest.raises(StorageOperationError):
            db.delete_collection("docs")
        # Nothing was taken apart, so asking again starts from the beginning.
        assert db.has_collection("docs") and db.get_collection("docs").count() == 1
        db.use_chunk_store(MemoryChunks())
        assert db.delete_collection("docs") is True and not db.has_collection("docs")

    def test_a_store_given_later_reaches_the_collections_already_open(self, tmp_path):
        store = MemoryChunks()
        db = VectrixDB(tmp_path)
        docs = db.create_collection("docs", DIM)
        assert db.chunk_store is None
        assert db.use_chunk_store(store) is store and db.chunk_store is store
        docs.add(ids=["a"], vectors=AXES[:1], texts=["alpha"])
        assert set(store.of("docs")) == {"a"}
        db.use_chunk_store(None)
        docs.add(ids=["b"], vectors=AXES[1:2], texts=["beta"])
        assert set(store.of("docs")) == {"a"}


class TestVectrixPassesItOn:
    def test_the_chunks_it_writes_carry_their_build_and_survive_clear(self, tmp_path):
        from vectrixdb import Vectrix

        store = MemoryChunks()
        db = Vectrix("docs", path=str(tmp_path), chunk_store=store)
        db.add(["the first chunk of text", "the second chunk of text"])
        rows = store.of("docs")
        assert len(rows) == 2
        # Stamped before the write, so the Builds page can group them.
        assert {row["metadata"]["_vx_build"] for row in rows.values()} == {db.index_build_id}
        db.clear()
        assert store.of("docs") == {}
        # The collection clear() makes again writes to the store as well.
        db.add(["one more"])
        assert len(store.of("docs")) == 1
        db.close()

    def test_a_database_handed_to_it_is_given_the_store(self, tmp_path):
        from vectrixdb import Vectrix

        store = MemoryChunks()
        handed = VectrixDB()
        db = Vectrix("docs", path=str(tmp_path), storage_backend=handed, chunk_store=store)
        assert handed.chunk_store is store
        db.add(["a chunk of text"])
        assert len(store.of("docs")) == 1
        db.close()


class TestOpening:
    def test_nothing_is_no_store(self):
        assert open_chunk_store(None) is None
        assert open_chunk_store("") is None and open_chunk_store("   ") is None

    def test_a_store_of_your_own_is_used_as_it_is(self):
        store = MemoryChunks()
        assert open_chunk_store(store) is store

    def test_an_object_that_is_not_a_store_is_refused_and_told_what_keep_chunks_is(self, tmp_path):
        from vectrixdb.documents import ChunkStore, LocalFiles

        for wrong in (object(), ChunkStore(LocalFiles(tmp_path))):
            with pytest.raises(TypeError) as refused:
                open_chunk_store(wrong)
            assert "collection(name)" in str(refused.value) and "keep_chunks" in str(refused.value)

    def test_a_cosmos_address_opens_cosmos_with_its_key(self, monkeypatch):
        opened = []
        monkeypatch.setattr(
            CosmosChunks,
            "open",
            classmethod(lambda cls, url, key=None: opened.append((url, key)) or "opened"),
        )
        assert (
            open_chunk_store("cosmos://acct.documents.azure.com/vectrixdb/chunks", key="k")
            == "opened"
        )
        assert opened == [("cosmos://acct.documents.azure.com/vectrixdb/chunks", "k")]

    def test_an_address_it_does_not_know_is_refused_without_repeating_it(self):
        with pytest.raises(ConfigurationError) as refused:
            open_chunk_store("postgresql://reader:hunter2@db.internal/chunks")
        assert "postgresql://" in str(refused.value) and "hunter2" not in str(refused.value)
        with pytest.raises(ConfigurationError) as refused:
            open_chunk_store("./chunks")
        assert "not a path" in str(refused.value)


class TestTheServerReadsItFromTheEnvironment:
    def test_unset_is_none_and_a_store_given_wins(self):
        from vectrixdb.api.server import chunk_store_from_env

        store = MemoryChunks()
        assert chunk_store_from_env(env={}) is None
        assert (
            chunk_store_from_env(
                store, env={"VECTRIXDB_CHUNK_STORE": "cosmos://a.documents.azure.com/d/c"}
            )
            is store
        )

    def test_the_address_is_opened_with_its_key_or_the_file_that_holds_it(
        self, monkeypatch, tmp_path
    ):
        from vectrixdb.api.server import chunk_store_from_env

        opened = []
        monkeypatch.setattr(
            CosmosChunks,
            "open",
            classmethod(lambda cls, url, key=None: opened.append((url, key)) or "opened"),
        )
        where = "cosmos://a.documents.azure.com/vectrixdb/chunks"
        assert (
            chunk_store_from_env(
                env={"VECTRIXDB_CHUNK_STORE": where, "VECTRIXDB_CHUNK_STORE_KEY": "inline"}
            )
            == "opened"
        )
        (tmp_path / "key").write_text("from-a-file\n", encoding="utf-8")
        chunk_store_from_env(
            env={
                "VECTRIXDB_CHUNK_STORE": where,
                "VECTRIXDB_CHUNK_STORE_KEY_FILE": str(tmp_path / "key"),
            }
        )
        chunk_store_from_env(env={"VECTRIXDB_CHUNK_STORE": where})
        assert opened == [(where, "inline"), (where, "from-a-file"), (where, None)]
        with pytest.raises(ConfigurationError):
            chunk_store_from_env(
                env={
                    "VECTRIXDB_CHUNK_STORE": where,
                    "VECTRIXDB_CHUNK_STORE_KEY": "a",
                    "VECTRIXDB_CHUNK_STORE_KEY_FILE": str(tmp_path / "key"),
                }
            )


class TestCheck:
    def _storage(self, tmp_path, where):
        from vectrixdb.check import run

        return [
            f
            for f in run(
                path=str(tmp_path), env={"VECTRIXDB_CHUNK_STORE": where, "VECTRIXDB_OFFLINE": "1"}
            )
            if "CHUNK_STORE" in f.text or "every chunk" in f.text
        ]

    def test_a_cosmos_address_is_named(self, tmp_path, monkeypatch):
        import importlib.util

        found = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a: object() if name == "azure.cosmos" else found(name, *a),
        )
        (finding,) = self._storage(tmp_path, "cosmos://acct.documents.azure.com/vectrixdb/chunks")
        assert (
            finding.level == "ok"
            and "Cosmos DB acct.documents.azure.com/vectrixdb/chunks" in finding.text
        )

    def test_anything_else_is_an_error(self, tmp_path, monkeypatch):
        import importlib.util

        # The SDK is there, so the address alone decides.
        found = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a: object() if name == "azure.cosmos" else found(name, *a),
        )
        for wrong in (
            "./chunks",
            "cosmos://acct.documents.azure.com/only-a-database",
            "cosmos://acct.documents.azure.com/a/b/c",
            "s3://bucket/chunks",
        ):
            (finding,) = self._storage(tmp_path, wrong)
            assert finding.level == "error" and "is not cosmos://" in finding.text, wrong

    def test_the_sdk_missing_is_an_error(self, tmp_path, monkeypatch):
        import importlib.util

        found = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a: None if name == "azure.cosmos" else found(name, *a),
        )
        (finding,) = self._storage(tmp_path, "cosmos://acct.documents.azure.com/vectrixdb/chunks")
        assert finding.level == "error" and "azure-cosmos" in finding.text
