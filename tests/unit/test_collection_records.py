"""Each collection's record in one store every server reads.

What is held to. A record is its name, where its files go and who may search
it, and it reads back as it was written on every database the sign-in store
runs on. An empty policy is refused where it is written; none means nobody
yet. A record says nothing about a collection's documents: the per-document
policy a collection keeps in its own metadata stands whatever the record says.
A store that cannot be read raises rather than answering "no policy", unless
a copy from earlier can stand in. Deleting a collection takes its record with
it, so one made again under the name answers nobody until it is given a
policy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_records import FakeContainer, FakePostgres, FakeTable  # noqa: E402

from vectrixdb.collection_records import (  # noqa: E402
    CollectionRecord,
    CollectionRecords,
    describe_collection_store,
    open_collection_store,
)
from vectrixdb.exceptions import (  # noqa: E402
    CollectionStoreUnavailable,
    ConfigurationError,
    PrincipalRequired,
)
from vectrixdb.policy import Equals, Overlap, Policy  # noqa: E402
from vectrixdb.signin.records import CosmosRecords, DynamoRecords, Records, SqlRecords  # noqa: E402

COVERAGE = Policy([Overlap("client_id", "clients", scope=True)], version="client coverage")
BY_DESK = Policy([Equals("desk", "desk")], version="by desk")
SECRET = "s" * 48

BACKENDS = {
    "sqlite-file": lambda tmp: SqlRecords.sqlite(tmp / "rules" / "collections.db"),
    "sqlite-memory": lambda tmp: SqlRecords.sqlite(":memory:"),
    "postgresql": lambda tmp: SqlRecords.postgres(
        "postgresql://vx@db.example.test/rules", connect=FakePostgres().connect
    ),
    "cosmos": lambda tmp: CosmosRecords(FakeContainer()),
    "dynamodb": lambda tmp: DynamoRecords(FakeTable()),
}


@pytest.fixture(params=list(BACKENDS))
def store(request, tmp_path):
    made = CollectionRecords(BACKENDS[request.param](tmp_path))
    yield made
    made.close()


class Flaky(Records):
    """A store that stops answering when told to, as a database does when it is restarted or cut off."""

    def __init__(self, inner: Records):
        self.inner = inner
        self.down = False
        self.reads = 0

    def _up(self):
        if self.down:
            raise ConnectionError("the database is not answering")

    def get(self, kind, key):
        self._up()
        self.reads += 1
        return self.inner.get(kind, key)

    def create(self, record):
        self._up()
        return self.inner.create(record)

    def replace(self, record):
        self._up()
        return self.inner.replace(record)

    def put(self, record):
        self._up()
        self.inner.put(record)

    def delete(self, kind, key, version=None):
        self._up()
        return self.inner.delete(kind, key, version)

    def query(self, kind, *, ix1=None, ix2=None):
        self._up()
        return self.inner.query(kind, ix1=ix1, ix2=ix2)

    def purge(self):
        self.inner.purge()

    def describe(self):
        return "the test's database"

    def close(self):
        self.inner.close()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def moments(monkeypatch):
    """The moments records are stamped with, one second apart, so a test never waits for a clock."""
    from vectrixdb import collection_records

    seconds = iter(range(10, 60))
    monkeypatch.setattr(
        collection_records, "_now", lambda: f"2026-09-23T10:00:{next(seconds):02d}Z"
    )


def rows():
    return (
        ["the covenant for the first borrower", "the covenant for the second borrower"],
        [{"client_id": "CL-1", "desk": "credit"}, {"client_id": "CL-2", "desk": "credit"}],
    )


# ------------------------------------------------------------------ the record ---


class TestTheRecord:
    def test_a_group_policy_kept_with_no_list_reads_as_nobody_yet(self, caplog):
        """Written before a list was needed: a group alone would let everyone in it search, so nobody may until people are added."""
        kept = {
            "path": "raw/media/",
            "policy": {"method": "token", "allow": [{"id": "g-1", "name": "Media"}]},
        }
        record = CollectionRecord.from_data("media", kept)
        assert record.policy is None and record.policy_object() is None
        assert "no list of people" in caplog.text
        with pytest.raises(ConfigurationError, match="Add the people who may search"):
            CollectionRecord(name="media", generation="g", policy=kept["policy"])

    AS_WRITTEN = {
        "name": "financial",
        "path": "raw/financial/",
        "policy": {
            "method": "token",
            "allow": [{"id": "g-1", "name": "HR"}],
            "people": [{"email": "ama@company.com"}],
        },
    }

    def test_a_record_as_a_person_writes_it_reads_back_the_same(self):
        record = CollectionRecord.from_json(self.AS_WRITTEN)
        assert (record.name, record.path) == ("financial", "raw/financial/")
        assert (
            record.policy_object().describe()
            == "security group HR, narrowed to 1 person on the list"
        )
        assert CollectionRecord.from_json(record.to_json()) == record
        assert record.to_json() == self.AS_WRITTEN

    def test_what_older_records_carried_is_read_past(self):
        old = {
            **self.AS_WRITTEN,
            "visibility": "public",
            "masking": True,
            "entitlement": {
                "version": 1,
                "rules": [{"kind": "Overlap", "doc": "client_id", "principal": "clients"}],
            },
        }
        record = CollectionRecord.from_json(old)
        assert record.to_json() == self.AS_WRITTEN, "the policy is the whole rule now"
        was_entitlement = {
            "name": "financial",
            "policy": {
                "version": 1,
                "rules": [{"kind": "Overlap", "doc": "client_id", "principal": "clients"}],
            },
        }
        assert CollectionRecord.from_json(was_entitlement).policy is None, (
            "a per-document policy where the policy goes reads as nobody yet"
        )

    def test_a_file_named_for_its_collection_needs_no_id_and_one_that_disagrees_is_refused(
        self, tmp_path
    ):
        only_rules = {k: v for k, v in self.AS_WRITTEN.items() if k != "name"}
        (tmp_path / "financial.json").write_text(
            __import__("json").dumps(only_rules), encoding="utf-8"
        )
        assert CollectionRecord.from_json(tmp_path / "financial.json").name == "financial"
        (tmp_path / "media.json").write_text(
            __import__("json").dumps(self.AS_WRITTEN), encoding="utf-8"
        )
        with pytest.raises(
            ConfigurationError, match="media.json says it is the record for 'financial'"
        ):
            CollectionRecord.from_json(tmp_path / "media.json")

    def test_what_cannot_be_used_is_refused_where_it_is_written_and_not_at_the_first_search(self):
        with pytest.raises(ConfigurationError, match="null is nobody yet"):
            CollectionRecord("financial", "g", policy={})
        with pytest.raises(ConfigurationError, match="token or store"):
            CollectionRecord("financial", "g", policy={"method": "roles"})
        with pytest.raises(ConfigurationError, match="name"):
            CollectionRecord(" ", "g")

    def test_a_record_with_no_policy_says_so(self):
        record = CollectionRecord("media", "g")
        assert record.policy_object() is None
        assert record.to_json()["policy"] is None, (
            "written out, so a person reading the file sees there is none"
        )


class TestThePolicyAsItIsKept:
    def test_the_default_is_left_out_so_every_fingerprint_made_before_stays_the_same(self):
        assert "on_incomplete_document" not in COVERAGE.to_dict()
        assert Policy.from_dict(COVERAGE.to_dict()).fingerprint == COVERAGE.fingerprint

    def test_what_to_do_with_an_incomplete_document_survives_being_kept(self):
        lenient = Policy(
            [Overlap("client_id", "clients", scope=True)],
            version="client coverage",
            on_incomplete_document="warn",
        )
        kept = Policy.from_dict(lenient.to_dict())
        assert kept.on_incomplete_document == "warn"
        assert kept.fingerprint == lenient.fingerprint != COVERAGE.fingerprint, (
            "a different rule about incomplete documents is a different policy"
        )


# ------------------------------------------------------------------- the store ---


class TestTheStore:
    def test_a_record_kept_reads_back_on_every_database(self, store):
        kept = store.put(
            CollectionRecord(
                "financial",
                "2026-09-22T10:15:00Z",
                path="raw/financial/",
                policy={"method": "store", "allow": [{"email": "ama@example.com"}]},
            ),
            by="04_push",
        )
        assert kept.created_at and kept.updated_at and kept.changed_by == "04_push"
        fresh = CollectionRecords(store._records)
        found = fresh.get("financial")
        assert found == kept and found.policy == {
            "method": "store",
            "allow": [{"email": "ama@example.com"}],
        }
        assert fresh.get("nothing") is None
        store.put(CollectionRecord("media", "2026-09-22T10:16:00Z"))
        assert store.names() == ["financial", "media"]
        assert [r.name for r in store.all()] == ["financial", "media"]

    def test_the_first_moment_is_kept_and_every_change_is_stamped(self, store, moments):
        first = store.put(CollectionRecord("financial", "g"))
        again = store.put(first, by="ada@example.com")
        assert again.created_at == first.created_at and again.updated_at > first.updated_at
        assert again.changed_by == "ada@example.com"

    def test_a_change_of_policy_keeps_the_path(self, store):
        store.put(CollectionRecord("financial", "g", path="raw/financial/"), by="04_push")
        store.set_policy(
            "financial",
            {"method": "store", "allow": [{"domain": "example.com"}]},
            by="ada@example.com",
        )
        record = CollectionRecords(store._records).get("financial")
        assert (record.path, record.policy, record.changed_by) == (
            "raw/financial/",
            {"method": "store", "allow": [{"domain": "example.com"}]},
            "ada@example.com",
        )
        store.set_policy("financial", None)
        assert CollectionRecords(store._records).get("financial").policy is None, (
            "nobody yet, and the path stays"
        )

    def test_a_collection_with_no_record_starts_with_nobody(self, store):
        made = store.set_policy("media", None, generation="2026-09-22T10:16:00Z")
        assert (made.policy, made.generation) == (None, "2026-09-22T10:16:00Z")

    def test_deleting_a_record_retires_the_collection(self, store):
        store.set_policy("financial", {"method": "store", "allow": [{"email": "ama@example.com"}]})
        assert store.delete("financial") and store.get("financial") is None
        assert CollectionRecords(store._records).get("financial") is None
        assert not store.delete("financial")


class TestReading:
    def test_a_record_read_a_moment_ago_is_used_and_a_later_one_asks_again(self, tmp_path):
        clock, shared = Clock(), SqlRecords.sqlite(tmp_path / "collections.db")
        here, there = (
            CollectionRecords(shared, fresh_for=30, clock=clock),
            CollectionRecords(shared),
        )
        there.set_policy("financial", {"method": "store", "allow": [{"email": "ama@example.com"}]})
        assert here.get("financial").policy == {
            "method": "store",
            "allow": [{"email": "ama@example.com"}],
        }
        there.set_policy("financial", {"method": "store", "allow": [{"domain": "example.com"}]})
        clock.now += 29
        assert here.get("financial").policy == {
            "method": "store",
            "allow": [{"email": "ama@example.com"}],
        }, "within fresh_for, the copy read a moment ago"
        clock.now += 2
        assert here.get("financial").policy == {
            "method": "store",
            "allow": [{"domain": "example.com"}],
        }

    def test_a_store_that_stops_answering_is_stood_in_for_by_what_was_read_before(self, caplog):
        clock, flaky = Clock(), Flaky(SqlRecords.sqlite(":memory:"))
        rules = CollectionRecords(flaky, fresh_for=30, clock=clock)
        rules.set_policy("financial", {"method": "store", "allow": [{"email": "ama@example.com"}]})
        clock.now += 3600
        flaky.down = True
        with caplog.at_level("WARNING", logger="vectrixdb.collection_records"):
            assert rules.get("financial").policy == {
                "method": "store",
                "allow": [{"email": "ama@example.com"}],
            }
        assert "could not be read" in caplog.text and "ConnectionError" in caplog.text

    def test_with_nothing_held_it_raises_rather_than_answer_no_policy(self):
        flaky = Flaky(SqlRecords.sqlite(":memory:"))
        CollectionRecords(flaky).set_policy(
            "financial", {"method": "store", "allow": [{"email": "ama@example.com"}]}
        )
        flaky.down = True
        with pytest.raises(
            CollectionStoreUnavailable,
            match=r"'financial' are kept in the test's database, which could not be read \(ConnectionError\)",
        ) as refused:
            CollectionRecords(flaky).get("financial")
        assert refused.value.collection == "financial"

    def test_opening_takes_an_address_a_path_or_a_store_already_made(self, tmp_path):
        assert open_collection_store(None) is None and open_collection_store("") is None
        made = open_collection_store(tmp_path / "collections.db")
        assert isinstance(made, CollectionRecords) and open_collection_store(made) is made
        assert str(tmp_path / "collections.db") in describe_collection_store(made)
        assert describe_collection_store(None).startswith("none")
        made.close()
        with pytest.raises(ConfigurationError, match="VECTRIXDB_COLLECTION_STORE is a path"):
            open_collection_store("mongodb://db.example.test/rules")


# ------------------------------------------------------------ the collections ---


class TestTheRecordSaysNothingAboutDocuments:
    def test_a_collections_own_policy_stands_whatever_its_record_says(self, tmp_path):
        from vectrixdb import Vectrix

        shared = CollectionRecords(SqlRecords.sqlite(tmp_path / "collections.db"), fresh_for=0)
        shared.set_policy(
            "walled",
            {"method": "store", "allow": [{"domain": "example.com"}]},
            by="ada@example.com",
        )
        db = Vectrix("walled", path=str(tmp_path), policy=COVERAGE, collection_store=shared)
        try:
            assert db.policy.fingerprint == COVERAGE.fingerprint
            assert db._collection.policy.fingerprint == COVERAGE.fingerprint
            with pytest.raises(PrincipalRequired):
                db.search("covenant")
        finally:
            db.close()
        assert shared.get("walled").to_json() == {
            "name": "walled",
            "policy": {"method": "store", "allow": [{"domain": "example.com"}]},
        }, "the record holds who may search it, and nothing about its documents"

    def test_deleting_a_collection_takes_its_record_with_it(self, tmp_path):
        from vectrixdb import VectrixDB

        shared = CollectionRecords(SqlRecords.sqlite(tmp_path / "collections.db"), fresh_for=0)
        shared.set_policy("walled", {"method": "store", "allow": [{"domain": "example.com"}]})
        db = VectrixDB(tmp_path / "server", collection_store=shared)
        db.create_collection("walled", 8)
        assert db.delete_collection("walled")
        assert shared.get("walled") is None, (
            "made again under the name, it answers nobody until it is given a policy"
        )

    def test_a_store_named_later_reaches_every_collection_already_open(self, tmp_path):
        from vectrixdb import VectrixDB

        db = VectrixDB(tmp_path / "server")
        db.create_collection("walled", 8)
        assert db.collection_store is None and db.get_collection("walled").policy is None
        shared = db.use_collection_store(tmp_path / "collections.db")
        assert db.collection_store is shared
        shared.set_policy("walled", {"method": "store", "allow": [{"domain": "example.com"}]})
        assert shared.get("walled").policy == {
            "method": "store",
            "allow": [{"domain": "example.com"}],
        }
        db.use_collection_store(None)
        assert db.collection_store is None


# ----------------------------------------------------------------- sign-in ---


class TestSignInForgetsACollectionsRecord:
    @pytest.fixture
    def two_servers(self, tmp_path):
        from vectrixdb.signin import SignInStore

        shared = SqlRecords.sqlite(tmp_path / "collections.db")
        first = SignInStore(SqlRecords.sqlite(":memory:"), [SECRET])
        second = SignInStore(SqlRecords.sqlite(":memory:"), [SECRET])
        first.use_collection_store(CollectionRecords(shared, fresh_for=0))
        second.use_collection_store(CollectionRecords(shared, fresh_for=0))
        return first, second, CollectionRecords(shared, fresh_for=0)

    def test_a_collection_deleted_on_one_server_takes_its_record_from_every_server(
        self, two_servers
    ):
        first, second, rules = two_servers
        rules.set_policy(
            "financial",
            {"method": "store", "allow": [{"domain": "example.com"}]},
            generation="2026-09-22T10:15:00Z",
        )
        assert rules.get("financial").policy == {
            "method": "store",
            "allow": [{"domain": "example.com"}],
        }
        second.forget_collection("financial")
        assert rules.get("financial") is None
        first.forget_collection("media")  # nothing there: nothing to forget, and no error


# ------------------------------------------------------------------ the server ---


class TestTheServer:
    def test_the_store_is_the_one_given_else_the_setting_else_none(self, tmp_path):
        from vectrixdb.api.server import collection_store_from_env

        given = CollectionRecords(SqlRecords.sqlite(":memory:"))
        assert (
            collection_store_from_env(given, env={"VECTRIXDB_COLLECTION_STORE": "ignored.db"})
            is given
        )
        assert collection_store_from_env(env={}) is None
        from_setting = collection_store_from_env(
            env={"VECTRIXDB_COLLECTION_STORE": str(tmp_path / "a.db")}
        )
        assert str(tmp_path / "a.db") in from_setting.describe()
        address = tmp_path / "address.txt"
        address.write_text(str(tmp_path / "b.db"), encoding="utf-8")
        from_file = collection_store_from_env(env={"VECTRIXDB_COLLECTION_STORE_FILE": str(address)})
        assert str(tmp_path / "b.db") in from_file.describe()
        with pytest.raises(ConfigurationError, match="both set"):
            collection_store_from_env(
                env={
                    "VECTRIXDB_COLLECTION_STORE": "a.db",
                    "VECTRIXDB_COLLECTION_STORE_FILE": str(address),
                }
            )
        for made in (from_setting, from_file):
            made.close()

    def test_the_key_comes_from_its_own_setting_and_never_from_the_address(self, monkeypatch):
        from vectrixdb import collection_records
        from vectrixdb.api.server import collection_store_from_env

        seen = {}
        monkeypatch.setattr(
            collection_records,
            "open_collection_store",
            lambda where, key=None: seen.update(where=where, key=key) or "opened",
        )
        env = {
            "VECTRIXDB_COLLECTION_STORE": "cosmos://acct.documents.azure.com/access/collection_records",
            "VECTRIXDB_COLLECTION_STORE_KEY": "account-key",
        }
        assert collection_store_from_env(env=env) == "opened"
        assert seen == {
            "where": "cosmos://acct.documents.azure.com/access/collection_records",
            "key": "account-key",
        }

    @pytest.fixture
    def served(self, tmp_path, monkeypatch):
        """A collection a server holds, and the store that says who may search it."""
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from vectrixdb import Vectrix
        from vectrixdb.api import server

        for name in ("VECTRIXDB_COLLECTION_STORE", "VECTRIXDB_SIGNIN"):
            monkeypatch.delenv(name, raising=False)
        # The server's own key: the host's own hand, which every collection's policy lets through.
        monkeypatch.setenv("VECTRIXDB_API_KEY", "the-full-api-key")
        flaky = Flaky(SqlRecords.sqlite(tmp_path / "collections.db"))
        # Everyone at the domain may search it; the server's own key is the host's hand.
        CollectionRecords(flaky).set_policy(
            "walled", {"method": "store", "allow": [{"domain": "example.com"}]}
        )
        on_server = Vectrix("walled", path=str(tmp_path / "server"))
        on_server.add(*rows())
        on_server.close()

        def serve(store):
            """One server at a time: the server module keeps its database in a module global."""
            return TestClient(
                server.create_app(
                    db_path=str(tmp_path / "server"), enable_dashboard=False, collection_store=store
                )
            )

        serve.flaky = flaky
        return serve

    def test_a_store_that_cannot_be_read_is_a_503_to_retry_not_a_refusal_to_believe(self, served):
        served.flaky.down = True
        with served(CollectionRecords(served.flaky)) as client:
            assert (
                client.get(
                    "/api/v1/collections/walled", headers={"api-key": "the-full-api-key"}
                ).status_code
                == 200
            ), "that it exists is not gated"
            answer = client.post(
                "/api/v1/collections/walled/text-search",
                json={"query_text": "covenant", "limit": 1},
                headers={"api-key": "the-full-api-key"},
            )
        assert answer.status_code == 503
        assert (
            "could not be read" in answer.json()["detail"]
            and "nothing was searched" in answer.json()["detail"]
        )


class TestTheCheck:
    def run(self, tmp_path, **env):
        from vectrixdb.check import run

        return [
            (f.level, f.text)
            for f in run(str(tmp_path), {"VECTRIXDB_OFFLINE": "1", **env})
            if f.area == "Storage"
        ]

    def test_a_path_is_ready_and_says_what_it_holds(self, tmp_path):
        found = self.run(tmp_path, VECTRIXDB_COLLECTION_STORE=str(tmp_path / "collections.db"))
        assert (
            "ok",
            f"Every collection's record, who may retrieve from it, who may see it and its masking, is read from {tmp_path / 'collections.db'}",
        ) in found

    def test_an_address_it_cannot_use_and_an_extra_that_is_missing_are_errors(
        self, tmp_path, monkeypatch
    ):
        found = self.run(tmp_path, VECTRIXDB_COLLECTION_STORE="mongodb://db.example.test/rules")
        assert any(
            level == "error" and "VECTRIXDB_COLLECTION_STORE is a path" in text
            for level, text in found
        )
        monkeypatch.setitem(sys.modules, "boto3", None)
        found = self.run(
            tmp_path, VECTRIXDB_COLLECTION_STORE="dynamodb://collections?region=ca-central-1"
        )
        assert (
            "error",
            "VECTRIXDB_COLLECTION_STORE needs boto3: pip install 'vectrixdb[aws]'",
        ) in found
        monkeypatch.setitem(sys.modules, "psycopg2", None)
        found = self.run(
            tmp_path, VECTRIXDB_COLLECTION_STORE="postgresql://vx@db.example.test/rules"
        )
        assert (
            "error",
            "VECTRIXDB_COLLECTION_STORE needs psycopg2: pip install 'vectrixdb[postgres]'",
        ) in found

    def test_the_address_and_its_file_twin_together_are_one_error(self, tmp_path):
        from vectrixdb.check import run

        twin = tmp_path / "address.txt"
        twin.write_text("collections.db", encoding="utf-8")
        env = {
            "VECTRIXDB_OFFLINE": "1",
            "VECTRIXDB_COLLECTION_STORE": "collections.db",
            "VECTRIXDB_COLLECTION_STORE_FILE": str(twin),
        }
        said = [
            f.text for f in run(str(tmp_path), env) if f.level == "error" and "both set" in f.text
        ]
        assert said == [
            "VECTRIXDB_COLLECTION_STORE and VECTRIXDB_COLLECTION_STORE_FILE are both set. Keep one, so there is no doubt which is meant"
        ]


def test_the_path_it_was_given_is_where_it_is(tmp_path):
    """A small thing that matters in a log: the store describes where it is, never with a key."""
    made = CollectionRecords.open(Path(tmp_path) / "collections.db")
    assert str(tmp_path) in made.describe()
    made.close()
