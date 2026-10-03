"""The object-lock sink: records where nothing can rewrite them.

Against an in-memory store that enforces what the real one enforces. The
claim under test is not that objects get written, it is that the sink
refuses a store where they could be altered, and that what it reads back is
what it wrote.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_object_store import FakeObjectStore, LockedObjectError  # noqa: E402

from vectrixdb.audit import DENY, AuditContext, IngestionRecord, RetrievalRecord, Spool
from vectrixdb.exceptions import AuditUnavailable, ConfigurationError
from vectrixdb.objectlock import ObjectLockSink, S3ObjectLockStore


def record(**overrides):
    fields = {
        "decision_id": RetrievalRecord.new_id(),
        "decided_at": datetime.now(timezone.utc),
        "collection": "lending",
        "principal_snapshot": {"roles": ["credit_analyst"]},
        "result_ids": ["a:0"],
        "outcome": "allowed",
    }
    fields.update(overrides)
    return RetrievalRecord(**fields)


@pytest.fixture
def store():
    return FakeObjectStore()


def sink_for(store, **kw):
    kw.setdefault("query_key", b"k")
    kw.setdefault("on_failure", DENY)
    kw.setdefault("retain_days", 2555)
    return ObjectLockSink(store, **kw)


class TestConstruction:
    def test_a_bucket_without_a_lock_is_refused(self):
        """The same put lands in an unlocked bucket and stays exactly as long
        as anybody with delete permission wants it to. That is a log."""
        with pytest.raises(ConfigurationError, match="no retention lock"):
            sink_for(FakeObjectStore(locked=False))

    def test_retention_has_no_default(self, store):
        with pytest.raises(TypeError):
            ObjectLockSink(store, query_key=b"k", on_failure=DENY)
        with pytest.raises(ConfigurationError, match="at least 1"):
            sink_for(store, retain_days=0)

    def test_compliance_is_the_default_mode(self, store):
        assert sink_for(store).mode == "COMPLIANCE"
        with pytest.raises(ConfigurationError, match="COMPLIANCE or GOVERNANCE"):
            sink_for(store, mode="whatever")

    def test_batching_needs_a_spool(self, store, tmp_path):
        """A record in a batch the store has not accepted is not in the
        store, and DENY has nothing to hold it in."""
        with pytest.raises(ConfigurationError, match="needs on_failure=Spool"):
            sink_for(store, batch_size=10)
        sink_for(store, batch_size=10, on_failure=Spool(tmp_path / "spool.jsonl"))

    def test_the_lock_configuration_is_kept(self, store):
        assert sink_for(store).lock_configuration["ObjectLockEnabled"] == "Enabled"


class TestWriting:
    def test_one_record_is_one_locked_object(self, store):
        sink = sink_for(store)
        sink.write(record())

        assert len(store.objects) == 1
        put = store.puts[0]
        assert put["mode"] == "COMPLIANCE"
        assert put["retain_until"] > datetime.now(timezone.utc) + timedelta(days=2554)

    def test_the_key_is_dated_sequenced_and_hashed(self, store):
        sink = sink_for(store, prefix="audit/")
        sink.write(record())
        sink.write(record())
        keys = sorted(store.objects)

        today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        assert all(k.startswith(f"audit/{today}/") for k in keys)
        assert keys[0].split("-")[-2] == "000001" and keys[1].split("-")[-2] == "000002"
        assert all(k.endswith(".jsonl") for k in keys)

    def test_the_object_cannot_be_overwritten_or_deleted(self, store):
        """What the lock is for, exercised against the store rather than
        asserted about it."""
        sink = sink_for(store)
        sink.write(record())
        key = next(iter(store.objects))

        with pytest.raises(LockedObjectError):
            store.delete(key)
        with pytest.raises(LockedObjectError):
            store.put_locked(key, b"x", retain_until=datetime.now(timezone.utc), mode="COMPLIANCE")

    def test_ingestion_records_go_the_same_way(self, store):
        sink = sink_for(store)
        sink.write_ingestion(
            IngestionRecord(
                ingestion_id="build_x", started_at=datetime.now(timezone.utc), collection="lending"
            )
        )
        assert sink.find("build_x")["record_kind"] == "ingestion"

    def test_a_batch_lands_when_it_fills(self, store, tmp_path):
        sink = sink_for(store, batch_size=3, on_failure=Spool(tmp_path / "spool.jsonl"))
        sink.write(record())
        sink.write(record())
        assert store.objects == {}

        sink.write(record())
        assert len(store.objects) == 1
        body = next(iter(store.objects.values()))[0].decode()
        assert body.count("\n") == 3

    def test_flush_writes_a_partial_batch(self, store, tmp_path):
        sink = sink_for(store, batch_size=10, on_failure=Spool(tmp_path / "spool.jsonl"))
        sink.write(record())
        assert sink.flush() == 1
        assert sink.flush() == 0
        assert len(store.objects) == 1


class TestFailure:
    def test_an_unreachable_store_under_deny_refuses_the_answer(self, store):
        store.fail_next = 1
        sink = sink_for(store)
        with pytest.raises(AuditUnavailable):
            sink.write(record())

    def test_an_unreachable_store_under_spool_keeps_the_record(self, store, tmp_path):
        spool = tmp_path / "spool.jsonl"
        store.fail_next = 1
        sink = sink_for(store, on_failure=Spool(spool))
        first = record()
        sink.write(first)

        assert store.objects == {}
        assert json.loads(spool.read_text().splitlines()[0])["decision_id"] == first.decision_id

        assert sink.drain() == 1
        assert sink.find(first.decision_id) is not None
        assert not spool.exists()

    def test_a_failed_batch_goes_to_the_spool_whole(self, store, tmp_path):
        spool = tmp_path / "spool.jsonl"
        sink = sink_for(store, batch_size=2, on_failure=Spool(spool))
        store.fail_next = 1
        sink.write(record())
        sink.write(record())

        assert store.objects == {}
        assert len(spool.read_text().splitlines()) == 2
        assert sink.drain() == 2
        assert len(store.objects) == 1

    def test_a_broken_store_is_a_bug_not_an_outage(self, store):
        """A TypeError spooled and forgotten is a bug nobody finds."""

        def broken(*a, **k):
            raise TypeError("wrong signature")

        store.put_locked = broken
        with pytest.raises(TypeError):
            sink_for(store).write(record())


class TestReading:
    def test_read_all_returns_what_was_written_in_order(self, store):
        sink = sink_for(store)
        ids = [record().decision_id for _ in range(3)]
        for id_ in ids:
            sink.write(record(decision_id=id_))
        assert [r["decision_id"] for r in sink.read_all()] == ids

    def test_a_substituted_body_is_caught_by_its_own_name(self, store):
        """The key carries the body's hash, so an object that does not hash
        to its name was not the one written under it. The lock should make
        this impossible; the check is for the day it did not."""
        sink = sink_for(store)
        sink.write(record())
        key = next(iter(store.objects))
        store.tamper(key, b'{"decision_id": "dec_forged"}\n')

        with pytest.raises(ConfigurationError, match="does not hash to its own name"):
            sink.read_all()

    def test_find_by_either_id(self, store):
        sink = sink_for(store)
        first = record()
        sink.write(first)
        assert sink.find(first.decision_id)["decision_id"] == first.decision_id
        assert sink.find("dec_nothing") is None


class TestAgainstAnAuditedCollection:
    def test_decisions_land_and_reproduce(self, store, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.policy import Overlap, Policy

        policy = Policy([Overlap("entitlements.allowed_roles", "roles")])
        sink = sink_for(store)
        db = Vectrix("lending", path=str(tmp_path / "db"), policy=policy, on_retrieval=sink)
        try:
            db.add(["a covenant"], metadata=[{"entitlements": {"allowed_roles": ["analyst"]}}])
            results = db.as_principal({"roles": ["analyst"]}, audit=AuditContext()).search(
                "covenant", limit=5
            )
            found = sink.find(results.decision_id)
            assert found["result_ids"] == results.ids
            assert db.reproduce(found).exact
            # One ingestion, one decision: two objects, both locked.
            assert len(store.objects) == 2
        finally:
            db.close()


class TestS3Adapter:
    """The boto3 calls, against a client-shaped object. No boto3, no AWS."""

    class _Client:
        def __init__(self, locked=True):
            self.locked = locked
            self.calls = []
            self.objects = {}

        def get_object_lock_configuration(self, Bucket):
            self.calls.append(("lock", Bucket))
            if not self.locked:
                raise Exception("An error occurred (ObjectLockConfigurationNotFoundError)")
            return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}

        def put_object(self, **kw):
            self.calls.append(("put", kw))
            self.objects[kw["Key"]] = kw["Body"]

        def list_objects_v2(self, **kw):
            keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
            return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

        def get_object(self, Bucket, Key):
            import io

            return {"Body": io.BytesIO(self.objects[Key])}

    def test_put_carries_the_lock_headers(self):
        client = self._Client()
        sink = sink_for(S3ObjectLockStore("audit-bucket", client=client), retain_days=30)
        sink.write(record())
        put = next(kw for name, kw in client.calls if name == "put")

        assert put["Bucket"] == "audit-bucket"
        assert put["ObjectLockMode"] == "COMPLIANCE"
        assert put["ObjectLockRetainUntilDate"] > datetime.now(timezone.utc) + timedelta(days=29)
        assert put["ContentType"] == "application/x-ndjson"

    def test_an_unlocked_bucket_reads_as_no_lock(self):
        assert (
            S3ObjectLockStore("b", client=self._Client(locked=False)).lock_configuration() is None
        )
        with pytest.raises(ConfigurationError, match="no retention lock"):
            sink_for(S3ObjectLockStore("b", client=self._Client(locked=False)))

    def test_round_trip_through_the_adapter(self):
        client = self._Client()
        sink = sink_for(S3ObjectLockStore("b", client=client))
        first = record()
        sink.write(first)
        assert sink.find(first.decision_id)["decision_id"] == first.decision_id

    def test_without_boto3_or_a_client_the_error_names_the_install(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_boto3(name, *a, **k):
            if name == "boto3":
                raise ImportError("no module named boto3")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_boto3)
        with pytest.raises(ConfigurationError, match="pip install boto3"):
            S3ObjectLockStore("b")
