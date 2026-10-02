"""Durability and concurrency for the SQLite backend.

These are the failures that burn production users and that a suite finishing in
three seconds never sees: data that does not survive a reopen, and writers that
corrupt each other. SQLite is the default backend, so this is the path almost
every user is actually on.
"""

import threading

import pytest

from vectrixdb import SQLiteStorage, StorageBackend, StorageConfig


@pytest.fixture
def config(temp_dir):
    return StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=temp_dir, sqlite_wal_mode=True)


def _open(config) -> SQLiteStorage:
    storage = SQLiteStorage(config)
    storage.connect()
    return storage


class TestDurability:
    """What is written must still be there after the process lets go of the file."""

    def test_data_survives_close_and_reopen(self, config):
        storage = _open(config)
        storage.create_collection("docs", {"dimension": 4})
        storage.insert("docs", "a", {"text": "first", "n": 1})
        storage.insert("docs", "b", {"text": "second", "n": 2})
        storage.flush()
        storage.close()

        reopened = _open(config)
        try:
            assert reopened.count("docs") == 2
            assert reopened.get("docs", "a")["text"] == "first"
            assert reopened.get("docs", "b")["n"] == 2
        finally:
            reopened.close()

    def test_collection_metadata_survives_reopen(self, config):
        storage = _open(config)
        storage.create_collection("docs", {"dimension": 384, "metric": "cosine"})
        storage.close()

        reopened = _open(config)
        try:
            assert "docs" in reopened.list_collections()
            stored = reopened.get_collection_config("docs")
            assert stored["dimension"] == 384
            assert stored["metric"] == "cosine"
        finally:
            reopened.close()

    def test_deletes_survive_reopen(self, config):
        storage = _open(config)
        storage.create_collection("docs", {"dimension": 4})
        for i in range(5):
            storage.insert("docs", f"id{i}", {"n": i})
        assert storage.delete("docs", "id2") is True
        storage.close()

        reopened = _open(config)
        try:
            assert reopened.count("docs") == 4
            assert reopened.get("docs", "id2") is None
        finally:
            reopened.close()

    def test_abrupt_close_without_flush_keeps_committed_rows(self, config):
        """Simulates the process going away without an orderly shutdown.

        WAL mode is enabled precisely so that committed writes survive this.
        """
        storage = _open(config)
        storage.create_collection("docs", {"dimension": 4})
        for i in range(20):
            storage.insert("docs", f"id{i}", {"n": i})

        # Drop the handles without calling close(): no checkpoint, no flush.
        storage._connections.clear()

        reopened = _open(config)
        try:
            assert reopened.count("docs") == 20
        finally:
            reopened.close()

    def test_reopen_of_an_untouched_database_is_empty_not_broken(self, config):
        storage = _open(config)
        storage.create_collection("docs", {"dimension": 4})
        storage.close()

        reopened = _open(config)
        try:
            assert reopened.count("docs") == 0
            assert reopened.get("docs", "nothing") is None
        finally:
            reopened.close()


class TestConcurrency:
    """Concurrent writers must not lose or corrupt rows."""

    def test_parallel_inserts_all_land(self, config):
        storage = _open(config)
        storage.create_collection("docs", {"dimension": 4})

        writers, per_writer = 4, 25
        errors = []

        def write(worker):
            try:
                for i in range(per_writer):
                    storage.insert("docs", f"w{worker}-{i}", {"worker": worker, "i": i})
            except Exception as exc:  # noqa: BLE001 - surfaced via the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        try:
            assert not errors, f"concurrent writers raised: {errors[:3]}"
            assert storage.count("docs") == writers * per_writer
            # Spot-check that rows belong to the writer that wrote them.
            row = storage.get("docs", "w2-7")
            assert row["worker"] == 2 and row["i"] == 7
        finally:
            storage.close()

    def test_reads_during_writes_never_see_torn_rows(self, config):
        storage = _open(config)
        storage.create_collection("docs", {"dimension": 4})
        for i in range(50):
            storage.insert("docs", f"seed{i}", {"n": i, "text": "x" * 32})

        problems = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                row = storage.get("docs", "seed10")
                if row is not None and (row.get("n") != 10 or row.get("text") != "x" * 32):
                    problems.append(row)

        def writer():
            for i in range(50, 150):
                storage.insert("docs", f"new{i}", {"n": i, "text": "y" * 32})

        r = threading.Thread(target=reader, daemon=True)
        w = threading.Thread(target=writer)
        r.start()
        w.start()
        w.join(timeout=60)
        stop.set()
        r.join(timeout=10)

        try:
            assert not problems, f"reader observed inconsistent rows: {problems[:2]}"
            assert storage.count("docs") == 150
        finally:
            storage.close()

    def test_concurrent_collection_creation_is_safe(self, config):
        storage = _open(config)
        errors = []

        def make(n):
            try:
                storage.create_collection(f"c{n}", {"dimension": 4})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=make, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        try:
            assert not errors, f"concurrent create_collection raised: {errors[:3]}"
            assert set(storage.list_collections()) >= {f"c{n}" for n in range(8)}
        finally:
            storage.close()


class TestDocumentTableSchema:
    """The document index needs its own `documents` schema in _documents.db.

    Before 2.2.0, `_get_connection` created a generic `documents` table
    (id, data, created_at, updated_at) on *every* database including the internal
    ones. `ensure_document_tables` then ran CREATE TABLE IF NOT EXISTS for the
    real schema, which did nothing because a table of that name already existed,
    and its follow-up index failed with "no such column: doc_type".

    The whole document index was therefore dead on SQLite, and the bare `except:`
    in `list_documents` reported it as an empty list, so the dashboard showed
    "No documents indexed" forever instead of an error.
    """

    def test_internal_databases_do_not_get_the_generic_schema(self, config):
        storage = _open(config)
        try:
            conn = storage._get_connection("_documents")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "documents" not in tables, (
                "the placeholder `documents` table would shadow the document index schema"
            )
        finally:
            storage.close()

    def test_real_collections_still_get_the_generic_schema(self, config):
        storage = _open(config)
        try:
            storage.create_collection("docs", {"dimension": 4})
            columns = {
                row[1]
                for row in storage._get_connection("docs")
                .execute("PRAGMA table_info(documents)")
                .fetchall()
            }
            assert {"id", "data", "created_at", "updated_at"} <= columns
        finally:
            storage.close()

    def test_ensure_document_tables_creates_the_right_columns(self, config):
        storage = _open(config)
        try:
            storage.ensure_document_tables()
            columns = {
                row[1]
                for row in storage._get_connection("_documents")
                .execute("PRAGMA table_info(documents)")
                .fetchall()
            }
            assert "doc_type" in columns
            assert "doc_id" in columns
        finally:
            storage.close()

    def test_legacy_placeholder_table_is_migrated(self, config):
        """A database written by <= 2.1.7 must start working after the upgrade."""
        storage = _open(config)
        try:
            # Recreate the pre-2.2.0 state by hand.
            conn = storage._get_connection("_documents")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT
                );
                """
            )
            conn.commit()

            storage.ensure_document_tables()

            columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
            assert "doc_type" in columns, "legacy placeholder table was not replaced"
        finally:
            storage.close()

    def test_a_populated_legacy_table_is_never_dropped(self, config):
        """If the placeholder somehow holds rows, refuse rather than destroy them."""
        from vectrixdb.exceptions import StorageOperationError

        storage = _open(config)
        try:
            conn = storage._get_connection("_documents")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT
                );
                """
            )
            conn.execute("INSERT INTO documents (id, data) VALUES ('x', '{}')")
            conn.commit()

            with pytest.raises(StorageOperationError, match="legacy"):
                storage.ensure_document_tables()

            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        finally:
            storage.close()

    def test_user_collections_starting_with_underscore_still_work(self, config):
        """An underscore prefix is not reserved.

        The first version of the internal-database rule keyed on a leading
        underscore, so a collection literally named "_notes" silently got no
        `documents` table and every insert failed with "no such table".
        """
        storage = _open(config)
        try:
            storage.create_collection("_notes", {"dimension": 4})
            storage.insert("_notes", "a", {"text": "hello"})
            assert storage.count("_notes") == 1
            assert storage.get("_notes", "a")["text"] == "hello"
        finally:
            storage.close()
