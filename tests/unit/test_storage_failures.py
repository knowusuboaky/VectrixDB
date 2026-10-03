"""Storage backends must fail loudly.

Before 2.2.0 every cloud backend wrapped its reads in a bare ``except:`` and
returned ``None``, ``[]`` or ``0``. That made an unreachable database
indistinguishable from an empty collection, which is the worst failure mode a
database client can have: the caller cheerfully carries on with no data.

These tests pin the new contract. Backends are driven with fakes rather than live
services, so they run everywhere and still exercise the real handler code.
"""

import sqlite3

import pytest

from vectrixdb import StorageConfig, StorageBackend, SQLiteStorage
from vectrixdb.exceptions import StorageOperationError, VectrixError
from vectrixdb.core.storage import _is_not_found, _sql_literal


class Boom(Exception):
    """Stands in for a driver-level failure: auth rejected, socket closed, throttled."""


class NotFound(Exception):
    """Stands in for an SDK's typed 'resource does not exist' error."""

    status_code = 404


class TestNotFoundProbe:
    """`_is_not_found` decides absence from failure; everything depends on it."""

    def test_recognises_status_code_404(self):
        assert _is_not_found(NotFound())

    def test_recognises_sdk_class_names(self):
        for name in (
            "CosmosResourceNotFoundError",
            "ResourceNotFoundError",
            "NotFoundError",
            "NoSuchTableError",
        ):
            exc = type(name, (Exception,), {})()
            assert _is_not_found(exc), f"{name} should count as absence"

    def test_does_not_treat_arbitrary_failures_as_absence(self):
        assert not _is_not_found(Boom("connection reset"))
        assert not _is_not_found(TimeoutError())
        assert not _is_not_found(PermissionError("403 Forbidden"))

    def test_nested_response_status_is_honoured(self):
        class WithResponse(Exception):
            response = type("R", (), {"status_code": 404})()

        assert _is_not_found(WithResponse())


class TestCosmosReadsRaise:
    """A Cosmos read that fails must raise, not report an empty result."""

    def _backend(self, container):
        from vectrixdb.core.storage import CosmosDBStorage

        storage = CosmosDBStorage.__new__(CosmosDBStorage)
        storage._get_container = lambda name: container
        storage.config = StorageConfig(backend=StorageBackend.COSMOSDB)
        return storage

    def test_get_raises_on_backend_failure(self):
        class Container:
            def read_item(self, **kw):
                raise Boom("TLS handshake failed")

        storage = self._backend(Container())
        with pytest.raises(StorageOperationError) as excinfo:
            storage.get("docs", "id-1")
        assert "CosmosDB" in str(excinfo.value)
        assert isinstance(excinfo.value, VectrixError)

    def test_get_returns_none_for_a_genuine_miss(self):
        class Container:
            def read_item(self, **kw):
                raise NotFound()

        assert self._backend(Container()).get("docs", "missing") is None

    def test_list_documents_raises_rather_than_returning_empty(self):
        class Container:
            def query_items(self, *a, **kw):
                raise Boom("throttled: 429")

        with pytest.raises(StorageOperationError):
            self._backend(Container()).list_documents()

    def test_delete_reports_false_only_for_a_real_miss(self):
        class Missing:
            def delete_item(self, **kw):
                raise NotFound()

        class Broken:
            def delete_item(self, **kw):
                raise Boom("quota exceeded")

        assert self._backend(Missing()).delete("docs", "gone") is False
        with pytest.raises(StorageOperationError):
            self._backend(Broken()).delete("docs", "id-1")

    def test_error_chains_the_original_cause(self):
        """`raise ... from exc` must be preserved so tracebacks stay diagnosable."""

        class Container:
            def read_item(self, **kw):
                raise Boom("root cause")

        with pytest.raises(StorageOperationError) as excinfo:
            self._backend(Container()).get("docs", "id-1")
        assert isinstance(excinfo.value.__cause__, Boom)
        assert "root cause" in str(excinfo.value.__cause__)


class TestOpenSearchReadsRaise:
    def _backend(self, client):
        from vectrixdb.core.storage import OpenSearchStorage

        storage = OpenSearchStorage.__new__(OpenSearchStorage)
        storage._client = client
        storage.config = StorageConfig(
            backend=StorageBackend.OPENSEARCH, opensearch_index_prefix="vx"
        )
        storage._index_name = lambda c: f"vx_{c}"
        return storage

    def test_count_raises_on_failure(self):
        class Client:
            def count(self, **kw):
                raise Boom("cluster_block_exception")

        with pytest.raises(StorageOperationError):
            self._backend(Client()).count("docs")

    def test_count_is_zero_for_a_missing_index(self):
        class Client:
            def count(self, **kw):
                raise NotFound()

        assert self._backend(Client()).count("docs") == 0

    def test_get_raises_on_failure(self):
        class Client:
            def search(self, **kw):
                raise Boom("no route to host")

        with pytest.raises(StorageOperationError):
            self._backend(Client()).get("docs", "id-1")


class _ConnProxy:
    """Delegates to a real sqlite3 connection, but lets `execute` misbehave.

    sqlite3.Connection is a C type and rejects attribute assignment, so the
    connection cache is swapped for this instead of patching the object.
    """

    def __init__(self, conn, on_execute):
        self._conn = conn
        self._on_execute = on_execute

    def execute(self, sql, *args, **kwargs):
        intercepted = self._on_execute(sql)
        if intercepted is not None:
            raise intercepted
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestSQLiteCleanupStaysTolerant:
    """Best-effort cleanup should stay best-effort, but must not swallow everything."""

    @staticmethod
    def _storage_with(temp_dir, on_execute):
        storage = SQLiteStorage(
            StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=temp_dir, sqlite_wal_mode=True)
        )
        storage.connect()
        storage.create_collection("docs", {"dimension": 4})
        # close() walks the cross-thread registry, so patch that.
        storage._all_connections = [
            (name, _ConnProxy(conn, on_execute)) for name, conn in storage._all_connections
        ]
        return storage

    def test_close_survives_a_failing_wal_checkpoint(self, temp_dir):
        """A checkpoint failure on shutdown is genuinely non-fatal and stays swallowed."""
        storage = self._storage_with(
            temp_dir,
            lambda sql: (
                sqlite3.OperationalError("database is locked") if "wal_checkpoint" in sql else None
            ),
        )
        storage.close()  # must not propagate

    def test_close_does_not_swallow_keyboard_interrupt(self, temp_dir):
        """The old bare `except:` caught KeyboardInterrupt. A narrow one must not."""
        storage = self._storage_with(
            temp_dir,
            lambda sql: KeyboardInterrupt() if "wal_checkpoint" in sql else None,
        )
        with pytest.raises(KeyboardInterrupt):
            storage.close()


class TestSQLLiteralEscaping:
    """Databricks SQL interpolates ids into query text; escaping must neutralise them."""

    def test_plain_values_are_unchanged(self):
        assert _sql_literal("doc-123") == "doc-123"

    def test_quotes_are_doubled(self):
        assert _sql_literal("o'brien") == "o''brien"

    @staticmethod
    def _escapes_cleanly(raw: str) -> bool:
        """Every quote doubled means no quote can terminate the enclosing literal.

        Verified by walking runs of quotes, each of which must have even length,
        and by confirming that unescaping returns exactly the original input.
        A substring check is the wrong tool: "x'' OR ''1" legitimately contains
        "' OR '" while being perfectly safe.
        """
        escaped = _sql_literal(raw)
        i = 0
        while i < len(escaped):
            if escaped[i] == "'":
                run = len(escaped[i:]) - len(escaped[i:].lstrip("'"))
                if run % 2 != 0:
                    return False
                i += run
            else:
                i += 1
        return escaped.replace("''", "'") == raw

    @pytest.mark.parametrize(
        "payload",
        [
            "x' OR '1'='1",
            "'; DROP TABLE users; --",
            "' UNION SELECT * FROM secrets --",
            "admin'--",
            "'" * 4,
            "no quotes at all",
        ],
    )
    def test_injection_payloads_cannot_escape_the_literal(self, payload):
        assert self._escapes_cleanly(payload), f"{payload!r} escaped its literal"

    def test_backslashes_are_doubled(self):
        assert _sql_literal("a\\b") == "a\\\\b"

    def test_control_characters_are_rejected(self):
        for payload in ("bad\nvalue", "bad\rvalue", "bad\x00value"):
            with pytest.raises(VectrixError):
                _sql_literal(payload)

    def test_non_strings_are_coerced(self):
        assert _sql_literal(42) == "42"
