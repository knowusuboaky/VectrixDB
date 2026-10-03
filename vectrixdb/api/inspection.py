"""Read routes that say what a collection *is*, not only what it holds.

The dashboard's collection pages are built on these: health, the policy as
data, the index builds a collection has been through, the extraction
quality of what is in it, the provenance of one chunk, the models on this
machine, and an audit trail when an operator has pointed the server at one.
The chunks a page counts come from the collection's chunk store when it has
one, which every instance writes, and otherwise from the table beside this
process: :mod:`vectrixdb.api.chunk_source` decides, in one place.

Two rules hold across every route here.

A policied collection gets configuration and nothing that moves with its
contents. Which rules it carries and what its fingerprint is are settings an
operator needs; how many chunks it holds, how many are below the quality
line, and which builds wrote how much are data, and this API resolves no
principals, so it cannot say who is entitled to know them. ``_servable``
refuses those routes with the same 403 every data route uses.

The audit trail is more sensitive than the index it audits, so it is never
served from an open server. It needs an API key configured, the request
authenticated with it, and an operator to have named the file. Even then
the records go out without their principal snapshot, without the ids of
the documents returned, and without the undisclosable count, because each
of those confirms something exists that a reader may have been refused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from ..lineage import _from_metadata
from ..quality import DEFAULT_THRESHOLD
from . import chunk_source

__all__ = ["router", "AUDIT_FIELDS", "AUDIT_PATH_ENV"]


# ============================================================================
# SETTINGS: the router, the audit fields, and where the trail is named
# ============================================================================
#
# The fields an audit record may show a reader, and the setting that names
# where the trail is written.

router = APIRouter()

#: Fields of an audit record the dashboard may show. Everything else stays
#: in the store. The snapshot, the result ids and the undisclosable count
#: are the ones that matter; the list is an allowlist rather than a denylist
#: so a field added to the record later is withheld until somebody decides.
AUDIT_FIELDS = (
    "decision_id",
    "ingestion_id",
    "decided_at",
    "started_at",
    "recorded_at",
    "collection",
    "backend",
    "index_build_id",
    "pushdown_mode",
    "principal_id",
    "principal_type",
    "policy_fingerprint",
    "policy_version",
    "rules_evaluated",
    "rules_denied",
    "query_fingerprint",
    "candidates_examined",
    "candidates_exhausted",
    "results_returned",
    "withheld_disclosable",
    "duration_ms",
    "padded_to_ms",
    "outcome",
    "documents_written",
    "documents_skipped",
    "embedding_model",
    "document_id",
    "source",
    "record_schema_version",
)

AUDIT_PATH_ENV = "VECTRIXDB_AUDIT_JSONL"


# ============================================================================
# READING THE CHUNKS: each point, each write, each text
# ============================================================================
#
# INPUT   a collection
# OUTPUT  every live point past the policy, for the library's own bookkeeping;
#         each chunk's time of writing and metadata; one chunk's text from the
#         store, or nothing when it has gone since
#
# The routes below read through these, so a collection with a chunk store and
# one without answer the same way.


def _server():
    """The server module, imported late: it imports this one at app build."""
    from . import server

    return server


def _each_point(collection, batch: int = 1000) -> Iterator[Any]:
    """Every live point, past the policy, for the library's own bookkeeping."""
    offset = 0
    while True:
        points, _ = collection._scroll_raw(limit=batch, offset=offset)
        if not points:
            return
        yield from points
        if len(points) < batch:
            return
        offset += batch


def _each_written(collection) -> Iterator[Any]:
    """Each chunk's time of writing and metadata; nothing for a collection that keeps neither.

    From the chunk store when the collection has one, which every instance
    writes, and otherwise from the table beside this process.
    """
    return iter(chunk_source.written(collection))


def _text_of(point) -> str:
    text = getattr(point, "text", None)
    if not text:
        text = (point.metadata or {}).get("text") or ""
    return str(text)


def _stored_text(store, point_id: str) -> str:
    """One chunk's text from the chunk store, or nothing when it has gone since."""
    point = store.get(point_id) if store is not None else None
    return _text_of(point) if point is not None else ""


# ============================================================================
# HEALTH: the services behind a collection, and its state
# ============================================================================
#
# INPUT   a collection
# OUTPUT  the services behind it, by the names people know them by; what state
#         it is in, as the collections page shows it
#
# Health is read, not computed from a model: the index, its tombstones, the
# last write, and whether every service it was built with is still here.

#: A store's class, as the name of the service somebody pays for.
_STORE_NAMES = {
    "SQLiteStorage": "SQLite",
    "MemoryStorage": "Memory",
    "AzureSearchStorage": "Azure AI Search",
    "OpenSearchStorage": "OpenSearch",
    "CosmosDBStorage": "Azure Cosmos DB",
    "LakebaseStorage": "Databricks Lakebase",
    "DeltaLakeStorage": "Delta Lake",
    "PostgresStorage": "PostgreSQL",
    "PgVectorStorage": "PostgreSQL pgvector",
}
_VECTOR_NAMES = {"azure": "Azure OpenAI", "bedrock": "Amazon Bedrock"}
_EXTRACTOR_NAMES = {
    "Textract": "Amazon Textract",
    "Transcribe": "Amazon Transcribe",
    "AzureDocumentIntelligence": "Azure Document Intelligence",
    "AzureSpeech": "Azure Speech",
    "RapidOcr": "RapidOCR",
    "Whisper": "Whisper",
    "Video": "Video frames and speech",
}


def _services(db: Any, collection: Any, name: str, *, with_extractors: bool) -> dict:
    """The services behind one collection, by the names people know them by.

    Where the vectors are stored, which models embedded them (two, when a
    store holds the collection's own vector and the service's), and which
    engines read the files, taken from what the document store recorded
    beside each document. Configuration, not content.
    """
    storage = getattr(db, "_storage", None)
    inner = getattr(storage, "_storage", storage)
    label = type(inner).__name__ if inner is not None else "SQLiteStorage"
    from ..easy import _embedded_model_name

    own = _embedded_model_name(collection.get_meta("embedding_model"))
    embedded = [own] if own else []
    names = getattr(inner, "vector_names", None)
    for vector in names() if callable(names) else ():
        if vector in _VECTOR_NAMES and _VECTOR_NAMES[vector] not in embedded:
            embedded.append(_VECTOR_NAMES[vector])
    extracted: list[str] = []
    if with_extractors:
        try:
            from .documents import store_for

            store = store_for(name)
            for entry in store.entries().values() if store is not None else ():
                raw = str(entry.get("extractor") or "").strip()
                if not raw:
                    continue
                shown = _EXTRACTOR_NAMES.get(
                    raw, raw.split("//")[-1].split("/")[0] if "://" in raw else raw
                )
                if shown not in extracted:
                    extracted.append(shown)
        except Exception:  # a card is not worth a failed health check
            extracted = []
    return {
        "stored_in": _STORE_NAMES.get(label, label.replace("Storage", "")),
        "embedded_by": embedded,
        "extracted_by": extracted[:6],
    }


@router.get("/api/v1/collections/{name}/health", tags=["inspect"])
async def collection_health(name: str):
    """What state a collection is in, as the collections page shows it.

    A policied collection answers with its configuration only.
    """
    srv = _server()
    db = srv.get_db()
    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    policy = collection.policy
    common = {
        "name": name,
        "policied": policy is not None,
        "policy_fingerprint": policy.fingerprint if policy is not None else None,
        "text_language": getattr(collection, "text_language", "en"),
        "has_text_index": collection._text_index is not None,
        "index_build_id": collection.get_meta("index_build_id"),
        "embedding_model": collection.get_meta("embedding_model"),
        "tags": list(collection.tags or []),
        "description": collection.description,
        "services": _services(srv.get_db(), collection, name, with_extractors=policy is None),
        # What a search of this collection can be. The page greys out the rest,
        # and its Graph tab explains itself rather than asking for a graph there is not.
        "capabilities": {
            "dense": True,
            "keyword": collection._text_index is not None,
            "hybrid": collection._text_index is not None,
            "graph": srv.has_graph(collection),
        },
    }
    if policy is not None:
        return {
            "ok": True,
            "data": {**common, "detail": "counts withheld: this API resolves no principals"},
        }

    ratio = collection.tombstone_ratio
    state = "healthy"
    advice = None
    if ratio > 0.20:
        state = "rebuild"
        advice = f"{ratio:.0%} of the index is deleted vectors; rebuild_index() clears it"
    return {
        "ok": True,
        "data": {
            **common,
            "count": chunk_source.count(collection),
            "tombstone_ratio": round(ratio, 4),
            "state": state,
            "advice": advice,
            "updated_at": chunk_source.changed_at(collection),
        },
    }


# ============================================================================
# THE POLICY, AS DATA
# ============================================================================
#
# INPUT   a collection and the request
# OUTPUT  its entitlement policy: the rules, their kinds, and the fingerprint
#
# The fingerprint is what a record and an audit line carry, so a policy can be
# told from another without reading it.


@router.get("/api/v1/collections/{name}/policy", tags=["inspect"])
async def collection_policy(name: str, req: Request):
    """The entitlement policy as data: rules, kinds and the fingerprint.

    Configuration, not contents, so a policied collection answers. It is
    still the shape of who may see what, so on a server with a key it needs
    that key.
    """
    srv = _server()
    if not srv.is_authenticated(req):
        raise HTTPException(status_code=403, detail="the policy needs the API key")
    db = srv.get_db()
    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")
    policy = collection.policy
    if policy is None:
        return {"ok": True, "data": {"name": name, "policy": None}}
    return {
        "ok": True,
        "data": {
            "name": name,
            "fingerprint": policy.fingerprint,
            "policy": policy.to_dict(),
            "note": (
                "a document missing any field a rule names is invisible to everybody; "
                "a denial is an empty result, never an error"
            ),
        },
    }


# ============================================================================
# BUILDS AND GROWTH
# ============================================================================
#
# INPUT   a collection, and how many days; or every collection
# OUTPUT  every index build that still has chunks in the collection; chunks
#         written on each of the last days, today last, counted from the
#         chunks that are here, for one collection or summed over them all
#         with the share that reads badly
#
# What was written and since deleted is not in the growth: it is counted from
# what exists.


@router.get("/api/v1/collections/{name}/builds", tags=["inspect"])
async def collection_builds(name: str):
    """Every index build that still has chunks in the collection.

    Read off the ``_vx_build`` stamp on each chunk, so a build whose chunks
    were all deleted since is no longer listed: this is what is here, not a
    history. The audit trail is the history.
    """
    srv = _server()
    collection = srv._servable(srv.get_db(), name)
    current = collection.get_meta("index_build_id")
    counts: dict[str, int] = {}
    # When each build first wrote, how its chunks read, and how many read
    # badly: a bad ingest is one build, and this is where it shows.
    first_written: dict[str, str] = {}
    scores: dict[str, list] = {}
    unstamped = 0
    for written_at, metadata in _each_written(collection):
        build = metadata.get("_vx_build")
        if not build:
            unstamped += 1
            continue
        counts[build] = counts.get(build, 0) + 1
        if written_at and (build not in first_written or str(written_at) < first_written[build]):
            first_written[build] = str(written_at)
        score = metadata.get("_vx_quality")
        if isinstance(score, (int, float)):
            scores.setdefault(build, []).append(float(score))
    builds = [
        {
            "build_id": b,
            "chunks": n,
            "current": b == current,
            "written_at": first_written.get(b),
            "quality": round(sum(scores[b]) / len(scores[b]), 4) if scores.get(b) else None,
            "low": sum(1 for s in scores.get(b, []) if s < DEFAULT_THRESHOLD),
        }
        for b, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {
        "ok": True,
        "data": {
            "name": name,
            "current": current,
            "builds": builds,
            "unstamped_chunks": unstamped,
            "quality_threshold": DEFAULT_THRESHOLD,
        },
    }


@router.get("/api/v1/collections/{name}/growth", tags=["inspect"])
async def collection_growth(name: str, days: int = 30):
    """Chunks written on each of the last days, today last, counted from the chunks that are here.

    Every chunk keeps the time it was first written, so this needs no log
    and no audit trail. It is what is here, not a history: a chunk written
    and since deleted is not counted, and one written again under the same
    id counts on the day it was first written. Days are UTC.
    """
    srv = _server()
    collection = srv._servable(srv.get_db(), name)
    days = max(1, min(int(days), 365))
    today = datetime.now(timezone.utc).date()
    first = today - timedelta(days=days - 1)
    written = [0] * days
    before = 0
    for written_at, _ in _each_written(collection):
        try:
            day = datetime.fromisoformat(str(written_at)).date()
        except ValueError:
            continue
        if day < first:
            before += 1
        elif day <= today:
            written[(day - first).days] += 1
    return {
        "ok": True,
        "data": {
            "name": name,
            "days": [(first + timedelta(days=n)).isoformat() for n in range(days)],
            "written": written,
            "before": before,
        },
    }


@router.get("/api/v1/growth", tags=["inspect"])
async def growth(days: int = 14):
    """Chunks written on each of the last days across every collection, today last, with how many of them read badly.

    The same count as a collection's own growth, summed over every
    collection that is here, and beside it ``low``: the chunks of each day
    whose quality stamp is under the line, which is where a bad ingest
    shows on the overview. Counts alone, no names. A collection whose
    chunks cannot be read is left out and counted in ``skipped``.
    """
    srv = _server()
    db = srv.get_db()
    days = max(1, min(int(days), 365))
    today = datetime.now(timezone.utc).date()
    first = today - timedelta(days=days - 1)
    written = [0] * days
    low = [0] * days
    before = 0
    counted = 0
    skipped = 0
    for info in db.list_collections():
        name = getattr(info, "name", None) or (info.get("name") if isinstance(info, dict) else None)
        if not name:
            continue
        try:
            collection = srv._servable(db, name)
            rows = list(_each_written(collection))
        except Exception:  # noqa: BLE001 - one collection that cannot be read does not empty the chart
            skipped += 1
            continue
        counted += 1
        for written_at, metadata in rows:
            try:
                day = datetime.fromisoformat(str(written_at)).date()
            except ValueError:
                continue
            if day < first:
                before += 1
            elif day <= today:
                at = (day - first).days
                written[at] += 1
                score = (metadata or {}).get("_vx_quality")
                if isinstance(score, (int, float)) and float(score) < DEFAULT_THRESHOLD:
                    low[at] += 1
    return {
        "ok": True,
        "data": {
            "days": [(first + timedelta(days=n)).isoformat() for n in range(days)],
            "written": written,
            "low": low,
            "before": before,
            "collections": counted,
            "skipped": skipped,
            "quality_threshold": DEFAULT_THRESHOLD,
        },
    }


# ============================================================================
# QUALITY
# ============================================================================
#
# INPUT   a collection, a threshold, how many of the worst, and whether to
#         show text
# OUTPUT  extraction quality across the collection from the _vx_quality
#         stamps, and the scorer's weakest signals for the worst chunks
#
# Text is shown only when asked, and only to a caller who may read it.

#: A signal under this is a reason. What each one failing looks like, in words.
_WEAK_SIGNAL = 0.7
_SIGNAL_WORDS = {
    # First, because text that loops passes every other signal: it is made of good words.
    "varied": "the same words over and over",
    "plain_characters": "symbols and stray glyphs",
    "whole_words": "broken words, or letters mixed with digits",
    "word_length": "words far too short or far too long",
    "known_words": "few ordinary words: a table, codes, or another language",
    "pronounceable": "runs of letters no word has",
}


def _why_low(text: str) -> list[str]:
    """The scorer's weakest signals for this text, worst first, at most two."""
    from ..quality import extraction_quality

    signals = extraction_quality(text).signals
    # In the order that tells a reader most. Noise fails nearly everything at
    # once, and "symbols" is the word for it, not "few ordinary words", which
    # is what a clean table of figures fails and nothing else does.
    return [_SIGNAL_WORDS[name] for name in _SIGNAL_WORDS if signals.get(name, 1.0) < _WEAK_SIGNAL][
        :2
    ]


@router.get("/api/v1/collections/{name}/quality", tags=["inspect"])
async def collection_quality(
    name: str,
    threshold: float = Query(default=DEFAULT_THRESHOLD, ge=0.0, le=1.0),
    worst: int = Query(default=5, ge=0, le=50),
    offset: int = Query(default=0, ge=0),
    text: bool = Query(default=True),
):
    """Extraction quality across the collection, from the ``_vx_quality`` stamps.

    Twenty bins, the count below the line, and the lowest few chunks. Each
    comes with ``reasons``: which of the scorer's signals it fails, in words,
    which is how a reader tells OCR noise from a table of numbers without
    reading either. ``below_line`` says whether it is actually under the
    threshold, because the lowest five of a clean collection are not bad.
    ``offset`` skips that many of the lowest, for the next page of them.
    ``text=false`` leaves the excerpt out, and the dashboard asks for that.
    """
    srv = _server()
    collection = srv._servable(srv.get_db(), name)
    store = chunk_source.shared(collection)
    if store is not None:
        # Only the scores from the chunk store, and the text of the few shown.
        rows: Iterator[tuple] = ((q, point_id, None) for point_id, q in store.scores())
    else:
        rows = (
            ((p.metadata or {}).get("_vx_quality"), str(p.id), _text_of(p))
            for p in _each_point(collection)
        )
    bins = [0] * 20
    below = 0
    scored = 0
    unscored = 0
    lowest: list[tuple[float, str, Optional[str]]] = []
    for q, point_id, body in rows:
        if q is None:
            unscored += 1
            continue
        q = float(q)
        scored += 1
        bins[min(19, int(q * 20))] += 1
        if q < threshold:
            below += 1
        lowest.append((q, point_id, body))
        if len(lowest) > (offset + worst) * 4:
            lowest.sort()
            del lowest[offset + worst :]
    lowest.sort()
    shown: list[tuple[float, str, str]] = [
        (q, i, full if full is not None else _stored_text(store, i))
        for q, i, full in lowest[offset : offset + worst]
    ]
    return {
        "ok": True,
        "data": {
            "name": name,
            "threshold": threshold,
            "scored": scored,
            "unscored": unscored,
            "below": below,
            "bins": bins,
            "offset": offset,
            "worst": [
                {
                    "id": i,
                    "quality": round(q, 3),
                    "below_line": q < threshold,
                    "reasons": _why_low(full),
                    **({"text": full[:160]} if text else {}),
                }
                for q, i, full in shown
            ],
        },
    }


# ============================================================================
# PROVENANCE
# ============================================================================
#
# INPUT   a collection and a chunk's id
# OUTPUT  where the chunk came from, as its metadata records it: the build
#         that stored it, the source and document version, the chunking, and
#         its quality
#
# One chunk, one request, recorded.


@router.get("/api/v1/collections/{name}/provenance/{point_id}", tags=["inspect"])
async def chunk_provenance(
    name: str, point_id: str, req: Request, text: bool = Query(default=True)
):
    """Where one chunk came from, as its metadata records it.

    ``text=false`` leaves the excerpt out. Where a chunk came from and what it
    says are different questions, and the dashboard asks only the first.
    """
    srv = _server()
    collection, principal = srv._servable_as(srv.get_db(), name, req)
    point = chunk_source.point(collection, point_id, principal)
    metadata = point.metadata if point is not None else None
    prov = _from_metadata(point_id, metadata)
    data = dict(vars(prov))
    if point is not None:
        data["quality"] = (point.metadata or {}).get("_vx_quality")
        if not text:
            pass
        elif srv.sees_content(req):
            data["text"] = _text_of(point)[:400]
        else:
            data["text_hidden"] = True
    return {"ok": True, "data": data}


# ============================================================================
# REBUILDING
# ============================================================================
#
# INPUT   a collection
# OUTPUT  the ANN index rebuilt from the live vectors, tombstones dropped
#
# The one write on this router.


@router.post("/api/v1/collections/{name}/rebuild", tags=["inspect"])
async def rebuild_collection(name: str):
    """Rebuild the ANN index from the live vectors, dropping tombstones."""
    srv = _server()
    collection = srv._servable(srv.get_db(), name)
    try:
        rebuilt = collection.rebuild_index()
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await srv.ws_manager.broadcast("index_rebuilt", {"collection": name, "vectors": rebuilt})
    return {
        "ok": True,
        "data": {"name": name, "vectors": rebuilt, "tombstone_ratio": collection.tombstone_ratio},
    }


# ============================================================================
# THE MODELS ON THIS MACHINE
# ============================================================================
#
# INPUT   the library's registry
# OUTPUT  every model it knows, and whether it is here
#
# So the page can say which download would make a collection openable.

_ROLES = {
    "dense": "dense, 100 languages",
    "dense_en": "dense, pre-2.2 collections",
    "bge_small_en": "dense, English default",
    "bge_base_en": "dense, English, larger",
    "sparse": "sparse, every script",
    "splade_pp_en": "sparse, neural",
    "reranker": "reranker, multilingual",
    "reranker_en": "reranker, English",
    "reranker_en_small": "reranker, English, smaller",
    "bge_reranker_base": "reranker, English, larger",
    "late_interaction": "late interaction, multilingual",
    "late_interaction_en": "late interaction, English",
    "colbert_v2": "late interaction, English, larger",
    "rebel": "graph extraction",
}


@router.get("/api/v1/models", tags=["inspect"])
async def models_on_this_machine():
    """Every model the library knows, and whether it is here.

    Nothing downloads because of this call.
    """
    from ..models import embedded

    models_dir = Path(embedded.get_models_dir())
    package_dir = Path(embedded.__file__).parent / "data"
    out = []
    for key, config in embedded.MODEL_CONFIG.items():
        # Each row is one model, so only its own directory counts: the bundled
        # English reranker is not the multilingual one.
        try:
            present = embedded.is_models_installed(key, exact=True)
        except Exception:  # pragma: no cover - a registry entry without files
            present = False
        location = None
        if present:
            dir_name = embedded.model_dir_name(key)
            # A download lands in the package folder too, unless
            # VECTRIXDB_MODELS_DIR says otherwise, so the folder alone does
            # not say the wheel brought it.
            shipped = dir_name in embedded.WHEEL_MODEL_DIRS
            location = (
                "wheel"
                if shipped and models_dir.resolve() == package_dir.resolve()
                else "downloaded"
            )
        out.append(
            {
                "type": key,
                "name": config.get("name"),
                "role": _ROLES.get(key, key),
                "size_mb": config.get("size_mb"),
                "present": present,
                "location": location,
                "description": config.get("description"),
            }
        )
    return {
        "ok": True,
        "data": {
            "models_dir": str(models_dir),
            "auto_download": os.environ.get("VECTRIXDB_AUTO_DOWNLOAD", "") == "1",
            "models": out,
        },
    }


# ============================================================================
# THE AUDIT TRAIL: where it is, and a page of it
# ============================================================================
#
# INPUT   the setting; a page, a find, a collection, and how many days
# OUTPUT  where decisions are recorded, as the server that writes them reads
#         it; the records newest first a page at a time, with the counts, and
#         the decisions a day by outcome and by collection from every record
#         in the window
#
# Served only when an API key is configured and presented, or to a person
# whose role reads the audit. A file, a Blob log and S3 are read back; a
# PostgreSQL table is written with INSERT only, so it is read where it is
# kept.


def _audit_where() -> Optional[str]:
    """Where the decisions are recorded, as the server that writes them reads it."""
    return _server().audit_where()


def _allowed(record: dict) -> dict:
    return {k: record[k] for k in AUDIT_FIELDS if k in record}


def _holding(records: list, q: Optional[str]) -> list:
    """The records with ``q`` somewhere in what they say, case aside; every record when there is no find."""
    needle = (q or "").strip().lower()
    if not needle:
        return records
    return [
        r
        for r in records
        if needle
        in " ".join(str(v) for v in r.values() if isinstance(v, (str, int, float))).lower()
    ]


def _decisions_a_day(records: list, days: int, now: Optional[float] = None) -> dict:
    """Decisions on each of the last ``days`` days by outcome, and the same by collection.

    Three outcomes, as the page colours them: allowed, denied, and refused,
    which here takes in undecidable, since both mean nobody was answered and
    somebody should look. Ingestions are not decisions and are left out. Days
    are UTC. A record with no time, or one this cannot read, is left out.
    """
    import time as _time
    from datetime import datetime, timezone

    today = int((_time.time() if now is None else now) // 86400)
    first = today - days + 1
    daily = {name: [0] * days for name in ("allowed", "denied", "refused")}
    by: dict = {}
    for rec in records:
        if "ingestion_id" in rec:
            continue
        try:
            moment = datetime.fromisoformat(str(rec.get("decided_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        day = int(moment.timestamp() // 86400)
        if not first <= day <= today:
            continue
        outcome = str(rec.get("outcome") or "")
        name = (
            "denied"
            if outcome.startswith("denied")
            else "refused"
            if outcome.startswith(("refused", "undecidable"))
            else "allowed"
        )
        daily[name][day - first] += 1
        row = by.setdefault(
            str(rec.get("collection") or ""), {"allowed": 0, "denied": 0, "refused": 0}
        )
        row[name] += 1
    ranked = sorted(by.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    return {
        "daily": {
            "days": [
                _time.strftime("%Y-%m-%d", _time.gmtime(d * 86400)) for d in range(first, today + 1)
            ],
            **daily,
        },
        "by_collection": [{"collection": name, **row} for name, row in ranked],
    }


@router.get("/api/v1/audit", tags=["inspect"])
async def audit_trail(
    req: Request,
    limit: int = Query(default=100, gt=0, le=1000),
    offset: int = Query(default=0, ge=0),
    q: Optional[str] = Query(
        default=None,
        description="A find: records that hold this in their collection, principal, outcome or id.",
    ),
    collection: Optional[str] = None,
    days: int = Query(
        default=14,
        gt=0,
        le=90,
        description="How many days the counts a day and by collection go back.",
    ),
):
    """The most recent audit records, newest first, a page at a time, from where the server records them.

    ``limit`` and ``offset`` cut the page and ``q`` finds within the records;
    ``total`` is how many match. The counts, the days and the collections are
    always counted from every record in the window, whatever page is listed.

    Served only when an API key is configured and presented, or to a person
    whose role reads the audit, and only when an operator has set
    ``VECTRIXDB_AUDIT_STORE``, or ``VECTRIXDB_AUDIT_JSONL`` for a file. A
    file, a Blob log and S3 are read back; a PostgreSQL table is written
    with INSERT only, so it is read where it is kept. On any other server
    this answers that the trail is not available here, and says why, so the
    fix is not a guess.
    """
    srv = _server()
    where = _audit_where()
    if srv.runtime_of(req) is not None:
        # Sign-in is on, and the door has already checked that this caller holds audit.read.
        pass
    elif srv.get_api_key() is None:
        raise HTTPException(
            status_code=403,
            detail="the audit trail is never served from a server without an API key",
        )
    elif not srv.is_authenticated(req):
        raise HTTPException(status_code=403, detail="the audit trail needs the API key")
    if where is None:
        return {
            "ok": True,
            "data": {
                "available": False,
                "reason": f"set VECTRIXDB_AUDIT_STORE to where decisions are recorded, or {AUDIT_PATH_ENV} to a JSONL file",
            },
        }
    from ..audit import audit_records_at, describe_audit_store

    shown = describe_audit_store(where)
    if "://" not in where and not Path(where).exists():
        return {"ok": True, "data": {"available": False, "reason": f"{where} does not exist"}}
    try:
        found = audit_records_at(where)
    except (
        Exception
    ) as exc:  # the store's own error, whichever store it is: the page says so rather than failing
        return {
            "ok": True,
            "data": {
                "available": False,
                "reason": f"{shown} could not be read ({type(exc).__name__}): {exc}",
            },
        }
    if found is None:
        return {
            "ok": True,
            "data": {
                "available": False,
                "reason": f"{shown} is written with INSERT only, so its records are read where they are kept",
            },
        }

    records: list[dict] = [
        _allowed(rec) for rec in found if not collection or rec.get("collection") == collection
    ]
    records.reverse()
    trend = _decisions_a_day(records, max(1, min(days, 90)))
    counts = {"decisions": 0, "ingestions": 0, "denied": 0, "undecidable": 0, "refused": 0}
    for rec in records:
        if "ingestion_id" in rec:
            counts["ingestions"] += 1
            continue
        counts["decisions"] += 1
        outcome = str(rec.get("outcome") or "")
        if outcome.startswith("denied"):
            counts["denied"] += 1
        elif outcome.startswith("undecidable"):
            counts["undecidable"] += 1
        elif outcome.startswith("refused"):
            counts["refused"] += 1
    listed = _holding(records, q)
    return {
        "ok": True,
        "data": {
            "available": True,
            "path": shown,
            "counts": counts,
            "records": listed[offset : offset + limit],
            "total": len(listed),
            "offset": offset,
            # Every record in the window is counted, not only the newest `limit` that are listed.
            "daily": trend["daily"],
            "by_collection": trend["by_collection"],
            "withheld": ["principal_snapshot", "result_ids", "withheld_undisclosable"],
        },
    }
