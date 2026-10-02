"""One contract, every storage backend.

The backends were written one at a time and diverged: Delta Lake could not be
instantiated, `get()` read a column its own schema did not have, and nothing
noticed because each backend had its own partial tests or none. This suite is
the definition of "a storage backend": every backend runs the same assertions.

Local backends run here. Cloud backends run the same suite when their
connection variables are set (see LIVE below); otherwise those parameters skip
with a message rather than pretending.
"""

import os
import sys
from pathlib import Path

import pytest

from vectrixdb.core.storage import (
    BaseStorage,
    InMemoryStorage,
    SQLiteStorage,
    StorageBackend,
    StorageConfig,
    create_storage,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOSTILE_ID = "id with spaces, 'quotes', \"doubles\", unicode ünï, and ;--"

# Cloud backends: name -> (backend, config builder). Each is skipped unless
# VECTRIXDB_LIVE_BACKENDS lists it and its variables are present.
LIVE = {
    "cosmosdb": (
        StorageBackend.COSMOSDB,
        lambda: StorageConfig(
            backend=StorageBackend.COSMOSDB,
            cosmos_endpoint=os.environ["VECTRIXDB_COSMOS_ENDPOINT"],
            cosmos_key=os.environ["VECTRIXDB_COSMOS_KEY"],
            cosmos_database=os.environ.get("VECTRIXDB_COSMOS_DATABASE", "vectrixdb_test"),
        ),
    ),
    "delta_lake": (
        StorageBackend.DELTA_LAKE,
        lambda: StorageConfig(
            backend=StorageBackend.DELTA_LAKE,
            delta_workspace_url=os.environ["VECTRIXDB_DELTA_WORKSPACE_URL"],
            delta_token=os.environ["VECTRIXDB_DELTA_TOKEN"],
            delta_catalog=os.environ.get("VECTRIXDB_DELTA_CATALOG", "main"),
            delta_schema=os.environ.get("VECTRIXDB_DELTA_SCHEMA", "vectrixdb_test"),
            delta_warehouse_id=os.environ.get("VECTRIXDB_DELTA_WAREHOUSE_ID"),
        ),
    ),
}


LIVE["azure_search"] = (
    StorageBackend.AZURE_SEARCH,
    lambda: StorageConfig(
        backend=StorageBackend.AZURE_SEARCH,
        azure_search_endpoint=os.environ["VECTRIXDB_AZURE_SEARCH_ENDPOINT"],
        azure_search_key=os.environ.get("VECTRIXDB_AZURE_SEARCH_KEY"),
        azure_search_index_prefix=os.environ.get("VECTRIXDB_AZURE_SEARCH_PREFIX", "vxtest"),
    ),
)

# Lakebase live: a Databricks Lakebase instance, which is PostgreSQL with
# pgvector. A token is the usual credential; a password works too.
LIVE["lakebase"] = (
    StorageBackend.LAKEBASE,
    lambda: StorageConfig(
        backend=StorageBackend.LAKEBASE,
        lakebase_host=os.environ["VECTRIXDB_LAKEBASE_HOST"],
        lakebase_port=int(os.environ.get("VECTRIXDB_LAKEBASE_PORT", "5432")),
        lakebase_database=os.environ.get("VECTRIXDB_LAKEBASE_DATABASE", "vectrixdb"),
        lakebase_user=os.environ.get("VECTRIXDB_LAKEBASE_USER"),
        lakebase_password=os.environ.get("VECTRIXDB_LAKEBASE_PASSWORD"),
        lakebase_token=os.environ.get("VECTRIXDB_LAKEBASE_TOKEN"),
        lakebase_schema=os.environ.get("VECTRIXDB_LAKEBASE_SCHEMA", "vectrixdb_test"),
    ),
)

# Aurora PostgreSQL live: any PostgreSQL with pgvector, which is what this
# backend actually speaks, Aurora or not.
LIVE["aurora_postgresql"] = (
    StorageBackend.AURORA_POSTGRESQL,
    lambda: StorageConfig(
        backend=StorageBackend.AURORA_POSTGRESQL,
        aurora_host=os.environ["VECTRIXDB_AURORA_HOST"],
        aurora_port=int(os.environ.get("VECTRIXDB_AURORA_PORT", "5432")),
        aurora_database=os.environ.get("VECTRIXDB_AURORA_DATABASE", "vectrixdb"),
        aurora_user=os.environ["VECTRIXDB_AURORA_USER"],
        aurora_password=os.environ["VECTRIXDB_AURORA_PASSWORD"],
        aurora_schema=os.environ.get("VECTRIXDB_AURORA_SCHEMA", "vectrixdb_test"),
    ),
)

# OpenSearch live: an AWS domain or Serverless collection. Credentials come
# from the usual boto3 chain (environment, profile, instance role); only the
# endpoint is required here. The mocked entry below runs on every push.
LIVE["opensearch"] = (
    StorageBackend.OPENSEARCH,
    lambda: StorageConfig(
        backend=StorageBackend.OPENSEARCH,
        opensearch_endpoint=os.environ["VECTRIXDB_OPENSEARCH_ENDPOINT"],
        opensearch_region=os.environ.get("VECTRIXDB_OPENSEARCH_REGION", "us-east-1"),
        opensearch_service=os.environ.get("VECTRIXDB_OPENSEARCH_SERVICE", "aoss"),
        opensearch_index_prefix=os.environ.get("VECTRIXDB_OPENSEARCH_PREFIX", "vxtest"),
    ),
)


def _azure_fake(tmp_path) -> BaseStorage:
    """The Azure backend over an in-memory stand-in for the SDK clients.

    Exercises every line of the backend except the HTTP calls, so the
    contract holds before a service is ever provisioned; the live entry
    above runs the same suite against a real one when its variables are set.
    """
    pytest.importorskip("azure.search.documents")
    from fake_azure_search import FakeIndexClient

    from vectrixdb.core.storage_azure import AzureSearchStorage

    fake = FakeIndexClient()
    storage = AzureSearchStorage(
        StorageConfig(backend=StorageBackend.AZURE_SEARCH, azure_search_index_prefix="t"),
        index_client=fake,
        client_factory=fake.get_search_client,
    )
    return storage


def _opensearch_fake(tmp_path) -> BaseStorage:
    """The OpenSearch backend over an in-memory stand-in for opensearch-py.

    The backend was only ever instantiated in this file, never driven through
    the contract, because it needs an AWS domain to talk to. The stand-in
    answers the same calls with the service's observable behaviour, so the
    contract runs on every push; the live entry runs the same suite against a
    real domain when VECTRIXDB_LIVE_BACKENDS names it.
    """
    from fake_opensearch import FakeOpenSearch

    from vectrixdb.core.storage import OpenSearchStorage

    return OpenSearchStorage(
        StorageConfig(backend=StorageBackend.OPENSEARCH, opensearch_index_prefix="t"),
        client=FakeOpenSearch(),
    )


def _delta_fake(tmp_path) -> BaseStorage:
    """The Delta Lake backend over an in-memory stand-in for databricks-sql-connector.

    DeltaLakeStorage.__init__ takes only a config: unlike the Azure and
    OpenSearch backends it has no client=/connection= constructor seam, so
    the fake cannot be handed in that way. connect() is replaced instead. The
    replacement runs the exact bootstrap statements the real connect() runs
    (CREATE SCHEMA, then the three CREATE TABLE statements), copied here
    because a monkeypatched connect() cannot call the original it replaces;
    from that point on every statement create_collection/insert/get/... sends
    is the backend's real, unmodified SQL, executed by FakeDatabricksConnection.
    """
    from fake_databricks import FakeDatabricksConnection

    from vectrixdb.core.storage import DeltaLakeStorage

    storage = DeltaLakeStorage(
        StorageConfig(
            backend=StorageBackend.DELTA_LAKE,
            delta_workspace_url="https://fake.cloud.databricks.com",
            delta_token="fake-token",
            delta_catalog="main",
            delta_schema="t",
        )
    )

    def _fake_connect() -> None:
        storage._conn = FakeDatabricksConnection()
        storage._cursor = storage._conn.cursor()
        storage._cursor.execute(
            f"CREATE SCHEMA IF NOT EXISTS `{storage._catalog}`.`{storage._schema}`"
        )
        storage._cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {storage._full_table_name("_vectrix_collections")} (
                name STRING NOT NULL,
                config STRING,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            ) USING DELTA
        """)
        storage._cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {storage._full_table_name("_vectrix_documents")} (
                doc_id STRING NOT NULL,
                title STRING,
                doc_type STRING,
                source_path STRING,
                etag STRING,
                content_hash STRING,
                page_count INT,
                section_count INT,
                node_count INT,
                indexed_at TIMESTAMP,
                last_synced TIMESTAMP,
                metadata STRING
            ) USING DELTA
        """)
        storage._cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {storage._full_table_name("_vectrix_nodes")} (
                node_id STRING NOT NULL,
                doc_id STRING NOT NULL,
                parent_id STRING,
                level INT,
                title STRING,
                text STRING,
                summary STRING,
                page_num INT,
                position INT,
                metadata STRING
            ) USING DELTA
        """)

    storage.connect = _fake_connect
    return storage


def _cosmos_fake(tmp_path) -> BaseStorage:
    """The Cosmos DB backend over an in-memory stand-in for azure-cosmos.

    CosmosDBStorage.__init__ takes only a config, with no seam for injecting
    a client the way AzureSearchStorage (index_client/client_factory) and
    OpenSearchStorage (client=) do, so connect() is monkeypatched here
    instead: it wires up self._client/self._database/self._containers by
    hand, using the same idempotent create_database_if_not_exists /
    create_container_if_not_exists the fake exposes for that purpose, and
    is never asked to reach a real Cosmos account. Every method after that,
    create_collection, insert, get, scan, and the rest, runs the real
    backend code unchanged against the fake, exactly as it would against a
    real Cosmos DB account.

    azure-cosmos itself does not need to be installed: importing fake_cosmos
    registers a stand-in azure.cosmos / azure.cosmos.exceptions in
    sys.modules, so the backend's own lazy imports of PartitionKey and
    CosmosResourceExistsError resolve without it. See fake_cosmos.py.
    """
    from fake_cosmos import FakeCosmosClient, PartitionKey

    from vectrixdb.core.storage import CosmosDBStorage

    storage = CosmosDBStorage(StorageConfig(backend=StorageBackend.COSMOSDB, cosmos_database="t"))

    def _connect() -> None:
        storage._client = FakeCosmosClient()
        storage._database = storage._client.create_database_if_not_exists(
            storage.config.cosmos_database
        )
        storage._containers["_meta"] = storage._database.create_container_if_not_exists(
            id="_meta", partition_key=PartitionKey(path="/type")
        )

    storage.connect = _connect
    return storage


def _lakebase_fake(tmp_path) -> BaseStorage:
    """The Lakebase backend over an in-memory stand-in for psycopg2.

    LakebaseStorage.__init__ takes only a config: no client=/conn=/
    connection= seam the way AzureSearchStorage and OpenSearchStorage have,
    so connect() is monkeypatched here instead, the same way _delta_fake and
    _cosmos_fake do it above. The replacement runs the exact bootstrap
    statements the real connect() runs (CREATE EXTENSION, CREATE SCHEMA,
    then the _vectrix_collections CREATE TABLE), copied here because a
    monkeypatched connect() cannot call the original it replaces; from that
    point on every statement create_collection/insert/get/... sends is the
    backend's real, unmodified SQL, executed by FakeConnection.

    psycopg2 itself does not need to be installed: connect() is the only
    place either backend imports it, and connect() is exactly what this
    replaces, so that import is never reached. See fake_postgres.py.
    """
    from fake_postgres import FakeConnection

    from vectrixdb.core.storage import LakebaseStorage

    storage = LakebaseStorage(StorageConfig(backend=StorageBackend.LAKEBASE, lakebase_schema="t"))

    def _connect() -> None:
        storage._conn = FakeConnection()
        storage._conn.autocommit = False
        with storage._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            storage._conn.commit()
        schema = storage.config.lakebase_schema or "public"
        collections_table = f'"{schema}"._vectrix_collections'
        with storage._conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {collections_table} (
                    name TEXT PRIMARY KEY,
                    dimension INTEGER,
                    description TEXT,
                    config JSONB,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            storage._conn.commit()

    storage.connect = _connect
    return storage


def _aurora_fake(tmp_path) -> BaseStorage:
    """The Aurora PostgreSQL backend over the same in-memory psycopg2 stand-in.

    AuroraPostgreSQLStorage.__init__ also takes only a config, with the same
    missing seam as Lakebase, so connect() is monkeypatched the same way:
    the replacement runs the real connect()'s bootstrap (CREATE EXTENSION,
    then the _vectrix_collections CREATE TABLE), copied here for the same
    reason a monkeypatched connect() cannot call the original. Both backends
    speak plain PostgreSQL plus pgvector, so fake_postgres.py serves both;
    see _lakebase_fake above for why psycopg2 need not be installed.
    """
    from fake_postgres import FakeConnection

    from vectrixdb.core.storage import AuroraPostgreSQLStorage

    storage = AuroraPostgreSQLStorage(
        StorageConfig(backend=StorageBackend.AURORA_POSTGRESQL, aurora_schema="t")
    )

    def _connect() -> None:
        storage._conn = FakeConnection()
        storage._conn.autocommit = True
        with storage._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {storage.config.aurora_schema}._vectrix_collections (
                    name VARCHAR(255) PRIMARY KEY,
                    dimension INTEGER,
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

    storage.connect = _connect
    return storage


def _live_enabled(name: str) -> bool:
    wanted = {n.strip() for n in os.environ.get("VECTRIXDB_LIVE_BACKENDS", "").split(",")}
    return name in wanted


def _make(kind: str, tmp_path) -> BaseStorage:
    if kind == "memory":
        storage = InMemoryStorage(StorageConfig(backend=StorageBackend.MEMORY))
    elif kind == "sqlite":
        storage = SQLiteStorage(
            StorageConfig(backend=StorageBackend.SQLITE, sqlite_path=str(tmp_path))
        )
    elif kind == "azure_fake":
        storage = _azure_fake(tmp_path)
    elif kind == "opensearch_fake":
        storage = _opensearch_fake(tmp_path)
    elif kind == "delta_lake_fake":
        storage = _delta_fake(tmp_path)
    elif kind == "cosmosdb_fake":
        storage = _cosmos_fake(tmp_path)
    elif kind == "lakebase_fake":
        storage = _lakebase_fake(tmp_path)
    elif kind == "aurora_fake":
        storage = _aurora_fake(tmp_path)
    else:
        if not _live_enabled(kind):
            pytest.skip(f"set VECTRIXDB_LIVE_BACKENDS={kind} and its variables to run this live")
        backend, build = LIVE[kind]
        try:
            storage = create_storage(build())
        except KeyError as missing:
            pytest.skip(f"{kind}: {missing} is not set")
    connect = getattr(storage, "connect", None)
    if callable(connect):
        connect()
    return storage


KINDS = [
    "memory",
    "sqlite",
    "azure_fake",
    "opensearch_fake",
    "delta_lake_fake",
    "cosmosdb_fake",
    "lakebase_fake",
    "aurora_fake",
] + [pytest.param(name, marks=pytest.mark.backend) for name in sorted(LIVE)]


@pytest.fixture(params=KINDS)
def storage(request, tmp_path):
    s = _make(request.param, tmp_path)
    yield s
    close = getattr(s, "close", None)
    if callable(close):
        close()


COLL = "contract"


@pytest.fixture
def collection(storage):
    storage.create_collection(COLL, {"mode": "dense", "dimension": 4})
    return COLL


def _doc(i, **extra):
    return {"_embedding": [float(i), 1.0, 0.0, 0.0], "text_content": f"text {i}", "i": i, **extra}


def _vector(doc):
    """The stored vector, whichever key the backend returns it under.

    Collection reads ``dense_embedding`` first and ``_embedding`` second, so
    both spellings are part of the contract; a backend may pick either.
    """
    vector = doc.get("dense_embedding")
    if vector is None:
        vector = doc.get("_embedding")
    assert vector is not None, f"no vector in {sorted(doc)}"
    return [float(x) for x in vector]


# Bugs in DeltaLakeStorage that running it against fake_databricks surfaced
# for the first time (see vectrixdb/core/storage.py; fixing them there is out
# of scope for the fake this file wires in, which only reports what it
# found). Keyed by test name rather than applied with a parametrize mark,
# because each bug belongs to the delta_lake_fake entry in KINDS, not to the
# test in isolation: every other backend must keep running these same
# assertions for real.
# every bug this table held is fixed; the contract runs for real
_DELTA_LAKE_KNOWN_BUGS = {}


@pytest.fixture(autouse=True)
def _xfail_known_delta_lake_bugs(request):
    """xfail(strict=True) the specific assertions delta_lake_fake trips over.

    Imperative rather than a static mark so it can be scoped to one KINDS
    entry: ``request.node.add_marker`` during fixture setup is seen by pytest
    before the test body runs, which is what lets a dynamically-added
    ``xfail`` still catch the failure that follows.
    """
    callspec = getattr(request.node, "callspec", None)
    if callspec is None or callspec.params.get("storage") != "delta_lake_fake":
        return
    reason = _DELTA_LAKE_KNOWN_BUGS.get(request.node.originalname)
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))


# Bugs in CosmosDBStorage that running it against fake_cosmos surfaced for the
# first time (see vectrixdb/core/storage.py; fixing them there is out of
# scope for the fake this file wires in, which only reports what it found).
# Keyed by test name rather than applied with a parametrize mark, for the
# same reason as _DELTA_LAKE_KNOWN_BUGS above: each bug belongs to the
# cosmosdb_fake entry in KINDS, not to the test in isolation, so every other
# backend must keep running these same assertions for real.
# every bug this table held is fixed; the contract runs for real
_COSMOSDB_KNOWN_BUGS = {}


@pytest.fixture(autouse=True)
def _xfail_known_cosmosdb_bugs(request):
    """xfail(strict=True) the specific assertions cosmosdb_fake trips over.

    Imperative rather than a static mark so it can be scoped to one KINDS
    entry: ``request.node.add_marker`` during fixture setup is seen by pytest
    before the test body runs, which is what lets a dynamically-added
    ``xfail`` still catch the failure that follows.
    """
    callspec = getattr(request.node, "callspec", None)
    if callspec is None or callspec.params.get("storage") != "cosmosdb_fake":
        return
    reason = _COSMOSDB_KNOWN_BUGS.get(request.node.originalname)
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))


# Bugs in LakebaseStorage that running it against fake_postgres surfaced for
# the first time (see vectrixdb/core/storage.py; fixing them there is out of
# scope for the fake this file wires in, which only reports what it found).
# Keyed by test name rather than applied with a parametrize mark, for the
# same reason as _DELTA_LAKE_KNOWN_BUGS above: each bug belongs to the
# lakebase_fake entry in KINDS, not to the test in isolation, so every other
# backend must keep running these same assertions for real.
# every bug this table held is fixed; the contract runs for real
_LAKEBASE_KNOWN_BUGS = {}


@pytest.fixture(autouse=True)
def _xfail_known_lakebase_bugs(request):
    """xfail(strict=True) the specific assertions lakebase_fake trips over.

    Imperative rather than a static mark so it can be scoped to one KINDS
    entry: ``request.node.add_marker`` during fixture setup is seen by pytest
    before the test body runs, which is what lets a dynamically-added
    ``xfail`` still catch the failure that follows.
    """
    callspec = getattr(request.node, "callspec", None)
    if callspec is None or callspec.params.get("storage") != "lakebase_fake":
        return
    reason = _LAKEBASE_KNOWN_BUGS.get(request.node.originalname)
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))


def test_lakebase_update_a_plain_field(tmp_path):
    storage = _lakebase_fake(tmp_path)
    storage.connect()
    storage.create_collection(COLL, {"mode": "dense", "dimension": 4})
    storage.insert(COLL, "d1", _doc(1))
    assert storage.update(COLL, "d1", {"i": 2}) is True
    storage.close()


# Bugs in AuroraPostgreSQLStorage that running it against fake_postgres
# surfaced for the first time (see vectrixdb/core/storage.py; fixing them
# there is out of scope for the fake this file wires in, which only reports
# what it found). Keyed by test name rather than applied with a parametrize
# mark, for the same reason as _DELTA_LAKE_KNOWN_BUGS above: each bug belongs
# to the aurora_fake entry in KINDS, not to the test in isolation, so every
# other backend must keep running these same assertions for real.
# every bug this table held is fixed; the contract runs for real
_AURORA_KNOWN_BUGS = {}


@pytest.fixture(autouse=True)
def _xfail_known_aurora_bugs(request):
    """xfail(strict=True) the specific assertions aurora_fake trips over.

    Imperative rather than a static mark so it can be scoped to one KINDS
    entry: ``request.node.add_marker`` during fixture setup is seen by pytest
    before the test body runs, which is what lets a dynamically-added
    ``xfail`` still catch the failure that follows.
    """
    callspec = getattr(request.node, "callspec", None)
    if callspec is None or callspec.params.get("storage") != "aurora_fake":
        return
    reason = _AURORA_KNOWN_BUGS.get(request.node.originalname)
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))


class TestEveryBackendIsComplete:
    """Every concrete backend implements the whole abstract interface."""

    @pytest.mark.parametrize(
        "cls_name",
        [
            "InMemoryStorage",
            "SQLiteStorage",
            "CosmosDBStorage",
            "LakebaseStorage",
            "DeltaLakeStorage",
            "OpenSearchStorage",
            "AuroraPostgreSQLStorage",
            "AzureSearchStorage",
        ],
    )
    def test_no_abstract_methods_left(self, cls_name):
        import vectrixdb.core.storage as mod

        cls = getattr(mod, cls_name)
        assert not getattr(cls, "__abstractmethods__", set()), (
            f"{cls_name} cannot be instantiated: {sorted(cls.__abstractmethods__)}"
        )


class TestCollections:
    def test_create_list_config_delete(self, storage):
        storage.create_collection("a", {"mode": "dense", "dimension": 4})
        storage.create_collection("b", {"mode": "hybrid"})
        assert {"a", "b"} <= set(storage.list_collections())
        assert storage.get_collection_config("a") == {"mode": "dense", "dimension": 4}
        storage.delete_collection("a")
        assert "a" not in storage.list_collections()
        assert storage.get_collection_config("a") is None

    def test_missing_config_is_none(self, storage):
        assert storage.get_collection_config("never-made") is None


class TestDocuments:
    def test_insert_get_update_delete(self, storage, collection):
        storage.insert(collection, "d1", _doc(1))
        got = storage.get(collection, "d1")
        assert got["i"] == 1 and got["text_content"] == "text 1"
        assert _vector(got) == [1.0, 1.0, 0.0, 0.0]

        assert storage.update(collection, "d1", {"i": 2}) is True
        assert storage.get(collection, "d1")["i"] == 2
        assert storage.update(collection, "nope", {"i": 3}) is False

        assert storage.delete(collection, "d1") is True
        assert storage.get(collection, "d1") is None

    def test_reinsert_replaces(self, storage, collection):
        storage.insert(collection, "d1", _doc(1))
        storage.insert(collection, "d1", _doc(9))
        assert storage.count(collection) == 1
        assert storage.get(collection, "d1")["i"] == 9

    def test_batch_operations_keep_order_and_report_missing(self, storage, collection):
        storage.insert_batch(collection, [(f"d{i}", _doc(i)) for i in range(5)])
        got = storage.get_batch(collection, ["d3", "missing", "d0"])
        assert got[0]["i"] == 3 and got[1] is None and got[2]["i"] == 0
        assert storage.count(collection) == 5
        assert storage.delete_batch(collection, ["d0", "d1", "missing"]) == 2
        assert storage.count(collection) == 3

    def test_hostile_ids_and_values_round_trip(self, storage, collection):
        storage.insert(collection, HOSTILE_ID, _doc(1, note=HOSTILE_ID))
        got = storage.get(collection, HOSTILE_ID)
        assert got is not None and got["note"] == HOSTILE_ID
        assert storage.delete(collection, HOSTILE_ID)

    def test_scan_pages_and_filters(self, storage, collection):
        storage.insert_batch(collection, [(f"d{i}", _doc(i)) for i in range(10)])
        page = list(storage.scan(collection, limit=4, offset=0))
        assert len(page) == 4
        rest = list(storage.scan(collection, limit=100, offset=4))
        assert len(rest) == 6
        assert {i for i, _ in page} | {i for i, _ in rest} == {f"d{i}" for i in range(10)}
        evens = list(storage.scan(collection, limit=100, filter_func=lambda d: d["i"] % 2 == 0))
        assert sorted(d["i"] for _, d in evens) == [0, 2, 4, 6, 8]

    def test_flush_and_reopen_keep_data(self, request, storage, collection, tmp_path):
        storage.insert(collection, "d1", _doc(1))
        storage.flush()
        if not isinstance(storage, SQLiteStorage):
            return
        storage.close()
        again = _make("sqlite", tmp_path)
        assert again.get(collection, "d1")["i"] == 1


class TestVectorSearch:
    def test_ranks_by_cosine(self, storage, collection):
        storage.insert_batch(
            collection,
            [
                ("x", {"_embedding": [1.0, 0.0, 0.0, 0.0]}),
                ("y", {"_embedding": [0.0, 1.0, 0.0, 0.0]}),
                ("d", {"_embedding": [0.7, 0.7, 0.0, 0.0]}),
            ],
        )
        hits = storage.vector_search(collection, [1.0, 0.1, 0.0, 0.0], limit=3)
        assert [h[0] for h in hits] == ["x", "d", "y"]
        assert all(len(h) == 3 for h in hits)
        assert not any(k.endswith("_embedding") for k in hits[0][1]), (
            "the payload should not echo the vector"
        )

    def test_limit_is_honoured(self, storage, collection):
        storage.insert_batch(collection, [(f"d{i}", _doc(i)) for i in range(8)])
        assert len(storage.vector_search(collection, [1.0, 1.0, 0.0, 0.0], limit=3)) == 3

    def test_empty_collection(self, storage, collection):
        assert storage.vector_search(collection, [1.0, 0.0, 0.0, 0.0], limit=3) == []

    def test_hybrid_runs_when_sparse_is_present(self, storage, collection):
        storage.insert_batch(
            collection,
            [
                ("a", {"_embedding": [1.0, 0.0, 0.0, 0.0], "sparse_embedding": {"7": 1.0}}),
                ("b", {"_embedding": [0.0, 1.0, 0.0, 0.0], "sparse_embedding": {"9": 1.0}}),
            ],
        )
        hits = storage.hybrid_search(collection, [1.0, 0.0, 0.0, 0.0], {7: 1.0}, limit=2)
        assert hits and hits[0][0] == "a"


class TestDocumentIndex:
    @pytest.fixture(autouse=True)
    def _only_where_implemented(self, storage):
        if type(storage).save_document is BaseStorage.save_document:
            pytest.skip(f"{type(storage).__name__} does not implement the document index")
        storage.ensure_document_tables()

    def test_document_round_trip(self, storage):
        storage.save_document(
            {
                "doc_id": "doc1",
                "title": "T",
                "doc_type": "pdf",
                "page_count": 3,
                "metadata": {"k": 1},
            }
        )
        got = storage.get_document("doc1")
        assert got["title"] == "T" and got["doc_type"] == "pdf" and got["page_count"] == 3
        assert got["metadata"] == {"k": 1}
        assert any(d["doc_id"] == "doc1" for d in storage.list_documents())
        assert storage.delete_document("doc1")
        assert storage.get_document("doc1") is None

    def test_nodes_and_children(self, storage):
        storage.save_node(
            {"node_id": "n1", "doc_id": "doc1", "title": "root", "text": "r", "position": 0}
        )
        storage.save_node(
            {
                "node_id": "n2",
                "doc_id": "doc1",
                "parent_id": "n1",
                "title": "child",
                "text": "c",
                "position": 1,
            }
        )
        assert storage.get_node("n2")["parent_id"] == "n1"
        assert [n["node_id"] for n in storage.get_document_nodes("doc1")] == ["n1", "n2"]
        assert [n["node_id"] for n in storage.get_child_nodes("n1")] == ["n2"]
        storage.delete_document_nodes("doc1")
        assert storage.get_document_nodes("doc1") == []


class TestCosmosDocumentContainers:
    def test_the_containers_are_made_once_not_on_every_save(self, tmp_path, monkeypatch):
        """save_document and save_node each asked Cosmos to create both
        document containers again: two requests answered 409 for every
        document and every graph node written."""
        storage = _cosmos_fake(tmp_path)
        storage.connect()
        database = type(storage._database)
        made = []
        real = database.create_container

        def counting(self, id, **kwargs):
            made.append(id)
            return real(self, id, **kwargs)

        monkeypatch.setattr(database, "create_container", counting)
        for i in range(3):
            storage.save_document(
                {
                    "doc_id": f"d{i}",
                    "title": "T",
                    "doc_type": "pdf",
                    "page_count": 1,
                    "metadata": {},
                }
            )
            storage.save_node(
                {"node_id": f"n{i}", "doc_id": f"d{i}", "title": "t", "text": "x", "position": 0}
            )
        assert sorted(made) == ["_documents", "_nodes"]
        assert storage.get_document("d2")["title"] == "T"
