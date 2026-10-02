"""The audit trail and the access log, appended where nothing rewrites them.

Both logs are one JSON object a line, and either can live in a file or in
append blobs, one a UTC day, in a container whose immutability policy keeps
every line as it was written. What is held to. A Blob log will not open a
container with no such policy, and says what to change when the policy
forbids appending. Several servers append to one day's blob, a blob made a
moment ago by another is appended to rather than made again, and a full blob
goes on in a numbered one. Lines read back in the order they were written,
and a day wholly before ``since`` is not read. The server writes its
decisions there and the Audit page reads them back, the access log does the
same for sign-ins and reads, and neither address is folded or shown with a
secret in it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_append_blobs import FakeAppendContainer, FakeBlobService  # noqa: E402

from vectrixdb.append_log import BlobDayLog, FileLog, is_blob_address, open_append_log  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402

ACCOUNT = "https://vxtest1234store.blob.core.windows.net"
SEPT_23 = datetime(2026, 9, 23, 10, 15, tzinfo=timezone.utc).timestamp()


def at(moment: float):
    return lambda: moment


@pytest.fixture
def blobs(monkeypatch):
    """A storage account on this machine, handed out as the managed identity would be."""
    from vectrixdb import evaluation

    service = FakeBlobService(audit=FakeAppendContainer())
    monkeypatch.setattr(evaluation, "_blob_client", lambda account: service)
    return service


class TestAFile:
    def test_lines_go_in_and_come_back_in_order(self, tmp_path):
        log = FileLog(tmp_path / "logs" / "audit.jsonl")
        assert log.lines() == []
        log.append('{"n": 1}')
        log.append('{"n": 2}\n')
        assert log.lines() == ['{"n": 1}', '{"n": 2}'] and log.describe().endswith("audit.jsonl")


class TestBlob:
    def test_a_container_that_would_let_a_line_be_changed_is_refused(self):
        with pytest.raises(ConfigurationError, match="no immutability policy"):
            BlobDayLog(FakeAppendContainer(locked=False), "decisions")
        opened = BlobDayLog(FakeAppendContainer(locked=False), "decisions", require_lock=False)
        assert opened.lock_configuration == {}
        assert BlobDayLog(FakeAppendContainer(), "decisions").lock_configuration == {"has_immutability_policy": True}

    def test_a_line_goes_into_its_days_blob_made_the_first_time(self):
        container = FakeAppendContainer()
        log = BlobDayLog(container, "decisions/", clock=at(SEPT_23))
        log.append('{"n": 1}')
        log.append('{"n": 2}')
        assert container.made == ["decisions/2026/09/23.jsonl"], "made once, not once a line"
        assert bytes(container.blobs["decisions/2026/09/23.jsonl"]) == b'{"n": 1}\n{"n": 2}\n'
        assert container.blocks["decisions/2026/09/23.jsonl"] == 2, "a line a block, appended whole or not at all"

    def test_several_servers_append_to_one_days_blob(self):
        container = FakeAppendContainer()
        first, second = (BlobDayLog(container, "access", clock=at(SEPT_23)) for _ in range(2))
        first.append("a")
        container.not_there_yet = 1  # the second server raced the first to make it
        second.append("b")
        first.append("c")
        assert container.made == ["access/2026/09/23.jsonl"]
        assert first.lines() == ["a", "b", "c"] == second.lines()

    def test_a_full_blob_goes_on_in_a_numbered_one_and_reads_back_in_order(self):
        container = FakeAppendContainer(limit=2)
        log = BlobDayLog(container, "access", clock=at(SEPT_23))
        for n in range(5):
            log.append(str(n))
        assert sorted(container.blobs) == ["access/2026/09/23.jsonl", "access/2026/09/23.p001.jsonl", "access/2026/09/23.p002.jsonl"]
        assert log.lines() == ["0", "1", "2", "3", "4"]
        later = BlobDayLog(container, "access", clock=at(SEPT_23))
        later.append("5")
        assert log.lines()[-1] == "5", "a server that starts later finds the part in use"

    def test_days_read_in_order_and_one_before_since_is_not_read(self):
        container = FakeAppendContainer()
        for day, line in ((22, "yesterday"), (23, "today"), (21, "before")):
            BlobDayLog(container, "d", clock=at(datetime(2026, 9, day, 12, tzinfo=timezone.utc).timestamp())).append(line)
        container.blobs["d/notes.txt"] = bytearray(b"not a day\n")
        log = BlobDayLog(container, "d")
        assert log.lines() == ["before", "yesterday", "today"], "a blob that is not a day is not read"
        assert log.lines(since=datetime(2026, 9, 22, 23, tzinfo=timezone.utc).timestamp()) == ["yesterday", "today"]

    def test_a_policy_that_forbids_appending_says_what_to_change(self):
        log = BlobDayLog(FakeAppendContainer(appends=False), "decisions", clock=at(SEPT_23))
        with pytest.raises(ConfigurationError, match="Allow protected append writes"):
            log.append("x")

    def test_an_address_opens_its_container_and_prefix(self, blobs):
        assert is_blob_address(f"{ACCOUNT}/audit/decisions") and not is_blob_address("/var/log/audit.jsonl")
        log = open_append_log(f"{ACCOUNT}/audit/decisions", setting="VECTRIXDB_AUDIT_STORE")
        assert blobs.asked == ["audit"] and log.describe() == "Blob vxtest1234store.blob.core.windows.net/audit/decisions"
        with pytest.raises(ConfigurationError, match="no container"):
            open_append_log(f"{ACCOUNT}/", setting="VECTRIXDB_AUDIT_STORE")
        with pytest.raises(ConfigurationError, match="VECTRIXDB_ACCESS_LOG is a path or https"):
            open_append_log("s3://bucket/access", setting="VECTRIXDB_ACCESS_LOG")
        assert isinstance(open_append_log("audit.jsonl", setting="VECTRIXDB_AUDIT_STORE"), FileLog)


class TestTheAuditStore:
    def test_each_address_is_the_sink_for_its_store(self, tmp_path, blobs):
        from vectrixdb.audit import DENY, AppendLogSink, JSONLSink, audit_sink_at

        made = audit_sink_at(f"{ACCOUNT}/audit/decisions", query_key=b"k", on_failure=DENY)
        assert isinstance(made, AppendLogSink)
        assert isinstance(audit_sink_at(tmp_path / "audit.jsonl", query_key=b"k", on_failure=DENY), JSONLSink)
        with pytest.raises(ConfigurationError, match="VECTRIXDB_AUDIT_RETAIN_DAYS"):
            audit_sink_at("s3://bucket/audit", query_key=b"k", on_failure=DENY)
        with pytest.raises(ConfigurationError, match="not mongodb://"):
            audit_sink_at("mongodb://db/audit", query_key=b"k", on_failure=DENY)

    def test_a_decision_goes_into_the_days_blob_and_is_found_again(self, blobs):
        from vectrixdb.audit import DENY, audit_records_at, audit_sink_at

        sink = audit_sink_at(f"{ACCOUNT}/audit/decisions", query_key=b"k", on_failure=DENY)
        sink._emit_raw({"decision_id": "dec_1", "collection": "financial", "outcome": "allowed"})
        sink._emit_raw({"ingestion_id": "ing_1", "collection": "financial"})
        assert [r.get("decision_id") or r.get("ingestion_id") for r in sink.read_all()] == ["dec_1", "ing_1"]
        assert sink.find("ing_1")["collection"] == "financial" and sink.find("nothing") is None
        assert audit_records_at(f"{ACCOUNT}/audit/decisions") == sink.read_all()
        (name,) = blobs.containers["audit"].blobs
        assert name.startswith("decisions/") and name.endswith(".jsonl")

    def test_a_store_that_is_down_refuses_the_answer_under_deny(self, blobs):
        from vectrixdb.audit import DENY, audit_sink_at
        from vectrixdb.exceptions import AuditUnavailable

        sink = audit_sink_at(f"{ACCOUNT}/audit/decisions", query_key=b"k", on_failure=DENY)

        class Down:
            def append(self, line):
                raise ConnectionError("the account is not answering")

        sink.log = Down()
        record = type("Record", (), {"to_dict": lambda self: {"decision_id": "dec_1"}, "recorded_at": None})()
        with pytest.raises(AuditUnavailable):
            sink.write(record)

    def test_a_table_is_not_read_back_and_nothing_shows_a_secret(self, tmp_path):
        from vectrixdb.audit import audit_records_at, audit_where, describe_audit_store

        assert audit_records_at("postgresql://writer@db.example.test/audit") is None
        (tmp_path / "audit.jsonl").write_text('{"decision_id": "dec_1"}\nnot json\n', encoding="utf-8")
        assert audit_records_at(tmp_path / "audit.jsonl") == [{"decision_id": "dec_1"}]
        assert describe_audit_store("postgresql://writer:hunter2@db.example.test/audit") == "PostgreSQL db.example.test/audit"
        assert describe_audit_store(f"{ACCOUNT}/audit/decisions?sig=secret") == f"{ACCOUNT}/audit/decisions"
        assert audit_where({"VECTRIXDB_AUDIT_JSONL": "a.jsonl"}) == "a.jsonl"
        assert audit_where({"VECTRIXDB_AUDIT_JSONL": "a.jsonl", "VECTRIXDB_AUDIT_STORE": "b.jsonl"}) == "b.jsonl"
        assert audit_where({}) is None


class TestTheAuditPage:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch, blobs):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from vectrixdb.api import server

        for name in ("VECTRIXDB_AUDIT_JSONL", "VECTRIXDB_SIGNIN", "VECTRIXDB_COLLECTION_STORE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("VECTRIXDB_API_KEY", "k")
        with TestClient(server.create_app(db_path=str(tmp_path / "db"), enable_dashboard=False)) as client:
            yield client

    def test_the_page_reads_the_decisions_back_from_blob(self, client, monkeypatch, blobs):
        from vectrixdb.audit import DENY, audit_sink_at

        monkeypatch.setenv("VECTRIXDB_AUDIT_STORE", f"{ACCOUNT}/audit/decisions")
        sink = audit_sink_at(f"{ACCOUNT}/audit/decisions", query_key=b"k", on_failure=DENY)
        now = datetime.now(timezone.utc).isoformat()
        sink._emit_raw({"decision_id": "dec_1", "collection": "financial", "outcome": "allowed", "decided_at": now, "principal_snapshot": {"clients": ["td"]}})
        sink._emit_raw({"decision_id": "dec_2", "collection": "financial", "outcome": "denied_in_scope", "decided_at": now})
        data = client.get("/api/v1/audit", headers={"api-key": "k"}).json()["data"]
        assert data["available"] is True and data["path"] == f"{ACCOUNT}/audit/decisions"
        assert [r["decision_id"] for r in data["records"]] == ["dec_2", "dec_1"]
        assert "principal_snapshot" not in data["records"][1], "the snapshot never leaves the store"
        assert data["counts"]["decisions"] == 2 and data["counts"]["denied"] == 1

    def test_a_table_says_it_is_read_where_it_is_kept(self, client, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_AUDIT_STORE", "postgresql://writer:hunter2@db.example.test/audit")
        data = client.get("/api/v1/audit", headers={"api-key": "k"}).json()["data"]
        assert data["available"] is False and "INSERT only" in data["reason"] and "hunter2" not in data["reason"]


class TestTheAccessLog:
    def test_sign_ins_and_reads_go_to_blob_and_the_pages_read_them_back(self, blobs):
        from vectrixdb.signin.access import AccessLog

        log = AccessLog(f"{ACCOUNT}/audit/access")
        assert log.kind == "blob" and log.path is None and log.where == "Blob vxtest1234store.blob.core.windows.net/audit/access"
        log.record("signin", who="ada@example.com", role="admin", method="email")
        log.record("search", who="ada@example.com", collection="financial", took_ms=12.5)
        log.record("read", who="key:nightly", method="key", collection="financial", item="td/ar2025.pdf#3")
        assert [line["event"] for line in log.recent()] == ["read", "search", "signin"]
        assert log.daily(3)["searches"][-1] == 1 and log.daily(3)["signins"][-1] == 1
        assert {row["who"] for row in log.readers(3)} == {"ada@example.com", "key:nightly"}
        (name,) = blobs.containers["audit"].blobs
        assert name == f"access/{time.strftime('%Y/%m/%d', time.gmtime())}.jsonl"

    def test_a_line_that_cannot_be_written_refuses_what_it_would_record(self, blobs):
        from vectrixdb.signin.access import AccessLog, AccessLogUnavailable

        log = AccessLog(f"{ACCOUNT}/audit/access")
        blobs.containers["audit"].appends = False
        with pytest.raises(AccessLogUnavailable, match="protected append writes"):
            log.record("signin", who="ada@example.com")

    def test_the_address_is_kept_whole_where_sign_in_is_configured(self, tmp_path):
        from vectrixdb.signin import SignInConfig

        env = {"VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SIGNIN_SECRET": "s" * 40, "VECTRIXDB_PUBLIC_URL": "http://localhost:7337"}
        blob = SignInConfig.from_env(str(tmp_path), {**env, "VECTRIXDB_ACCESS_LOG": f"{ACCOUNT}/audit/access"})
        assert blob.access_log == f"{ACCOUNT}/audit/access", "a path would fold its // into one"
        file = SignInConfig.from_env(str(tmp_path), env)
        assert file.access_log == tmp_path / "auth" / "access.jsonl"


class TestTheCheck:
    def found(self, tmp_path, **env):
        from vectrixdb.check import run

        return [(f.level, f.area, f.text) for f in run(str(tmp_path), {"VECTRIXDB_OFFLINE": "1", **env})]

    def test_an_s3_store_needs_its_days_and_a_blob_store_is_ready(self, tmp_path):
        found = self.found(tmp_path, VECTRIXDB_AUDIT_STORE="s3://bucket/audit", VECTRIXDB_AUDIT_QUERY_KEY="q")
        assert any(level == "error" and "VECTRIXDB_AUDIT_RETAIN_DAYS" in text for level, _, text in found)
        found = self.found(tmp_path, VECTRIXDB_AUDIT_STORE=f"{ACCOUNT}/audit/decisions?sig=secret", VECTRIXDB_AUDIT_QUERY_KEY="q")
        assert ("ok", "Audit", f"Search decisions under a policy are written to {ACCOUNT}/audit/decisions") in found

    def test_both_names_set_is_said_and_an_unknown_address_is_an_error(self, tmp_path):
        found = self.found(tmp_path, VECTRIXDB_AUDIT_STORE="a.jsonl", VECTRIXDB_AUDIT_JSONL="b.jsonl", VECTRIXDB_AUDIT_QUERY_KEY="q")
        assert any(level == "warn" and "both set" in text for level, _, text in found)
        found = self.found(tmp_path, VECTRIXDB_AUDIT_STORE="mongodb://db/audit", VECTRIXDB_AUDIT_QUERY_KEY="q")
        assert any(level == "error" and area == "Audit" for level, area, _ in found)

    def test_an_access_log_in_blob_is_ready(self, tmp_path):
        env = {"VECTRIXDB_SIGNIN": "email", "VECTRIXDB_SIGNIN_SECRET": "s" * 40, "VECTRIXDB_PUBLIC_URL": "http://localhost:7337"}
        found = self.found(tmp_path, **env, VECTRIXDB_ACCESS_LOG=f"{ACCOUNT}/audit/access")
        assert any(level == "ok" and area == "Access log" and "Appended to Blob" in text for level, area, text in found)
        found = self.found(tmp_path, **env, VECTRIXDB_ACCESS_LOG="s3://bucket/access")
        assert any(level == "error" and area == "Access log" for level, area, _ in found)
