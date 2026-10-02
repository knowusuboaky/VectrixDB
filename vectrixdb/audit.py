"""The record of one policied read, and where it goes.

The library emits, the host stores, and the library never picks a location.
An embedded library writing audit records somewhere the host did not ask for
is the same mistake as writing to stdout, with worse consequences: these
records are the evidence in a dispute, and their retention is usually longer
than the data they describe.

Two things about this record are easy to get wrong and expensive to discover
late.

**It is more sensitive than the index it audits.** It says, per principal,
which restricted documents exist and who was refused them. Somebody holding
the log can reconstruct an entire ethical wall and the book of business behind
it. It needs *stricter* access control than the collection, not the same, and
certainly not looser because "they are only logs". Put it somewhere the thing
being audited cannot rewrite: a separate database, a role with INSERT and no
UPDATE or DELETE, or an object store under an immutability lock. Never a file
on the application server that rotates away.

**The principal snapshot is stored inline, not as a pointer.** A pointer into
an entitlement store with ninety-day retention is worthless in an audit kept
seven years, and "was the entitlement in force at the time current?" is the
first question anybody asks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional, Sequence

from .exceptions import AuditUnavailable, ConfigurationError, DependencyError

__all__ = [
    "RECORD_SCHEMA_VERSION",
    "AuditContext",
    "RetrievalRecord",
    "IngestionRecord",
    "AuditSink",
    "MemorySink",
    "JSONLSink",
    "PostgresAuditSink",
    "AppendLogSink",
    "DENY",
    "Spool",
    "STORE_ENV",
    "audit_records_at",
    "audit_sink_at",
    "audit_where",
    "describe_audit_store",
]


# ============================================================================
# SETTINGS: the store's setting, and the record schema's version
# ============================================================================
#
# Which environment variable names the store, and the version every record
# carries so a reader knows its shape.

#: The setting that names where a server records its decisions.
STORE_ENV = "VECTRIXDB_AUDIT_STORE"

#: These records outlive the code that wrote them by years. A version costs
#: nothing now and is the difference between parsing and guessing later.
RECORD_SCHEMA_VERSION = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


# ============================================================================
# WHAT ONLY THE HOST KNOWS
# ============================================================================
#
# INPUT   the host's own facts
# OUTPUT  the fields the library cannot work out for itself, carried on every
#         record
#
# The library emits, the host stores, and the library never picks a location.


@dataclass(frozen=True)
class AuditContext:
    """The fields the library cannot work out for itself.

    Who the principal is, what request this belongs to, and when their
    entitlements were actually resolved. All optional, because a record with
    some of them is more useful than no record, but `snapshot_taken_at` is the
    one worth wiring up: without it there is no `principal_snapshot_age_ms`,
    and that is the field an access decision is challenged on.
    """

    principal_id: Optional[str] = None
    #: "user" or "service". The read path and the write path land in the same
    #: store and are audited against different questions.
    principal_type: str = "user"
    trace_id: Optional[str] = None
    #: When the host resolved these entitlements, not when it cached them.
    snapshot_taken_at: Optional[datetime] = None


# ============================================================================
# THE RECORD: one write, and one policied read
# ============================================================================
#
# INPUT   what a write or a read did
# OUTPUT  an ingestion record, and a retrieval record, each as it will be
#         stored
#
# Query text is never stored, only its keyed fingerprint.


@dataclass
class IngestionRecord:
    """One write, as it will be stored.

    A different question from a retrieval record, which is why it is a
    different shape rather than a flag on the same one. A read asks whether
    this person should have seen this. A write asks whether these documents
    are supposed to be here and whether you can show where they came from.

    The write path runs as a service principal, not a user, and that is not a
    detail: the two land in the same store and an investigator filtering by
    `principal_type` is asking one question or the other.
    """

    ingestion_id: str
    started_at: datetime
    collection: str
    backend: Optional[str] = None
    trace_id: Optional[str] = None
    recorded_at: Optional[datetime] = None
    record_schema_version: int = RECORD_SCHEMA_VERSION

    #: Who ran the ingestion. A service, almost always, and saying so is what
    #: keeps it apart from a read in the same store.
    principal_id: Optional[str] = None
    principal_type: str = "service"

    #: What went in. `skipped` is near-duplicate detection turning work away,
    #: which is a normal outcome and not a failure.
    documents_written: int = 0
    documents_skipped: int = 0

    #: Lineage. The model decides what the vectors mean, so a collection
    #: rebuilt with a different one is not the same index however similar it
    #: looks.
    embedding_model: Optional[str] = None
    #: The policy in force when these documents were written, which is not
    #: necessarily the one in force when they are read.
    policy_fingerprint: Optional[str] = None

    #: Provenance, when the write came through add_document: where the
    #: text came from, a hash of it, and how it was cut. A plain add() of
    #: texts has none of these, and says so with None rather than a guess.
    source: Optional[str] = None
    document_id: Optional[str] = None
    document_version: Optional[str] = None
    chunking: Optional[Mapping[str, Any]] = None
    #: Every id this write stored. The chunk carries its build id too, so
    #: the join runs both ways: from a chunk to the write that put it
    #: there, and from a write to everything it put there.
    ids_written: Sequence[str] = ()

    duration_ms: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_schema_version": self.record_schema_version,
            "record_kind": "ingestion",
            "ingestion_id": self.ingestion_id,
            "trace_id": self.trace_id,
            "started_at": _stamp(self.started_at),
            "recorded_at": _stamp(self.recorded_at),
            "collection": self.collection,
            "backend": self.backend,
            "principal_id": self.principal_id,
            "principal_type": self.principal_type,
            "documents_written": self.documents_written,
            "documents_skipped": self.documents_skipped,
            "embedding_model": self.embedding_model,
            "policy_fingerprint": self.policy_fingerprint,
            "source": self.source,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "chunking": dict(self.chunking) if self.chunking is not None else None,
            "ids_written": list(self.ids_written),
            "duration_ms": self.duration_ms,
        }


@dataclass
class RetrievalRecord:
    """One policied read, as it will be stored."""

    # -- identity and lineage
    decision_id: str
    decided_at: datetime
    collection: str
    backend: Optional[str] = None
    trace_id: Optional[str] = None
    recorded_at: Optional[datetime] = None
    record_schema_version: int = RECORD_SCHEMA_VERSION
    #: Which ingestion produced the index in force at query time. Every
    #: write mints a new one. A collection written before this existed has
    #: none, which reads as None rather than as a guess.
    index_build_id: Optional[str] = None
    #: "engine" when the backend applied the filter in the query, "post" when
    #: it filtered after fetching. None until backends declare it.
    pushdown_mode: Optional[str] = None

    # -- principal
    principal_id: Optional[str] = None
    principal_type: str = "user"
    principal_snapshot: Mapping[str, Any] = field(default_factory=dict)
    principal_snapshot_taken_at: Optional[datetime] = None
    principal_snapshot_age_ms: Optional[float] = None

    # -- policy
    policy_fingerprint: Optional[str] = None
    policy_version: Optional[str] = None
    rules_evaluated: Sequence[str] = ()
    rules_denied: Sequence[str] = ()

    # -- retrieval
    query_fingerprint: Optional[str] = None
    candidates_examined: Optional[int] = None
    #: True when the index had nothing further to offer, so the withheld
    #: counts below are exact. False when the audit window filled first and
    #: they are lower bounds. A count you cannot testify to should not look
    #: like one you can.
    candidates_exhausted: bool = True
    results_returned: int = 0
    #: Which chunks were handed over. Without these the record can say
    #: that seven documents were returned and not which seven, and the
    #: question that drives all of this is "where did that come from".
    #: The log is already the more sensitive artifact; this adds nothing
    #: to what it confirms.
    result_ids: Sequence[str] = ()
    #: Withheld from a principal who matched every scope rule. Safe to tell
    #: them about: they already know the thing exists.
    withheld_disclosable: Optional[int] = None
    #: Withheld from a principal outside the scope entirely. **Never surface
    #: this.** Reporting it confirms the documents are there, which is the one
    #: thing an ethical wall exists to prevent.
    withheld_undisclosable: Optional[int] = None

    # -- timing
    duration_ms: Optional[float] = None
    #: What the caller observed, once a timing floor padded it. The gap
    #: between this and duration_ms is the side channel that was closed.
    padded_to_ms: Optional[float] = None

    # -- outcome
    outcome: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Flat, append-only friendly, JSON-serialisable."""
        return {
            "record_schema_version": self.record_schema_version,
            "decision_id": self.decision_id,
            "trace_id": self.trace_id,
            "decided_at": _stamp(self.decided_at),
            "recorded_at": _stamp(self.recorded_at),
            "collection": self.collection,
            "index_build_id": self.index_build_id,
            "backend": self.backend,
            "pushdown_mode": self.pushdown_mode,
            "principal_id": self.principal_id,
            "principal_type": self.principal_type,
            "principal_snapshot": dict(self.principal_snapshot),
            "principal_snapshot_taken_at": _stamp(self.principal_snapshot_taken_at),
            "principal_snapshot_age_ms": self.principal_snapshot_age_ms,
            "policy_fingerprint": self.policy_fingerprint,
            "policy_version": self.policy_version,
            "rules_evaluated": list(self.rules_evaluated),
            "rules_denied": list(self.rules_denied),
            "query_fingerprint": self.query_fingerprint,
            "candidates_examined": self.candidates_examined,
            "candidates_exhausted": self.candidates_exhausted,
            "results_returned": self.results_returned,
            "result_ids": list(self.result_ids),
            "withheld_disclosable": self.withheld_disclosable,
            "withheld_undisclosable": self.withheld_undisclosable,
            "duration_ms": self.duration_ms,
            "padded_to_ms": self.padded_to_ms,
            "outcome": self.outcome,
        }

    @staticmethod
    def new_id() -> str:
        return "dec_" + uuid.uuid4().hex[:16]


# ============================================================================
# WHEN THE RECORD CANNOT BE WRITTEN
# ============================================================================
#
# INPUT   a sink that failed
# OUTPUT  no audit, no answer; or answer anyway, and spool the record to a
#         local file for later
#
# The host chooses; the library never quietly drops a record.


class _Deny:
    """No audit, no answer."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "DENY"


#: Fail the read when the record cannot be written. The right choice wherever
#: an unaudited answer would be worse than no answer.
DENY = _Deny()


@dataclass(frozen=True)
class Spool:
    """Answer anyway, and buffer the record to a local file for later.

    The other defensible choice, and a business decision rather than a
    technical one, which is why neither is the default. Drain it with
    :meth:`AuditSink.drain` once the real sink is reachable again.
    """

    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


# ============================================================================
# THE SINKS: memory, a file, PostgreSQL, and an append log
# ============================================================================
#
# INPUT   a decision record
# OUTPUT  kept in a list, for tests; one JSON line appended to a file; one row
#         in a PostgreSQL table; one line in a log nothing rewrites
#
# Each sink says what happens when it cannot write, and the failure policy
# above decides.


class AuditSink:
    """Where a decision record goes, and what happens when it cannot.

    Subclasses implement :meth:`_emit`. Everything else here is the part that
    should not be reimplemented per host: the failure policy, the spool, and
    the keyed query fingerprint.
    """

    #: Exceptions from :meth:`_emit` that mean "the store is unreachable"
    #: rather than "this code is wrong". Subclasses widen it: a PostgreSQL
    #: sink adds its driver's errors. Anything outside it propagates
    #: unchanged, because a TypeError quietly spooled and forgotten is a bug
    #: that never gets found, and reporting it as an outage sends whoever is
    #: on call to look at a database that is fine.
    TRANSIENT: ClassVar[tuple] = (OSError,)

    def __init__(self, *, query_key: bytes, on_failure: Any) -> None:
        """
        Args:
            query_key: the HMAC key for `query_fingerprint`. Required, and it
                belongs somewhere the audit store cannot reach. A bare
                SHA-256 over a low-entropy query space is enumerable by
                whoever holds the log, which makes it a correlation key that
                reads like a redaction.
            on_failure: `DENY` or `Spool(path)`. There is deliberately no
                default: which one is right is a decision about the business,
                not about the code.
        """
        if not query_key:
            raise ConfigurationError(
                "an audit sink needs a query_key. The query fingerprint is an HMAC, not a "
                "bare hash, because a hash over a low-entropy query space can be enumerated "
                "by anyone holding the log. Keep the key outside the audit store."
            )
        if on_failure is not DENY and not isinstance(on_failure, Spool):
            raise ConfigurationError(
                "on_failure must be DENY or Spool(path). It has no default: whether a failed "
                "audit write should deny the answer or buffer and continue is a business "
                "decision the library must not make for you."
            )
        self._query_key = bytes(query_key)
        self.on_failure = on_failure

    # -- to implement ----------------------------------------------------

    def _emit(self, record: RetrievalRecord) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- provided --------------------------------------------------------

    def fingerprint_query(self, text: str) -> str:
        """A correlation key for the query that does not carry the query.

        The query itself is often the sensitive part: "covenant thresholds
        for CL-40219" names the client. If you need the plaintext for quality
        work it belongs in a separate store, at a higher classification than
        this one and with a shorter retention.
        """
        digest = hmac.new(self._query_key, text.encode("utf-8"), hashlib.sha256)
        return "hmac-sha256:" + digest.hexdigest()

    def write_ingestion(self, record: "IngestionRecord") -> None:
        """Store one ingestion record, under the same failure policy.

        Deliberately the same path as :meth:`write`: an audit that stays up
        for reads and silently drops writes is not an audit, and a sink
        author should not have to remember two sets of rules.
        """
        self.write(record)

    def write(self, record: Any) -> None:
        """Store one record, or apply the failure policy."""
        record.recorded_at = _now()
        try:
            self._emit(record)
        except self.TRANSIENT as exc:
            if isinstance(self.on_failure, Spool):
                self._to_spool(record)
                return
            raise AuditUnavailable(type(self).__name__, str(exc)) from exc

    def _to_spool(self, record: RetrievalRecord) -> None:
        spool = self.on_failure
        assert isinstance(spool, Spool)
        spool.path.parent.mkdir(parents=True, exist_ok=True)
        with open(spool.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def drain(self) -> int:
        """Replay a spool into the sink and clear it. Returns how many moved.

        Stops at the first record that will not write and leaves the rest in
        place, so a sink that is still down does not lose the backlog.
        """
        if not isinstance(self.on_failure, Spool):
            return 0
        path = self.on_failure.path
        if not path.exists():
            return 0

        lines = path.read_text(encoding="utf-8").splitlines()
        moved = 0
        for index, line in enumerate(lines):
            if not line.strip():
                moved += 1
                continue
            try:
                self._emit_raw(json.loads(line))
            except self.TRANSIENT:
                remaining = lines[index:]
                path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
                return moved
            moved += 1
        path.unlink()
        return moved

    def _emit_raw(self, payload: Mapping[str, Any]) -> None:
        """Write an already-serialised record, used when draining a spool."""
        raise NotImplementedError  # pragma: no cover - overridden below

    def find(self, record_id: str) -> Optional[Mapping[str, Any]]:
        """One record by decision id or ingestion id, as a plain dict.

        Where a reproduction starts. The base sink cannot read, because a
        sink built for a store that only grants INSERT has nothing to read
        with, so this is what a subclass with a read path overrides.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot read records back. Fetch the record from "
            f"your audit store and pass it to reproduce() directly."
        )


class MemorySink(AuditSink):
    """Keeps records in a list. For tests, and for a host that wants to
    forward them into its own logging rather than a store of its own."""

    def __init__(self, *, query_key: bytes = b"test-key", on_failure: Any = DENY) -> None:
        super().__init__(query_key=query_key, on_failure=on_failure)
        self.records: list[RetrievalRecord] = []
        self.raw: list[Mapping[str, Any]] = []

    def _emit(self, record: RetrievalRecord) -> None:
        self.records.append(record)
        self.raw.append(record.to_dict())

    def _emit_raw(self, payload: Mapping[str, Any]) -> None:
        self.raw.append(payload)

    def find(self, record_id: str) -> Optional[Mapping[str, Any]]:
        for payload in self.raw:
            if record_id in (payload.get("decision_id"), payload.get("ingestion_id")):
                return payload
        return None


class JSONLSink(AuditSink):
    """Append one JSON object per line to a local file.

    The simplest thing that is honest, and the right shape for shipping into
    an object store under an immutability lock. It is *not* a system of
    record on its own: a file on the application server rotates away, dies
    with a scale-in, and is rewritable by whatever can write to it. Point it
    at storage you cannot edit, or use it as the spool in front of something
    you cannot.
    """

    def __init__(self, path: Any, *, query_key: bytes, on_failure: Any) -> None:
        super().__init__(query_key=query_key, on_failure=on_failure)
        self.path = Path(path)

    def _write_line(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            # A record that is only in the page cache is not an audit record.
            os.fsync(handle.fileno())

    def _emit(self, record: RetrievalRecord) -> None:
        self._write_line(record.to_dict())

    def _emit_raw(self, payload: Mapping[str, Any]) -> None:
        self._write_line(payload)

    def read_all(self) -> list[dict[str, Any]]:
        """Every record written so far. For tests and small investigations."""
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def find(self, record_id: str) -> Optional[Mapping[str, Any]]:
        for payload in self.read_all():
            if record_id in (payload.get("decision_id"), payload.get("ingestion_id")):
                return payload
        return None


class PostgresAuditSink(AuditSink):
    """Append one row per decision to a PostgreSQL table.

    The sink most deployments should be using, and the reason is the grants
    rather than the code. Point it at a **separate database** from the index,
    with a role that holds ``INSERT`` and neither ``UPDATE`` nor ``DELETE``,
    so the thing being audited cannot rewrite its own history. That is a
    property of how you provision it, not of anything written here, which is
    why the DDL and the grants ship as text you run rather than as something
    this issues for you: a role that can create the table can drop it.

        >>> sink = PostgresAuditSink(
        ...     dsn="postgresql://audit_writer@audit-host/audit",
        ...     query_key=DEPLOYMENT_KEY,
        ...     on_failure=Spool("/var/spool/vectrixdb-audit.jsonl"),
        ... )

    ``Spool`` is usually the right failure policy here rather than ``DENY``:
    the audit database is a different machine from the index, and a network
    blip that stops every search is its own kind of outage. That is a
    judgement about your service, which is why neither is a default.
    """

    #: Run this as a DBA, once, before pointing a sink at the table.
    SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    decision_id                  TEXT PRIMARY KEY,
    record_schema_version        INTEGER          NOT NULL,
    trace_id                     TEXT,
    decided_at                   TIMESTAMPTZ      NOT NULL,
    recorded_at                  TIMESTAMPTZ      NOT NULL,
    collection                   TEXT             NOT NULL,
    index_build_id               TEXT,
    backend                      TEXT,
    pushdown_mode                TEXT,
    principal_id                 TEXT,
    principal_type               TEXT,
    principal_snapshot           JSONB            NOT NULL,
    principal_snapshot_taken_at  TIMESTAMPTZ,
    principal_snapshot_age_ms    DOUBLE PRECISION,
    policy_fingerprint           TEXT,
    policy_version               TEXT,
    rules_evaluated              JSONB,
    rules_denied                 JSONB,
    query_fingerprint            TEXT,
    candidates_examined          INTEGER,
    candidates_exhausted         BOOLEAN,
    results_returned             INTEGER,
    result_ids                   JSONB,
    withheld_disclosable         INTEGER,
    withheld_undisclosable       INTEGER,
    duration_ms                  DOUBLE PRECISION,
    padded_to_ms                 DOUBLE PRECISION,
    outcome                      TEXT
);
"""

    #: The half that makes it an audit table rather than a log the audited
    #: application can edit.
    GRANTS = """
REVOKE ALL ON {table} FROM PUBLIC;
GRANT INSERT ON {table} TO {writer};
-- Deliberately no UPDATE, no DELETE, no TRUNCATE, and no ownership.
-- Reading is a separate grant to whoever investigates, and that role should
-- not be the application's: this table says which restricted documents exist
-- and who was refused them, so it is more sensitive than the index it audits.
"""

    COLUMNS = (
        "decision_id",
        "record_schema_version",
        "trace_id",
        "decided_at",
        "recorded_at",
        "collection",
        "index_build_id",
        "backend",
        "pushdown_mode",
        "principal_id",
        "principal_type",
        "principal_snapshot",
        "principal_snapshot_taken_at",
        "principal_snapshot_age_ms",
        "policy_fingerprint",
        "policy_version",
        "rules_evaluated",
        "rules_denied",
        "query_fingerprint",
        "candidates_examined",
        "candidates_exhausted",
        "results_returned",
        "result_ids",
        "withheld_disclosable",
        "withheld_undisclosable",
        "duration_ms",
        "padded_to_ms",
        "outcome",
    )

    #: Columns holding JSON, which need an explicit cast: a str bound to a
    #: jsonb column is a type error rather than a value.
    JSON_COLUMNS = frozenset(
        {"principal_snapshot", "rules_evaluated", "rules_denied", "result_ids"}
    )

    #: What a table created by an earlier VectrixDB needs, column by column.
    #: Adding rather than recreating, because recreating takes the history.
    SCHEMA_UPGRADE = """
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS result_ids JSONB;
"""
    INGESTION_SCHEMA_UPGRADE = """
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS document_id TEXT;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS document_version TEXT;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS chunking JSONB;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS ids_written JSONB;
"""

    def __init__(
        self,
        *,
        query_key: bytes,
        on_failure: Any,
        dsn: Optional[str] = None,
        connection: Any = None,
        table: str = "retrieval_decisions",
        ingestion_table: str = "ingestion_events",
        schema: str = "public",
    ) -> None:
        """
        Args:
            dsn: a libpq connection string. Ignored when `connection` is given.
            connection: an open DB-API connection to use instead of opening
                one. The seam exists so this is testable without a database,
                which the storage backends notably lack.
            table: unquoted table name, checked rather than bound, because an
                identifier cannot be a query parameter.
        """
        super().__init__(query_key=query_key, on_failure=on_failure)
        if not dsn and connection is None:
            raise ConfigurationError("PostgresAuditSink needs either a dsn or a connection")
        if not ingestion_table.replace("_", "").isalnum():
            raise ConfigurationError(
                f"ingestion_table must be a plain identifier: {ingestion_table!r}"
            )
        if not table.replace("_", "").isalnum() or not schema.replace("_", "").isalnum():
            raise ConfigurationError(
                f"table and schema must be plain identifiers, got {schema!r}.{table!r}"
            )
        self.dsn = dsn
        self.table = table
        self.ingestion_table = ingestion_table
        self.schema = schema
        self._conn = connection

        # psycopg2 may not be installed, so its error classes cannot be named
        # at module scope. A dead connection has to read as an outage rather
        # than a bug, or the failure policy never gets its chance.
        transient: tuple = (OSError,)
        try:
            import psycopg2

            transient = transient + (psycopg2.Error,)
        except ImportError:  # pragma: no cover - depends on install shape
            pass
        self.TRANSIENT = transient  # type: ignore[misc]

        self._verify_table()

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    def _connect(self) -> Any:
        if self._conn is None:
            try:
                import psycopg2
            except ImportError as exc:  # pragma: no cover - depends on install
                raise DependencyError("psycopg2", extra="postgres") from exc
            self._conn = psycopg2.connect(self.dsn)
        return self._conn

    def _drop_connection(self) -> None:
        """Forget a connection that failed, so the next write reconnects.

        Without this one network blip poisons the sink for the life of the
        process and every later write spools for no reason.
        """
        if self._conn is not None and self.dsn:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - already broken
                pass
            self._conn = None

    def _verify_table(self) -> None:
        """Fail at construction rather than at the first decision.

        A missing audit table found on the first policied search is found in
        production, under a failure policy, at the worst possible moment.
        """
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (self.schema, self.table),
            )
            present = {row[0] for row in cur.fetchall()}

        if not present:
            raise ConfigurationError(
                f"the audit table {self.qualified} does not exist. Create it as a DBA and "
                f"grant the application INSERT and nothing else:\n"
                + self.SCHEMA.format(table=self.qualified)
                + self.GRANTS.format(table=self.qualified, writer="your_app_role")
            )
        missing = [c for c in self.COLUMNS if c not in present]
        if missing:
            raise ConfigurationError(
                f"the audit table {self.qualified} is missing {missing}. It was probably "
                f"created by an older VectrixDB. Add the columns rather than recreating the "
                f"table, which would take the history with it:\n"
                + self.SCHEMA_UPGRADE.format(table=self.qualified)
            )

    def _insert(self, payload: Mapping[str, Any]) -> None:
        self._insert_into(self.qualified, self.COLUMNS, "decision_id", payload, self.JSON_COLUMNS)

    def _insert_into(self, table, columns, key, payload, json_columns) -> None:
        placeholders = ", ".join("%s::jsonb" if c in json_columns else "%s" for c in columns)
        values = [
            json.dumps(payload.get(c)) if c in json_columns else payload.get(c) for c in columns
        ]
        statement = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            # Draining a spool can replay a record that already landed. A
            # duplicate is not a second decision, nor a second ingestion.
            f"ON CONFLICT ({key}) DO NOTHING"
        )

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(statement, values)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # pragma: no cover - already broken
                pass
            self._drop_connection()
            raise

    #: Ingestion records are a different shape from decisions, so they get
    #: their own table rather than a nullable half of one. Same grants: the
    #: application inserts and can do nothing else.
    INGESTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    ingestion_id           TEXT PRIMARY KEY,
    record_schema_version  INTEGER          NOT NULL,
    trace_id               TEXT,
    started_at             TIMESTAMPTZ      NOT NULL,
    recorded_at            TIMESTAMPTZ      NOT NULL,
    collection             TEXT             NOT NULL,
    backend                TEXT,
    principal_id           TEXT,
    principal_type         TEXT,
    documents_written      INTEGER,
    documents_skipped      INTEGER,
    embedding_model        TEXT,
    policy_fingerprint     TEXT,
    source                 TEXT,
    document_id            TEXT,
    document_version       TEXT,
    chunking               JSONB,
    ids_written            JSONB,
    duration_ms            DOUBLE PRECISION
);
"""

    INGESTION_COLUMNS = (
        "ingestion_id",
        "record_schema_version",
        "trace_id",
        "started_at",
        "recorded_at",
        "collection",
        "backend",
        "principal_id",
        "principal_type",
        "documents_written",
        "documents_skipped",
        "embedding_model",
        "policy_fingerprint",
        "source",
        "document_id",
        "document_version",
        "chunking",
        "ids_written",
        "duration_ms",
    )
    INGESTION_JSON_COLUMNS = frozenset({"chunking", "ids_written"})

    def find(self, record_id: str) -> Optional[Mapping[str, Any]]:
        """One record by id, from the decisions table or the ingestion table.

        Needs SELECT, which the application role deliberately does not
        have: this is for whoever investigates, connected as themselves.
        """
        for table, columns, key, json_columns in (
            (self.qualified, self.COLUMNS, "decision_id", self.JSON_COLUMNS),
            (
                f"{self.schema}.{self.ingestion_table}",
                self.INGESTION_COLUMNS,
                "ingestion_id",
                self.INGESTION_JSON_COLUMNS,
            ),
        ):
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(columns)} FROM {table} WHERE {key} = %s", (record_id,)
                )
                row = cur.fetchone()
            if row is None:
                continue
            payload = dict(zip(columns, row))
            for column in json_columns:
                value = payload.get(column)
                if isinstance(value, str):
                    payload[column] = json.loads(value)
            if key == "ingestion_id":
                payload["record_kind"] = "ingestion"
            return payload
        return None

    def _emit(self, record: Any) -> None:
        payload = record.to_dict()
        if payload.get("record_kind") == "ingestion":
            self._insert_into(
                f"{self.schema}.{self.ingestion_table}",
                self.INGESTION_COLUMNS,
                "ingestion_id",
                payload,
                json_columns=self.INGESTION_JSON_COLUMNS,
            )
        else:
            self._insert(payload)

    def _emit_raw(self, payload: Mapping[str, Any]) -> None:
        if payload.get("record_kind") == "ingestion":
            self._insert_into(
                f"{self.schema}.{self.ingestion_table}",
                self.INGESTION_COLUMNS,
                "ingestion_id",
                payload,
                json_columns=self.INGESTION_JSON_COLUMNS,
            )
        else:
            self._insert(payload)


def _azure_errors() -> tuple:
    try:
        from azure.core.exceptions import AzureError
    except ImportError:  # pragma: no cover - the azure extra is optional
        return ()
    return (AzureError,)


class AppendLogSink(AuditSink):
    """One JSON line a record, appended to a log nothing rewrites.

    ``log`` is a path, a Blob address, or a log already made: see
    :mod:`vectrixdb.append_log`. At a Blob address the records go into one
    append blob a UTC day, in a container whose immutability policy keeps
    every line as it was written until the retention runs out, and the sink
    will not open a container without one. Several servers write to one day's
    blob safely, and the Audit page reads it back.
    """

    def __init__(self, log: Any, *, query_key: bytes, on_failure: Any) -> None:
        super().__init__(query_key=query_key, on_failure=on_failure)
        from .append_log import open_append_log

        self.log = open_append_log(log, setting=STORE_ENV)
        # The store's own failures, whatever it is; a misconfigured container
        # raises ConfigurationError, which is not one of them and is not spooled.
        self.TRANSIENT = (OSError, ConnectionError, TimeoutError) + _azure_errors()  # type: ignore[misc]

    def _emit(self, record: Any) -> None:
        self._emit_raw(record.to_dict())

    def _emit_raw(self, payload: Mapping[str, Any]) -> None:
        self.log.append(json.dumps(payload, sort_keys=True, default=str))

    def read_all(self) -> list[dict[str, Any]]:
        """Every record, oldest first. A line that does not parse is skipped."""
        return _parsed(self.log.lines())

    def find(self, record_id: str) -> Optional[Mapping[str, Any]]:
        for payload in self.read_all():
            if record_id in (payload.get("decision_id"), payload.get("ingestion_id")):
                return payload
        return None


# ============================================================================
# WHERE, AND WHAT IS THERE
# ============================================================================
#
# INPUT   the environment, or an address
# OUTPUT  where a server records its decisions; the store in words fit for a
#         log or a page, no password, no signature, no query string; the sink
#         an address names; every record kept there, oldest first, or None
#         where this cannot read them
#
# A file, a Blob log and S3 are read back; a PostgreSQL table is written with
# INSERT only, so it is read where it is kept.


def _parsed(lines: Sequence[str]) -> list[dict[str, Any]]:
    found = []
    for line in lines:
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def _scheme(where: str) -> str:
    return where.split("://", 1)[0].lower() if "://" in where else ""


def audit_where(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Where a server records its decisions: ``VECTRIXDB_AUDIT_STORE``, else ``VECTRIXDB_AUDIT_JSONL``, else None.

    The store is a path, a Blob address, ``s3://`` or ``postgresql://``: see
    :func:`audit_sink_at`. It may be read from the file
    ``VECTRIXDB_AUDIT_STORE_FILE`` names, for an address holding a password.
    ``VECTRIXDB_AUDIT_JSONL`` is the older name for the first of those, a
    file, and still works.
    """
    from .signin.keys import env_secret

    source = os.environ if env is None else env
    stored = str(env_secret(source, STORE_ENV) or "").strip()
    return stored or str(source.get("VECTRIXDB_AUDIT_JSONL", "") or "").strip() or None


def describe_audit_store(where: Any) -> str:
    """Where records go, in words fit for a log or a page: no password, no signature, no query string."""
    from urllib.parse import urlsplit, urlunsplit

    text = str(where).strip()
    scheme = _scheme(text)
    if not scheme:
        return text
    if scheme in ("postgres", "postgresql"):
        from .signin.records import describe_where

        return describe_where(text)
    parts = urlsplit(text)
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


def audit_sink_at(where: Any, *, query_key: bytes, on_failure: Any, retain_days: Optional[int] = None) -> AuditSink:
    """The sink an address names, whichever store it is in.

        a path                                                         a JSON line a record, in a file
        https://<account>.blob.core.windows.net/<container>/<prefix>   append blobs under an immutability policy
        s3://<bucket>/<prefix>                                         an object a record, under Object Lock
        postgresql://<user>@<host>/<database>                          a row a record, in a table the server may only insert into

    ``retain_days`` is how long S3 must refuse to change each object, which
    only S3 is told per object; it has no default, because how long an audit
    trail is kept is a business decision. Blob keeps records as long as its
    container's policy says, and PostgreSQL as long as its grants allow.
    """
    text = str(where).strip()
    scheme = _scheme(text)
    if scheme in ("postgres", "postgresql"):
        return PostgresAuditSink(dsn=text, query_key=query_key, on_failure=on_failure)
    if scheme == "s3":
        from urllib.parse import urlparse

        from .objectlock import ObjectLockSink, S3ObjectLockStore

        if not retain_days:
            raise ConfigurationError(
                f"{STORE_ENV} is an s3:// address, where each record is kept under Object Lock for as many days "
                "as VECTRIXDB_AUDIT_RETAIN_DAYS says. It has no default: how long an audit trail is kept is a "
                "business decision"
            )
        parsed = urlparse(text)
        prefix = parsed.path.strip("/")
        return ObjectLockSink(
            S3ObjectLockStore(parsed.netloc),
            query_key=query_key,
            on_failure=on_failure,
            retain_days=int(retain_days),
            prefix=f"{prefix}/" if prefix else "vectrixdb-audit/",
        )
    from .append_log import is_blob_address

    if is_blob_address(text):
        return AppendLogSink(text, query_key=query_key, on_failure=on_failure)
    if scheme:
        raise ConfigurationError(
            f"{STORE_ENV} is a path, https://<account>.blob.core.windows.net/<container>/<prefix>, "
            f"s3://<bucket>/<prefix> or postgresql://<user>@<host>/<database>, not {scheme}://"
        )
    return JSONLSink(text, query_key=query_key, on_failure=on_failure)


def audit_records_at(where: Any) -> Optional[list[dict[str, Any]]]:
    """Every record kept at ``where``, oldest first, for a page to show; None where this cannot read them.

    A file, a Blob log and S3 are read. A PostgreSQL table is not: the role a
    server writes with holds INSERT and nothing else, which is the point of
    it, so its records are read where they are kept.
    """
    text = str(where).strip()
    scheme = _scheme(text)
    if scheme in ("postgres", "postgresql"):
        return None
    if scheme == "s3":
        from urllib.parse import urlparse

        from .objectlock import S3ObjectLockStore, read_locked

        parsed = urlparse(text)
        prefix = parsed.path.strip("/")
        return read_locked(S3ObjectLockStore(parsed.netloc), f"{prefix}/" if prefix else "vectrixdb-audit/")
    from .append_log import FileLog, open_append_log

    if scheme:
        return _parsed(open_append_log(text, setting=STORE_ENV).lines())
    return _parsed(FileLog(text).lines())
