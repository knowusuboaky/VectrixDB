"""The record of one policied read, and where it goes.

Two properties carry the weight here. The counts in the record have to be
right, because they are the evidence in a dispute. And the *undisclosable*
count must never reach the caller, because reporting it confirms that
documents exist outside the scope they were refused, which is the thing an
ethical wall is built to prevent. The record knows; the reader does not.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vectrixdb.audit import (
    DENY,
    RECORD_SCHEMA_VERSION,
    AuditContext,
    AuditSink,
    JSONLSink,
    MemorySink,
    RetrievalRecord,
    Spool,
)
from vectrixdb.exceptions import AuditUnavailable, ConfigurationError
from vectrixdb.policy import AtMost, Excludes, Overlap, Policy

LENDING = Policy(
    [
        Overlap("entitlements.allowed_roles", "roles"),
        AtMost("entitlements.classification_rank", "clearance_rank"),
        Overlap("entitlements.client_id", "client_coverage", scope=True),
        Excludes("entitlements.client_id", "wall_restrictions", scope=True),
    ],
    version="lending-v3",
)

ROWS = [
    ("Covenant: fixed charge coverage ratio not less than 1.15x", "CL-40219", 2),
    ("Covenant schedule tested quarterly against borrower accounts", "CL-40219", 2),
    ("Internal credit memo: covenant headroom is thin into Q3", "CL-40219", 5),
    ("Facility covenant for the other borrower, semi-annual", "CL-51330", 2),
]

#: On the deal team, cleared to 3. Loses the rank-5 memo inside a scope held.
ANALYST_A = {
    "roles": ["credit_analyst"],
    "clearance_rank": 3,
    "client_coverage": ["CL-40219"],
    "wall_restrictions": [],
}
#: Walled off CL-40219 entirely, with a book of their own.
ANALYST_B = {
    "roles": ["credit_analyst"],
    "clearance_rank": 3,
    "client_coverage": ["CL-51330"],
    "wall_restrictions": ["CL-40219"],
}


def chunk(client, rank):
    return {
        "entitlements": {
            "allowed_roles": ["credit_analyst"],
            "classification_rank": rank,
            "client_id": client,
        }
    }


@pytest.fixture
def sink():
    return MemorySink(query_key=b"deployment-key", on_failure=DENY)


@pytest.fixture
def db(tmp_path, sink):
    from vectrixdb import Vectrix

    db = Vectrix("lending", path=str(tmp_path), policy=LENDING, on_retrieval=sink)
    db.add(
        [text for text, _, _ in ROWS],
        metadata=[chunk(client, rank) for _, client, rank in ROWS],
    )
    yield db
    db.close()


# --------------------------------------------------------------------------
# The sink refuses to be configured carelessly
# --------------------------------------------------------------------------


class TestSinkConfiguration:
    def test_a_query_key_is_required(self):
        """A bare hash over a low-entropy query space is enumerable by
        whoever holds the log, which makes it a correlation key that reads
        like a redaction."""
        with pytest.raises(ConfigurationError, match="needs a query_key"):
            MemorySink(query_key=b"", on_failure=DENY)

    def test_a_failure_policy_is_required_and_has_no_default(self):
        """Whether a failed audit write should deny the answer or buffer and
        continue is a decision about the business, not the code."""
        with pytest.raises(ConfigurationError, match="no default"):
            JSONLSink("x.jsonl", query_key=b"k", on_failure="whatever")

    def test_the_fingerprint_is_keyed_not_a_bare_hash(self):
        import hashlib

        one = MemorySink(query_key=b"key-one", on_failure=DENY)
        two = MemorySink(query_key=b"key-two", on_failure=DENY)
        query = "covenant thresholds for this facility"

        assert one.fingerprint_query(query) == one.fingerprint_query(query)
        assert one.fingerprint_query(query) != two.fingerprint_query(query)
        assert hashlib.sha256(query.encode()).hexdigest() not in one.fingerprint_query(query)
        assert one.fingerprint_query(query).startswith("hmac-sha256:")


# --------------------------------------------------------------------------
# What the record says
# --------------------------------------------------------------------------


class TestTheRecord:
    def test_an_in_scope_redaction_is_counted_apart_from_a_wall(self, db, sink):
        """A is on the deal team and loses the memo to clearance, and cannot
        see B's book at all. One of each, and the record says which is
        which."""
        db.as_principal(ANALYST_A).search("covenant thresholds", limit=10)
        record = sink.records[-1]

        assert record.results_returned == 2
        assert record.withheld_disclosable == 1
        assert record.withheld_undisclosable == 1
        assert record.outcome == "allowed_with_redaction"

    def test_a_walled_principal_has_everything_undisclosable(self, db, sink):
        db.as_principal(ANALYST_B).search("covenant thresholds", limit=10)
        record = sink.records[-1]

        assert record.withheld_disclosable == 0
        assert record.withheld_undisclosable == 3

    def test_the_undisclosable_count_never_reaches_the_caller(self, db, sink):
        """The whole asymmetry, in one test. The log carries the full
        picture; the reader learns nothing. This is also why the audit store
        needs tighter access control than the index it audits."""
        results = db.as_principal(ANALYST_B).search("covenant thresholds", limit=10)
        record = sink.records[-1]

        assert record.withheld_undisclosable == 3
        payload = json.dumps(results.to_dict())
        assert "undisclosable" not in payload
        assert "3" not in [str(v) for v in (results.cut_count,)]
        assert not hasattr(results, "withheld_undisclosable")

    def test_the_principal_snapshot_is_inline_not_a_pointer(self, db, sink):
        """A pointer into an entitlement store with ninety-day retention is
        worthless in an audit kept seven years."""
        db.as_principal(ANALYST_A).search("covenant", limit=5)
        assert sink.records[-1].principal_snapshot == ANALYST_A

    def test_the_snapshot_age_comes_from_when_it_was_resolved(self, db, sink):
        taken = datetime.now(timezone.utc) - timedelta(seconds=41)
        db.as_principal(ANALYST_A, audit=AuditContext(snapshot_taken_at=taken)).search(
            "covenant", limit=5
        )
        age = sink.records[-1].principal_snapshot_age_ms
        assert 40_000 < age < 43_000

    def test_without_a_taken_at_there_is_no_age_rather_than_a_zero(self, db, sink):
        """Zero would read as "resolved this instant", which is the one
        answer a dispute must not be given by accident."""
        db.as_principal(ANALYST_A).search("covenant", limit=5)
        assert sink.records[-1].principal_snapshot_age_ms is None

    def test_the_host_fields_are_carried_through(self, db, sink):
        context = AuditContext(principal_id="u_10442", principal_type="user", trace_id="req_abc")
        db.as_principal(ANALYST_A, audit=context).search("covenant", limit=5)
        record = sink.records[-1]
        assert (record.principal_id, record.principal_type, record.trace_id) == (
            "u_10442",
            "user",
            "req_abc",
        )

    def test_the_policy_is_identified_by_fingerprint_and_label(self, db, sink):
        db.as_principal(ANALYST_A).search("covenant", limit=5)
        record = sink.records[-1]
        assert record.policy_fingerprint == LENDING.fingerprint
        assert record.policy_version == "lending-v3"
        assert len(record.rules_evaluated) == 4

    def test_the_record_carries_its_own_schema_version(self, db, sink):
        """These outlive the code that wrote them by years."""
        db.as_principal(ANALYST_A).search("covenant", limit=5)
        assert sink.raw[-1]["record_schema_version"] == RECORD_SCHEMA_VERSION

    def test_recorded_at_is_stamped_by_the_sink_not_the_decision(self, db, sink):
        """They differ by hours when a spool drains after an outage, and you
        need both to reason about the gap."""
        db.as_principal(ANALYST_A).search("covenant", limit=5)
        record = sink.records[-1]
        assert record.recorded_at is not None
        assert record.recorded_at >= record.decided_at

    def test_a_principal_matching_nothing_is_still_a_decision(self, db, sink):
        """What a revoked user looks like. Skipping the search is right; not
        recording that it happened is not."""
        db.as_principal(dict(ANALYST_A, roles=[])).search("covenant", limit=5)
        record = sink.records[-1]
        assert record.outcome == "denied_out_of_scope"
        assert record.results_returned == 0
        assert record.candidates_examined == 0

    def test_every_field_survives_serialisation(self, db, sink):
        db.as_principal(ANALYST_A, audit=AuditContext(principal_id="u1")).search("c", limit=5)
        payload = sink.raw[-1]
        json.dumps(payload)  # must not raise
        for key in (
            "decision_id",
            "decided_at",
            "recorded_at",
            "collection",
            "principal_snapshot",
            "policy_fingerprint",
            "query_fingerprint",
            "withheld_disclosable",
            "withheld_undisclosable",
            "outcome",
        ):
            assert key in payload, key

    def test_counts_say_when_they_are_only_lower_bounds(self, db, sink):
        """A count you cannot testify to should not look like one you can."""
        db.as_principal(ANALYST_A).search("covenant", limit=10)
        assert sink.records[-1].candidates_exhausted is True


# --------------------------------------------------------------------------
# The audited path must answer the same question as the plain one
# --------------------------------------------------------------------------


class TestTheTwoPathsAgree:
    def test_attaching_a_sink_does_not_change_what_comes_back(self, tmp_path, sink):
        """There are two search paths now. A record that costs you different
        results is worse than no record."""
        from vectrixdb import Vectrix

        audited = Vectrix("a", path=str(tmp_path / "a"), policy=LENDING, on_retrieval=sink)
        plain = Vectrix("b", path=str(tmp_path / "b"), policy=LENDING)
        try:
            for db in (audited, plain):
                db.add(
                    [text for text, _, _ in ROWS],
                    metadata=[chunk(c, r) for _, c, r in ROWS],
                )

            for principal in (ANALYST_A, ANALYST_B):
                one = audited.as_principal(principal).search("covenant thresholds", limit=10)
                two = plain.as_principal(principal).search("covenant thresholds", limit=10)
                assert [r.text for r in one] == [r.text for r in two]
        finally:
            audited.close()
            plain.close()

    def test_no_sink_means_no_record_and_the_ordinary_path(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("c", path=str(tmp_path), policy=LENDING)
        try:
            db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
            assert db.on_retrieval is None
            assert len(db.as_principal(ANALYST_A).search("covenant", limit=5)) == 1
        finally:
            db.close()


# --------------------------------------------------------------------------
# What happens when the sink is down
# --------------------------------------------------------------------------


class _BrokenSink(MemorySink):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.broken = True

    def _emit(self, record):
        if self.broken:
            raise OSError("audit store unreachable")
        super()._emit(record)

    def _emit_raw(self, payload):
        # The drain path has to fail too while the store is down, or a
        # backlog is lost the first time anybody retries.
        if self.broken:
            raise OSError("audit store unreachable")
        super()._emit_raw(payload)


class TestFailurePolicy:
    def test_deny_means_no_audit_no_answer(self, tmp_path):
        from vectrixdb import Vectrix

        broken = _BrokenSink(query_key=b"k", on_failure=DENY)
        db = Vectrix("d", path=str(tmp_path), policy=LENDING, on_retrieval=broken)
        try:
            broken.broken = False
            db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
            broken.broken = True
            with pytest.raises(AuditUnavailable, match="failure policy is DENY"):
                db.as_principal(ANALYST_A).search("covenant", limit=5)
        finally:
            db.close()

    def test_deny_stops_a_write_too(self, tmp_path):
        """The same rule on the other path. An audit that stays up for reads
        and lets unrecorded writes through is not an audit."""
        from vectrixdb import Vectrix

        broken = _BrokenSink(query_key=b"k", on_failure=DENY)
        db = Vectrix("w", path=str(tmp_path), policy=LENDING, on_retrieval=broken)
        try:
            with pytest.raises(AuditUnavailable):
                db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
        finally:
            db.close()

    def test_spool_answers_and_buffers(self, tmp_path):
        from vectrixdb import Vectrix

        spool = tmp_path / "spool" / "audit.jsonl"
        broken = _BrokenSink(query_key=b"k", on_failure=Spool(spool))
        db = Vectrix("e", path=str(tmp_path / "db"), policy=LENDING, on_retrieval=broken)
        try:
            db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
            results = db.as_principal(ANALYST_A).search("covenant", limit=5)

            assert len(results) == 1, "the read completes"
            assert broken.records == [], "and nothing reached the sink"
            assert spool.exists()
            buffered = json.loads(spool.read_text(encoding="utf-8").splitlines()[0])
            assert buffered["policy_fingerprint"] == LENDING.fingerprint
        finally:
            db.close()

    def test_draining_replays_the_backlog_and_clears_it(self, tmp_path):
        spool = tmp_path / "audit.jsonl"
        sink = _BrokenSink(query_key=b"k", on_failure=Spool(spool))

        for _ in range(3):
            sink.write(RetrievalRecord(RetrievalRecord.new_id(), datetime.now(timezone.utc), "c"))
        assert len(spool.read_text(encoding="utf-8").splitlines()) == 3

        sink.broken = False
        assert sink.drain() == 3
        assert not spool.exists()
        assert len(sink.raw) == 3

    def test_draining_a_sink_that_is_still_down_keeps_the_backlog(self, tmp_path):
        """Losing the buffer because the store came back for one write and
        then went away again would be the worst of both."""
        spool = tmp_path / "audit.jsonl"
        sink = _BrokenSink(query_key=b"k", on_failure=Spool(spool))
        for _ in range(3):
            sink.write(RetrievalRecord(RetrievalRecord.new_id(), datetime.now(timezone.utc), "c"))

        assert sink.drain() == 0
        assert len(spool.read_text(encoding="utf-8").splitlines()) == 3

    def test_draining_without_a_spool_is_a_no_op(self):
        assert MemorySink(query_key=b"k", on_failure=DENY).drain() == 0


# --------------------------------------------------------------------------
# The file sink
# --------------------------------------------------------------------------


class TestJSONLSink:
    def test_one_record_per_line_and_it_reads_back(self, tmp_path):
        path = tmp_path / "nested" / "audit.jsonl"
        sink = JSONLSink(path, query_key=b"k", on_failure=DENY)

        for _ in range(3):
            sink.write(RetrievalRecord(RetrievalRecord.new_id(), datetime.now(timezone.utc), "c"))

        assert len(path.read_text(encoding="utf-8").splitlines()) == 3
        back = sink.read_all()
        assert len(back) == 3
        assert {r["collection"] for r in back} == {"c"}

    def test_it_appends_rather_than_rewrites(self, tmp_path):
        """An audit file a process can truncate is not an audit file. This
        only proves the open mode; durability past that is the storage's
        job, which is why the docstring points at object lock."""
        path = tmp_path / "audit.jsonl"
        first = JSONLSink(path, query_key=b"k", on_failure=DENY)
        first.write(RetrievalRecord("dec_1", datetime.now(timezone.utc), "c"))

        second = JSONLSink(path, query_key=b"k", on_failure=DENY)
        second.write(RetrievalRecord("dec_2", datetime.now(timezone.utc), "c"))

        assert [r["decision_id"] for r in second.read_all()] == ["dec_1", "dec_2"]

    def test_reading_a_file_that_is_not_there_is_empty_not_an_error(self, tmp_path):
        assert JSONLSink(tmp_path / "nope.jsonl", query_key=b"k", on_failure=DENY).read_all() == []


def test_the_base_sink_has_to_be_subclassed():
    class Bare(AuditSink):
        pass

    with pytest.raises(NotImplementedError):
        Bare(query_key=b"k", on_failure=DENY).write(
            RetrievalRecord("dec_x", datetime.now(timezone.utc), "c")
        )


# --------------------------------------------------------------------------
# Tying an answer to its record, and to the index that produced it
# --------------------------------------------------------------------------


class TestCorrelation:
    """A record nobody can reach from an answer is a record that gets read
    once, during the investigation that discovers it cannot be reached."""

    def test_the_answer_carries_the_id_of_its_own_record(self, db, sink):
        results = db.as_principal(ANALYST_A).search("covenant thresholds", limit=10)
        assert results.decision_id is not None
        assert results.decision_id == sink.records[-1].decision_id

    def test_two_searches_get_two_ids(self, db, sink):
        first = db.as_principal(ANALYST_A).search("covenant", limit=5)
        second = db.as_principal(ANALYST_A).search("covenant", limit=5)
        assert first.decision_id != second.decision_id
        assert {r.decision_id for r in sink.records[-2:]} == {
            first.decision_id,
            second.decision_id,
        }

    def test_it_survives_serialisation(self, db, sink):
        results = db.as_principal(ANALYST_A).search("covenant", limit=5)
        payload = json.dumps(results.to_dict())
        assert results.decision_id in payload

        from vectrixdb.easy import Results

        assert Results.from_dict(json.loads(payload)).decision_id == results.decision_id

    def test_an_unaudited_search_has_none_rather_than_a_made_up_one(self, tmp_path):
        """No sink, no record, so there is nothing for an id to point at.
        None says that; an id would be a reference to nothing."""
        from vectrixdb import Vectrix

        plain = Vectrix("plain", path=str(tmp_path), policy=LENDING)
        try:
            plain.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
            results = plain.as_principal(ANALYST_A).search("covenant", limit=5)
            assert results.decision_id is None
        finally:
            plain.close()

    def test_showing_it_to_a_caller_reveals_nothing(self, db, sink):
        """It is safe to put in a response, a support ticket or a screenshot:
        it names a decision, never a document."""
        results = db.as_principal(ANALYST_B).search("covenant thresholds", limit=10)
        assert results.decision_id.startswith("dec_")
        for text, _, _ in ROWS:
            assert text not in results.decision_id
        assert "CL-40219" not in results.decision_id


class TestIndexBuildId:
    """Which index answered. The first thing anybody needs when an assistant
    has said something wrong, and the thread every other lineage question
    hangs off."""

    def test_a_write_stamps_a_build_and_the_record_names_it(self, db, sink):
        db.as_principal(ANALYST_A).search("covenant", limit=5)
        assert sink.records[-1].index_build_id == db.index_build_id
        assert db.index_build_id.startswith("build_")

    def test_a_later_write_moves_it(self, db, sink):
        before = db.index_build_id
        db.add(["A further covenant schedule"], metadata=[chunk("CL-40219", 2)])
        assert db.index_build_id != before

        db.as_principal(ANALYST_A).search("covenant", limit=5)
        assert sink.records[-1].index_build_id == db.index_build_id

    def test_it_is_read_per_search_rather_than_cached_on_the_view(self, db, sink):
        """A view made before a write would otherwise keep naming the build
        that preceded it, which is the one answer the field must not give."""
        view = db.as_principal(ANALYST_A)
        db.add(["Yet another covenant"], metadata=[chunk("CL-40219", 2)])

        view.search("covenant", limit=5)
        assert sink.records[-1].index_build_id == db.index_build_id

    def test_a_refusal_names_the_build_that_refused(self, db, sink):
        db.as_principal(dict(ANALYST_A, roles=[])).search("covenant", limit=5)
        assert sink.records[-1].index_build_id == db.index_build_id

    def test_it_is_stamped_whether_or_not_anything_is_auditing(self, tmp_path):
        """So the field is populated the day auditing is switched on, rather
        than starting from whenever somebody remembered to."""
        from vectrixdb import Vectrix

        plain = Vectrix("plain", path=str(tmp_path))
        try:
            assert plain.index_build_id is None
            plain.add(["covenant ratio"])
            assert plain.index_build_id is not None
        finally:
            plain.close()

    def test_it_survives_reopening(self, tmp_path):
        from vectrixdb import Vectrix

        first = Vectrix("persist", path=str(tmp_path))
        first.add(["covenant ratio"])
        stamped = first.index_build_id
        first.close()

        again = Vectrix("persist", path=str(tmp_path))
        try:
            assert again.index_build_id == stamped
        finally:
            again.close()


# --------------------------------------------------------------------------
# The PostgreSQL sink
# --------------------------------------------------------------------------


def _pg_connection():
    """A stand-in for psycopg2, running the sink's real SQL against sqlite3.

    The same fake both PostgreSQL storage backends are tested against. No
    database, no Docker, per the project's mocks-only rule; a live run would
    be a gated nightly job.
    """
    import sys
    from pathlib import Path

    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from fake_postgres import FakeConnection

    return FakeConnection()


def _with_table(conn, table="public.retrieval_decisions"):
    """Run both shipped DDL scripts verbatim, semicolons and all.

    Both, because a write is audited as well as a read and they go to
    different tables: an ingestion record is a different shape from a
    decision, not a nullable half of one.
    """
    from vectrixdb.audit import PostgresAuditSink

    with conn.cursor() as cur:
        cur.execute(PostgresAuditSink.SCHEMA.format(table=table))
        cur.execute(PostgresAuditSink.INGESTION_SCHEMA.format(table="public.ingestion_events"))
    conn.commit()
    return conn


def _record(**overrides):
    from vectrixdb.audit import RetrievalRecord

    fields = {
        "decision_id": RetrievalRecord.new_id(),
        "decided_at": datetime.now(timezone.utc),
        "collection": "lending",
        "principal_id": "u_20871",
        "principal_snapshot": {"roles": ["credit_analyst"], "clearance_rank": 3},
        "policy_fingerprint": "sha256:abc",
        "policy_version": "lending-v3",
        "rules_evaluated": ["Overlap(a <- b)", "Excludes(c <- d)"],
        "rules_denied": ["Excludes(c <- d)"],
        "query_fingerprint": "hmac-sha256:deadbeef",
        "candidates_examined": 7,
        "results_returned": 0,
        "withheld_disclosable": 0,
        "withheld_undisclosable": 7,
        "duration_ms": 12.5,
        "outcome": "denied_out_of_scope",
    }
    fields.update(overrides)
    return RetrievalRecord(**fields)


class TestPostgresSink:
    def test_a_missing_table_is_caught_at_construction_with_the_ddl(self):
        """Finding this on the first policied search means finding it in
        production, under a failure policy, at the worst possible moment."""
        from vectrixdb.audit import DENY, PostgresAuditSink

        with pytest.raises(ConfigurationError) as info:
            PostgresAuditSink(connection=_pg_connection(), query_key=b"k", on_failure=DENY)

        message = str(info.value)
        assert "does not exist" in message
        assert "CREATE TABLE" in message
        assert "GRANT INSERT" in message

    def test_a_table_missing_a_column_names_what_is_missing(self):
        """And says to add the column rather than recreate the table, which
        would take the history with it."""
        from vectrixdb.audit import DENY, PostgresAuditSink

        conn = _pg_connection()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE public.retrieval_decisions ("
                "decision_id TEXT PRIMARY KEY, collection TEXT)"
            )
        conn.commit()

        with pytest.raises(ConfigurationError, match="is missing"):
            PostgresAuditSink(connection=conn, query_key=b"k", on_failure=DENY)

    def test_a_record_round_trips_through_real_sql(self):
        from vectrixdb.audit import DENY, PostgresAuditSink

        conn = _with_table(_pg_connection())
        sink = PostgresAuditSink(connection=conn, query_key=b"k", on_failure=DENY)
        sink.write(_record(decision_id="dec_round_trip"))

        with conn.cursor() as cur:
            cur.execute(
                "SELECT principal_snapshot, rules_denied, withheld_undisclosable, outcome "
                "FROM public.retrieval_decisions WHERE decision_id = %s",
                ("dec_round_trip",),
            )
            snapshot, denied, undisclosable, outcome = tuple(cur.fetchone())

        # JSONB, not a string that looks like JSON.
        assert snapshot == {"roles": ["credit_analyst"], "clearance_rank": 3}
        assert denied == ["Excludes(c <- d)"]
        assert undisclosable == 7
        assert outcome == "denied_out_of_scope"

    def test_replaying_a_decision_does_not_double_count_it(self):
        """A spool drains after the store comes back, and some of what it
        holds may already have landed. A duplicate is not a second decision."""
        from vectrixdb.audit import DENY, PostgresAuditSink

        conn = _with_table(_pg_connection())
        sink = PostgresAuditSink(connection=conn, query_key=b"k", on_failure=DENY)

        record = _record(decision_id="dec_twice")
        sink.write(record)
        sink.write(record)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.retrieval_decisions")
            assert tuple(cur.fetchone())[0] == 1

    def test_the_columns_match_the_record_exactly(self):
        """A field added to the record and not to the table is a field the
        audit silently stops keeping, which nothing else would catch."""
        from vectrixdb.audit import PostgresAuditSink

        assert set(_record().to_dict()) == set(PostgresAuditSink.COLUMNS)

    def test_the_shipped_ddl_covers_every_column(self):
        from vectrixdb.audit import PostgresAuditSink

        ddl = PostgresAuditSink.SCHEMA.format(table="t")
        for column in PostgresAuditSink.COLUMNS:
            assert f"    {column} " in ddl, column

    def test_the_grants_withhold_the_ones_that_rewrite_history(self):
        """The grants are the deliverable here, not the SQL. A role that can
        UPDATE or DELETE makes this a log the audited application edits."""
        from vectrixdb.audit import PostgresAuditSink

        grants = PostgresAuditSink.GRANTS.format(table="t", writer="app")
        assert "GRANT INSERT ON t TO app;" in grants
        assert "REVOKE ALL ON t FROM PUBLIC;" in grants
        statements = [line for line in grants.splitlines() if not line.strip().startswith("--")]
        body = "\n".join(statements).upper()
        for forbidden in ("GRANT UPDATE", "GRANT DELETE", "GRANT TRUNCATE", "GRANT ALL"):
            assert forbidden not in body

    def test_an_identifier_that_is_not_an_identifier_is_refused(self):
        """The table name is interpolated rather than bound, because an
        identifier cannot be a query parameter, so it is checked instead."""
        from vectrixdb.audit import DENY, PostgresAuditSink

        with pytest.raises(ConfigurationError, match="plain identifiers"):
            PostgresAuditSink(
                connection=_pg_connection(),
                query_key=b"k",
                on_failure=DENY,
                table="decisions; DROP TABLE users",
            )

    def test_it_needs_somewhere_to_connect(self):
        from vectrixdb.audit import DENY, PostgresAuditSink

        with pytest.raises(ConfigurationError, match="dsn or a connection"):
            PostgresAuditSink(query_key=b"k", on_failure=DENY)

    def test_a_store_that_goes_away_spools_rather_than_failing_the_read(self):
        from vectrixdb.audit import PostgresAuditSink, Spool

        conn = _with_table(_pg_connection())
        spool = Path(tempfile.mkdtemp()) / "audit.jsonl"
        sink = PostgresAuditSink(connection=conn, query_key=b"k", on_failure=Spool(spool))

        class _Dead:
            def cursor(self, *a, **k):
                raise OSError("server closed the connection unexpectedly")

            def rollback(self):
                pass

        sink._conn = _Dead()
        sink.write(_record(decision_id="dec_while_down"))

        assert spool.exists()
        assert json.loads(spool.read_text(encoding="utf-8").splitlines()[0])["decision_id"] == (
            "dec_while_down"
        )

    def test_draining_the_spool_lands_the_backlog(self):
        from vectrixdb.audit import PostgresAuditSink, Spool

        conn = _with_table(_pg_connection())
        spool = Path(tempfile.mkdtemp()) / "audit.jsonl"
        sink = PostgresAuditSink(connection=conn, query_key=b"k", on_failure=Spool(spool))

        class _Dead:
            def cursor(self, *a, **k):
                raise OSError("server closed the connection unexpectedly")

            def rollback(self):
                pass

        live, sink._conn = sink._conn, _Dead()
        for index in range(3):
            sink.write(_record(decision_id=f"dec_backlog_{index}"))

        sink._conn = live
        assert sink.drain() == 3
        assert not spool.exists()

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.retrieval_decisions")
            assert tuple(cur.fetchone())[0] == 3

    def test_a_policied_search_lands_a_row(self, tmp_path):
        """The whole path, end to end: a policy, a principal, a search, and a
        row in an append-only table."""
        from vectrixdb import Vectrix
        from vectrixdb.audit import DENY, AuditContext, PostgresAuditSink

        conn = _with_table(_pg_connection())
        sink = PostgresAuditSink(connection=conn, query_key=b"k", on_failure=DENY)

        db = Vectrix("lending", path=str(tmp_path), policy=LENDING, on_retrieval=sink)
        try:
            db.add(
                [text for text, _, _ in ROWS],
                metadata=[chunk(client, rank) for _, client, rank in ROWS],
            )
            db.as_principal(ANALYST_B, audit=AuditContext(principal_id="u_20871")).search(
                "covenant thresholds", limit=10
            )
        finally:
            db.close()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT principal_id, withheld_disclosable, withheld_undisclosable, "
                "policy_fingerprint FROM public.retrieval_decisions"
            )
            principal_id, disclosable, undisclosable, fingerprint = tuple(cur.fetchone())

        assert principal_id == "u_20871"
        assert (disclosable, undisclosable) == (0, 3)
        assert fingerprint == LENDING.fingerprint

    def test_an_older_table_is_told_which_columns_to_add(self):
        """Schema version 2 added result_ids. A table from version 1 refuses
        at construction and prints the ALTER TABLE rather than a CREATE, so
        nobody recreates a table that holds the history."""
        from vectrixdb.audit import DENY, PostgresAuditSink

        conn = _pg_connection()
        older = "\n".join(
            line for line in PostgresAuditSink.SCHEMA.splitlines() if "result_ids" not in line
        )
        assert "result_ids" not in older
        with conn.cursor() as cur:
            cur.execute(older.format(table="public.retrieval_decisions"))
            cur.execute(PostgresAuditSink.INGESTION_SCHEMA.format(table="public.ingestion_events"))
        conn.commit()

        with pytest.raises(ConfigurationError) as info:
            PostgresAuditSink(connection=conn, query_key=b"k", on_failure=DENY)

        assert "['result_ids']" in str(info.value)
        assert "ALTER TABLE public.retrieval_decisions ADD COLUMN IF NOT EXISTS result_ids" in str(
            info.value
        )

    def test_find_reads_a_decision_and_an_ingestion_back_through_real_sql(self):
        """Where a reproduction starts. Needs SELECT, which the application
        role deliberately lacks; an investigator connects as themselves."""
        from vectrixdb.audit import DENY, IngestionRecord, PostgresAuditSink

        conn = _with_table(_pg_connection())
        sink = PostgresAuditSink(connection=conn, query_key=b"k", on_failure=DENY)
        record = _record(result_ids=["a:0", "a:1"], results_returned=2)
        sink.write(record)
        sink.write_ingestion(
            IngestionRecord(
                ingestion_id="build_find",
                started_at=datetime.now(timezone.utc),
                collection="lending",
                chunking={"strategy": "recursive", "chunk_size": 60, "overlap": 10},
                ids_written=["a:0", "a:1"],
            )
        )

        found = sink.find(record.decision_id)
        assert found["decision_id"] == record.decision_id
        assert found["result_ids"] == ["a:0", "a:1"]
        assert found["principal_snapshot"] == record.principal_snapshot

        ingestion = sink.find("build_find")
        assert ingestion["record_kind"] == "ingestion"
        assert ingestion["chunking"]["chunk_size"] == 60
        assert ingestion["ids_written"] == ["a:0", "a:1"]
        assert sink.find("dec_nothing") is None


class TestTheWritePath:
    """A different audit question from a read, which is why it is a different
    record rather than a flag on the same one. A read asks whether somebody
    should have seen what they saw. A write asks whether these documents are
    supposed to be here and whether you can show where they came from.
    """

    @pytest.fixture
    def db(self, tmp_path, sink):
        from vectrixdb import Vectrix

        db = Vectrix("lending", path=str(tmp_path), policy=LENDING, on_retrieval=sink)
        yield db
        db.close()

    def test_a_write_produces_a_record(self, db, sink):
        db.add(["covenant ratio", "covenant schedule"], metadata=[chunk("CL-40219", 2)] * 2)

        record = sink.raw[-1]
        assert record["record_kind"] == "ingestion"
        assert record["documents_written"] == 2
        assert record["collection"] == "lending"

    def test_it_runs_as_a_service_not_a_principal(self, db, sink):
        """The two land in the same store, and an investigator filtering on
        principal_type is asking one question or the other."""
        db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
        assert sink.raw[-1]["principal_type"] == "service"

    def test_the_ingestion_id_is_the_build_a_later_read_names(self, db, sink):
        """The join. From an answer, to the decision that produced it, to the
        ingestion that put the documents there."""
        db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
        ingestion = sink.raw[-1]["ingestion_id"]

        db.as_principal(ANALYST_A).search("covenant", limit=5)
        decision = sink.records[-1]

        assert decision.index_build_id == ingestion

    def test_a_second_write_is_a_second_ingestion(self, db, sink):
        db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
        first = sink.raw[-1]["ingestion_id"]
        db.add(["covenant schedule"], metadata=[chunk("CL-40219", 2)])
        second = sink.raw[-1]["ingestion_id"]
        assert first != second

    def test_it_carries_the_lineage_that_makes_the_index_reproducible(self, db, sink):
        """The model decides what the vectors mean, so a collection rebuilt
        with a different one is not the same index however similar it looks."""
        db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
        record = sink.raw[-1]
        assert record["embedding_model"]
        assert record["policy_fingerprint"] == LENDING.fingerprint

    def test_skipped_documents_are_counted_apart_from_written(self, tmp_path, sink):
        """Near-duplicate detection turning work away is a normal outcome,
        not a failure, and not the same number as what went in."""
        from vectrixdb import Vectrix

        db = Vectrix("dedupe", path=str(tmp_path), on_retrieval=sink)
        try:
            db.add(["the same covenant text"])
            db.add(
                ["the same covenant text", "an entirely different schedule"],
                dedupe=0.9,
            )
            record = sink.raw[-1]
            assert record["documents_written"] == 1
            assert record["documents_skipped"] == 1
        finally:
            db.close()

    def test_an_add_that_writes_nothing_mints_no_build_and_no_record(self, tmp_path, sink):
        """An ingestion record is about a build. A fully deduplicated add
        changes no index, so there is no build for a later read to name and
        nothing for a record to describe. Worth knowing rather than worth
        inventing an id for."""
        from vectrixdb import Vectrix

        db = Vectrix("nothing", path=str(tmp_path), on_retrieval=sink)
        try:
            db.add(["the same covenant text"])
            build = db.index_build_id
            before = len(sink.raw)

            db.add(["the same covenant text"], dedupe=0.9)

            assert db.index_build_id == build
            assert len(sink.raw) == before
        finally:
            db.close()

    def test_no_sink_means_no_record(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("quiet", path=str(tmp_path))
        try:
            db.add(["covenant ratio"])
            assert db.on_retrieval is None
        finally:
            db.close()

    def test_it_goes_through_the_same_failure_policy(self, tmp_path):
        """An audit that stays up for reads and silently drops writes is not
        an audit."""
        from vectrixdb import Vectrix
        from vectrixdb.audit import Spool

        spool = tmp_path / "spool" / "audit.jsonl"
        broken = _BrokenSink(query_key=b"k", on_failure=Spool(spool))
        db = Vectrix("spooled", path=str(tmp_path / "db"), on_retrieval=broken)
        try:
            db.add(["covenant ratio"])
        finally:
            db.close()

        assert spool.exists()
        buffered = json.loads(spool.read_text(encoding="utf-8").splitlines()[0])
        assert buffered["record_kind"] == "ingestion"

    def test_postgres_puts_it_in_its_own_table(self, tmp_path):
        """A different shape from a decision, so its own table rather than a
        nullable half of one."""
        from vectrixdb import Vectrix
        from vectrixdb.audit import DENY, PostgresAuditSink

        conn = _with_table(_pg_connection())
        with conn.cursor() as cur:
            cur.execute(PostgresAuditSink.INGESTION_SCHEMA.format(table="public.ingestion_events"))
        conn.commit()

        sink = PostgresAuditSink(connection=conn, query_key=b"k", on_failure=DENY)
        db = Vectrix("lending", path=str(tmp_path), policy=LENDING, on_retrieval=sink)
        try:
            db.add(["covenant ratio"], metadata=[chunk("CL-40219", 2)])
            db.as_principal(ANALYST_A).search("covenant", limit=5)
        finally:
            db.close()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT ingestion_id, documents_written, principal_type "
                "FROM public.ingestion_events"
            )
            ingestion_id, written, kind = tuple(cur.fetchone())
            cur.execute("SELECT index_build_id FROM public.retrieval_decisions")
            named = tuple(cur.fetchone())[0]

        assert (written, kind) == (1, "service")
        assert named == ingestion_id, "the two tables join on the build"

    def test_the_ingestion_columns_match_the_record(self):
        """A field added to one and not the other is a field the audit
        silently stops keeping."""
        from datetime import datetime, timezone

        from vectrixdb.audit import IngestionRecord, PostgresAuditSink

        record = IngestionRecord(
            ingestion_id="build_x", started_at=datetime.now(timezone.utc), collection="c"
        )
        payload = set(record.to_dict()) - {"record_kind"}
        assert payload == set(PostgresAuditSink.INGESTION_COLUMNS)
