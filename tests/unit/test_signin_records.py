"""The record layer sign-in keeps its state in: one contract, the same on every database.

It runs against a SQLite file and SQLite in memory, against PostgreSQL's own
statements (run by SQLite through a connection shaped like psycopg2's), and
against stand-ins for a Cosmos DB container and a DynamoDB table. What must
hold everywhere: a record made is there, and a second with its key is refused
unless the first is past its time; a write from a stale read loses; a delete
with a stale version does nothing; a query finds by kind and by either index,
and never returns what has expired. Then how a store is chosen from a setting,
and what is said about it, which never includes a password.
"""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_records import CosmosError, DynamoError, FakeContainer, FakePostgres, FakeTable  # noqa: E402

from vectrixdb.exceptions import ConfigurationError, DependencyError  # noqa: E402
from vectrixdb.signin.records import (
    CosmosRecords,
    DynamoRecords,
    Record,
    SqlRecords,
    describe_where,
    open_records,
)  # noqa: E402

BACKENDS = {
    "sqlite-file": lambda tmp: SqlRecords.sqlite(tmp / "auth" / "signin.db"),
    "sqlite-memory": lambda tmp: SqlRecords.sqlite(":memory:"),
    "postgresql": lambda tmp: SqlRecords.postgres(
        "postgresql://vx@db.example.test/signin", connect=FakePostgres().connect
    ),
    "cosmos": lambda tmp: CosmosRecords(FakeContainer()),
    "dynamodb": lambda tmp: DynamoRecords(FakeTable()),
}


@pytest.fixture(params=list(BACKENDS))
def records(request, tmp_path):
    made = BACKENDS[request.param](tmp_path)
    yield made
    made.close()


class TestTheContract:
    def test_a_record_made_is_there_and_a_second_with_its_key_is_refused(self, records):
        first = Record("person", "ada@example.com", {"role": "admin"})
        assert records.create(first) and first.version is not None
        assert not records.create(Record("person", "ada@example.com", {"role": "viewer"}))
        found = records.get("person", "ada@example.com")
        assert found.data == {"role": "admin"} and found.version == first.version
        assert records.get("person", "sam@example.com") is None
        assert records.get("session", "ada@example.com") is None, "a key belongs to its kind"

    def test_a_key_past_its_time_is_free_again(self, records):
        assert records.create(Record("link", "t1", {"used": False}, expires=time.time() - 1))
        assert records.get("link", "t1") is None, (
            "gone the moment its time is up, whether or not anything has cleared it"
        )
        assert records.create(Record("link", "t1", {"used": True}, expires=time.time() + 60))
        assert records.get("link", "t1").data == {"used": True}

    def test_a_write_from_a_stale_read_loses(self, records):
        records.create(Record("attempt", "code:ada", {"count": 0}))
        mine, theirs = records.get("attempt", "code:ada"), records.get("attempt", "code:ada")
        theirs.data["count"] = 1
        assert records.replace(theirs)
        mine.data["count"] = 5
        assert not records.replace(mine), "somebody wrote it after this was read"
        assert records.get("attempt", "code:ada").data == {"count": 1}
        again = records.get("attempt", "code:ada")
        again.data["count"] = 2
        assert records.replace(again) and records.replace(again), (
            "a write carries the version it made"
        )
        assert not records.replace(Record("attempt", "nobody", {}, version=again.version)), (
            "and there is nothing to write back to"
        )

    def test_a_delete_with_a_stale_version_does_nothing_and_one_without_always_goes(self, records):
        made = Record("challenge", "h1", {"purpose": "a"})
        records.create(made)
        stale = made.version
        changed = records.get("challenge", "h1")
        changed.data["purpose"] = "b"
        records.replace(changed)
        assert not records.delete("challenge", "h1", stale)
        assert records.delete("challenge", "h1", changed.version)
        assert not records.delete("challenge", "h1"), "nothing left to delete"
        records.create(Record("challenge", "h2", {}))
        assert records.delete("challenge", "h2") and records.get("challenge", "h2") is None

    def test_put_writes_whatever_is_there(self, records):
        records.put(Record("visibility", "docs", {"audience": "guests"}))
        records.put(Record("visibility", "docs", {"audience": "everyone"}))
        assert records.get("visibility", "docs").data == {"audience": "everyone"}

    def test_a_query_finds_by_kind_and_by_either_index_and_never_what_has_expired(self, records):
        now = time.time()
        for n in range(5):
            owner = "ada@example.com" if n < 3 else "sam@example.com"
            records.create(
                Record(
                    "session",
                    f"s{n}",
                    {"n": n},
                    expires=now + 60,
                    ix1=owner,
                    ix2="ada" if n % 2 == 0 else "sam",
                )
            )
        records.create(
            Record("session", "old", {"n": 9}, expires=now - 1, ix1="ada@example.com", ix2="ada")
        )
        records.create(Record("person", "ada@example.com", {}))
        assert sorted(r.key for r in records.query("session")) == ["s0", "s1", "s2", "s3", "s4"]
        assert sorted(r.key for r in records.query("session", ix1="ada@example.com")) == [
            "s0",
            "s1",
            "s2",
        ]
        assert sorted(r.key for r in records.query("session", ix2="ada")) == ["s0", "s2", "s4"]
        assert sorted(
            r.key for r in records.query("session", ix1="ada@example.com", ix2="ada")
        ) == ["s0", "s2"]
        assert [r.key for r in records.query("person")] == ["ada@example.com"]
        found = records.query("session", ix1="sam@example.com")[0]
        found.data["n"] = 99
        assert records.replace(found), "what a query found can be written back"

    def test_what_a_record_holds_comes_back_as_it_went_in(self, records):
        data = {
            "text": "Ünïcode · 中文",
            "nested": {"list": [1, 2.5, None, True], "empty": {}},
            "big": 2**53,
            "at": 1_800_000_000.123456,
        }
        key = "odd/key?with#chars\\and|bars"
        records.create(Record("person", key, data, ix1="a|b"))
        assert records.get("person", key).data == data
        assert [r.key for r in records.query("person", ix1="a|b")] == [key]

    def test_clearing_out_what_has_expired_is_safe_at_any_time(self, records):
        records.create(Record("link", "gone", {}, expires=time.time() - 5))
        records.create(Record("link", "kept", {}, expires=time.time() + 60))
        records.purge()
        records.purge()
        assert records.get("link", "gone") is None and records.get("link", "kept") is not None


class TestChoosingWhere:
    def test_a_path_or_a_sqlite_address_is_a_file(self, tmp_path):
        made = open_records(tmp_path / "a" / "signin.db")
        assert made.path == tmp_path / "a" / "signin.db" and made.path.exists()
        made.close()
        target = tmp_path / "b" / "state.db"
        made = open_records(f"sqlite:///{target.as_posix()}")
        assert made.path == target and target.exists()
        made.close()
        with pytest.raises(ConfigurationError, match="sqlite:///"):
            open_records("sqlite://relative.db")

    def test_each_service_needs_its_extra_and_says_which(self, monkeypatch):
        for module in ("psycopg2", "azure.cosmos", "boto3"):
            monkeypatch.setitem(sys.modules, module, None)
        with pytest.raises(DependencyError, match=r"vectrixdb\[postgres\]"):
            open_records("postgresql://vx@db.example.test/signin")
        with pytest.raises(DependencyError, match=r"vectrixdb\[azure\]"):
            open_records("cosmos://acct.documents.azure.com/vectrixdb/signin")
        with pytest.raises(DependencyError, match=r"vectrixdb\[aws\]"):
            open_records("dynamodb://vx-signin?region=ca-central-1")

    def test_an_address_it_does_not_know_is_refused_with_the_ones_it_does(self):
        with pytest.raises(ConfigurationError, match="postgresql://.*dynamodb://"):
            open_records("mongodb://db.example.test/signin")

    def test_what_is_said_of_where_never_shows_a_password(self):
        said = describe_where(
            "postgresql://vx:hunter2@db.example.test:5432/signin?sslmode=require&password=hunter2"
        )
        assert said == "PostgreSQL db.example.test:5432/signin" and "hunter2" not in said
        assert (
            describe_where("cosmos://acct.documents.azure.com/vectrixdb/signin")
            == "Cosmos DB acct.documents.azure.com/vectrixdb/signin"
        )
        assert (
            describe_where("dynamodb://vx-signin?region=ca-central-1")
            == "DynamoDB table vx-signin in ca-central-1"
        )
        assert (
            describe_where(Path("data") / "auth" / "signin.db")
            == f"SQLite file {Path('data') / 'auth' / 'signin.db'}"
        )
        assert describe_where(":memory:") == "SQLite in memory"


class TestPostgreSQL:
    def test_the_table_and_the_password_stay_out_of_the_address_it_connects_with(self):
        server = FakePostgres()
        made = SqlRecords.postgres(
            "postgresql://vx@db.example.test/signin?sslmode=require&table=team_signin",
            password="pw",
            connect=server.connect,
        )
        assert server.opened == [
            ("postgresql://vx@db.example.test/signin?sslmode=require", {"password": "pw"})
        ]
        assert any(
            s.startswith("CREATE TABLE IF NOT EXISTS team_signin ")
            for s in server.connections[0].statements
        )
        assert (
            made.describe() == "PostgreSQL db.example.test/signin"
            and made.sqlite_connection is None
        )

    def test_a_dropped_connection_is_opened_again_once(self):
        server = FakePostgres()
        made = SqlRecords.postgres("postgresql://vx@db.example.test/signin", connect=server.connect)
        made.create(Record("person", "ada@example.com", {"role": "admin"}))
        server.fail_next = 1
        assert made.get("person", "ada@example.com").data == {"role": "admin"}, (
            "a restarted database costs nothing"
        )
        assert len(server.connections) == 2 and server.connections[0].closed
        server.fail_next = 2
        with pytest.raises(Exception, match="closed the connection"):
            made.get("person", "ada@example.com")

    def test_a_table_that_is_there_is_used_without_being_made_again(self):
        server = FakePostgres()
        SqlRecords.postgres(
            "postgresql://vx@db.example.test/signin", connect=server.connect
        ).close()
        again = SqlRecords.postgres(
            "postgresql://vx@db.example.test/signin", connect=server.connect
        )
        assert not any(s.startswith("CREATE") for s in server.connections[-1].statements), (
            "a user who may only read and write it is enough"
        )
        again.close()

    def test_a_table_this_user_may_not_make_is_described_exactly(self):
        server = FakePostgres()
        opened = server.connect

        def refusing(dsn, **options):
            connection = opened(dsn, **options)
            real = connection.cursor

            class Cursor:
                def __init__(self):
                    self.inner = real()

                def execute(self, sql, params=()):
                    if sql.startswith("CREATE"):
                        raise RuntimeError("permission denied for schema public")
                    self.inner.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(self.inner, name)

            connection.cursor = Cursor
            return connection

        with pytest.raises(
            ConfigurationError,
            match=r"permission denied for schema public.*CREATE TABLE IF NOT EXISTS vectrixdb_signin",
        ):
            SqlRecords.postgres("postgresql://vx@db.example.test/signin", connect=refusing)

    def test_a_table_name_that_could_be_anything_else_is_refused(self):
        with pytest.raises(ConfigurationError, match="table"):
            SqlRecords.postgres(
                "postgresql://vx@db.example.test/signin?table=x;drop",
                connect=FakePostgres().connect,
            )


def _package(monkeypatch, name: str) -> None:
    """A parent package for a stand-in module, when the real one is not installed."""
    if name not in sys.modules:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


class TestCosmosDB:
    def sdk(self, monkeypatch, *, there: bool = True, may_make: bool = True) -> dict:
        seen: dict = {}

        class Container(FakeContainer):
            def read(self):
                if not there:
                    raise CosmosError(404)

        class Database:
            def get_container_client(self, name):
                seen["container"] = name
                return Container()

            def create_container_if_not_exists(self, id, partition_key, default_ttl):  # noqa: A002 - the SDK's name
                if not may_make:
                    raise CosmosError(403)
                seen["made"] = (id, partition_key.path, default_ttl)
                return Container()

        class Client:
            def __init__(self, endpoint, credential):
                seen.update(endpoint=endpoint, credential=credential)

            def get_database_client(self, name):
                seen["database"] = name
                return Database()

            def create_database_if_not_exists(self, name):
                return Database()

        cosmos = types.ModuleType("azure.cosmos")
        cosmos.CosmosClient = Client
        cosmos.PartitionKey = lambda path: types.SimpleNamespace(path=path)
        identity = types.ModuleType("azure.identity")
        identity.DefaultAzureCredential = lambda: "the machine's own identity"
        _package(monkeypatch, "azure")
        monkeypatch.setitem(sys.modules, "azure.cosmos", cosmos)
        monkeypatch.setitem(sys.modules, "azure.identity", identity)
        return seen

    def test_an_account_key_or_else_the_machine_signs_in(self, monkeypatch):
        seen = self.sdk(monkeypatch)
        made = open_records("cosmos://acct.documents.azure.com/vectrixdb/signin", key="account-key")
        assert seen == {
            "endpoint": "https://acct.documents.azure.com:443/",
            "credential": "account-key",
            "database": "vectrixdb",
            "container": "signin",
        }
        assert made.describe() == "Cosmos DB acct.documents.azure.com/vectrixdb/signin"
        open_records("cosmos://acct.documents.azure.com/vectrixdb/signin")
        assert seen["credential"] == "the machine's own identity"

    def test_a_container_that_is_not_there_is_made_partitioned_by_kind_with_time_to_live(
        self, monkeypatch
    ):
        seen = self.sdk(monkeypatch, there=False)
        open_records("cosmos://acct.documents.azure.com/vectrixdb/signin", key="k")
        assert seen["made"] == ("signin", "/kind", -1)

    def test_one_this_server_may_not_make_is_described_exactly(self, monkeypatch):
        self.sdk(monkeypatch, there=False, may_make=False)
        with pytest.raises(
            ConfigurationError, match=r"vectrixdb/signin .*partition key /kind and time to live on"
        ):
            open_records("cosmos://acct.documents.azure.com/vectrixdb/signin", key="k")

    def test_an_address_without_a_database_and_a_container_is_refused(self, monkeypatch):
        self.sdk(monkeypatch)
        with pytest.raises(ConfigurationError, match="<database>/<container>"):
            open_records("cosmos://acct.documents.azure.com/signin", key="k")


class TestCosmosItemsReadAsWhatTheyAre:
    """In the portal an item says what it is: its kind and key in the id, its times as dates."""

    def test_the_id_is_the_kind_and_the_key_with_only_what_cosmos_refuses_escaped(self):
        container = FakeContainer()
        made = CosmosRecords(container)
        made.put(Record("person", "ama@bank.example", {"role": "admin"}))
        made.put(Record("collection", "raw/2026 #1?", {}))
        made.put(Record("collection", "raw%2F2026 #1?", {}))
        ids = sorted(item_id for _, item_id in container.items)
        assert ids == [
            "collection.raw%252F2026 %231%3F",
            "collection.raw%2F2026 %231%3F",
            "person.ama@bank.example",
        ]
        assert (
            made.get("collection", "raw/2026 #1?") is not None
            and made.get("collection", "raw%2F2026 #1?") is not None
        )

    def test_a_key_too_long_for_an_id_becomes_its_hash_and_never_meets_an_escaped_one(self):
        container = FakeContainer()
        made = CosmosRecords(container)
        long_key = "k" * 300
        made.put(Record("person", long_key, {"n": 1}))
        ((_, item_id),) = container.items
        assert item_id.startswith("person.%sha256-") and len(item_id) <= 255
        assert made.get("person", long_key).data == {"n": 1}
        assert [r.key for r in made.query("person")] == [long_key], (
            "the key itself is kept whole beside the id"
        )

    def test_times_are_written_as_dates_beside_the_numbers_the_code_compares(self):
        container = FakeContainer()
        made = CosmosRecords(container)
        made.put(Record("session", "s1", {}, expires=1_800_000_000.25))
        made.put(Record("person", "ada@example.com", {}))
        session = container.items[("session", "session.s1")]
        assert (
            session["expires"] == "2027-01-15T08:00:00.250000Z"
            and session["expires_at"] == 1_800_000_000.25
        )
        assert session["updated_at"].endswith("Z") and isinstance(session["ttl"], int)
        person = container.items[("person", "person.ada@example.com")]
        assert person["expires"] is None and person["expires_at"] is None and "ttl" not in person

    def test_an_item_written_before_the_dates_still_reads(self):
        container = FakeContainer()
        made = CosmosRecords(container)
        soon = time.time() + 600
        container.items[("session", "session.old")] = {
            "id": "session.old",
            "kind": "session",
            "key": "old",
            "data": {"n": 1},
            "expires": soon,
            "ttl": 600,
            "_etag": "e1",
        }
        container.items[("session", "session.dated")] = {
            "id": "session.dated",
            "kind": "session",
            "key": "dated",
            "data": {"n": 2},
            "expires": "2099-01-01T00:00:00Z",
            "ttl": 600,
            "_etag": "e2",
        }
        assert made.get("session", "old").expires == soon
        assert made.get("session", "dated").expires == 4070908800.0


class TestDynamoDB:
    def sdk(self, monkeypatch, *, there: bool = True, may_make: bool = True) -> dict:
        seen: dict = {}

        class Table(FakeTable):
            def load(self):
                if not there:
                    raise DynamoError("ResourceNotFoundException")

            def wait_until_exists(self):
                seen["waited"] = True

        class Client:
            def update_time_to_live(self, TableName, TimeToLiveSpecification):  # noqa: N803
                seen["ttl"] = (TableName, TimeToLiveSpecification)

        class Resource:
            meta = types.SimpleNamespace(client=Client())

            def Table(self, name):  # noqa: N802 - boto3's name
                seen["table"] = name
                return Table()

            def create_table(self, **spec):
                if not may_make:
                    raise DynamoError("AccessDeniedException")
                seen["spec"] = spec
                return Table()

        boto3 = types.ModuleType("boto3")

        def resource(service, region_name=None, endpoint_url=None):
            seen.update(service=service, region=region_name, endpoint=endpoint_url)
            return Resource()

        boto3.resource = resource
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        return seen

    def test_the_table_region_and_a_local_endpoint_come_from_the_address(self, monkeypatch):
        seen = self.sdk(monkeypatch)
        made = open_records(
            "dynamodb://vx-signin?region=ca-central-1&endpoint=http://localhost:8000"
        )
        assert (seen["service"], seen["table"], seen["region"], seen["endpoint"]) == (
            "dynamodb",
            "vx-signin",
            "ca-central-1",
            "http://localhost:8000",
        )
        assert made.describe() == "DynamoDB table vx-signin in ca-central-1"

    def test_a_table_that_is_not_there_is_made_with_its_indexes_and_time_to_live(self, monkeypatch):
        seen = self.sdk(monkeypatch, there=False)
        open_records("dynamodb://vx-signin?region=ca-central-1")
        spec = seen["spec"]
        assert spec["TableName"] == "vx-signin" and spec["BillingMode"] == "PAY_PER_REQUEST"
        assert spec["KeySchema"] == [
            {"AttributeName": "kind", "KeyType": "HASH"},
            {"AttributeName": "key", "KeyType": "RANGE"},
        ]
        assert [
            (i["IndexName"], i["KeySchema"][0]["AttributeName"])
            for i in spec["GlobalSecondaryIndexes"]
        ] == [("ix1", "g1"), ("ix2", "g2")]
        assert seen["waited"] and seen["ttl"] == (
            "vx-signin",
            {"Enabled": True, "AttributeName": "expires_at"},
        )

    def test_one_this_server_may_not_make_is_described_exactly(self, monkeypatch):
        self.sdk(monkeypatch, there=False, may_make=False)
        with pytest.raises(ConfigurationError, match="vx-signin .*time to live on expires_at"):
            open_records("dynamodb://vx-signin?region=ca-central-1")
