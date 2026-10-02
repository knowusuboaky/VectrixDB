"""Where an answer came from, and whether it can be shown again.

One question drives this module: the assistant told someone something wrong,
where did it come from? It is harder than warehouse lineage because the
transformation includes chunking and embedding rather than joins, and it is
answered here in three steps.

* :func:`provenance_of` walks a chunk back to the write that stored it: the
  document, a hash of its text, the source it was loaded from, how it was cut,
  and the build id of the ingestion, which is the key into the ingestion
  record.
* :func:`reproduce` takes a decision record and says which chunks that
  principal could reach, then and now. It is exact when the index build and
  the policy fingerprint on the record match the collection it is run
  against, and it says so plainly when they do not, because a re-run over a
  different index is evidence of a different thing.
* :func:`write_evidence_pack` writes the artifacts a model risk validator
  asks for, generated from the objects rather than assembled by hand. Not a
  compliance claim: the files only.

Nothing here is reachable by a principal. Lineage is the host's question.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "ChunkProvenance",
    "provenance_of",
    "Reproduction",
    "reproduce",
    "EvidencePack",
    "write_evidence_pack",
]


# ============================================================================
# PROVENANCE: chunk to write
# ============================================================================
#
# INPUT   a collection and chunk ids
# OUTPUT  what each stored chunk says about where it came from, walked back to
#         the write that stored it
#
# Every chunk carries its build, its source and document version, its chunking
# and its quality, so the walk needs no log.


@dataclass(frozen=True)
class ChunkProvenance:
    """What a stored chunk says about where it came from.

    Every field is read from the chunk's own metadata, stamped when it was
    written. ``present`` is False for an id the collection no longer holds,
    and then everything else is None: a deleted chunk has no provenance to
    offer, and inventing one from a later write would be worse than saying
    so.
    """

    id: str
    present: bool
    build_id: Optional[str] = None
    document_id: Optional[str] = None
    document_version: Optional[str] = None
    source: Optional[str] = None
    chunk_index: Optional[int] = None
    chunk_strategy: Optional[str] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    page: Optional[int] = None
    heading: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "present": self.present,
            "build_id": self.build_id,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "source": self.source,
            "chunk_index": self.chunk_index,
            "chunk_strategy": self.chunk_strategy,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "page": self.page,
            "heading": self.heading,
        }


def _from_metadata(id_: str, metadata: Optional[Mapping[str, Any]]) -> ChunkProvenance:
    if metadata is None:
        return ChunkProvenance(id=id_, present=False)
    return ChunkProvenance(
        id=id_,
        present=True,
        build_id=metadata.get("_vx_build"),
        document_id=metadata.get("_vx_doc"),
        document_version=metadata.get("_vx_doc_version"),
        source=metadata.get("source"),
        chunk_index=metadata.get("_vx_chunk"),
        chunk_strategy=metadata.get("_vx_chunk_strategy"),
        chunk_size=metadata.get("_vx_chunk_size"),
        chunk_overlap=metadata.get("_vx_chunk_overlap"),
        page=metadata.get("page"),
        heading=metadata.get("heading"),
    )


def provenance_of(db: Any, ids: Iterable[str]) -> list[ChunkProvenance]:
    """Walk each chunk back to the write that stored it.

    Reads the collection directly, past the policy: this is the host asking
    where its own documents came from, not a principal reading them.
    """
    ids = list(ids)
    collection = db._collection
    points = collection._get_batch_raw(ids)
    return [
        _from_metadata(id_, point.metadata if point is not None else None)
        for id_, point in zip(ids, points)
    ]


# ============================================================================
# REPRODUCTION: decision to reachable set
# ============================================================================
#
# INPUT   a decision record, and a snapshot when the collection has moved on
# OUTPUT  what the record can be shown to have meant: which chunks the
#         recorded principal could reach, restated, in a scratch copy of the
#         export when a snapshot is given
#
# One question drives this module: the assistant told someone something wrong,
# where did it come from?


@dataclass
class Reproduction:
    """What a decision record can be shown to have meant.

    ``exact`` is the claim a validator wants and the one this is careful
    about. It is True only when the index build and the policy fingerprint
    on the record are the ones this reproduction ran against, because then
    the reachable set computed now is the reachable set that existed when
    the decision was made. Every write mints a new build id, so a matching
    build id is a matching index and not a guess. When either differs this
    is a re-run against a different index, still useful, and labelled.
    """

    decision_id: str
    decided_at: Optional[str]
    exact: bool
    same_build: bool
    same_policy: bool
    recorded_build: Optional[str]
    current_build: Optional[str]
    recorded_policy: Optional[str]
    current_policy: Optional[str]
    principal: Mapping[str, Any]
    #: False for a record written before result ids were kept, and then the
    #: three lists below are empty because nothing is known, not because
    #: nothing was returned.
    results_known: bool
    returned: tuple[str, ...]
    still_reachable: tuple[str, ...]
    no_longer_reachable: tuple[str, ...]
    missing: tuple[str, ...]
    reachable_now: tuple[str, ...]
    provenance: list[ChunkProvenance] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decided_at": self.decided_at,
            "exact": self.exact,
            "same_build": self.same_build,
            "same_policy": self.same_policy,
            "recorded_build": self.recorded_build,
            "current_build": self.current_build,
            "recorded_policy": self.recorded_policy,
            "current_policy": self.current_policy,
            "principal": dict(self.principal),
            "results_known": self.results_known,
            "returned": list(self.returned),
            "still_reachable": list(self.still_reachable),
            "no_longer_reachable": list(self.no_longer_reachable),
            "missing": list(self.missing),
            "reachable_now": list(self.reachable_now),
            "provenance": [p.to_dict() for p in self.provenance],
            "notes": list(self.notes),
        }

    def report(self) -> str:
        """The reproduction as a paragraph somebody can paste into a finding."""
        lines = [
            f"decision {self.decision_id}" + (f" at {self.decided_at}" if self.decided_at else "")
        ]
        if self.exact:
            lines.append(
                f"  EXACT: index build {self.current_build} and policy {self.current_policy} "
                f"are the ones the decision was made against"
            )
        else:
            lines.append("  RE-RUN, not a reproduction:")
            if not self.same_build:
                lines.append(
                    f"    the index has moved: recorded {self.recorded_build}, "
                    f"now {self.current_build}"
                )
            if not self.same_policy:
                lines.append(
                    f"    the policy has changed: recorded {self.recorded_policy}, "
                    f"now {self.current_policy}"
                )
        lines.append(f"  reachable by this principal now: {len(self.reachable_now)} chunk(s)")
        if self.results_known:
            lines.append(
                f"  returned then: {len(self.returned)}, of which "
                f"{len(self.still_reachable)} still reachable, "
                f"{len(self.no_longer_reachable)} no longer reachable, "
                f"{len(self.missing)} no longer in the collection"
            )
        else:
            lines.append("  returned then: not recorded (record predates result ids)")
        for chunk in self.provenance:
            if not chunk.present:
                lines.append(f"    {chunk.id}: gone")
                continue
            where = chunk.source or chunk.document_id or "unknown source"
            lines.append(
                f"    {chunk.id}: {where}"
                + (f" v{chunk.document_version}" if chunk.document_version else "")
                + (f", chunk {chunk.chunk_index}" if chunk.chunk_index is not None else "")
                + (f", build {chunk.build_id}" if chunk.build_id else "")
            )
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


def _as_record(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    raise TypeError(
        f"reproduce() takes a RetrievalRecord or the dict a sink stored, got {type(record).__name__}"
    )


def reproduce(db: Any, record: Any, snapshot: Optional[Any] = None) -> Reproduction:
    """Restate a decision: which chunks the recorded principal could reach.

    ``record`` is a :class:`~vectrixdb.audit.RetrievalRecord` or the dict a
    sink stored, and ``db`` is the collection to run it against. Pass
    ``snapshot`` to run against an export of the build the record names
    instead, which is how a reproduction stays exact after the index has
    moved on: keep an export per build and hand the right one back.
    """
    payload = _as_record(record)
    if snapshot is not None:
        return _reproduce_from_snapshot(db, payload, Path(snapshot))

    recorded_build = payload.get("index_build_id")
    current_build = db.index_build_id
    policy = db.policy
    recorded_policy = payload.get("policy_fingerprint")
    current_policy = policy.fingerprint if policy is not None else None
    principal = dict(payload.get("principal_snapshot") or {})

    same_build = recorded_build is not None and recorded_build == current_build
    same_policy = recorded_policy is not None and recorded_policy == current_policy
    notes: list[str] = []
    if recorded_build is None:
        notes.append("the record names no index build, so it predates build ids")
    if policy is None:
        notes.append(
            "this collection carries no policy, so nothing can be decided for the principal"
        )

    reachable: list[str] = []
    if policy is not None:
        for id_, _, metadata in db._collection._iter_documents_raw():
            if policy.decide(principal, metadata).allowed:
                reachable.append(id_)
    reachable_set = set(reachable)

    raw_ids = payload.get("result_ids")
    results_known = raw_ids is not None and (
        len(raw_ids) > 0 or int(payload.get("results_returned") or 0) == 0
    )
    returned = tuple(raw_ids or ())
    present = {p.id for p in provenance_of(db, returned) if p.present} if returned else set()
    still = tuple(i for i in returned if i in reachable_set)
    lost = tuple(i for i in returned if i in present and i not in reachable_set)
    missing = tuple(i for i in returned if i not in present)

    return Reproduction(
        decision_id=str(payload.get("decision_id")),
        decided_at=payload.get("decided_at"),
        exact=same_build and same_policy and policy is not None,
        same_build=same_build,
        same_policy=same_policy,
        recorded_build=recorded_build,
        current_build=current_build,
        recorded_policy=recorded_policy,
        current_policy=current_policy,
        principal=principal,
        results_known=results_known,
        returned=returned,
        still_reachable=still,
        no_longer_reachable=lost,
        missing=missing,
        reachable_now=tuple(reachable),
        provenance=provenance_of(db, returned) if returned else [],
        notes=notes,
    )


def _reproduce_from_snapshot(db: Any, payload: Mapping[str, Any], snapshot: Path) -> Reproduction:
    """Open the export in a scratch directory, reproduce there, and clean up.

    The snapshot carries the collection's metadata table, so its build id
    and its policy are the ones that were exported, which is what makes a
    reproduction against it exact rather than approximate.
    """
    from .snapshot import import_snapshot

    scratch = Path(tempfile.mkdtemp(prefix="vectrixdb-reproduce-"))
    opened = import_snapshot(snapshot, scratch)
    try:
        result = reproduce(opened, payload)
        result.notes.append(f"run against snapshot {snapshot}")
        return result
    finally:
        try:
            opened.close()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ============================================================================
# THE EVIDENCE PACK
# ============================================================================
#
# INPUT   a collection and a directory
# OUTPUT  the files written, and where: one pass over the chunks' origins, the
#         decisions summarised, the controls in force, the schema version, and
#         a README saying what each file is
#
# What an auditor is handed, made by one call.


@dataclass(frozen=True)
class EvidencePack:
    """The files written, and where."""

    directory: Path
    files: tuple[Path, ...]


def _records_from(db: Any, sink: Any, records: Optional[Iterable[Mapping[str, Any]]]) -> list:
    if records is not None:
        return [_as_record(r) for r in records]
    sink = sink if sink is not None else getattr(db, "on_retrieval", None)
    if sink is None:
        return []
    for attr in ("read_all", "raw"):
        source = getattr(sink, attr, None)
        if source is None:
            continue
        found = source() if callable(source) else source
        return [_as_record(r) for r in found]
    return []


def _scan_lineage(db: Any) -> dict[str, Any]:
    """One pass over the collection: what the chunks say about their origins."""
    chunks = 0
    builds: set[str] = set()
    documents: set[str] = set()
    sources: set[str] = set()
    chunkings: set[tuple] = set()
    unstamped = 0
    scored = 0
    low = 0
    from .quality import DEFAULT_THRESHOLD

    for _, _, metadata in db._collection._iter_documents_raw():
        chunks += 1
        quality = metadata.get("_vx_quality")
        if quality is not None:
            scored += 1
            if quality < DEFAULT_THRESHOLD:
                low += 1
        build = metadata.get("_vx_build")
        if build:
            builds.add(build)
        else:
            unstamped += 1
        if metadata.get("_vx_doc"):
            documents.add(metadata["_vx_doc"])
        if metadata.get("source"):
            sources.add(str(metadata["source"]))
        if metadata.get("_vx_chunk_strategy"):
            chunkings.add(
                (
                    metadata.get("_vx_chunk_strategy"),
                    metadata.get("_vx_chunk_size"),
                    metadata.get("_vx_chunk_overlap"),
                )
            )
    return {
        "chunks": chunks,
        "chunks_without_a_build_id": unstamped,
        "builds_seen": sorted(builds),
        "documents": len(documents),
        "distinct_sources": len(sources),
        "chunks_scored_for_extraction_quality": scored,
        "chunks_below_quality_threshold": low,
        "quality_threshold": DEFAULT_THRESHOLD,
        "chunking_configurations": [
            {"strategy": s, "chunk_size": n, "overlap": o} for s, n, o in sorted(chunkings, key=str)
        ],
    }


def _summarise_decisions(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    decisions = [r for r in records if r.get("record_kind") != "ingestion"]
    ingestions = [r for r in records if r.get("record_kind") == "ingestion"]
    by_outcome: dict[str, int] = {}
    for r in decisions:
        by_outcome[str(r.get("outcome"))] = by_outcome.get(str(r.get("outcome")), 0) + 1
    stamps = sorted(str(r.get("decided_at")) for r in decisions if r.get("decided_at"))
    return {
        "decisions": len(decisions),
        "by_outcome": dict(sorted(by_outcome.items())),
        "first_decided_at": stamps[0] if stamps else None,
        "last_decided_at": stamps[-1] if stamps else None,
        "distinct_principals": len(
            {r.get("principal_id") for r in decisions if r.get("principal_id")}
        ),
        "distinct_builds_answering": len(
            {r.get("index_build_id") for r in decisions if r.get("index_build_id")}
        ),
        "decisions_with_result_ids": sum(1 for r in decisions if r.get("result_ids") is not None),
        "ingestions": len(ingestions),
        "documents_written": sum(int(r.get("documents_written") or 0) for r in ingestions),
        "withheld_disclosable_total": sum(
            int(r.get("withheld_disclosable") or 0) for r in decisions
        ),
        # Deliberately absent: the undisclosable total. A summary is the kind
        # of thing that ends up on a dashboard, and that number confirms
        # documents exist outside a scope somebody was refused.
    }


def _controls(db: Any) -> dict[str, Any]:
    policy = db.policy
    sink = getattr(db, "on_retrieval", None)
    on_failure = getattr(sink, "on_failure", None) if sink is not None else None
    return {
        "entitlement_policy": {
            "present": policy is not None,
            "fingerprint": policy.fingerprint if policy else None,
            "version": policy.version if policy else None,
            "rules": [r.describe() for r in policy.rules] if policy else [],
            "scope_rules": [r.describe() for r in policy.scope_rules] if policy else [],
            "required_document_fields": list(policy.required_document_fields) if policy else [],
            "on_incomplete_document": policy.on_incomplete_document if policy else None,
            "require_pushdown": bool(policy.require_pushdown) if policy else None,
            "db_role_key": policy.db_role_key if policy else None,
        },
        "enforcement": {
            "where": "Collection, so every caller including the REST server",
            "pushdown_mode": db.pushdown_mode.value,
            "read_paths": "search, keyword_search, sparse_search, get, get_batch, iter_documents, "
            "count, list_ids and scroll refuse without a principal; hybrid_search, "
            "search_with_acl and enterprise_search refuse outright",
            "rest_api": "refuses a policied collection with 403 and withholds its size",
            "fail_closed": "an absent document field denies; an empty principal value matches "
            "nothing; a missing principal key raises PrincipalIncomplete",
            "sparse_statistics": "computed over the documents the principal may see",
            "timing_floor_seconds": getattr(db, "_timing_floor", None),
        },
        "audit": {
            "sink": type(sink).__name__ if sink is not None else None,
            "on_failure": type(on_failure).__name__ if on_failure is not None else None,
            "query_fingerprint": "HMAC-SHA256 under a key held outside the store"
            if sink is not None
            else None,
            "record_schema_version": _schema_version(),
            "records_carry_result_ids": True,
            "ingestion_records": True,
        },
        "lineage": {
            "index_build_id_on_every_write": True,
            "build_id_on_every_chunk": True,
            "document_version_hash": "sha256 of the loaded text, first 16 hex, via add_document",
        },
    }


def _schema_version() -> int:
    from .audit import RECORD_SCHEMA_VERSION

    return RECORD_SCHEMA_VERSION


_README = """# Evidence pack for {name}

Generated by VectrixDB {version} on {when}.

These are artifacts, not a compliance claim. They are the parts of an SR 11-7
or OSFI E-23 style validation that a library can generate about itself: what
the model is, what data it answers from, which controls are in force, and what
the audit trail holds. The judgement about whether that is enough for your
model's tier is the validator's, and nothing in this directory makes it.

| File | What it answers |
| --- | --- |
| `model.json` | What is this? Embedding model, dimension, search mode, backend, collection. |
| `lineage.json` | What does it answer from? Chunks, documents, sources, chunking configurations, the builds that wrote them. |
| `policy.json` | Who may see what? The entitlement policy as data, with its fingerprint. |
| `controls.json` | What is enforced, where, and what fails closed. |
| `decisions.json` | What has the audit trail recorded? Counts by outcome, the date range, how many builds answered. |

Absent on purpose: the audit records themselves and the total of documents
withheld out of scope. The records say which restricted documents exist and
who was refused them, which makes them more sensitive than the index they
audit, and that total confirms documents exist outside a scope somebody was
refused. Both stay in the audit store, under its grants.

To restate any single decision, take its `decision_id` from the record and run
`db.reproduce(record)`, against a snapshot of the build the record names if
the index has moved since.
"""


def write_evidence_pack(
    db: Any,
    directory: Any,
    *,
    sink: Any = None,
    records: Optional[Iterable[Mapping[str, Any]]] = None,
) -> EvidencePack:
    """Write the evidence artifacts for ``db`` into ``directory``.

    ``records`` is the audit trail to summarise, as dicts. When it is not
    given, the sink attached to ``db`` (or ``sink``) is asked, and a sink that
    cannot read back, which is what a sink pointed at an insert-only store
    is, contributes an empty summary that says so rather than a guess.
    """
    from . import __version__

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    when = datetime.now(timezone.utc).isoformat()
    policy = db.policy

    model = {
        "collection": db.name,
        "generated_at": when,
        "vectrixdb_version": __version__,
        "embedding_model": db.model_name,
        "dense_model": getattr(db, "dense_model_name", None),
        "dimension": db.dimension,
        "search_mode": db.default_mode,
        "backend": getattr(db._collection, "_backend", None),
        "storage_backend": str(db.storage_backend)
        if getattr(db, "storage_backend", None)
        else None,
        "index_build_id": db.index_build_id,
    }
    lineage = {"generated_at": when, "index_build_id": db.index_build_id, **_scan_lineage(db)}
    policy_doc = {
        "generated_at": when,
        "present": policy is not None,
        "fingerprint": policy.fingerprint if policy else None,
        "version": policy.version if policy else None,
        "policy": policy.to_dict() if policy else None,
    }
    controls = {"generated_at": when, **_controls(db)}
    found = _records_from(db, sink, records)
    decisions = {"generated_at": when, **_summarise_decisions(found)}
    if not found and records is None:
        decisions["note"] = (
            "no records were summarised: the attached sink cannot read back, or none is "
            "attached. Pass records= from your audit store."
        )

    files = []
    for name, payload in (
        ("model.json", model),
        ("lineage.json", lineage),
        ("policy.json", policy_doc),
        ("controls.json", controls),
        ("decisions.json", decisions),
    ):
        path = out / name
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        files.append(path)
    readme = out / "README.md"
    readme.write_text(
        _README.format(name=db.name, version=__version__, when=when), encoding="utf-8"
    )
    files.insert(0, readme)
    return EvidencePack(directory=out, files=tuple(files))
