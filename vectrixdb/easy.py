"""
VectrixDB Easy API - The Simplest Vector Database in the World

Zero config. Text in, results out. One line for everything.

Example:
    >>> from vectrixdb import Vectrix
    >>>
    >>> # Create and add - ONE LINE
    >>> db = Vectrix("my_docs").add(["Python is great", "Machine learning is fun"])
    >>>
    >>> # Search - ONE LINE
    >>> results = db.search("programming")
    >>>
    >>> # Full power - STILL ONE LINE
    >>> results = db.search("AI", mode="ultimate")  # dense + sparse + rerank

Comparison with competitors:

    # Chroma (4 lines)
    client = chromadb.Client()
    collection = client.create_collection("docs")
    collection.add(documents=["text"], ids=["1"])
    results = collection.query(query_texts=["query"])

    # Pinecone (5+ lines + API key + manual embedding)
    pinecone.init(api_key="...")
    index = pinecone.Index("docs")
    embedding = model.encode("text")  # manual!
    index.upsert(vectors=[...])
    results = index.query(vector=embedding)

    # VectrixDB (1 line each)
    db = Vectrix("docs").add(["text"])
    results = db.search("query")

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import os
import hashlib
from pathlib import Path

from .exceptions import (
    ConfigurationError,
    DependencyError,
    DocumentNotFoundError,
    GraphUnavailable,
    ModelMismatchWarning,
    SparseModelUnavailableWarning,
)
import warnings
import html as _html
import contextlib
import copy
import threading
import json
import uuid
from datetime import datetime
from ._ranking import apply_score_gap, fit_to_budget
from .core import relevance as _relevance
from .tracing import traced
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    Literal,
)
from collections.abc import Mapping
import re
from dataclasses import dataclass, field, replace
import logging
import math
import time
import numpy as np

if TYPE_CHECKING:
    from .ingest import LoadedDocument
    from .audit import AuditContext, AuditSink
    from .policy import Policy


# ============================================================================
# SETTINGS: the deny sentinel, the clock, the search modes, and the image vector
# ============================================================================
#
# A filter that matches nothing, since an empty Filter matches everything; the
# clock; the modes search() accepts; and the vector index image_embedder
# fills.

#: A filter no document can satisfy, used when a policy cannot match anything.
#: It is a sentinel rather than an empty filter because an empty filter means
#: the opposite: `Filter()` with no conditions matches every document, and
#: that is exactly the shape a bug here would take.
_DENY_EVERYTHING = object()


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


__all__ = [
    "SearchMode",
    "Result",
    "Results",
    "RevocationReport",
    "Vectrix",
    "create",
    "open",
    "quick_search",
]

#: The search modes ``search()`` accepts. Named so other modules can annotate
#: against it instead of restating the literal.
SearchMode = Literal["dense", "sparse", "hybrid", "ultimate", "graph"]
#: The vector index image_embedder fills, and the name to ask for it by: search(vectors="image").
IMAGE_VECTOR = "image"


# ============================================================================
# THE RESULT TYPES
# ============================================================================
#
# INPUT   what a collection's search returned
# OUTPUT  one Result with its verdict, a Results list with convenient access,
#         and the report of what a revoke did
#
# The cross-encoder's verdict is carried when there is one, so a page can say
# why a result is where it is.


def _judgement(r: Any) -> Dict[str, Any]:
    """What a collection's result says about how well it matches, as candidate keys."""
    out: Dict[str, Any] = {}
    if getattr(r, "relevance", None) is not None:
        out["similarity"], out["similarity_kind"] = r.relevance, r.relevance_kind
    if getattr(r, "relevances", None):
        out["similarities"] = dict(r.relevances)
    if getattr(r, "matched_by", None):
        out["found_by"] = list(r.matched_by)
    return out


def _verdict(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """The Result fields for one candidate: the cross-encoder's verdict when
    it gave one, because it read the pair; the similarity otherwise."""
    relevance: Optional[float]
    kind: Optional[str]
    if candidate.get("judged") is not None:
        relevance, kind = candidate["judged"], "reranker"
    else:
        relevance, kind = candidate.get("similarity"), candidate.get("similarity_kind")
    measured = (
        candidate.get("similarity") if candidate.get("similarity_kind") == "similarity" else None
    )
    return {
        "relevance": relevance,
        "relevance_kind": kind if relevance is not None else None,
        "similarity": measured,
        "matched_by": list(candidate["found_by"]) if candidate.get("found_by") else None,
        "relevances": dict(candidate["similarities"]) if candidate.get("similarities") else None,
    }


@dataclass
class Result:
    """Single search result."""

    id: str
    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Filled by search(explain=True): the parts the score was built from, so a
    # ranking can be tuned rather than guessed at. Keys depend on the mode:
    # dense, bm25, rrf_dense, rrf_sparse, fused, colbert, rerank, graph_boost.
    explain: Optional[Dict[str, Any]] = None

    # How well this matches, from 0 to 1, whatever the mode and whichever
    # engine answered, or None when there is no honest number. ``score``
    # orders the list, and in a hybrid search it is a sum of reciprocal ranks
    # that tops out near 0.02 and says nothing about how good a match is.
    # This is the one to put a threshold on:
    #
    #     hits = db.search(question)
    #     if not hits or (hits[0].relevance or 0) < 0.45:
    #         answer = "I could not find that."
    #
    # ``relevance_kind`` says what the number is: "reranker" when a
    # cross-encoder judged the pair, which is the best signal there is,
    # "similarity" for the cosine similarity, "relative" for a keyword score
    # shown as a share of the best hit, "distance" for a collection whose
    # metric is not cosine. See vectrixdb.core.relevance.
    relevance: Optional[float] = None
    relevance_kind: Optional[str] = None
    # The cosine similarity on its own, when it is known, whatever
    # ``relevance`` turned out to be. A reranker's verdict and a similarity
    # live on different scales, so a threshold belongs on one of them, and a
    # search that sometimes reranks and sometimes does not can hold this one.
    similarity: Optional[float] = None
    matched_by: Optional[List[str]] = None  # "meaning", "keywords"
    relevances: Optional[Dict[str, float]] = None  # per dense vector, where a store holds two

    @property
    def citation(self) -> str:
        """What to write in square brackets to cite this chunk.

        The string add_document() stamped, or one built from the source,
        page and heading the chunk carries, or the id when it carries none.
        """
        from .citations import citation_of

        return citation_of(self.metadata, self.id)

    @property
    def readable_citation(self) -> str:
        """The citation as a person reads it: ``report.pdf, pp. 39-40``.

        The number printed on the page where the PDF numbers its own pages,
        a range when the chunk runs onto the next, ``slide 3``, ``11:05`` in
        a recording. What to show beside an answer; ``citation`` is what the
        answer cites, and the link that opens the page.
        """
        from .citations import readable_citation_of

        return readable_citation_of(self.metadata, self.id)

    def __repr__(self):
        preview = self.text[:50] + "..." if len(self.text) > 50 else self.text
        return f"Result(score={self.score:.4f}, text='{preview}')"

    def to_dict(self) -> Dict[str, Any]:
        """A plain dict, JSON-serialisable when the metadata is."""
        row = {
            "id": self.id,
            "text": self.text,
            "score": self.score,
            "metadata": dict(self.metadata),
        }
        if self.explain is not None:
            row["explain"] = dict(self.explain)
        return row

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Result":
        return cls(
            id=data["id"],
            text=data.get("text", ""),
            score=float(data.get("score", 0.0)),
            metadata=dict(data.get("metadata") or {}),
            explain=data.get("explain"),
        )

    def _repr_html_(self) -> str:
        return (
            f"<code>Result</code> score={self.score:.4f} "
            f'<span style="color:#666">{_html.escape(self.text[:120])}</span>'
        )


@dataclass(frozen=True)
class RevocationReport:
    """What one :meth:`Vectrix.revoke` actually did.

    ``matched`` and ``changed`` are separate numbers and the gap between them
    is usually the interesting part: matched many and changed none means the
    revocation had already been applied, matched none means the filter is
    wrong. One combined count would hide both.
    """

    matched: int
    changed: int
    at: datetime
    fields: tuple

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matched": self.matched,
            "changed": self.changed,
            "at": self.at.isoformat(),
            "fields": list(self.fields),
        }


def _apply_revocation(
    metadata: Dict[str, Any],
    set_fields: Optional[Dict[str, Any]],
    unset_fields: Optional[Iterable[str]],
) -> Dict[str, Any]:
    """A copy of ``metadata`` with the dotted paths set and unset.

    Dotted, because entitlements live in a nested object on the chunk and
    ``entitlements.client_id`` is how every other part of this reads them.
    """
    import copy as _copy

    updated = _copy.deepcopy(dict(metadata))

    for path, value in (set_fields or {}).items():
        target = updated
        parts = path.split(".")
        for part in parts[:-1]:
            nxt = target.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                target[part] = nxt
            target = nxt
        target[parts[-1]] = value

    for path in unset_fields or ():
        parts = path.split(".")
        holder: Any = updated
        for part in parts[:-1]:
            holder = holder.get(part) if isinstance(holder, dict) else None
            if holder is None:
                break
        if isinstance(holder, dict):
            # Removed, not set to None. An absent field and a null one are
            # different to the filter engine, and under a policy the first
            # denies while the second is a value like any other.
            holder.pop(parts[-1], None)

    return updated


@dataclass
class Results:
    """Search results with convenient access."""

    items: List[Result]
    query: str
    mode: str
    time_ms: float

    # Set by search(token_budget=..., score_gap=...). Without a budget nothing
    # is cut and token_estimate still reports what the list would cost, so a
    # caller can see the number before deciding on one.
    truncated: bool = False
    cut_count: int = 0
    token_estimate: int = 0
    token_budget: Optional[int] = None

    # Set when mode="graph" answered from vectors alone, with the reason. A
    # caller who asked for the graph can then tell a fallback from a hit
    # instead of trusting a result that merely looks plausible.
    degraded: Optional[str] = None

    # Set when a policied search wrote a decision record, and equal to that
    # record's decision_id. It is what ties "the assistant told me this" to
    # one row in the audit store; without it a dispute has to be resolved by
    # correlating on wall-clock time, which is not a resolution. Opaque, and
    # safe to show: it names a decision, never a document.
    decision_id: Optional[str] = None

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]

    @property
    def texts(self) -> List[str]:
        """Get all result texts."""
        return [r.text for r in self.items]

    @property
    def ids(self) -> List[str]:
        """Get all result IDs."""
        return [r.id for r in self.items]

    @property
    def scores(self) -> List[float]:
        """Get all scores."""
        return [r.score for r in self.items]

    def figures(self, top: Optional[int] = None) -> List["Result"]:
        """The results that are figures, best first, ``top`` of them at most.
        What to hand a model that can see, beside the text."""
        found = [r for r in self.items if (r.metadata or {}).get("figure")]
        return found if top is None else found[: max(int(top), 0)]

    def citations(self) -> List[str]:
        """Every distinct citation among the results, best first."""
        out: List[str] = []
        for r in self.items:
            c = r.citation
            if c not in out:
                out.append(c)
        return out

    def sources(self, max_chars: Optional[int] = None) -> str:
        """The results as a sources block for a prompt: one ``[citation]: text`` per result.

        Newlines inside a text become spaces, so one source stays one
        line, and ``max_chars`` stops adding sources once the block would
        pass it, keeping whole sources rather than cutting one. Put
        ``CITATION_INSTRUCTION`` in the system prompt beside it and check
        the answer with ``validate_answer()``.
        """
        lines: List[str] = []
        total = 0
        for r in self.items:
            line = f"[{r.citation}]: " + " ".join(r.text.split())
            if max_chars is not None and lines and total + len(line) + 2 > max_chars:
                break
            lines.append(line)
            total += len(line) + 2
        return "\n\n".join(lines)

    @property
    def top(self) -> Optional[Result]:
        """Get top result."""
        return self.items[0] if self.items else None

    def __repr__(self):
        cut = f", {self.cut_count} cut" if self.truncated else ""
        return (
            f"Results({len(self.items)} results for '{self.query[:30]}...' "
            f"in {self.time_ms:.1f}ms{cut})"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Everything, as plain data. ``from_dict`` reverses it."""
        return {
            "query": self.query,
            "mode": self.mode,
            "time_ms": self.time_ms,
            "truncated": self.truncated,
            "cut_count": self.cut_count,
            "token_estimate": self.token_estimate,
            "token_budget": self.token_budget,
            "degraded": self.degraded,
            "decision_id": self.decision_id,
            "items": [r.to_dict() for r in self.items],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Results":
        return cls(
            items=[Result.from_dict(r) for r in data.get("items", [])],
            query=data.get("query", ""),
            mode=data.get("mode", "dense"),
            time_ms=float(data.get("time_ms", 0.0)),
            truncated=bool(data.get("truncated", False)),
            cut_count=int(data.get("cut_count", 0)),
            token_estimate=int(data.get("token_estimate", 0)),
            token_budget=data.get("token_budget"),
            degraded=data.get("degraded"),
            decision_id=data.get("decision_id"),
        )

    def _rows(self) -> List[Dict[str, Any]]:
        return [
            {"id": r.id, "score": r.score, "text": r.text, "metadata": r.metadata}
            for r in self.items
        ]

    def to_pandas(self):
        """A DataFrame with id, score, text and metadata columns. Needs pandas."""
        try:
            import pandas as pd
        except ImportError as exc:
            raise DependencyError("pandas") from exc
        return pd.DataFrame(self._rows(), columns=["id", "score", "text", "metadata"])

    def to_polars(self):
        """A polars DataFrame with id, score, text and metadata columns. Needs polars."""
        try:
            import polars as pl
        except ImportError as exc:
            raise DependencyError("polars") from exc
        return pl.DataFrame(
            {
                "id": [r.id for r in self.items],
                "score": [r.score for r in self.items],
                "text": [r.text for r in self.items],
                "metadata": [r.metadata for r in self.items],
            }
        )

    def _repr_html_(self) -> str:
        """A table in notebooks; the plain repr everywhere else."""
        cut = f" ({self.cut_count} cut to fit {self.token_budget} tokens)" if self.truncated else ""
        degraded = (
            f'<div style="color:#a60">answered from vectors only: {_html.escape(self.degraded)}</div>'
            if self.degraded
            else ""
        )
        rows = "".join(
            "<tr>"
            f'<td style="text-align:right;font-variant-numeric:tabular-nums">{r.score:.4f}</td>'
            f"<td>{_html.escape(r.text[:200])}</td>"
            f'<td style="color:#888"><code>{_html.escape(r.id[:16])}</code></td>'
            "</tr>"
            for r in self.items
        )
        return (
            f"<div><b>{len(self.items)}</b> results for <i>{_html.escape(self.query[:80])}</i> "
            f"in {self.time_ms:.1f} ms{cut}{degraded}"
            "<table><thead><tr><th>score</th><th>text</th><th>id</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )


# ============================================================================
# AFTER CLOSE
# ============================================================================
#
# INPUT   any use of a closed collection
# OUTPUT  a clear failure naming close()
#
# Stands in for the collection, so nothing half-works.


class _Closed:
    """Stands in for the collection after close(), so any use fails clearly.

    Without it a closed Vectrix raised AttributeError from deep inside the
    collection code, or worse, half-worked on a connection that was gone.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        raise ConfigurationError(
            f"Vectrix({self._name!r}) is closed. Open it again with Vectrix({self._name!r}, ...)."
        )


# ============================================================================
# THE HELPERS: rows, the open collection, the add report, the text cache, and a model's name
# ============================================================================
#
# INPUT   a storage row; a recorded model name
# OUTPUT  the caller's metadata from a row; the open collection; what the last
#         add did; document texts backed by the collection; whether two
#         recorded model names mean the same vectors, and the name a model is
#         shown by
#
# The small pieces the main class leans on.

#: Maps the public registry labels for bundled models onto the keys that
#: DenseEmbedder actually understands. Without this the default model name never
#: resolves and every embedded lookup fails.
logger = logging.getLogger(__name__)


_ROW_FIELDS = {"text_content", "created_at", "updated_at", "id", "idx"}


def _row_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    """The caller's metadata from a storage row: everything that is not a
    standard field or an embedding, or the nested dict when a backend nests."""
    nested = row.get("metadata")
    if isinstance(nested, dict):
        return dict(nested)
    return {
        k: v
        for k, v in row.items()
        if k not in _ROW_FIELDS and not k.endswith("_embedding") and not k.startswith("_")
    }


def _coll(db: "Vectrix") -> Any:
    """The open collection, typed loosely: it is assigned after construction."""
    return db._collection


_RECHUNK_OPTIONS = (
    "chunk",
    "chunk_size",
    "overlap",
    "parent_size",
    "embed_heading",
    "dedupe",
    "threshold",
    "on_low_quality",
    "quality_threshold",
    "progress",
    "cut_with",
    "context_with",
    "late",
)
_RECORDED_CHUNKING = ("chunk", "chunk_size", "overlap", "parent_size", "embed_heading")


def _rechunk_options(options: Mapping[str, Any], name: str) -> None:
    unknown = sorted(set(options) - set(_RECHUNK_OPTIONS))
    if unknown:
        raise TypeError(f"{name}() does not take {', '.join(unknown)}")


def _rechunk_settings(entry: Mapping[str, Any], options: Mapping[str, Any]) -> Dict[str, Any]:
    """What a kept document is cut with again: what it was cut with before, as
    add_document takes it, under what was asked now. A model's note was
    recorded as a flag and cannot be called again from one."""
    kept = {
        k: v for k, v in (entry.get("chunking") or {}).items() if v is not None and k != "context"
    }
    return {**kept, **options}


@dataclass
class RechunkedDocument:
    """One document in a :class:`RechunkPreview`: its chunks in the index now
    and after, and the settings it was cut with and would be cut with."""

    doc_id: str
    chunks_now: int
    chunks_after: int
    chunking_now: Dict[str, Any] = field(default_factory=dict)
    chunking_after: Dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.chunks_now != self.chunks_after or any(
            self.chunking_now.get(k) != v for k, v in self.chunking_after.items() if v is not None
        )


@dataclass
class RechunkPreview:
    """What :meth:`Vectrix.rechunk` would do, from :meth:`Vectrix.rechunk_preview`."""

    documents: List[RechunkedDocument] = field(default_factory=list)

    @property
    def chunks_now(self) -> int:
        return sum(d.chunks_now for d in self.documents)

    @property
    def chunks_after(self) -> int:
        return sum(d.chunks_after for d in self.documents)

    @property
    def changed(self) -> List[RechunkedDocument]:
        """The documents whose chunks or settings would change."""
        return [d for d in self.documents if d.changed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "documents": [
                {
                    "doc_id": d.doc_id,
                    "chunks_now": d.chunks_now,
                    "chunks_after": d.chunks_after,
                    "chunking_now": dict(d.chunking_now),
                    "chunking_after": dict(d.chunking_after),
                    "changed": d.changed,
                }
                for d in self.documents
            ],
            "chunks_now": self.chunks_now,
            "chunks_after": self.chunks_after,
            "changed": len(self.changed),
        }

    def __str__(self) -> str:
        return (
            f"{len(self.documents)} documents, {len(self.changed)} would change: "
            f"{self.chunks_now} chunks now, {self.chunks_after} after"
        )


@dataclass
class AddReport:
    """What the last ``add()`` did: the ids stored, and for ``dedupe=``, the
    ids skipped as ``(skipped_id, duplicate_of, similarity)``."""

    added: List[str] = field(default_factory=list)
    skipped: List[Any] = field(default_factory=list)


class _TextCache(dict):
    """Document texts, backed by the collection when not already in memory.

    ``Vectrix`` populated this on ``add()`` only, so a process that merely
    opened an existing database found it empty and every search result carried
    ``text=''``. The text is persisted with the point, so a miss is a lookup
    rather than a loss.
    """

    def __init__(self, owner: "Vectrix") -> None:
        super().__init__()
        self._owner = owner

    def _lookup(self, doc_id: str) -> Optional[str]:
        collection = getattr(self._owner, "_collection", None)
        if collection is None:
            return None
        try:
            point = collection.get(doc_id)
        except Exception:  # pragma: no cover - backend specific
            return None
        text = getattr(point, "text", None) if point is not None else None
        if text:
            super().__setitem__(doc_id, text)
        return text

    def get(self, doc_id, default=None):
        found = super().get(doc_id)
        if found:
            return found
        return self._lookup(doc_id) or default

    def __contains__(self, doc_id) -> bool:
        return super().__contains__(doc_id) or bool(self._lookup(doc_id))

    def __getitem__(self, doc_id):
        found = super().get(doc_id)
        if found:
            return found
        text = self._lookup(doc_id)
        if text is None:
            raise KeyError(doc_id)
        return text


_EMBEDDED_MODEL_KEYS = {
    "vectrixdb/bge-small-en-v1.5": "bge_small_en",
    # The label every collection created before 2.2 records. The model behind
    # it was always e5-small-v2; the name was wrong from the start and is kept
    # so those collections resolve to the vectors they were built with.
    "vectrixdb/all-MiniLM-L6-v2": "dense_en",
    "vectrixdb/e5-small-v2": "dense_en",
    "vectrixdb/multilingual": "dense",
    "vectrixdb/colbert": "colbert",
}


def _same_embedded_model(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two recorded model names mean the same vectors.

    A name and the embedder key it resolves to are one model: a server
    before 2.2 recorded the key, ``bge_small_en``, where the library records
    ``vectrixdb/bge-small-en-v1.5``, and the two must not read as a mismatch.
    """
    if a is None or b is None:
        return a == b
    return _EMBEDDED_MODEL_KEYS.get(a, a) == _EMBEDDED_MODEL_KEYS.get(b, b)


def _embedded_model_name(recorded: Optional[str]) -> Optional[str]:
    """The name a recorded model is shown by: a bare embedder key becomes the
    library's name for it, and anything else is shown as written."""
    if recorded is None or recorded in _EMBEDDED_MODEL_KEYS:
        return recorded
    names = {key: name for name, key in _EMBEDDED_MODEL_KEYS.items()}
    return names.get(recorded, recorded)


# ============================================================================
# THE MAIN API: Vectrix
# ============================================================================
#
# INPUT   a name, or a path
# OUTPUT  the collection: add, search, ask, delete, revoke, policies,
#         evaluation, and everything else, one line each
#
# Zero configuration: text in, results out.


class Vectrix:
    """
    Vectrix - The simplest vector database.

    Example:
        >>> db = Vectrix("my_collection")
        >>> db.add(["doc 1", "doc 2", "doc 3"])
        >>> results = db.search("query")
        >>> print(results.top.text)

    With metadata:
        >>> db.add(
        ...     texts=["Python guide", "ML tutorial"],
        ...     metadata=[{"category": "programming"}, {"category": "ai"}]
        ... )
        >>> results = db.search("code", filter={"category": "programming"})

    Full power:
        >>> results = db.search(
        ...     "machine learning",
        ...     mode="ultimate",    # dense + sparse + late interaction
        ...     rerank="mmr",       # diversity reranking
        ...     limit=10
        ... )
    """

    # Default embedding model (bundled, no network calls)
    _default_model = "vectrixdb/bge-small-en-v1.5"  # Bundled ONNX model, INT8
    # What collections created before 2.2 were built with (it was e5-small-v2
    # under a wrong label). A collection with vectors and no recorded model
    # is one of those and is opened with it, not with the new default.
    _legacy_model = "vectrixdb/all-MiniLM-L6-v2"
    _default_dimension = 384

    # Shared model cache
    _model_cache: Dict[str, Any] = {}

    # Sparse embedder (BM25) - shared instance
    _sparse_embedder = None

    # Reranker - shared instance
    _reranker = None

    # Bundled model aliases for easy selection
    # These map to MODEL_CONFIG keys in embedded.py
    _DENSE_ALIASES = {
        "multilingual": "dense",  # Multilingual (download on first use)
        "multi": "dense",
        "e5-small": "dense_en",  # Fetched once on first use since 2.2 (21 MiB)
        "bge-small": "bge_small_en",  # Bundled, the default
        "e5-small-fp32": "e5_small",  # Download from GitHub (127MB)
        # Higher quality (v1.9.0)
        "bge-base": "bge_base_en",  # Download from GitHub (~110MB INT8)
        "bge-base-en-v1.5": "bge_base_en",
        "bge": "bge_base_en",  # Default BGE points to base (higher quality)
    }

    _SPARSE_ALIASES = {
        "bm25": "sparse",  # Bundled BM25 vocabulary (1MB)
        "splade": "splade_pp_en",  # Download from GitHub release (508MB)
        "splade++": "splade_pp_en",
        "neural": "splade_pp_en",  # Neural sparse (SPLADE++)
    }

    _RERANKER_ALIASES = {
        "l6": "reranker_en_l6",  # Download from GitHub (87MB)
        "L6": "reranker_en_l6",
        "l12": "reranker_en",  # Bundled (33MB)
        "L12": "reranker_en",
        "minilm-l6": "reranker_en_l6",
        "minilm-l12": "reranker_en",
        # Higher quality (v1.9.0)
        "bge-reranker": "bge_reranker_base",  # Download from GitHub (~110MB INT8)
        "bge-reranker-base": "bge_reranker_base",
    }

    _LATE_INTERACTION_ALIASES = {
        "colbert": "late_interaction_en",  # Bundled (33MB)
        "colbert-small": "late_interaction_en",
        "answerai-colbert": "late_interaction_en",
        # Higher quality (v1.9.0)
        "colbert-v2": "colbert_v2",  # Download from GitHub (~110MB INT8)
        "colbertv2": "colbert_v2",
        "colbertv2.0": "colbert_v2",
    }

    # Supported model prefixes and their handlers
    _MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
        # Bundled models (no network calls after setup)
        "vectrixdb/bge-small-en-v1.5": {"type": "embedded", "dimension": 384},
        "vectrixdb/all-MiniLM-L6-v2": {"type": "embedded", "dimension": 384},
        "vectrixdb/bm25": {"type": "sparse"},
        "vectrixdb/ms-marco-MiniLM-L-6-v2": {"type": "reranker"},
        # Qdrant FastEmbed models (cached after first download)
        "qdrant/all-MiniLM-L6-v2": {
            "type": "fastembed",
            "dimension": 384,
            "model_id": "sentence-transformers/all-MiniLM-L6-v2",
        },
        "qdrant/bge-small-en-v1.5": {
            "type": "fastembed",
            "dimension": 384,
            "model_id": "BAAI/bge-small-en-v1.5",
        },
        "qdrant/bge-base-en-v1.5": {
            "type": "fastembed",
            "dimension": 768,
            "model_id": "BAAI/bge-base-en-v1.5",
        },
        "qdrant/bge-large-en-v1.5": {
            "type": "fastembed",
            "dimension": 1024,
            "model_id": "BAAI/bge-large-en-v1.5",
        },
        "qdrant/bm25": {"type": "fastembed-sparse", "model_id": "Qdrant/bm25"},
        "qdrant/colbert-v2": {
            "type": "fastembed-colbert",
            "dimension": 128,
            "model_id": "colbert-ir/colbertv2.0",
        },
        "qdrant/clip-ViT-B-32": {
            "type": "fastembed",
            "dimension": 512,
            "model_id": "Qdrant/clip-ViT-B-32-text",
        },
        # Sentence-transformers models (HuggingFace)
        "sentence-transformers/all-MiniLM-L6-v2": {
            "type": "sentence-transformers",
            "dimension": 384,
        },
        "sentence-transformers/all-mpnet-base-v2": {
            "type": "sentence-transformers",
            "dimension": 768,
        },
        "sentence-transformers/all-MiniLM-L12-v2": {
            "type": "sentence-transformers",
            "dimension": 384,
        },
        "sentence-transformers/paraphrase-MiniLM-L6-v2": {
            "type": "sentence-transformers",
            "dimension": 384,
        },
        "sentence-transformers/multi-qa-MiniLM-L6-cos-v1": {
            "type": "sentence-transformers",
            "dimension": 384,
        },
        # BAAI models (via sentence-transformers or fastembed)
        "BAAI/bge-small-en-v1.5": {"type": "sentence-transformers", "dimension": 384},
        "BAAI/bge-base-en-v1.5": {"type": "sentence-transformers", "dimension": 768},
        "BAAI/bge-large-en-v1.5": {"type": "sentence-transformers", "dimension": 1024},
        "BAAI/bge-m3": {"type": "sentence-transformers", "dimension": 1024},
        # OpenAI (requires embed_fn)
        "openai/text-embedding-3-small": {"type": "openai", "dimension": 1536},
        "openai/text-embedding-3-large": {"type": "openai", "dimension": 3072},
        "openai/text-embedding-ada-002": {"type": "openai", "dimension": 1536},
        # Cohere (requires embed_fn)
        "cohere/embed-english-v3.0": {"type": "cohere", "dimension": 1024},
        "cohere/embed-multilingual-v3.0": {"type": "cohere", "dimension": 1024},
        # Voyage AI (requires embed_fn)
        "voyage/voyage-3": {"type": "voyage", "dimension": 1024},
        "voyage/voyage-3-lite": {"type": "voyage", "dimension": 512},
        # Jina AI
        "jina/jina-embeddings-v2-base-en": {"type": "sentence-transformers", "dimension": 768},
        "jina/jina-embeddings-v2-small-en": {"type": "sentence-transformers", "dimension": 512},
    }

    def __init__(
        self,
        name: str = "default",
        path: str = "./vectrixdb_data",
        model: Optional[str] = None,
        dimension: Optional[int] = None,
        embed_fn: Any = None,
        model_path: Optional[str] = None,
        language: Optional[str] = None,
        tier: str = "dense",
        # New: Search mode and model selection
        mode: Optional[Literal["dense", "hybrid", "ultimate", "graph"]] = None,
        dense_model: Any = None,
        sparse_model: Optional[str] = None,
        reranker_model: Optional[str] = None,
        late_interaction_model: Optional[str] = None,
        # Storage backend (Lakebase, DeltaLake, CosmosDB, etc.)
        storage_backend: Any = None,
        # Collection metadata
        description: Optional[str] = None,
        # Knowledge graph (graph mode only)
        graphrag_config: Any = None,
        # Exact token counts for token_budget; the default is a 4-chars estimate
        token_counter: Any = None,
        # BM25 weight per metadata field, e.g. {"title": 2.0}
        text_boosts: Optional[Dict[str, float]] = None,
        # What language the texts are, for the sparse half of hybrid search.
        # "en" stems and drops stopwords; anything else indexes words as
        # they are, in any script, which is right for everything that is
        # not English. Persisted with the collection.
        text_language: str = "en",
        # Memory-map the index instead of loading it; searches only
        readonly: bool = False,
        # Vectors per shard. None, the default, builds one index in memory,
        # which is what every collection has always done. Set it and the
        # index seals a shard to disk whenever it fills and memory-maps it,
        # so only one shard is ever in memory and the collection can be
        # written past RAM. Needs a path on disk.
        shard_size: Optional[int] = None,
        # Cache vectors by content hash beside the collection, so re-adding
        # unchanged text never embeds it again. Only for collections on disk.
        embedding_cache: bool = True,
        # Timing hooks: called after every search() and add() with a small
        # dict (op, ms, count, mode, collection). For the host's own metrics;
        # the library never imports a metrics client.
        on_search: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_add: Optional[Callable[[Dict[str, Any]], None]] = None,
        # An entitlement policy. Once a collection carries one, a read needs
        # a principal: db.as_principal({...}).search(...). Reopening without
        # passing it again does not drop it, the collection keeps it.
        policy: Optional["Policy"] = None,
        # Where a decision record goes after every policied search. The
        # library builds the record and never chooses a destination.
        on_retrieval: Optional["AuditSink"] = None,
        # Seconds. Every read is held until it has taken a whole number
        # of these, so how long an answer took stops being a measure of
        # how much the principal was allowed to see.
        timing_floor: Optional[float] = None,
        # Who reads which file type. A mapping of suffix to callable, an
        # ExtractorRegistry, or an HttpExtractor. A suffix named here goes
        # to that extractor in add_document; everything else is read by
        # the built-in readers. See vectrixdb.extract.
        extractors: Any = None,
        # Keep the Markdown every document was indexed from, so it can be
        # cut a different way without extracting it again, shown, and
        # audited. True keeps it beside the collection; a folder, or a
        # vectrixdb.documents.DocumentStore, puts it anywhere else. Never
        # the folder the originals are in.
        keep_source: Any = None,
        # Keep the chunks each document was cut into, one JSON Lines file a
        # document, so what was embedded can be read without the index. True
        # keeps them beside the collection; a folder, or a
        # vectrixdb.documents.ChunkStore, puts them anywhere else.
        keep_chunks: Any = None,
        # Keep a document's Markdown before cutting it, and cut what was kept.
        # Reading a file then ends when its Markdown is written, and a later
        # step that fails leaves the Markdown for the next try, which starts
        # from it rather than reading the file again. Needs keep_source.
        markdown_first: bool = False,
        # What describes a figure: a callable of the image bytes and a
        # context, returning the description. It runs once, at ingestion,
        # on figures whose image is at hand and that have no description
        # yet. See vectrixdb.ingest.describe_figures.
        describe_figures: Any = None,
        # Reads the pages of a PDF that have no text layer: a callable of one
        # page drawn as a PNG, returning its lines in reading order. Only
        # pages with next to no text are drawn and sent, so a PDF that is
        # text throughout never calls it. Unlike registering an extractor
        # for ".pdf", this keeps the document's figures.
        page_ocr: Any = None,
        # Where the enclosing sections small chunks point back to are kept.
        # A path, or cosmos://<account>.documents.azure.com/<db>/<container>
        # when more than one process reads them, which on anything that
        # scales out is the difference between parent-child retrieval
        # working and quietly returning nothing. Left out, a file beside
        # the collection, or memory when it has no path.
        parent_store: Any = None,
        # Where a copy of every chunk is kept for the collection pages, which
        # every write here reaches. Only for when more than one process
        # writes: the instance that ingested a document is not the one
        # drawing the page, and the table beside it holds only its own. The
        # address, cosmos://<account>.documents.azure.com/<db>/<container>,
        # or a store of your own; see vectrixdb.chunk_store. Not keep_chunks,
        # which keeps a file a document. Left out, the pages read the table.
        chunk_store: Any = None,
        # Where every collection's rules are kept, shared by every server: its
        # policy, who may see it, and whether it is masked. A path,
        # sqlite:///, postgresql://, cosmos:// or dynamodb://, or a store of
        # your own; see vectrixdb.collection_records. With a record there for
        # this collection, its policy is the one enforced, and a different
        # policy= refuses to open. Left out, the policy lives in the
        # collection's own metadata, as before.
        collection_store: Any = None,
        # A model that embeds pictures and words into one space, CLIP or
        # SigLIP, brought by you: an object with embed_images(list of bytes),
        # embed_texts(list of str) and dimension. Every figure's image is
        # embedded into a vector index of its own, named "image", so a figure
        # can be found by what it looks like: search(vectors="image").
        image_embedder: Any = None,
        # A reranker you bring: anything with rerank(query, texts, top=None)
        # returning (index, score) pairs, best first. It takes the place of
        # the bundled cross-encoder in every mode that reranks.
        # vectrixdb.models.bedrock.BedrockReranker is one.
        reranker: Any = None,
    ):
        """
        Create or open a VectrixDB collection.

        Args:
            name: Collection name
            path: Storage path (default: ./vectrixdb_data)
            model: Embedding model identifier (deprecated, use dense_model instead)
            dimension: Vector dimension (auto-detected from model)
            embed_fn: Custom embedding function: fn(texts: List[str]) -> np.ndarray
            model_path: Path to custom ONNX model directory
            language: Which bundled models to prefer. The default is English:
                      a fresh collection records vectrixdb/bge-small-en-v1.5.
                      "multi" selects the multilingual models, which are not
                      bundled and have to be fetched.
            tier: Storage tier (deprecated, use 'mode' instead)
            mode: Default search mode - validates required models at creation:
                  - "dense": Vector similarity only (fastest)
                  - "hybrid": Dense + Sparse + Reranker (balanced)
                  - "ultimate": Dense + Sparse + Reranker + ColBERT (best quality)
                  - "graph": Ultimate + Knowledge Graph (for GraphRAG)
            dense_model: Dense embedding model (bundled alias or HuggingFace path)
                  Bundled: "bge-small" (the default), "e5-small" (the default
                  before 2.2, kept so older collections open unchanged)
                  Fetch yourself: "bge-base", "e5-small-fp32". Naming one
                  raises on first use with the URL and the directory to
                  unpack into; nothing downloads on its own.
                  HuggingFace: "BAAI/bge-large-en-v1.5", "intfloat/e5-large-v2", etc.
            sparse_model: Sparse embedding model (bundled alias or HuggingFace path)
                  Bundled: "bm25"
                  Fetch yourself: "splade", "splade++". A learned sparse model
                  also needs a storage backend with its own hybrid search;
                  on a local collection the sparse half is always BM25, and
                  naming one here warns.
                  HuggingFace: "naver/splade-cocondenser-ensembledistil", etc.
            reranker_model: Cross-encoder reranker model (bundled alias or HuggingFace path)
                  Bundled: "L12"
                  Fetch yourself: "L6", "bge-reranker"
                  HuggingFace: "cross-encoder/ms-marco-MiniLM-L-12-v2", etc.
            late_interaction_model: ColBERT model (bundled alias or HuggingFace path)
                  Bundled: "colbert"
                  Fetch yourself: "colbert-v2"
                  HuggingFace: "colbert-ir/colbertv2.0", etc.
            storage_backend: External storage backend (VectrixDB instance with Lakebase, DeltaLake, etc.)
                  When provided, uses external storage instead of local SQLite.
                  Schema adapts based on mode:
                  - dense: dense_embedding only
                  - hybrid: dense_embedding + sparse_embedding
                  - ultimate: + late_interaction_embedding
                  - graph: + graph tables
            description: Optional description for the collection (stored in _vectrix_collections)
            token_counter: fn(text) -> int used by ``token_budget``. Without one,
                  tokens are estimated at four characters each, which is within
                  about twenty percent for English prose and needs no download.
            text_boosts: BM25 weight per metadata field, e.g. ``{"title": 2.0}``,
                  so a keyword hit in a title outranks the same hit in the body.
                  Stored with the collection and applied on every open.
            readonly: Memory-map the ANN index instead of loading it. Opening is
                  instant and a collection larger than RAM can be searched; any
                  write raises ``ConfigurationError``.

        Examples:
            # Basic usage (bundled models, offline)
            >>> db = Vectrix("docs")
            >>> results = db.search("query")

            # With mode and model selection (bundled)
            >>> db = Vectrix(
            ...     "docs",
            ...     mode="hybrid",
            ...     dense_model="bge-small",
            ...     sparse_model="splade",
            ...     reranker_model="L6",
            ... )
            >>> results = db.search("query")  # Uses hybrid by default

            # With HuggingFace models
            >>> db = Vectrix(
            ...     "docs",
            ...     mode="ultimate",
            ...     dense_model="BAAI/bge-large-en-v1.5",
            ...     sparse_model="naver/splade-cocondenser-ensembledistil",
            ...     reranker_model="cross-encoder/ms-marco-MiniLM-L-12-v2",
            ...     late_interaction_model="colbert-ir/colbertv2.0",
            ... )

            # Mixed bundled + HuggingFace
            >>> db = Vectrix(
            ...     "docs",
            ...     mode="hybrid",
            ...     dense_model="bge-small",  # Bundled
            ...     sparse_model="splade",     # Bundled
            ...     reranker_model="cross-encoder/ms-marco-TinyBERT-L-2-v2",  # HuggingFace
            ... )

            # With storage backend (Lakebase)
            >>> from vectrixdb import VectrixDB
            >>> lakebase = VectrixDB.with_lakebase(host="...", database="...", user="...", password="...")
            >>> db = Vectrix(
            ...     "products",
            ...     mode="ultimate",
            ...     dense_model="bge-small",
            ...     sparse_model="splade",
            ...     reranker_model="L6",
            ...     late_interaction_model="colbert",
            ...     storage_backend=lakebase,
            ... )
            >>> db.add(texts=["Product A", "Product B"])  # Stores all embeddings in Lakebase
            >>> results = db.search("query")  # Full ultimate search from Lakebase
        """
        # More than one dense model: the first is the collection's own, as
        # ever, and each of the others gets an index of its own beside it.
        # An entry is a model name, or (label, embed_fn, dimension).
        self._named_specs: List[Any] = []
        if isinstance(dense_model, (list, tuple)):
            if not dense_model:
                raise ConfigurationError(
                    "dense_model is a model name, or a list that starts with one"
                )
            self._named_specs = list(dense_model[1:])
            dense_model = dense_model[0]
            if dense_model is not None and not isinstance(dense_model, str):
                raise ConfigurationError(
                    "the first dense model is the collection's own: a model name, or None with embed_fn="
                )
        self._image_embedder = image_embedder
        if image_embedder is not None:
            for method in ("embed_images", "embed_texts"):
                if not callable(getattr(image_embedder, method, None)):
                    raise TypeError(
                        f"image_embedder has {method}(a list) returning one vector each, and this one has no {method}"
                    )
            if (
                not isinstance(getattr(image_embedder, "dimension", None), int)
                or image_embedder.dimension < 1
            ):
                raise TypeError(
                    "image_embedder has a dimension, the length of its vectors, as a whole number"
                )
            if any(
                spec == IMAGE_VECTOR
                or (isinstance(spec, (list, tuple)) and spec and spec[0] == IMAGE_VECTOR)
                for spec in self._named_specs
            ):
                raise ConfigurationError(
                    f"{IMAGE_VECTOR!r} names the vectors image_embedder makes, so no dense model can be called that"
                )
            # The words of a question go through the model's text side, which is
            # what makes a sentence comparable with a picture at all.
            self._named_specs.append(
                (IMAGE_VECTOR, image_embedder.embed_texts, image_embedder.dimension)
            )
        self._named: Dict[str, "Vectrix"] = {}
        self._named_scope = threading.local()
        self._home_scope = threading.local()
        self.name = name
        self.path = path
        self.embed_fn = embed_fn
        self.model_path = model_path
        self.language = language
        self.storage_backend = storage_backend
        self.description = description
        self._graphrag_config = graphrag_config
        self.token_counter = token_counter
        self._memory: Any = None
        self.text_boosts: Dict[str, float] = dict(text_boosts or {})
        self.text_language = text_language
        self.shard_size = int(shard_size) if shard_size else None
        self.readonly = readonly
        self._embedding_cache_enabled = embedding_cache
        self.on_search = on_search
        self.on_add = on_add
        self._extractors: Any = None
        if extractors is not None:
            from .extract import resolve

            # Resolved here so a mistake is an error at construction, not
            # at the first PDF.
            self._extractors = resolve(extractors)
        self._keep_source = keep_source
        self._documents: Any = None
        self._keep_chunks = keep_chunks
        self._chunk_files: Any = None
        self._markdown_first = bool(markdown_first)
        if describe_figures is not None and not callable(describe_figures):
            raise TypeError(f"describe_figures is callable, got {type(describe_figures).__name__}")
        self._describe_figures = describe_figures
        if page_ocr is not None and not callable(page_ocr):
            raise TypeError(f"page_ocr is callable, got {type(page_ocr).__name__}")
        self._page_ocr = page_ocr
        self._parent_store = parent_store
        self._chunk_store_given = chunk_store
        self._collection_store_given = collection_store
        if reranker is not None and not callable(getattr(reranker, "rerank", None)):
            raise TypeError("reranker has a rerank(query, texts, top=None) method")
        self._external_reranker = reranker
        self._policy: Optional["Policy"] = None
        if timing_floor is not None and timing_floor <= 0:
            raise ConfigurationError(
                f"timing_floor is a number of seconds above zero, got {timing_floor!r}. "
                f"Pass None to turn it off."
            )
        self._timing_floor: Optional[float] = timing_floor
        self._policy_requested: Optional["Policy"] = policy
        self._principal: Optional[Mapping[str, Any]] = None
        self._audit_context: Optional["AuditContext"] = None
        self.on_retrieval = on_retrieval
        # Set to a dict on the throwaway copy an audited search makes, for
        # Collection's withheld counts to land in. None everywhere else,
        # because those counts must never travel with results a caller holds.
        self._policy_counts: Optional[Dict[str, Any]] = None
        # Set by add_document around its add(), read by the ingestion record.
        self._ingestion_provenance: Optional[Dict[str, Any]] = None

        # Handle mode/tier (mode takes precedence)
        self.default_mode = mode.lower() if mode else (tier.lower() if tier else "dense")
        self.tier = self.default_mode  # Keep for backwards compatibility

        # Store model selections
        self.dense_model_name = dense_model
        self._model_explicit = any(
            x is not None for x in (model, dense_model, embed_fn, model_path)
        )
        self.sparse_model_name = sparse_model
        self.reranker_model_name = reranker_model
        self.late_interaction_model_name = late_interaction_model

        # Validate mode
        valid_modes = ["dense", "hybrid", "ultimate", "graph"]
        if self.default_mode not in valid_modes:
            raise ValueError(f"Invalid mode '{self.default_mode}'. Must be one of: {valid_modes}")

        # Validate required models for the selected mode
        self._validate_mode_models()

        # Validate storage backend compatibility with mode
        self._validate_storage_backend_mode(storage_backend)

        # A store whose own embedder is the collection's: Azure AI Search
        # with embeddings="azure", where the index holds a deployment's
        # vectors and nothing else.
        store_fn = getattr(self._vector_store(), "default_embed_fn", None)
        if store_fn is not None and all(
            x is None for x in (model, dense_model, embed_fn, model_path)
        ):
            self.embed_fn = store_fn
            config = self._vector_store().config
            if getattr(config, "opensearch_embeddings", "vectrixdb") == "bedrock":
                fn = config.opensearch_embed_fn
                dimension = (
                    dimension
                    or config.opensearch_embed_dimensions
                    or getattr(fn, "dimensions", None)
                )
                model = str(getattr(fn, "label", "bedrock"))
            else:
                spec = getattr(config, "azure_search_vectorizer", None) or {}
                dimension = dimension or spec.get("dimensions")
                model = "azure-openai:" + str(
                    spec.get("model") or spec.get("deployment") or "custom"
                )

        # Parse model identifier (for dense model)
        self._parse_model(model or dense_model, dimension)

        self._model: Any = None
        self._db: Any = None
        self._collection: Any = None
        self._texts: Dict[str, str] = _TextCache(self)  # id -> text, storage-backed
        self._instance_reranker: Any = None  # Instance-level reranker
        self._instance_late_interaction: Any = None  # Instance-level late interaction
        self._instance_sparse_embedder: Any = None  # Instance-level sparse embedder

        self._init_db()
        self._warn_if_sparse_model_is_unreachable()

    def _warn_if_sparse_model_is_unreachable(self) -> None:
        """Say so when the named sparse model cannot be the one that runs.

        The sparse half of a hybrid search reaches a learned model only
        through a storage backend's own hybrid search. On a local collection
        it is the BM25 text index, whatever ``sparse_model`` says, so asking
        for SPLADE quietly returned BM25 results: identical scores, no
        warning, and no way to tell from the outside.
        """
        name = self.sparse_model_name
        if not name or self.default_mode not in ("hybrid", "ultimate", "graph"):
            return
        resolved = self._SPARSE_ALIASES.get(name, name)
        if resolved == "sparse" or getattr(self, "_using_storage_backend", False):
            return
        warnings.warn(
            f"sparse_model={name!r} is ignored on a local collection: the sparse half of "
            f"hybrid search here is the bundled BM25 index, and the results you get will "
            f"be BM25 results. A learned sparse model runs only against a storage backend "
            f"that implements its own hybrid search.",
            SparseModelUnavailableWarning,
            stacklevel=3,
        )

    def _validate_mode_models(self):
        """Validate that required models are configured for the selected mode."""
        mode = self.default_mode

        if mode == "dense":
            # No additional models required
            pass
        elif mode == "hybrid":
            # Requires: dense + sparse + reranker
            if self.sparse_model_name is None:
                self.sparse_model_name = "bm25"  # Default to BM25
            if self.reranker_model_name is None:
                self.reranker_model_name = "L12"  # bundled, so it works offline
        elif mode == "ultimate":
            # Requires: dense + sparse + reranker + late_interaction
            if self.sparse_model_name is None:
                self.sparse_model_name = "bm25"
            if self.reranker_model_name is None:
                self.reranker_model_name = "L12"  # bundled, so it works offline
            if self.late_interaction_model_name is None:
                self.late_interaction_model_name = "colbert"
        elif mode == "graph":
            # Same as ultimate + graph (graph handled separately)
            if self.sparse_model_name is None:
                self.sparse_model_name = "bm25"
            if self.reranker_model_name is None:
                self.reranker_model_name = "L12"  # bundled, so it works offline
            if self.late_interaction_model_name is None:
                self.late_interaction_model_name = "colbert"

    def _validate_storage_backend_mode(self, storage_backend):
        """
        Validate that the storage backend supports the selected mode.

        OpenSearch does not support 'ultimate' or 'graph' modes because:
        - ColBERT (late interaction) requires client-side MaxSim computation
        - OpenSearch k-NN only supports dense vectors, not token-level embeddings
        """
        if storage_backend is None:
            return

        is_opensearch = False
        if hasattr(storage_backend, "_storage"):
            class_name = storage_backend._storage.__class__.__name__
            if class_name == "OpenSearchStorage":
                is_opensearch = True
        elif hasattr(storage_backend, "__class__"):
            if storage_backend.__class__.__name__ == "OpenSearchStorage":
                is_opensearch = True

        if is_opensearch and self.default_mode in ("ultimate", "graph"):
            raise ValueError(
                f"OpenSearch does not support '{self.default_mode}' mode. "
                f"ColBERT (late interaction) requires MaxSim computation that "
                f"OpenSearch k-NN cannot perform natively. "
                f"Use mode='dense' or mode='hybrid' with OpenSearch, "
                f"or switch to Aurora PostgreSQL for full mode support."
            )

    def _is_huggingface_model(self, model_name: str) -> bool:
        """Check if model name is a HuggingFace model (contains /)."""
        if model_name is None:
            return False
        return (
            "/" in model_name
            and not model_name.startswith("vectrixdb/")
            and not model_name.startswith("github:")
        )

    def _is_github_model(self, model_name: str) -> bool:
        """Check if model name is a GitHub release (starts with github:)."""
        if model_name is None:
            return False
        return model_name.startswith("github:")

    def _download_github_model(self, model_name: str, model_type: str) -> str:
        """
        Download model from GitHub release and return path.

        Args:
            model_name: Model name in format "github:release-tag"
            model_type: Type of model ("sparse", "reranker", "late_interaction")

        Returns:
            Path to downloaded model directory
        """
        import tempfile
        import zipfile
        import urllib.request
        import shutil

        # Parse release tag
        release_tag = model_name.replace("github:", "")

        # GitHub release URL
        github_repo = "knowusuboaky/VectrixDB"

        # Map model types to expected zip file names
        zip_names = {
            "sparse": "sparse.zip",
            "reranker": "reranker.zip",
            "late_interaction": "late_interaction.zip",
            "dense": "dense.zip",
        }

        zip_name = zip_names.get(model_type, f"{model_type}.zip")
        url = f"https://github.com/{github_repo}/releases/download/{release_tag}/{zip_name}"

        # Download to cache directory
        cache_dir = Path.home() / ".cache" / "vectrixdb" / "github" / release_tag
        model_dir = cache_dir / model_type

        # Check if already downloaded
        if model_dir.exists() and (model_dir / "model.onnx").exists():
            return str(model_dir)

        # Create cache directory
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Download zip file
        logger.info("downloading the %s model from release %r", model_type, release_tag)
        zip_path = cache_dir / zip_name

        try:
            urllib.request.urlretrieve(url, zip_path)
        except Exception as e:
            from .exceptions import ModelDownloadError
            from .models.downloader import publish_commands

            # The one source a "github:" name has. A 404 is a release nobody
            # has made, not a network fault, and the message says which.
            missing = getattr(e, "code", None) == 404
            lines = [
                f"Could not fetch the {model_type} model from the GitHub release {release_tag!r}. Sources tried:",
                f"  1. {url}",
                f"     {'HTTP 404: this release, or its asset, does not exist' if missing else e}",
            ]
            if missing:
                lines.append("A maintainer publishes it with:")
                lines += [f"  {command}" for command in publish_commands(model_type, release_tag)]
            raise ModelDownloadError("\n".join(lines)) from e

        # Extract zip file
        from .models.downloader import _inside

        with zipfile.ZipFile(zip_path, "r") as zf:
            # Every entry inside the cache directory, or none extracted.
            for member in zf.namelist():
                _inside(cache_dir, member)
            zf.extractall(cache_dir)

        # Clean up zip
        zip_path.unlink()

        # Find extracted model directory
        for item in cache_dir.iterdir():
            if item.is_dir() and (item / "model.onnx").exists():
                # Rename to standard name
                if item.name != model_type:
                    target = cache_dir / model_type
                    if target.exists():
                        shutil.rmtree(target)
                    item.rename(target)
                    return str(target)
                return str(item)

        # Check if files are directly in cache_dir
        if (cache_dir / "model.onnx").exists():
            model_dir.mkdir(exist_ok=True)
            for f in cache_dir.glob("*"):
                if f.is_file():
                    shutil.move(str(f), str(model_dir / f.name))
            return str(model_dir)

        raise RuntimeError(f"Model not found after extracting {zip_name}")

    # Past this share of tombstones, search() says so on Results.degraded.
    # Measured at 20,000 vectors: a fifth deleted took a query from 1.0 ms to
    # 18.0 ms, which is worth telling someone about.
    _TOMBSTONE_NOTE_AT = 0.2

    def _validate_search_mode(self, mode: str):
        """Validate that the requested search mode is compatible with configured models."""
        # Mode hierarchy: dense < hybrid < ultimate < graph
        mode_levels = {"dense": 1, "sparse": 1, "hybrid": 2, "ultimate": 3, "graph": 4}
        default_level = mode_levels.get(self.default_mode, 1)
        requested_level = mode_levels.get(mode, 1)

        if requested_level > default_level:
            raise ValueError(
                f"Cannot use '{mode}' mode. Instance configured for '{self.default_mode}' mode. "
                f"You can only use modes at or below your configured level: "
                f"{[m for m, l in mode_levels.items() if l <= default_level]}"
            )

    # Bundled model folder names (in vectrixdb/models/data/)
    # Model folder names that map to embedded ONNX models
    # These are either bundled with the package or downloaded from GitHub releases
    _BUNDLED_MODEL_FOLDERS = {
        # Bundled with pip install
        "dense_en",
        "colbert",
        "reranker_en",
        "sparse",
        # Downloaded from GitHub releases (cached after first use)
        "bge_base_en",
        "bge_small_en",
        "e5_small",
        "bge_reranker_base",
        "reranker_en_l6",
        "colbert_v2",
        "late_interaction_en",
        "splade_pp_en",
    }

    def _parse_model(self, model: Optional[str], dimension: Optional[int]) -> None:
        """Parse model identifier and set up embedding configuration."""
        # "openai:<model>" is any OpenAI-compatible endpoint; "plugin:<name>"
        # is an installed embedder plugin. Both become an embed_fn.
        if isinstance(model, str) and model.startswith("openai:") and self.embed_fn is None:
            from .models.openai_compat import OpenAIEmbedder

            embedder = OpenAIEmbedder(model[len("openai:") :])
            self.embed_fn = embedder
            dimension = int(dimension or embedder.dimension)
        elif isinstance(model, str) and model.startswith("plugin:") and self.embed_fn is None:
            from . import plugins

            factory = plugins.load("embedders", model[len("plugin:") :])
            instance = factory() if isinstance(factory, type) else factory
            self.embed_fn = instance.embed if hasattr(instance, "embed") else instance
            dimension = int(dimension or getattr(instance, "dimension", 0) or 0)

        # Custom embedding function provided
        if self.embed_fn is not None:
            self.model_type = "custom"
            self.model_name = model or "custom"
            self.dimension = dimension or self._get_dimension_from_registry(model) or 384
            return

        # Custom ONNX model path provided
        if self.model_path is not None:
            self.model_type = "custom-onnx"
            self.model_name = "custom-onnx"
            self.dimension = dimension or 384
            return

        # No model specified - use bundled default
        if model is None:
            self.model_type = "embedded"
            self.model_name = self._default_model
            self.dimension = dimension or 384
            return

        # Resolve dense model aliases (e.g., "e5-small" -> "dense_en")
        resolved_model = self._DENSE_ALIASES.get(model, model)

        # Check if it's a bundled model folder name
        if resolved_model in self._BUNDLED_MODEL_FOLDERS:
            self.model_type = "embedded"
            self.model_name = resolved_model
            # Get dimension from model config or use default
            model_dimensions = {
                "bge_base_en": 768,  # bge-base-en-v1.5
                "colbert_v2": 128,  # colbertv2.0 token embeddings
                "late_interaction_en": 128,  # answerai-colbert-small-v1
            }
            self.dimension = dimension or model_dimensions.get(resolved_model, 384)
            return

        # Check if it's a github: model (needs download)
        if resolved_model.startswith("github:"):
            self.model_type = "embedded"
            self.model_name = resolved_model
            self.dimension = dimension or 384
            return

        # Check registry for known models
        if model in self._MODEL_REGISTRY:
            config = self._MODEL_REGISTRY[model]
            self.model_type = config["type"]
            self.model_name = model
            self.dimension = dimension or config.get("dimension", 384)
            return

        # Handle prefix patterns
        if model.startswith("vectrixdb/"):
            self.model_type = "embedded"
            self.model_name = model
            self.dimension = dimension or 384
        elif model.startswith("qdrant/"):
            self.model_type = "fastembed"
            self.model_name = model
            self.dimension = dimension or 384
        elif (
            model.startswith("sentence-transformers/")
            or model.startswith("BAAI/")
            or model.startswith("jina/")
        ):
            self.model_type = "sentence-transformers"
            self.model_name = model
            self.dimension = dimension or self._get_model_dimension(model)
        elif model.startswith("openai/"):
            if self.embed_fn is None:
                raise ValueError(
                    f"Model '{model}' requires embed_fn parameter.\n"
                    f"Example: Vectrix('docs', model='{model}', embed_fn=your_openai_function)"
                )
            self.model_type = "openai"
            self.model_name = model
            self.dimension = dimension or 1536
        elif model.startswith("cohere/"):
            if self.embed_fn is None:
                raise ValueError(
                    f"Model '{model}' requires embed_fn parameter.\n"
                    f"Example: Vectrix('docs', model='{model}', embed_fn=your_cohere_function)"
                )
            self.model_type = "cohere"
            self.model_name = model
            self.dimension = dimension or 1024
        elif model.startswith("voyage/"):
            if self.embed_fn is None:
                raise ValueError(
                    f"Model '{model}' requires embed_fn parameter.\n"
                    f"Example: Vectrix('docs', model='{model}', embed_fn=your_voyage_function)"
                )
            self.model_type = "voyage"
            self.model_name = model
            self.dimension = dimension or 1024
        else:
            # Assume sentence-transformers for unknown models
            self.model_type = "sentence-transformers"
            self.model_name = model
            self.dimension = dimension or self._get_model_dimension(model)

    def _get_dimension_from_registry(self, model: Optional[str]) -> Optional[int]:
        """Get dimension from model registry."""
        if model and model in self._MODEL_REGISTRY:
            return self._MODEL_REGISTRY[model].get("dimension")
        return None

    def _get_model_dimension(self, model_name: str) -> int:
        """Get dimension for known models."""
        dimensions = {
            "all-MiniLM-L6-v2": 384,
            "all-mpnet-base-v2": 768,
            "all-MiniLM-L12-v2": 384,
            "paraphrase-MiniLM-L6-v2": 384,
            "multi-qa-MiniLM-L6-cos-v1": 384,
            "msmarco-MiniLM-L6-cos-v5": 384,
        }
        return dimensions.get(model_name, 384)

    def _init_db(self):
        """Initialize the database and collection."""
        # Use storage backend if provided, otherwise use local SQLite
        if self.storage_backend is not None:
            self._db = self.storage_backend
            self._using_storage_backend = True
        else:
            from .core.database import VectrixDB

            self._db = VectrixDB(self.path, readonly=self.readonly)
            self._using_storage_backend = False

        # On the database rather than the collection, so a collection that
        # clear() makes again keeps writing to it.
        if getattr(self, "_chunk_store_given", None) is not None:
            self._db.use_chunk_store(self._chunk_store_given)
        # The same, and before the policy is set up below, which reads it.
        if getattr(self, "_collection_store_given", None) is not None:
            self._db.use_collection_store(self._collection_store_given)

        # Store schema config for internal use (what embeddings to generate)
        self._schema_config = self._get_schema_config()

        # Built on first use. tier="graph" documented itself as
        # "Ultimate + Knowledge Graph" but never touched GraphRAG: it configured
        # three models and then ran ultimate search, so graph mode silently
        # returned dense similarity scores and no graph existed.
        self._graph_pipeline: Any = None

        # Reasons graph search has already been reported as unavailable, so the
        # fallback below warns once each instead of on every query.
        self._graph_warnings = set()
        self._rerank_warnings = set()

        try:
            self._collection = self._db.get_collection(self.name)
        except KeyError:
            # get_collection raises KeyError when the collection does not exist
            # yet, which is the expected path here: create it below. Catching
            # only KeyError means a storage failure now propagates as a
            # StorageOperationError instead of being mistaken for "not found"
            # and silently triggering a create against a broken backend.
            # Create collection with mode tag so storage backend knows the schema
            # Tags are used by VectrixDB to infer mode for storage backends
            if self.readonly:
                raise ConfigurationError(
                    f"Collection {self.name!r} does not exist at {self.path!r}; "
                    "nothing to open read-only."
                )
            mode_tags = [self.default_mode.capitalize()]  # e.g., ["Ultimate"]
            self._collection = self._db.create_collection(
                name=self.name,
                dimension=self.dimension,
                metric="cosine",
                description=self.description,
                enable_text_index=True,
                tags=mode_tags,
                text_boosts=self.text_boosts,
                shard_size=self.shard_size,
                text_language=self.text_language,
            )

        self._setup_ingestion(self._embedding_cache_enabled)
        self._setup_policy(self._policy_requested)
        self._setup_named_vectors()

    # -------------------------------------------------------------------------
    # Ingestion: chunking, parents, near-duplicates, the embedding cache and
    # the embedding model the collection was built with.
    # -------------------------------------------------------------------------

    def _setup_policy(self, policy: Optional["Policy"]) -> None:
        """Load the collection's policy, or record the one it was given.

        A policy travels with the data it governs. Opening a policied
        collection without naming the policy still enforces it, because the
        alternative is that forgetting an argument turns the control off,
        which is the failure this whole feature exists to remove.

        The collection's own metadata decides, for every server alike: a
        collection's record, where a server keeps one, says who may search
        it and nothing about its documents.
        """
        self._setup_policy_from_meta(policy)

    def _setup_policy_from_meta(self, policy: Optional["Policy"]) -> None:
        """The policy from the collection's own metadata, or the one it was given, recorded there."""
        from .exceptions import PolicyMismatch
        from .policy import Policy

        stored_json = None
        try:
            stored_json = _coll(self).get_meta("entitlement_policy")
        except Exception:  # pragma: no cover - backends without the meta table
            self._policy = policy
            return

        stored = Policy.from_dict(json.loads(stored_json)) if stored_json else None

        if stored is not None and policy is not None:
            if stored.fingerprint != policy.fingerprint:
                raise PolicyMismatch(stored.fingerprint, policy.fingerprint)
        if stored is not None:
            self._policy = stored
            if policy is not None:
                # The stored policy carries the rules; whether this deployment
                # insists on engine-side filtering is the caller's to say, and
                # is deliberately not part of what a collection remembers.
                stored.require_pushdown = policy.require_pushdown
                # A policy stored before its role key was persisted lacks it;
                # record the one given, since the collection reads that row.
                if stored.db_role_key is None and policy.db_role_key is not None:
                    stored.db_role_key = policy.db_role_key
                    if not self.readonly:
                        try:
                            _coll(self).set_meta("entitlement_policy", json.dumps(stored.to_dict()))
                            _coll(self)._policy_loaded = False
                        except Exception:  # pragma: no cover - read-only media
                            pass
            self._require_pushdown_if_asked()
            _coll(self)._policy_enforced = True
            return

        self._policy = policy
        if policy is not None and not self.readonly:
            try:
                _coll(self).set_meta("entitlement_policy", json.dumps(policy.to_dict()))
                # The collection reads its policy from that row, and the
                # pushdown check below asks the collection, so it has to
                # be there first.
                _coll(self)._policy_loaded = False
            except Exception:  # pragma: no cover - read-only media
                pass
        if self._policy is not None:
            self._require_pushdown_if_asked()
            _coll(self)._policy_enforced = True

    @property
    def index_build_id(self) -> Optional[str]:
        """Which ingestion produced the index as it stands.

        The thread that ties an answer back to where it came from: source
        system, document version and chunking configuration all hang off the
        write that stamped this. A collection written before 2.2 has none,
        which reads as None rather than as a guess.
        """
        try:
            return _coll(self).get_meta("index_build_id")
        except Exception:  # pragma: no cover - backends without the meta table
            return None

    def _mint_build(self) -> Optional[str]:
        """A new build id, not yet recorded. Minted before a write so the
        chunks it stores can carry it; committed after, so a write that fails
        leaves the previous build in force."""
        if self.readonly:
            return None
        return "build_" + uuid.uuid4().hex[:16]

    def _commit_build(self, build: Optional[str]) -> Optional[str]:
        if build is None:
            return None
        try:
            _coll(self).set_meta("index_build_id", build)
        except Exception:  # pragma: no cover - read-only media
            return None
        return build

    def _stamp_build(self) -> Optional[str]:
        """Record that the index has changed, and return the new build id.

        Every mutation mints one, whether or not anything is auditing, so the
        field is populated the day somebody turns auditing on rather than
        starting from whenever they remembered to. It doubles as the
        ingestion id, which is what joins a decision record to the write that
        put the documents there.

        Every mutation, not every add: a delete, a clear, a re-embed and an
        index rebuild all change what a query can answer, and reproduction
        rests on "same build id, same index". A build id that survived a
        delete would let a reproduction call itself exact against an index
        that had lost documents.
        """
        return self._commit_build(self._mint_build())

    def _require_pushdown_if_asked(self) -> None:
        """Refuse now if the policy wanted the filter to run in the engine.

        Nothing pushes a policy filter down today, so this refuses whenever
        it is asked for. That is the honest answer and the reason the option
        exists: a deployment that needs engine-side enforcement should be
        told it does not have it, rather than assume it from the fact that a
        policy is attached.
        """
        from .core.types import FilterPushdown
        from .exceptions import PushdownUnavailable

        if self._policy is None or not self._policy.require_pushdown:
            return
        where, mode = _coll(self)._pushdown_source()
        if mode is not FilterPushdown.ENGINE:
            raise PushdownUnavailable(where)

    @property
    def pushdown_mode(self):
        """Where a filter runs for this collection: ``ENGINE`` or ``POST``."""
        return _coll(self).pushdown_mode

    @property
    def policy(self) -> Optional["Policy"]:
        """The entitlement policy this collection carries, if any."""
        return self._policy

    @property
    def _policy(self) -> Optional["Policy"]:
        """The policy in force here, which every decision this handle records names.

        The one set when the collection was opened.
        """
        return self.__dict__.get("_policy_held")

    @_policy.setter
    def _policy(self, value: Optional["Policy"]) -> None:
        self.__dict__["_policy_held"] = value

    @property
    def principal(self) -> Optional[Mapping[str, Any]]:
        """The principal this view reads as, if any."""
        return self._principal

    def as_principal(
        self,
        principal: Mapping[str, Any],
        audit: Optional["AuditContext"] = None,
    ) -> "Vectrix":
        """A view of this collection that reads as one principal.

        The principal is whatever the host resolved from its own directory,
        CRM or control room. Nothing here validates it and nothing here can:
        a library that believed a principal it was handed would be pretending
        to an authority it does not have.

        The view shares this collection's index and model, so it is cheap,
        and closing either closes both.

            >>> view = db.as_principal({"roles": ["credit_analyst"], ...})
            >>> view.search("covenant thresholds")

        `audit` carries what the library cannot work out for itself: who this
        principal is, which request it belongs to, and when their entitlements
        were actually resolved. That last one is where
        `principal_snapshot_age_ms` comes from, and it is the field an access
        decision gets challenged on months later.
        """
        if not isinstance(principal, Mapping):
            raise TypeError(f"a principal is a mapping, got {type(principal).__name__}")
        view = copy.copy(self)
        view._principal = dict(principal)
        view._audit_context = audit
        return view

    def _require_principal(self, op: str) -> Mapping[str, Any]:
        """The principal for a gated read, or refuse."""
        from .exceptions import PrincipalRequired

        if self._principal is None:
            raise PrincipalRequired(self.name)
        return self._principal

    def _refuse_under_policy(self, op: str, why: str, host_allowed: bool = True) -> None:
        """Refuse a read this policy cannot decide.

        Read paths fall into three tiers, and they are not the same problem.

        * **Content a policy can decide** (``search``, ``get``, ``similar``)
          refuses without a principal. That is the headline property: these
          are what a request handler calls, and a forgotten ``as_principal``
          there is exactly the leak this feature exists to prevent.
        * **Content a policy cannot decide** (``recall``, ``context``) refuses
          whenever a policy exists at all, host or not, because there is no
          principal that would make it safe.
        * **Administrative** (``count``, ``export``, the graph traversals) is
          the host's business. The base object is the host's handle by
          construction: ``as_principal`` returns a copy and nothing leads back
          to the original, so a principal only ever holds a view.
        """
        from .exceptions import PolicyError

        if self._policy is None:
            return
        if host_allowed and self._principal is None:
            return
        raise PolicyError(
            f"{op} is not available to a principal on a collection with an entitlement "
            f"policy: {why} Reach for the collection through its host rather than "
            f"through a principal view."
        )

    def _pad_to_floor(self, elapsed_ms: float) -> float:
        """Sleep until this call has taken a whole number of timing floors.

        Rounds up to the next multiple rather than padding to a single
        deadline, so a query that overruns the floor does not report its true
        length: the channel is quantised rather than merely delayed, and what
        leaks is which multiple rather than the duration.

        Returns the milliseconds to report, which is the padded figure. The
        audit record keeps the real one, because the host paying for the
        latency is entitled to know what it bought.
        """
        # Kept for the decision record, which reports what the work cost
        # rather than what the caller was allowed to see.
        self._last_unpadded_ms = elapsed_ms
        floor_ms = self._timing_floor * 1000.0 if self._timing_floor else None
        if floor_ms is None:
            return elapsed_ms
        quanta = math.floor(elapsed_ms / floor_ms) + 1
        target_ms = quanta * floor_ms
        remaining = (target_ms - elapsed_ms) / 1000.0
        if remaining > 0:
            time.sleep(remaining)
        return target_ms

    def _policy_filter(self, caller_filter: Optional[Dict[str, Any]], op: str) -> Any:
        """The filter a policied read runs under.

        The policy itself is applied by `Collection`, which is also the
        layer the REST server and everything else built on `VectrixDB` goes
        through. Compiling it here as well would work, and would mean a gap
        in either layer stayed invisible because the other covered it.

        The principal check stays here because this is where the useful error
        lives: this is the API a request handler calls, and a forgotten
        `as_principal` should say so in those terms rather than from two
        layers down.
        """
        from .exceptions import PrincipalRequired

        if self._policy is None:
            return caller_filter
        if self._principal is None:
            raise PrincipalRequired(self.name)
        if self._policy.compile(self._principal) is None:
            # The principal matches nothing at all, so there is no search to
            # run. Collection would reach the same answer; short-circuiting
            # here saves the round trip and leaks nothing about documents.
            return _DENY_EVERYTHING
        return caller_filter

    def _audited_search(self, query: str, limit: int, caller_filter: Any, kwargs: dict) -> Results:
        """Search, evaluate the policy in Python, and record the decision.

        The counts are why this exists. `withheld_disclosable` and
        `withheld_undisclosable` need the documents the policy rejected, and
        a filter applied by the engine throws them away, so this fetches a
        wider window with the caller's filter alone and decides here.
        """
        import time as _time

        from .audit import AuditContext, RetrievalRecord

        assert self._policy is not None and self.on_retrieval is not None
        context = self._audit_context or AuditContext()
        started = _time.perf_counter()
        decided_at = _utcnow()
        decision_id = RetrievalRecord.new_id()

        # A throwaway copy that keeps the policy and the principal, so the
        # search below is the real, enforced one. It differs only in having
        # no sink, which stops this recursing, and in carrying a dict for
        # Collection's counts to land in.
        inner = copy.copy(self)
        inner.on_retrieval = None
        inner._policy_counts = {}
        results = inner.search(query, limit=limit, filter=caller_filter, **kwargs)
        results.decision_id = decision_id

        counts = inner._policy_counts
        examined = counts.get("examined")
        disclosable = counts.get("disclosable")
        undisclosable = counts.get("undisclosable")
        kept = list(results)

        age_ms = None
        if context.snapshot_taken_at is not None:
            age_ms = (decided_at - context.snapshot_taken_at).total_seconds() * 1000.0

        record = RetrievalRecord(
            decision_id=decision_id,
            decided_at=decided_at,
            collection=self.name,
            backend=getattr(self._collection, "_backend", None),
            pushdown_mode=self.pushdown_mode.value,
            # Read per search rather than cached: a view made before a write
            # would otherwise name the build that preceded it, and the whole
            # point of the field is to say which index actually answered.
            index_build_id=self.index_build_id,
            trace_id=context.trace_id,
            principal_id=context.principal_id,
            principal_type=context.principal_type,
            principal_snapshot=dict(self._principal or {}),
            principal_snapshot_taken_at=context.snapshot_taken_at,
            principal_snapshot_age_ms=age_ms,
            policy_fingerprint=self._policy.fingerprint,
            policy_version=self._policy.version,
            rules_evaluated=[r.describe() for r in self._policy.rules],
            # Which rules rejected what is Collection's to report next;
            # it decides per candidate and this layer no longer sees them.
            rules_denied=[],
            query_fingerprint=self.on_retrieval.fingerprint_query(query),
            candidates_examined=examined,
            # Collection decides every candidate the index offered it, so
            # these are exact rather than bounded by an audit window.
            candidates_exhausted=examined is not None,
            results_returned=len(kept),
            result_ids=[r.id for r in kept],
            withheld_disclosable=disclosable,
            withheld_undisclosable=undisclosable,
            # The measured span includes any sleep the timing floor added.
            # The record keeps the true cost and the padded figure apart,
            # because the gap between them is the channel that was closed.
            duration_ms=(
                getattr(inner, "_last_unpadded_ms", None)
                if self._timing_floor
                else (_time.perf_counter() - started) * 1000.0
            ),
            padded_to_ms=results.time_ms if self._timing_floor else None,
            outcome=self._policy.outcome(len(kept), disclosable or 0, undisclosable or 0).value,
        )
        self.on_retrieval.write(record)
        return results

    def _check_metadata_contract(self, metadata: List[Dict], ids: List[str]) -> None:
        """Refuse a document the policy could never show anybody.

        The two failures land in different places, which is the argument for
        checking here. Written, the chunk is invisible to every principal,
        because an absent field denies, and the symptom reads as a
        permissions problem: somebody goes looking in the entitlement
        resolver. Refused, it is a pipeline bug with a stack trace pointing at
        the pipeline.
        """
        from .exceptions import MetadataContractError, MetadataContractWarning

        policy = self._policy
        if policy is None or policy.on_incomplete_document == "allow":
            return

        for index, meta in enumerate(metadata):
            missing = policy.missing_fields(meta or {})
            undecidable = policy.undecidable_fields(meta or {})
            if not missing and not undecidable:
                continue
            doc_id = ids[index] if index < len(ids) else None
            if policy.on_incomplete_document == "warn":
                faults = []
                if missing:
                    faults.append(f"is missing {missing}")
                for path, value, accepts in undecidable:
                    faults.append(f"stamps {path}={value!r}, where the rule needs {accepts}")
                warnings.warn(
                    f"document {index} ({doc_id}) " + ", and ".join(faults) + ", which this "
                    f"collection's entitlement policy decides by, so no principal will "
                    f"see it. Written anyway because on_incomplete_document is warn.",
                    MetadataContractWarning,
                    stacklevel=3,
                )
                continue
            raise MetadataContractError(index, missing, doc_id, undecidable)

    def _record_ingestion(self, build: Optional[str], started: float) -> None:
        """Write an ingestion record, if anything is listening.

        A different audit question from a read: not whether somebody should
        have seen this, but whether these documents are supposed to be here
        and where they came from. It runs as a service rather than a
        principal, which is why the record says so.
        """
        import time as _time

        from .audit import AuditContext, IngestionRecord

        if self.on_retrieval is None or build is None:
            return
        context = self._audit_context or AuditContext()
        report = self.last_add_report
        provenance = getattr(self, "_ingestion_provenance", None) or {}
        self.on_retrieval.write_ingestion(
            IngestionRecord(
                ingestion_id=build,
                started_at=_utcnow(),
                collection=self.name,
                backend=getattr(self._collection, "_backend", None),
                trace_id=context.trace_id,
                principal_id=context.principal_id,
                principal_type="service",
                documents_written=len(report.added) if report else 0,
                documents_skipped=len(report.skipped) if report else 0,
                embedding_model=self.model_name,
                policy_fingerprint=self._policy.fingerprint if self._policy else None,
                source=provenance.get("source"),
                document_id=provenance.get("document_id"),
                document_version=provenance.get("document_version"),
                chunking=provenance.get("chunking"),
                ids_written=list(report.added) if report else [],
                duration_ms=(_time.perf_counter() - started) * 1000.0,
            )
        )

    def _record_refusal(self, query: str, outcome: str) -> None:
        """Record a decision that never reached the index.

        A principal who matches nothing is still a decision, and one worth
        having in the log: it is what a revoked user looks like.
        """
        import time as _time

        from .audit import AuditContext, RetrievalRecord

        if self.on_retrieval is None or self._policy is None:
            return
        context = self._audit_context or AuditContext()
        decided_at = _utcnow()
        age_ms = None
        if context.snapshot_taken_at is not None:
            age_ms = (decided_at - context.snapshot_taken_at).total_seconds() * 1000.0
        _time.perf_counter()
        self.on_retrieval.write(
            RetrievalRecord(
                decision_id=RetrievalRecord.new_id(),
                decided_at=decided_at,
                collection=self.name,
                index_build_id=self.index_build_id,
                trace_id=context.trace_id,
                principal_id=context.principal_id,
                principal_type=context.principal_type,
                principal_snapshot=dict(self._principal or {}),
                principal_snapshot_taken_at=context.snapshot_taken_at,
                principal_snapshot_age_ms=age_ms,
                policy_fingerprint=self._policy.fingerprint,
                policy_version=self._policy.version,
                rules_evaluated=[r.describe() for r in self._policy.rules],
                query_fingerprint=self.on_retrieval.fingerprint_query(query),
                candidates_examined=0,
                results_returned=0,
                withheld_disclosable=0,
                withheld_undisclosable=0,
                duration_ms=0.0,
                outcome=outcome,
            )
        )

    def _setup_ingestion(self, embedding_cache: bool) -> None:
        from .ingest import EmbeddingCache, ParentStore

        base = Path(self.path) if self.path else None
        where = getattr(self, "_parent_store", None)
        if not where:
            where = base / f"{self.name}.parents.db" if base else None
        self._parents = ParentStore(where)
        from .documents import open_chunks, open_store

        self._documents = open_store(
            getattr(self, "_keep_source", None), base / f"{self.name}.documents" if base else None
        )
        self._chunk_files = open_chunks(
            getattr(self, "_keep_chunks", None), base / f"{self.name}.chunks" if base else None
        )
        if getattr(self, "_markdown_first", False) and self._documents is None:
            raise ConfigurationError(
                f"Collection {self.name!r} was asked to keep each document's Markdown first, and keeps none: "
                "open it with keep_source as well (True, a folder, or a DocumentStore)."
            )
        self._dedupe_index: Any = None  # built on first dedupe= request
        self._embed_cache = (
            EmbeddingCache(base / "_embed_cache.db")
            if base and embedding_cache and not self.readonly and self.model_type != "custom"
            else None
        )
        self.last_add_report: Optional[AddReport] = None

        # The model a collection was built with is part of the collection.
        # Opening it with another model makes every score meaningless, so say
        # so once, loudly, and point at the fix.
        recorded = None
        try:
            recorded = _coll(self).get_meta("embedding_model")
        except Exception:  # pragma: no cover - backends without the meta table
            return
        if recorded is None:
            # A collection with vectors and no recorded model predates 2.2,
            # when the English default was e5-small-v2. Open it with that
            # model, not the new default, so its vectors still mean
            # something; reembed() moves it to the new default.
            try:
                has_vectors = self._count_all() > 0
            except Exception:  # pragma: no cover
                has_vectors = False
            if has_vectors and not self._model_explicit and self.model_type == "embedded":
                self.model_name = self._legacy_model
                self._model = None
            if not self.readonly:
                try:
                    _coll(self).set_meta("embedding_model", self.model_name)
                except Exception:  # pragma: no cover - read-only media
                    pass
        elif not _same_embedded_model(recorded, self.model_name):
            warnings.warn(
                f"Collection {self.name!r} was built with embedding model {recorded!r} "
                f"but is being opened with {self.model_name!r}. Search scores will be "
                "wrong until you call reembed(), or open it with the original model.",
                ModelMismatchWarning,
                stacklevel=3,
            )

    @property
    def embedding_model(self) -> Optional[str]:
        """The embedding model this collection was built with, as recorded."""
        try:
            return _coll(self).get_meta("embedding_model")
        except Exception:  # pragma: no cover
            return None

    @property
    def dedupe_index(self):
        """The MinHash index over stored texts, built on first use."""
        if self._dedupe_index is None:
            from .ingest import NearDuplicateIndex

            base = Path(self.path) if self.path else None
            self._dedupe_index = NearDuplicateIndex(
                base / f"{self.name}.minhash.json" if base else None
            )
            if len(self._dedupe_index) == 0 and self._count_all():
                # First dedupe on an existing collection: index what is there.
                for doc_id, text, _ in _coll(self)._iter_documents_raw():
                    if text:
                        self._dedupe_index.add(doc_id, text)
                self._dedupe_index.save()
        return self._dedupe_index

    def _embed_cached(self, texts: List[str], progress: Optional[bool]) -> np.ndarray:
        """Embed through the content cache when there is one."""
        cache = getattr(self, "_embed_cache", None)
        if cache is None or not texts:
            return self._embed_batched(texts, progress)
        cached = cache.get_many(self.model_name, texts)
        missing = [i for i, v in enumerate(cached) if v is None]
        if not missing:
            return np.vstack(cached).astype(np.float32)
        fresh = self._embed_batched([texts[i] for i in missing], progress)
        if not isinstance(fresh, np.ndarray) or fresh.ndim != 2:
            return self._embed_batched(texts, progress)
        cache.put_many(self.model_name, [texts[i] for i in missing], fresh)
        out = np.empty((len(texts), fresh.shape[1]), dtype=np.float32)
        for row, vector in enumerate(cached):
            if vector is not None:
                out[row] = vector
        for j, i in enumerate(missing):
            out[i] = fresh[j]
        return out

    @traced(
        "add_document",
        before=lambda a: {
            "collection": a["self"].name,
            "kind": a["kind"],
            "chunking": a["chunk"],
        },
        after=lambda added: {"chunks": added},
    )
    def add_document(
        self,
        source: Union[str, Path, "LoadedDocument"],
        *,
        chunk: str = "recursive",
        chunk_size: int = 1000,
        overlap: int = 200,
        parent_size: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        doc_id: Optional[str] = None,
        dedupe: Optional[float] = None,
        kind: Optional[str] = None,
        threshold: float = 0.1,
        progress: Optional[bool] = None,
        on_low_quality: str = "warn",
        quality_threshold: Optional[float] = None,
        embed_heading: bool = False,
        source_version: Optional[str] = None,
        cut_with: Optional[Callable[[str, List[Tuple[int, int]]], Sequence[int]]] = None,
        context_with: Optional[Callable[[str, str, int, int], Optional[str]]] = None,
        late: bool = False,
        index: bool = True,
    ) -> int:
        """Load, chunk and add one document. Returns the number of chunks added.

        ``source`` is a file path (PDF, DOCX, PPTX, XLSX, CSV, HTML, Markdown
        or text, or any suffix this collection has an extractor for), a
        string of Markdown, or a :class:`vectrixdb.ingest.LoadedDocument`. Every
        chunk carries ``_vx_doc`` (the document id), ``_vx_chunk`` (its
        position), its character offsets, and ``page`` and ``heading`` when
        the source has them, on top of ``metadata``.

        ``chunk`` is the strategy: recursive, sentence, semantic, markdown,
        fixed or llm (see :func:`vectrixdb.ingest.chunk`). ``llm`` needs
        ``cut_with``, which :func:`vectrixdb.chunk_models.llm_cutter` makes
        from a chat model. ``context_with``, from
        :func:`vectrixdb.chunk_models.context_writer`, has a model write a
        note for every chunk placing it in its document, which goes in front
        of the chunk for the embedder. ``late=True`` embeds late: each chunk
        is the mean of its own tokens read in the whole document, with a
        model that pools the mean of its tokens. ``parent_size`` turns on
        parent-child retrieval: chunks are grouped into sections of about
        that many characters (or by heading when there are headings), the
        sections are stored beside the collection, and ``search(parents=True)``
        returns the section a matching chunk belongs to. ``dedupe`` skips
        chunks whose MinHash similarity to a stored text is at or above that
        value; see ``last_add_report`` for what was skipped.

        Every chunk is scored for extraction quality and carries the score as
        ``_vx_quality``. A document whose mean score falls below
        ``quality_threshold`` (the measured default in ``vectrixdb.quality``)
        reads as a failed extraction: ``on_low_quality`` is ``"warn"`` to
        write it and say so, ``"reject"`` to refuse it with
        :class:`ExtractionQualityError`, ``"allow"`` for silence. Warn by
        default rather than reject, because the detector is measured on
        English prose and a deployment ingesting something else should see
        the scores before it lets them refuse anything.

        ``index=False`` keeps the document's Markdown, with its metadata, and
        stops: nothing is cut, embedded or written to the index, and 0 is
        returned. It needs ``keep_source``. The document waits in the store
        until :meth:`rechunk` cuts it, which is how a deployment reads files
        before it has decided how to cut them.

        ``embed_heading`` puts the headings a chunk sits under in front of
        the text handed to the embedder, ``Terms > Late fees: Payment is due
        within 30 days``, and nowhere else: the stored text, the keyword
        index and what a result shows are the chunk as written. A section
        that never names its own subject is found by it that way. The
        prefix is kept on the chunk as ``_vx_embed_prefix``, so
        ``reembed()`` embeds the same thing again.

        A Markdown file's front matter becomes the document's metadata, and so
        every chunk's; ``metadata=`` wins where both name a key, and a
        ``doc_id`` there is the document's id when none is passed. With
        ``keep_source`` the Markdown as chunked is kept under that id, with
        ``source_version``, whatever the original's store calls its version,
        an ETag or a hash of its bytes, recorded beside it.
        """
        from .ingest import LoadedDocument, load, markdown_document, prepare_document
        from .quality import DEFAULT_THRESHOLD

        if on_low_quality not in ("reject", "warn", "allow"):
            raise ValueError(
                f"on_low_quality is 'reject', 'warn' or 'allow', got {on_low_quality!r}"
            )
        cutoff = DEFAULT_THRESHOLD if quality_threshold is None else quality_threshold

        if isinstance(source, LoadedDocument):
            doc = source
        elif isinstance(source, Path) or (
            isinstance(source, str)
            and len(source) < 4096
            and "\n" not in source
            # os.path.isfile, not Path.is_file: a long line of text is not a
            # file name, and on Linux Path.is_file raises ENAMETOOLONG for it.
            and os.path.isfile(source)
        ):
            doc = load(
                source,
                kind=kind,
                extractors=self._extractors,
                images=self.wants_images(),
                ocr=self.page_ocr,
            )
        else:
            # A string gets what a markdown file gets: its images become
            # figure lines and its tables become rows.
            doc = markdown_document(str(source))
        if doc.figures and doc.images:
            from .ingest import describe_figures as _describe

            # Decorative images go, and the rest are described, before the
            # text is cut: a description is part of what gets chunked.
            doc = _describe(
                doc, self._describe_figures, name=str(doc.metadata.get("filename") or "")
            )
        if not doc.text.strip():
            self.last_add_report = AddReport(added=[], skipped=[])
            return 0

        front_id = doc.metadata.get("doc_id")
        doc_id = (
            doc_id
            or (front_id if isinstance(front_id, str) and front_id else None)
            or self._generate_id(doc.text)
        )
        asked = {
            "chunk": chunk,
            "chunk_size": chunk_size,
            "overlap": overlap,
            "parent_size": parent_size,
            "embed_heading": embed_heading,
        }
        # A model's note and late embedding change the chunks as the index
        # holds them, so they are recorded too, or a document cut with a note
        # reads the same as one cut without.
        if context_with is not None:
            asked["context"] = True
        if late:
            asked["late"] = True
        rechunking = bool(getattr(self, "_rechunking", False))
        if not index:
            if self._documents is None:
                raise ConfigurationError(
                    f"Collection {self.name!r} was asked to keep a document without indexing it, and keeps no "
                    "documents: open it with keep_source (True, a folder, or a DocumentStore)."
                )
            import hashlib

            # Kept, and nothing more: no chunking is recorded, which is how
            # a document still waiting to be cut is told from one that was.
            kept = self._documents.entry(doc_id)
            if (
                kept is None
                or kept.get("version") != hashlib.sha256(doc.text.encode()).hexdigest()[:16]
                or kept.get("source_version") != source_version
                or kept.get("chunking")
            ):
                self._documents.put(
                    doc_id, doc, source_version=source_version, user_metadata=dict(metadata or {})
                )
            self.last_add_report = AddReport(added=[], skipped=[])
            return 0
        if self._markdown_first and self._documents is not None and not rechunking:
            # Reading ends here: the Markdown is kept before anything is cut,
            # and what is cut is what was kept, read back. A document already
            # kept as it is, a second try after a later step failed, is not
            # written again, so it still says when it was really read.
            import hashlib

            kept = self._documents.entry(doc_id)
            if (
                kept is None
                or kept.get("version") != hashlib.sha256(doc.text.encode()).hexdigest()[:16]
                or kept.get("source_version") != source_version
            ):
                self._documents.put(
                    doc_id,
                    doc,
                    source_version=source_version,
                    chunking=asked,
                    user_metadata=dict(metadata or {}),
                )
            doc = self._documents.get(doc_id, images=True)
        prepared = prepare_document(
            doc,
            doc_id,
            chunk=chunk,
            chunk_size=chunk_size,
            overlap=overlap,
            parent_size=parent_size,
            metadata=metadata,
            embed_heading=embed_heading,
            threshold=threshold,
            embed=self._embed if chunk == "semantic" else None,
            quality_threshold=cutoff,
            cut_with=cut_with,
            context_with=context_with,
        )
        if prepared is None:
            self.last_add_report = AddReport(added=[], skipped=[])
            return 0
        vectors = None
        if late:
            from .chunk_models import late_ready, late_vectors

            model = self.embed_fn if self.model_type == "custom" else getattr(self, "model", None)
            why = late_ready(model)
            if why:
                raise ConfigurationError(why)
            vectors = late_vectors(
                model,
                prepared.doc.text,
                [(m["_vx_start"], m["_vx_end"]) for m in prepared.metadata],
            )
            for m in prepared.metadata:
                # Read in its document, not alone: reembed() would embed it alone.
                m["_vx_late"] = True
        # Extraction quality, per chunk on the chunk and per document at
        # the gate, because one garbled page in a clean report is worth
        # knowing about and not worth refusing the report for.
        if prepared.quality < cutoff and on_low_quality != "allow":
            from .exceptions import ExtractionQualityError, ExtractionQualityWarning

            if on_low_quality == "reject":
                raise ExtractionQualityError(doc_id, prepared.quality, cutoff)
            warnings.warn(
                f"document {doc_id} scores {prepared.quality:.2f} on extraction quality, "
                f"below {cutoff:.2f}: its text reads as a failed extraction. Written "
                f"anyway because on_low_quality is warn.",
                ExtractionQualityWarning,
                stacklevel=3,  # past the tracing wrapper, to the caller
            )
        for parent_id, parent_text, parent_meta in prepared.parents:
            self._parents.put(parent_id, parent_text, parent_meta)
        texts, metas, ids, version = (
            prepared.texts,
            prepared.metadata,
            prepared.ids,
            prepared.version,
        )
        if self._chunk_files is not None:
            # The chunks as cut, a line each, before anything embeds them.
            self._chunk_files.put(doc_id, ids, texts, metas)
        base_source = metas[0].get("source") if metas else None
        self._ingestion_provenance = {
            "source": base_source,
            "document_id": doc_id,
            "document_version": version,
            "chunking": prepared.chunking,
        }
        try:
            self.add(
                texts, metadata=metas, ids=ids, dedupe=dedupe, progress=progress, vectors=vectors
            )
        finally:
            self._ingestion_provenance = None
        self._embed_figure_images(doc, texts, metas, ids)
        if self._documents is not None and not rechunking and not self._markdown_first:
            # Kept after the write, so the store never holds a document the
            # index refused: a contract failure or a rejected extraction
            # raises before this line. With markdown_first it was kept first,
            # on purpose, and a later failure leaves it for the next try.
            self._documents.put(
                doc_id,
                doc,
                source_version=source_version,
                chunking=asked,
                user_metadata=dict(metadata or {}),
            )
        return len(self.last_add_report.added) if self.last_add_report else len(ids)

    @property
    def parents_are_at(self) -> str:
        """Where this collection's parent sections are kept, for a log line."""
        return self._parents.where

    @property
    def page_ocr(self) -> Any:
        """What reads a page of a PDF that has no text layer, or None.

        A property rather than the private attribute, because a worker on
        the other side of a queue reads it off the collection to read a blob
        the same way ``add_document`` reads a file. The two used to differ
        and the difference was invisible: the same scanned report indexed
        well from a folder and emptily from a bucket.
        """
        return self._page_ocr

    def wants_images(self) -> bool:
        """Whether reading a document should keep its pictures.

        True when something here does anything with one: a describer to write
        what it shows, or an image embedder to index what it looks like.
        Asked by :meth:`add_document` and by the ingestion worker, so a
        document read from a file and the same document read from a bucket
        come back with the same figures in them.
        """
        return self._describe_figures is not None or self._image_embedder is not None

    def _embed_figure_images(
        self, doc: Any, texts: Sequence[str], metas: Sequence[Dict[str, Any]], ids: Sequence[str]
    ) -> None:
        """Embed each figure's picture into the image index, under the figure chunk's own id.

        Only figures whose image is at hand: a figure line in Markdown that
        points at a file nobody has is still found by its caption and its
        description, as every figure was before. A chunk the write skipped as
        a duplicate is skipped here too, so the image index never names a
        chunk the collection does not hold.
        """
        side = self._named.get(IMAGE_VECTOR)
        if side is None or self._image_embedder is None:
            return
        written = set(self.last_add_report.added) if self.last_add_report is not None else set(ids)
        images = getattr(doc, "images", None) or {}
        found = [
            (chunk_id, text, meta, images[str(meta.get("figure_src"))])
            for chunk_id, text, meta in zip(ids, texts, metas)
            if meta.get("figure_src")
            and str(meta.get("figure_src")) in images
            and chunk_id in written
        ]
        # A chunk id written again as something else, a paragraph where a figure
        # was, must not keep the old figure's picture under it.
        pictured = {chunk_id for chunk_id, _, _, _ in found}
        no_longer = [chunk_id for chunk_id in ids if chunk_id not in pictured]
        if no_longer and side.count():
            side.delete(no_longer)
        if not found:
            return
        try:
            vectors = np.asarray(
                self._image_embedder.embed_images([data for _, _, _, data in found]),
                dtype=np.float32,
            )
        except Exception as exc:
            from .exceptions import ExtractionError

            raise ExtractionError(
                f"embedding the figures of {doc.metadata.get('filename') or 'a document'} failed: {exc}"
            ) from exc
        if vectors.shape != (len(found), self._image_embedder.dimension):
            raise ConfigurationError(
                f"image_embedder.embed_images returned {vectors.shape} for {len(found)} images; "
                f"it should be ({len(found)}, {self._image_embedder.dimension})"
            )
        side.add(
            [text for _, text, _, _ in found],
            metadata=[dict(meta) for _, _, meta, _ in found],
            ids=[chunk_id for chunk_id, _, _, _ in found],
            vectors=vectors,
            progress=False,
        )

    # -- named dense vectors, locally ---------------------------------------

    def _setup_named_vectors(self) -> None:
        """Open one sidecar collection per extra dense model.

        The sidecar is a plain dense collection holding the same ids, texts
        and metadata, embedded by the other model. Everything a collection
        already does, persistence, deletes, rebuilds, works on it unchanged,
        which is the reason it is a collection and not a second index bolted
        into this one. What is recorded in this collection's own metadata is
        the list of labels, so reopening without the list still finds them.
        """
        recorded = None
        try:
            raw = _coll(self).get_meta("named_dense_vectors")
            recorded = json.loads(raw) if raw else None
        except Exception:
            recorded = None
        specs = list(self._named_specs)
        if not specs and not recorded:
            return
        if self._policy is not None:
            raise ConfigurationError(
                f"Collection {self.name!r} carries an entitlement policy, and named dense vectors are not "
                "offered under one here: results fused from two indexes would not be the results the decision "
                "record names. On Azure AI Search, embeddings='both' fuses inside the service, in one request "
                "under one filter, and that is the supported way to have two vectors under a policy."
            )
        if self._using_storage_backend:
            raise ConfigurationError(
                "A list of dense models builds local indexes. On a storage backend the second vector is the "
                "backend's to hold: VectrixDB.with_azure_search(embeddings='both')."
            )
        if not specs:
            raise ConfigurationError(
                f"Collection {self.name!r} was built with named dense vectors {recorded}; open it with the same "
                "dense_model list, so writes reach every index."
            )
        labels: List[str] = []
        for number, spec in enumerate(specs, start=1):
            options: Dict[str, Any]
            if isinstance(spec, str):
                label, options = spec, {"dense_model": spec}
            elif isinstance(spec, (list, tuple)) and len(spec) == 3 and callable(spec[1]):
                label, options = str(spec[0]), {"embed_fn": spec[1], "dimension": int(spec[2])}
            else:
                raise ConfigurationError(
                    "an extra dense model is a model name, or (label, embed_fn, dimension), got "
                    + repr(spec)
                )
            if label in labels or label in ("both", "all", "own"):
                raise ConfigurationError(f"{label!r} cannot name a dense vector here: it is taken")
            labels.append(label)
            # A folder of its own, beside the collection: a second database
            # handle on the collection's own folder would hold its files open,
            # and the sidecars would turn up in the list of collections.
            self._named[label] = Vectrix(
                f"v{number}",
                path=str(Path(self.path) / f"{self.name}.vectors"),
                mode="dense",
                text_language=self.text_language,
                readonly=self.readonly,
                embedding_cache=self._embedding_cache_enabled,
                **options,
            )
        if recorded and list(recorded) != labels:
            for side in self._named.values():
                side.close()
            raise ConfigurationError(
                f"Collection {self.name!r} was built with named dense vectors {list(recorded)} and was opened "
                f"with {labels}. The vectors already written were made by the first list."
            )
        if not recorded and not self.readonly:
            _coll(self).set_meta("named_dense_vectors", json.dumps(labels))

    def vector_names(self) -> List[str]:
        """The dense vectors a search here can be answered from: ``own`` for
        the collection's model, then each named one. On a storage backend,
        the names the backend holds."""
        store = self._vector_store()
        if store is not None:
            return list(store.vector_names())
        return ["own", *self._named] if self._named else []

    def _search_named(self, vectors: Optional[str], call: Dict[str, Any]) -> "Results":
        """Search every chosen index and fuse the lists by rank."""
        import time

        started = time.time()
        names = self.vector_names()
        if vectors in (None, "both"):
            # Every model that read the words. Pictures are asked for by name:
            # fused in by rank, the best-looking figure would sit near the top
            # of every search, including the ones that are not about a figure.
            chosen = [n for n in names if n != IMAGE_VECTOR]
        elif vectors == "all":
            chosen = names
        elif vectors in names:
            chosen = [vectors]
        else:
            raise ConfigurationError(
                f"vectors={vectors!r} is not one of this collection's dense vectors: {', '.join(names)}"
            )
        if chosen == ["own"]:
            # One list is nothing to fuse: the search as it would have been.
            return self.search(**call)
        limit = call["limit"]
        wide = max(limit * 3, 30)
        runs: Dict[str, List[Result]] = {}
        primary: Optional[Results] = None
        if "own" in chosen:
            primary = self.search(
                **{**call, "limit": wide, "token_budget": None, "score_gap": None, "parents": False}
            )
            runs["own"] = list(primary.items)
        for label in chosen:
            if label == "own":
                continue
            side = self._named[label].search(
                call["query"], limit=wide, mode="dense", filter=call.get("filter")
            )
            runs[label] = list(side.items)
        fused: Dict[str, float] = {}
        ranks: Dict[str, Dict[str, int]] = {}
        for label, items in runs.items():
            for rank, item in enumerate(items, start=1):
                fused[item.id] = fused.get(item.id, 0.0) + 1.0 / (60 + rank)
                ranks.setdefault(item.id, {})[label] = rank
        order = sorted(fused, key=lambda i: (-fused[i], i))[:limit]
        known = {r.id: r for r in runs.get("own", [])}
        missing = [i for i in order if i not in known]
        for found in self.get(missing) if missing else []:
            known[found.id] = found
        items = []
        for doc_id in order:
            base = known.get(doc_id)
            if base is None:
                continue
            explain = dict(base.explain or {}) if call.get("explain") else None
            if explain is not None:
                explain["vector_ranks"] = ranks[doc_id]
            items.append(
                Result(
                    id=doc_id,
                    text=base.text,
                    score=round(fused[doc_id], 6),
                    metadata=base.metadata,
                    explain=explain,
                )
            )
        if call.get("parents"):
            items = self._to_parents(items)
        items = apply_score_gap(items, call.get("score_gap"), lambda r: r.score)
        items, cut, tokens = fit_to_budget(
            items, call.get("token_budget"), lambda r: r.text, self.token_counter
        )
        return Results(
            items=items,
            query=call["query"],
            mode=primary.mode if primary is not None else "dense",
            time_ms=(time.time() - started) * 1000.0,
            truncated=cut > 0,
            cut_count=cut,
            token_estimate=tokens,
            token_budget=call.get("token_budget"),
            degraded=primary.degraded if primary is not None else None,
        )

    # -- two homes ------------------------------------------------------------

    def _home(self) -> Optional[str]:
        """Which home this thread's search was told to ask, or None for the usual routing."""
        return getattr(self._home_scope, "home", None)

    def _home_kwargs(self) -> Dict[str, Any]:
        home = self._home()
        if home == "store":
            return {"use_backend": True, "strict_backend": True}
        if home == "local":
            return {"use_backend": False}
        return {}

    def _search_homes(self, homes: str, call: Dict[str, Any]) -> "Results":
        """Ask the store, the local index, or both and fuse them by rank."""
        import dataclasses
        import time

        if homes not in ("store", "local", "both"):
            raise ConfigurationError(f"homes is 'store', 'local' or 'both', got {homes!r}")
        storage = self._backend_storage()
        if storage is None:
            raise ConfigurationError(
                "homes= chooses between a store and the index beside this process, and this collection is not kept "
                "on a store. Open it with storage_backend=VectrixDB.with_azure_search(...) or with_opensearch(...)."
            )
        if self._policy is not None:
            raise ConfigurationError(
                f"Collection {self.name!r} carries an entitlement policy, and homes= is not offered under one: "
                "a decision record names one engine and one set of counts, and a list fused from two would have neither. "
                "Under a policy the store stays the engine, and a search it cannot answer raises."
            )
        mode = (call.get("mode") or self.default_mode or "dense").lower()
        if mode in ("ultimate", "graph"):
            raise ConfigurationError(
                f"homes= is for dense, keyword and hybrid searches. mode={mode!r} reads late-interaction vectors or "
                "a graph, which live in the local index only, so there is one home to ask."
            )
        if homes != "store" and call.get("vectors") not in (None, "vectrixdb", "own"):
            raise ConfigurationError(
                f"vectors={call.get('vectors')!r} is held by the store alone. The local index has the collection's "
                "own vectors and no others, so homes='local' and homes='both' cannot ask it for them."
            )

        def ask(home: str, **changes: Any) -> "Results":
            self._home_scope.home = home
            try:
                return self.search(**{**call, **changes})
            finally:
                self._home_scope.home = None

        if homes != "both":
            return ask(homes)

        started = time.time()
        limit = call["limit"]
        wide = {
            "limit": max(limit * 3, 30),
            "token_budget": None,
            "score_gap": None,
            "parents": False,
        }
        try:
            from_store = ask("store", **wide)
        except (ValueError, TypeError, ConfigurationError):
            # The caller's mistake, a mode this handle was not opened for, is
            # not the service being away, and answering anyway would hide it.
            raise
        except Exception as exc:  # noqa: BLE001 - unreachable, refused, timed out: the local home still answers
            logger.warning("the store did not answer, so the local index answered alone: %s", exc)
            alone = ask("local")
            alone.degraded = (
                f"the store did not answer ({type(exc).__name__}), so this came from the local index alone"
                + (f"; {alone.degraded}" if alone.degraded else "")
            )
            return alone
        from_here = ask("local", **wide)
        runs = {"store": list(from_store.items), "local": list(from_here.items)}
        fused: Dict[str, float] = {}
        ranks: Dict[str, Dict[str, int]] = {}
        known: Dict[str, Result] = {}
        for home, found in runs.items():
            for rank, item in enumerate(found, start=1):
                fused[item.id] = fused.get(item.id, 0.0) + 1.0 / (60 + rank)
                ranks.setdefault(item.id, {})[home] = rank
                known.setdefault(item.id, item)
        items = []
        for doc_id in sorted(fused, key=lambda i: (-fused[i], i))[:limit]:
            explain = None
            if call.get("explain"):
                explain = {**(known[doc_id].explain or {}), "home_ranks": ranks[doc_id]}
            items.append(
                dataclasses.replace(known[doc_id], score=round(fused[doc_id], 6), explain=explain)
            )
        if call.get("parents"):
            items = self._to_parents(items)
        items = apply_score_gap(items, call.get("score_gap"), lambda r: r.score)
        items, cut, tokens = fit_to_budget(
            items, call.get("token_budget"), lambda r: r.text, self.token_counter
        )
        return Results(
            items=items,
            query=call["query"],
            mode=from_store.mode,
            time_ms=(time.time() - started) * 1000.0,
            truncated=cut > 0,
            cut_count=cut,
            token_estimate=tokens,
            token_budget=call.get("token_budget"),
            degraded=from_store.degraded or from_here.degraded,
        )

    def _vector_store(self) -> Any:
        """The storage object, when it holds named dense vectors and so can
        be asked which of them answers a search. None for every other store."""
        backend = getattr(self, "storage_backend", None)
        storage = getattr(backend, "_storage", backend)
        return storage if callable(getattr(storage, "using_vectors", None)) else None

    def _stage_named_vectors(self, ids: List[str], texts: List[str]) -> None:
        """Embed the vectors a store holds beyond the collection's own, through
        the embedding cache, and hand them over for the insert that follows."""
        store = self._vector_store()
        embedders = store.named_embedders() if store is not None else {}
        for name, (label, fn) in embedders.items():
            cache = getattr(self, "_embed_cache", None)
            found = cache.get_many(label, texts) if cache is not None else [None] * len(texts)
            missing = [i for i, v in enumerate(found) if v is None]
            if missing:
                fresh = np.asarray(fn([texts[i] for i in missing]), dtype=np.float32)
                if cache is not None:
                    cache.put_many(label, [texts[i] for i in missing], fresh)
                for i, vector in zip(missing, fresh):
                    found[i] = vector
            store.stage_named_vectors(
                {name: {i: [float(x) for x in v] for i, v in zip(ids, found)}}
            )

    def with_figures(self, results: "Results") -> "Results":
        """The same results, with the figures their text refers to brought along.

        A paragraph that says ``as shown in Figure 3`` is an answer with a
        hole in it. The figure is fetched the way ``get()`` fetches anything,
        so under a policy a figure this principal may not see stays out. It
        is placed straight after the chunk that named it, at that chunk's
        score, and is not added twice.
        """
        present = {r.id for r in results.items}
        out: List[Result] = []
        for item in results.items:
            out.append(item)
            wanted = [
                i for i in (item.metadata or {}).get("_vx_refers_to") or [] if i not in present
            ]
            for figure in self.get(wanted) if wanted else []:
                present.add(figure.id)
                out.append(
                    Result(
                        id=figure.id, text=figure.text, score=item.score, metadata=figure.metadata
                    )
                )
        return replace(results, items=out)

    @staticmethod
    def _texts_to_embed(texts: List[str], metadata: Any) -> List[str]:
        from .ingest import texts_to_embed

        return texts_to_embed(texts, metadata)

    def delete_document(self, doc_id: str) -> int:
        """Delete every chunk of a document added with ``add_document``, and
        its parent sections. Returns the number of chunks removed."""
        ids = [i for i, _, m in _coll(self)._iter_documents_raw() if m.get("_vx_doc") == doc_id]
        if ids:
            self.delete(ids)
        self._parents.delete_document(doc_id)
        if self._documents is not None and not getattr(self, "_rechunking", False):
            self._documents.delete(doc_id)
        if self._chunk_files is not None:
            # Cut again or gone: either way the old chunks no longer say
            # what the index holds.
            self._chunk_files.delete(doc_id)
        return len(ids)

    # -- the document store ------------------------------------------------

    def _store(self) -> Any:
        if self._documents is None:
            raise ConfigurationError(
                f"Collection {self.name!r} does not keep its documents. Open it with keep_source=True "
                "(or a folder, or a DocumentStore) and the Markdown each document was indexed from is kept."
            )
        return self._documents

    @property
    def documents(self) -> Any:
        """The :class:`~vectrixdb.documents.DocumentStore`, or None."""
        return self._documents

    @property
    def sources(self) -> Any:
        """The feeds and pages this collection keeps up with: :class:`~vectrixdb.sources.Sources`.

        ``db.sources.add("https://example.com/feed.xml", every="6h")``, then
        ``db.sources.refresh()`` from a schedule writes what is new or changed.
        """
        held: Any = getattr(self, "_sources", None)
        if held is None:
            from .sources import Sources

            held = Sources.of(self)
            self._sources = held
        return held

    @property
    def kept_chunks(self) -> Any:
        """The :class:`~vectrixdb.documents.ChunkStore`, or None."""
        return self._chunk_files

    @property
    def markdown_first(self) -> bool:
        """Whether a document's Markdown is kept before it is cut; see ``Vectrix(markdown_first=)``."""
        return self._markdown_first

    def document(self, doc_id: str, images: bool = False) -> "LoadedDocument":
        """The document as it was indexed: its text, pages, headings and
        figures. Needs ``keep_source``."""
        return self._store().get(doc_id, images=images)

    def figure_bytes(self, hit: Any) -> Optional[bytes]:
        """The image behind a figure result, from the document store.
        None when the hit is not a figure, or its image was not kept."""
        meta = getattr(hit, "metadata", None) or (hit if isinstance(hit, dict) else {})
        src, doc_id = meta.get("figure_src"), meta.get("_vx_doc")
        if not src or not doc_id or self._documents is None:
            return None
        return self._documents.figure_bytes(doc_id, src)

    @traced(
        "rechunk",
        before=lambda a: {"collection": a["self"].name, "preview": False},
        after=lambda written: {"chunks": written},
    )
    def rechunk(
        self, doc_id: Optional[Union[str, List[str]]] = None, where: Any = None, **options: Any
    ) -> int:
        """Cut kept documents again and re-index them. Returns the chunks written.

        Reads the kept Markdown and never calls an extractor, so a new chunk
        size over ten thousand OCRed pages costs an embedding pass and not
        an OCR bill. ``options`` are ``add_document``'s, ``chunk``,
        ``chunk_size``, ``overlap``, ``parent_size``, ``embed_heading``; what
        is not given is what the document was last indexed with, and its
        metadata is what it was written with. With no ``doc_id`` every kept
        document is done, which is also how an empty index is filled from
        the store alone. ``where`` takes a function of a document's entry.

        Every document is its own ingestion, with its own record and its own
        build, as it was when it first went in.
        """
        from .documents import matching

        store = self._store()
        wanted = [doc_id] if isinstance(doc_id, str) else doc_id
        _rechunk_options(options, "rechunk")
        written = 0
        for one in matching(store, where, wanted):
            entry = store.entry(one)
            if entry is None:
                raise DocumentNotFoundError(one)
            doc = store.get(one, images=True)
            settings = _rechunk_settings(entry, options)
            self._rechunking = True
            try:
                self.delete_document(one)
                written += self.add_document(
                    doc,
                    doc_id=one,
                    metadata=entry.get("user_metadata") or None,
                    source_version=entry.get("source_version"),
                    **settings,
                )
            finally:
                self._rechunking = False
            recorded = {
                k: settings.get(k)
                for k in ("chunk", "chunk_size", "overlap", "parent_size", "embed_heading")
            }
            if settings.get("context_with") is not None:
                recorded["context"] = True
            if settings.get("late"):
                recorded["late"] = True
            store.put(
                one,
                doc,
                source_version=entry.get("source_version"),
                chunking=recorded,
                user_metadata=entry.get("user_metadata") or {},
            )
        return written

    @traced(
        "rechunk",
        before=lambda a: {"collection": a["self"].name, "preview": True},
        after=lambda planned: {
            "documents": len(planned.documents),
            "chunks": planned.chunks_after,
        },
    )
    def rechunk_preview(
        self, doc_id: Optional[Union[str, List[str]]] = None, where: Any = None, **options: Any
    ) -> "RechunkPreview":
        """What :meth:`rechunk` with the same arguments would do, without doing it.

        Each kept document is cut with the settings it would get and the
        chunks are counted beside the ones the index holds for it now.
        Nothing is written, deleted or embedded, so a preview over a whole
        collection costs reading its Markdown, apart from what semantic
        chunking asks the embedder and llm chunking asks ``cut_with``. A
        model's note (``context_with``) is not written: it does not move a
        cut. The counts are before ``dedupe``, which can only lower them.
        """
        from .documents import matching
        from .ingest import prepare_document

        store = self._store()
        wanted = [doc_id] if isinstance(doc_id, str) else doc_id
        _rechunk_options(options, "rechunk_preview")
        held: Dict[str, int] = {}
        for _, _, meta in _coll(self)._iter_documents_raw():
            one = meta.get("_vx_doc")
            if one is not None:
                held[one] = held.get(one, 0) + 1
        rows: List[RechunkedDocument] = []
        for one in matching(store, where, wanted):
            entry = store.entry(one)
            if entry is None:
                raise DocumentNotFoundError(one)
            settings = _rechunk_settings(entry, options)
            chunk = settings.get("chunk", "recursive")
            prepared = prepare_document(
                store.get(one),
                one,
                chunk=chunk,
                chunk_size=settings.get("chunk_size", 1000),
                overlap=settings.get("overlap", 200),
                parent_size=settings.get("parent_size"),
                metadata=entry.get("user_metadata") or None,
                embed_heading=bool(settings.get("embed_heading", False)),
                threshold=settings.get("threshold", 0.1),
                embed=self._embed if chunk == "semantic" else None,
                quality_threshold=settings.get("quality_threshold"),
                cut_with=settings.get("cut_with"),
            )
            rows.append(
                RechunkedDocument(
                    doc_id=one,
                    chunks_now=held.get(one, 0),
                    chunks_after=len(prepared.ids) if prepared is not None else 0,
                    chunking_now={
                        k: v
                        for k, v in (entry.get("chunking") or {}).items()
                        if k in _RECORDED_CHUNKING
                    },
                    chunking_after={k: settings.get(k) for k in _RECORDED_CHUNKING},
                )
            )
        return RechunkPreview(documents=rows)

    def reextract(
        self, where: Any = None, fetcher: Any = None, doc_id: Optional[Union[str, List[str]]] = None
    ) -> int:
        """Read originals again, for the documents ``where`` picks. Returns how many.

        For when the reader got better: ``where=lambda e: e.get("extractor") !=
        current`` re-reads only what the old one read. The original is fetched
        from the ``source`` kept with the document, through ``fetcher``, a
        :class:`~vectrixdb.worker.LocalFetcher` by default, and goes through
        this collection's extractors. A document whose text comes back the
        same is left alone.
        """
        import hashlib

        from .documents import matching
        from .ingest import load_bytes
        from .worker import LocalFetcher

        store = self._store()
        fetch = fetcher or LocalFetcher()
        wanted = [doc_id] if isinstance(doc_id, str) else doc_id
        done = 0
        for one in matching(store, where, wanted):
            entry = store.entry(one) or {}
            source = entry.get("source")
            if not source:
                continue
            data = fetch.fetch(source)
            name = entry.get("filename") or str(source).replace("\\", "/").rsplit("/", 1)[-1]
            doc = load_bytes(
                data,
                name,
                extractors=self._extractors,
                source=source,
                images=self.wants_images(),
                ocr=self.page_ocr,
            )
            done += 1
            if hashlib.sha256(doc.text.encode()).hexdigest()[:16] == entry.get("version"):
                continue
            # What it was cut with before, as add_document takes it: a model's
            # note was recorded as a flag and cannot be called again from one.
            kept = {
                k: v
                for k, v in (entry.get("chunking") or {}).items()
                if v is not None and k != "context"
            }
            self._rechunking = True
            try:
                self.delete_document(one)
            finally:
                self._rechunking = False
            self.add_document(
                doc,
                doc_id=one,
                metadata=entry.get("user_metadata") or None,
                source_version=entry.get("source_version"),
                **kept,
            )
        return done

    def reembed(self, progress: Optional[bool] = None, batch_size: int = 256) -> int:
        """Re-embed every stored text with the current model and rebuild the
        index. Returns the number of documents re-embedded.

        This is the migration for a model change: open the collection with
        the new model (which warns), call ``reembed()``, and the warning goes
        away because the recorded model is updated at the end. The new model
        must have the collection's dimension; a different width needs a new
        collection, which ``export()`` and ``add()`` cover.
        """
        if self.readonly:
            raise ConfigurationError(
                f"Collection {self.name!r} is open read-only; reembed() writes."
            )
        if self.dimension and _coll(self).dimension != self.dimension:
            raise ConfigurationError(
                f"Collection {self.name!r} holds {_coll(self).dimension}-dimensional vectors "
                f"and the current model produces {self.dimension}. Create a new collection for "
                "the new model and add the documents to it."
            )
        total = 0
        batch_ids: List[str] = []
        batch_texts: List[str] = []
        batch_meta: List[Dict[str, Any]] = []

        def flush() -> None:
            nonlocal total
            if not batch_ids:
                return
            vectors = self._embed_cached(self._texts_to_embed(batch_texts, batch_meta), progress)
            _coll(self).add(
                ids=list(batch_ids),
                vectors=vectors,
                metadata=list(batch_meta),
                texts=list(batch_texts),
            )
            total += len(batch_ids)
            batch_ids.clear()
            batch_texts.clear()
            batch_meta.clear()

        for doc_id, text, meta in list(_coll(self)._iter_documents_raw()):
            if not text:
                continue
            batch_ids.append(doc_id)
            batch_texts.append(text)
            batch_meta.append(meta)
            if len(batch_ids) >= batch_size:
                flush()
        flush()
        _coll(self).save()
        try:
            _coll(self).set_meta("embedding_model", self.model_name)
        except Exception:  # pragma: no cover
            pass
        # A re-embed rewrites every vector, so what a query answers changed.
        self._stamp_build()
        return total

    def _get_schema_config(self) -> Dict[str, Any]:
        """Get schema configuration based on mode for storage backends."""
        if (
            not self._using_storage_backend
            if hasattr(self, "_using_storage_backend")
            else self.storage_backend is None
        ):
            return {}  # Local SQLite doesn't need extra config

        # Schema adapts based on mode
        config = {
            "mode": self.default_mode,
            "store_dense": True,  # Always store dense
            "store_sparse": self.default_mode in ("hybrid", "ultimate", "graph"),
            "store_late_interaction": self.default_mode in ("ultimate", "graph"),
            "store_text": True,  # Always store text for reranker
        }
        return config

    @property
    def model(self):
        """Lazy load embedding model based on model_type."""
        if self._model is None:
            # Custom embedding function - no model needed
            if self.model_type == "custom":
                return None

            cache_key = self.model_path or self.model_name

            if cache_key in self._model_cache:
                self._model = self._model_cache[cache_key]
            elif self.model_type == "custom-onnx":
                # Custom ONNX model from user-provided path
                try:
                    from .models import DenseEmbedder
                    from pathlib import Path

                    self._model = DenseEmbedder(
                        model_dir=Path(self.model_path or ""), dimension=self.dimension
                    )
                    self._model_cache[cache_key] = self._model
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to load custom ONNX model from {self.model_path}: {e}"
                    )
            elif self.model_type == "embedded":
                # Use bundled ONNX model - NO NETWORK CALLS
                try:
                    from .models import DenseEmbedder

                    # self.model_name is a registry label such as
                    # "vectrixdb/all-MiniLM-L6-v2"; DenseEmbedder expects one of
                    # its own keys or aliases ("dense_en", "e5-small", ...).
                    # Passing the label straight through raised
                    # "Unknown model: vectrixdb/all-MiniLM-L6-v2" and broke the
                    # one-line quick start on a clean install.
                    embedded_name = _EMBEDDED_MODEL_KEYS.get(self.model_name, self.model_name)
                    self._model = DenseEmbedder(model=embedded_name, language=self.language)
                    self._model_cache[cache_key] = self._model
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to load embedded model: {e}\nRun: vectrixdb download-models"
                    )
            elif self.model_type == "fastembed":
                # Qdrant FastEmbed (cached after first download)
                try:
                    from fastembed import TextEmbedding

                    # Get model_id from registry or use model name
                    model_id = self._MODEL_REGISTRY.get(self.model_name, {}).get(
                        "model_id", self.model_name.replace("qdrant/", "")
                    )
                    self._model = TextEmbedding(model_name=model_id)
                    self._model_cache[cache_key] = self._model
                except ImportError:
                    raise ImportError(
                        "Install fastembed: pip install fastembed\n"
                        "Or use the bundled model: Vectrix('docs')"
                    )
            elif self.model_type == "fastembed-sparse":
                # Qdrant FastEmbed Sparse (BM25)
                try:
                    from fastembed import SparseTextEmbedding

                    model_id = self._MODEL_REGISTRY.get(self.model_name, {}).get(
                        "model_id", "Qdrant/bm25"
                    )
                    self._model = SparseTextEmbedding(model_name=model_id)
                    self._model_cache[cache_key] = self._model
                except ImportError:
                    raise ImportError(
                        "Install fastembed: pip install fastembed\n"
                        "Or use bundled model: Vectrix('docs', model='vectrixdb/bm25')"
                    )
            elif self.model_type == "fastembed-colbert":
                # Qdrant FastEmbed ColBERT (Late Interaction)
                try:
                    from fastembed import LateInteractionTextEmbedding

                    model_id = self._MODEL_REGISTRY.get(self.model_name, {}).get(
                        "model_id", "colbert-ir/colbertv2.0"
                    )
                    self._model = LateInteractionTextEmbedding(model_name=model_id)
                    self._model_cache[cache_key] = self._model
                except ImportError:
                    raise ImportError("Install fastembed: pip install fastembed")
            elif self.model_type == "sentence-transformers":
                # Sentence-transformers (requires network for first download)
                try:
                    from sentence_transformers import SentenceTransformer

                    # Remove prefix if present
                    model_id = self.model_name
                    for prefix in ["sentence-transformers/", "BAAI/", "jina/"]:
                        if model_id.startswith(prefix):
                            model_id = self.model_name  # Keep full name for BAAI/jina
                            break
                    if model_id.startswith("sentence-transformers/"):
                        model_id = model_id.replace("sentence-transformers/", "")
                    self._model = SentenceTransformer(model_id)
                    self._model_cache[cache_key] = self._model
                except ImportError:
                    raise ImportError(
                        "Install sentence-transformers: pip install sentence-transformers\n"
                        "Or use the bundled model: Vectrix('docs')"
                    )
            else:
                # OpenAI, Cohere, Voyage, etc. - require custom function
                raise ValueError(f"Model '{self.model_name}' requires embed_fn parameter.")
        return self._model

    @property
    def sparse_embedder(self):
        """Lazy load sparse embedder with model selection."""
        if self._instance_sparse_embedder is None:
            model_name = self.sparse_model_name

            # Check if it's a GitHub release model
            if model_name and self._is_github_model(model_name):
                model_path = self._download_github_model(model_name, "sparse")
                from .models import SparseEmbedder

                self._instance_sparse_embedder = SparseEmbedder(model_dir=Path(model_path))
            # Check if it's a HuggingFace model
            elif model_name and self._is_huggingface_model(model_name):
                # Use HuggingFace SPLADE model
                try:
                    from transformers import AutoModelForMaskedLM, AutoTokenizer
                    import torch

                    class HuggingFaceSparseEmbedder:
                        def __init__(self, model_id):
                            self.tokenizer = AutoTokenizer.from_pretrained(model_id)
                            self.model = AutoModelForMaskedLM.from_pretrained(model_id)
                            self.model.eval()

                        def embed(self, texts):
                            if isinstance(texts, str):
                                texts = [texts]
                            results = []
                            with torch.no_grad():
                                for text in texts:
                                    inputs = self.tokenizer(
                                        text,
                                        return_tensors="pt",
                                        padding=True,
                                        truncation=True,
                                        max_length=512,
                                    )
                                    outputs = self.model(**inputs)
                                    # SPLADE: log(1 + ReLU(logits)) * attention_mask
                                    logits = outputs.logits
                                    splade_rep = torch.log(1 + torch.relu(logits)) * inputs[
                                        "attention_mask"
                                    ].unsqueeze(-1)
                                    splade_rep = torch.max(splade_rep, dim=1).values.squeeze()
                                    # Convert to sparse dict
                                    non_zero = torch.nonzero(splade_rep).squeeze(-1)
                                    sparse_dict = {
                                        idx.item(): splade_rep[idx].item() for idx in non_zero
                                    }
                                    results.append(sparse_dict)
                            return results

                    self._instance_sparse_embedder = HuggingFaceSparseEmbedder(model_name)
                except ImportError:
                    raise ImportError(
                        f"Install transformers for HuggingFace models: pip install transformers torch"
                    )
            else:
                # Use bundled ONNX model
                from .models import SparseEmbedder

                # Resolve alias to model name
                resolved_model = (
                    self._SPARSE_ALIASES.get(model_name, model_name) if model_name else "sparse"
                )
                self._instance_sparse_embedder = SparseEmbedder(model=resolved_model)

        return self._instance_sparse_embedder

    @property
    def reranker(self):
        """Lazy load cross-encoder reranker with model selection."""
        if self._instance_reranker is None:
            model_name = self.reranker_model_name

            # Check if it's a GitHub release model
            if model_name and self._is_github_model(model_name):
                model_path = self._download_github_model(model_name, "reranker")
                from .models import RerankerEmbedder

                self._instance_reranker = RerankerEmbedder(model_dir=Path(model_path))
            # Check if it's a HuggingFace model
            elif model_name and self._is_huggingface_model(model_name):
                # Use HuggingFace cross-encoder
                try:
                    from sentence_transformers import CrossEncoder

                    class HuggingFaceReranker:
                        def __init__(self, model_id):
                            self.model = CrossEncoder(model_id)

                        def rerank(self, query: str, documents: list, limit: Optional[int] = None):
                            pairs = [[query, doc] for doc in documents]
                            scores = self.model.predict(pairs)
                            # Return sorted indices by score (descending)
                            sorted_indices = sorted(
                                range(len(scores)), key=lambda i: scores[i], reverse=True
                            )
                            if limit:
                                sorted_indices = sorted_indices[:limit]
                            return [(idx, scores[idx]) for idx in sorted_indices]

                    self._instance_reranker = HuggingFaceReranker(model_name)
                except ImportError:
                    raise ImportError(
                        f"Install sentence-transformers for HuggingFace reranker: pip install sentence-transformers"
                    )
            else:
                # Use bundled ONNX model
                from .models import RerankerEmbedder

                # Resolve alias to model name
                resolved_model = (
                    self._RERANKER_ALIASES.get(model_name, model_name) if model_name else None
                )
                self._instance_reranker = RerankerEmbedder(
                    model=resolved_model, language=self.language
                )

        return self._instance_reranker

    @property
    def late_interaction(self):
        """Lazy load late interaction (ColBERT) embedder with model selection."""
        if self._instance_late_interaction is None:
            model_name = self.late_interaction_model_name

            # Check if it's a GitHub release model
            if model_name and self._is_github_model(model_name):
                model_path = self._download_github_model(model_name, "late_interaction")
                from .models import LateInteractionEmbedder

                self._instance_late_interaction = LateInteractionEmbedder(
                    model_dir=Path(model_path)
                )
            # Check if it's a HuggingFace model
            elif model_name and self._is_huggingface_model(model_name):
                # Use HuggingFace ColBERT
                try:
                    from colbert.infra import ColBERTConfig
                    from colbert.modeling.checkpoint import Checkpoint

                    class HuggingFaceColBERT:
                        def __init__(self, model_id):
                            config = ColBERTConfig(checkpoint=model_id)
                            self.checkpoint = Checkpoint(model_id, colbert_config=config)

                        def embed_query(self, query: str):
                            return self.checkpoint.queryFromText([query])[0]

                        def embed_documents(self, documents: list):
                            return self.checkpoint.docFromText(documents)

                        def score(self, query_emb, doc_emb):
                            # MaxSim scoring
                            import torch

                            scores = torch.einsum("qd,pd->qp", query_emb, doc_emb)
                            return scores.max(dim=-1).values.sum().item()

                    self._instance_late_interaction = HuggingFaceColBERT(model_name)
                except ImportError:
                    # Fallback to fastembed
                    try:
                        from fastembed import LateInteractionTextEmbedding

                        self._instance_late_interaction = LateInteractionTextEmbedding(
                            model_name=model_name
                        )
                    except ImportError:
                        raise ImportError(
                            f"Install colbert-ai or fastembed for ColBERT: pip install colbert-ai[torch] or pip install fastembed"
                        )
            else:
                # Use bundled ONNX model
                from .models import LateInteractionEmbedder

                # Resolve alias to model config key
                resolved_model = (
                    self._LATE_INTERACTION_ALIASES.get(model_name, model_name)
                    if model_name
                    else None
                )

                # Check if it's a known ColBERT model
                colbert_models = {
                    "colbert",
                    "colbert-small",
                    "answerai-colbert",  # Original aliases
                    "late_interaction_en",  # Resolved key for English ColBERT
                    "colbert-v2",
                    "colbertv2",
                    "colbertv2.0",  # Higher quality aliases
                    "colbert_v2",  # Resolved key for ColBERT v2
                }
                if model_name in colbert_models or resolved_model in {
                    "late_interaction_en",
                    "colbert_v2",
                }:
                    # Use bundled/cached ONNX ColBERT with resolved model name
                    self._instance_late_interaction = LateInteractionEmbedder(
                        model=resolved_model, language="en"
                    )
                else:
                    # Use specified language or default (multilingual BGE-M3)
                    self._instance_late_interaction = LateInteractionEmbedder(
                        language=self.language
                    )

        return self._instance_late_interaction

    def _generate_id(self, text: str) -> str:
        """Generate deterministic ID from text."""
        return hashlib.md5(text.encode()).hexdigest()[:12]

    def _embed(self, texts: Union[str, List[str]]) -> Any:
        """Embed text(s) to vectors: an array for dense models, a list of
        sparse dicts or token matrices for the fastembed sparse and ColBERT
        model types."""
        if isinstance(texts, str):
            texts = [texts]

        if self.model_type == "custom":
            # Use custom embedding function
            result = self.embed_fn(texts)
            if not isinstance(result, np.ndarray):
                result = np.array(result, dtype=np.float32)
            return result
        elif self.model_type in ("embedded", "custom-onnx"):
            # Use bundled or custom ONNX model
            return self.model.embed(texts)
        elif self.model_type == "fastembed":
            # Use Qdrant FastEmbed (returns generator)
            embeddings = list(self.model.embed(texts))
            return np.array(embeddings, dtype=np.float32)
        elif self.model_type == "fastembed-sparse":
            # Use Qdrant FastEmbed Sparse - returns sparse vectors
            # This returns a different format, handled separately
            return list(self.model.embed(texts))
        elif self.model_type == "fastembed-colbert":
            # Use Qdrant FastEmbed ColBERT (Late Interaction)
            embeddings = list(self.model.embed(texts))
            return embeddings  # Returns list of token embeddings
        elif self.model_type == "sentence-transformers":
            # Use sentence-transformers
            return self.model.encode(texts, show_progress_bar=len(texts) > 100)
        else:
            # OpenAI, Cohere, Voyage, etc. - must use embed_fn
            if self.embed_fn:
                result = self.embed_fn(texts)
                if not isinstance(result, np.ndarray):
                    result = np.array(result, dtype=np.float32)
                return result
            raise ValueError(f"Model type '{self.model_type}' requires embed_fn")

    def embed(self, texts: Union[str, List[str]]) -> np.ndarray:
        """Embed text with the collection's model: shape (n, dimension), float32.

        The same vectors ``add()`` stores and ``search()`` queries with, so
        anything computed on them lines up with what the collection does.
        """
        vectors = np.asarray(self._embed(texts), dtype=np.float32)
        return vectors.reshape(1, -1) if vectors.ndim == 1 else vectors

    def _embed_batched(self, texts: List[str], progress: Optional[bool]) -> np.ndarray:
        """Embed in chunks of 64 with a progress bar when one is wanted.

        One call for the lot is fastest, so chunking only happens when there is
        a bar to advance. Without tqdm the bar is silently skipped; a missing
        optional dependency is not a reason to fail an add.
        """
        show = progress if progress is not None else len(texts) >= 200
        if not show or len(texts) <= 64:
            return self._embed(texts)
        try:
            from tqdm.auto import tqdm
        except ImportError:
            return self._embed(texts)
        chunks = []
        for start in tqdm(
            range(0, len(texts), 64), desc=f"Embedding into {self.name}", unit="batch"
        ):
            chunk = self._embed(texts[start : start + 64])
            if not isinstance(chunk, np.ndarray):
                return self._embed(texts)
            chunks.append(chunk)
        return np.vstack(chunks)

    def _embed_sparse(self, texts: Union[str, List[str]]) -> List[Dict[int, float]]:
        """Generate sparse BM25 embeddings (no network calls)."""
        if isinstance(texts, str):
            texts = [texts]
        return self.sparse_embedder.embed(texts)

    def _rerank_with_cross_encoder(
        self, query: str, candidates: List[Dict], limit: int
    ) -> List[Dict]:
        """Rerank candidates using cross-encoder."""
        if not candidates or not self.reranker_model_name:
            return candidates[:limit]

        try:
            reranker = self.reranker
            if hasattr(reranker, "rerank"):
                # HuggingFace reranker
                docs = [c["text"] for c in candidates]
                reranked_indices = reranker.rerank(query, docs, limit=limit)
                return [candidates[idx] for idx, _ in reranked_indices]
            else:
                # Bundled reranker - use cross-encoder scoring
                pairs = [(query, c["text"]) for c in candidates]
                scores_list = reranker.score_pairs(pairs)
                sorted_candidates = sorted(
                    zip(candidates, scores_list), key=lambda x: x[1], reverse=True
                )
                return [c for c, _ in sorted_candidates[:limit]]
        except Exception:
            # Fall back to original scores
            return candidates[:limit]

    # =========================================================================
    # Core Operations
    # =========================================================================

    def add(
        self,
        texts: Union[str, List[str]],
        metadata: Union[Dict, List[Dict], None] = None,
        ids: Optional[List[str]] = None,
        vectors: Optional["np.ndarray"] = None,
        progress: Optional[bool] = None,
        dedupe: Optional[float] = None,
    ) -> Vectrix:
        """
        Add texts to the collection.

        Args:
            texts: Single text or list of texts
            metadata: Optional metadata for each text
            ids: Optional custom IDs (auto-generated if not provided)
            vectors: Precomputed embeddings, shape (n, dimension). Skips the
                model, for callers who already have vectors from elsewhere.
            progress: Show a progress bar while embedding. None means "when
                there is enough to watch" (200 texts or more, tqdm installed);
                True forces it, False silences it.
            dedupe: Skip a text whose MinHash similarity (word 3-shingles) to
                a stored text, or to an earlier text in this call, is at or
                above this value; 0.9 catches re-pastes and near-identical
                paragraphs. What was skipped is in ``last_add_report``.

        Returns:
            Self for chaining

        Example:
            >>> db = Vectrix("docs").add(["text 1", "text 2"])
            >>> db.add("another text", metadata={"source": "web"})

        With storage backend, embeddings are generated based on mode:
            - dense: dense_embedding only
            - hybrid: dense_embedding + sparse_embedding
            - ultimate: + late_interaction_embedding
            - graph: + graph relationships
        """
        # Normalize inputs
        if isinstance(texts, str):
            texts = [texts]

        if metadata is None:
            metadata = [{} for _ in texts]
        elif isinstance(metadata, dict):
            metadata = [metadata]

        if ids is None:
            ids = [self._generate_id(t) for t in texts]

        self._check_metadata_contract(metadata, ids)

        add_started = time.perf_counter()
        report = AddReport(added=list(ids), skipped=[])
        if dedupe is not None:
            index = self.dedupe_index
            keep = []
            signatures = []
            for i, text in enumerate(texts):
                sig = index.signature(text)
                hit = index.query(text, threshold=dedupe, signature=sig)
                if hit is not None:
                    report.skipped.append((ids[i], hit[0], round(hit[1], 3)))
                    continue
                index.add(ids[i], text, signature=sig)
                keep.append(i)
                signatures.append(sig)
            if len(keep) < len(texts):
                texts = [texts[i] for i in keep]
                metadata = [metadata[i] for i in keep]
                if vectors is not None:
                    vectors = np.asarray(vectors)[keep]
                ids = [ids[i] for i in keep]
            report.added = list(ids)
            index.save()
        self.last_add_report = report
        if not texts:
            return self

        # The build this write will be, stamped on every chunk before the
        # write so a chunk can always name the ingestion that stored it.
        build = self._mint_build()
        if build is not None:
            metadata = [dict(m or {}) for m in metadata]
            for m in metadata:
                m["_vx_build"] = build

        # Dense vectors: supplied, or computed here.
        if vectors is not None:
            dense_vectors = np.asarray(vectors, dtype=np.float32)
            if dense_vectors.ndim == 1:
                dense_vectors = dense_vectors.reshape(1, -1)
            if dense_vectors.shape[0] != len(texts):
                raise ValueError(
                    f"{len(texts)} texts were given but {dense_vectors.shape[0]} vectors"
                )
            if self.dimension and dense_vectors.shape[1] != self.dimension:
                raise ValueError(
                    f"Vectors have dimension {dense_vectors.shape[1]}, "
                    f"the collection expects {self.dimension}"
                )
        else:
            dense_vectors = self._embed_cached(self._texts_to_embed(texts, metadata), progress)

        # A store with a second dense vector gets it now, through the cache.
        self._stage_named_vectors(ids, self._texts_to_embed(texts, metadata))
        for label, side in self._named.items():
            if label == IMAGE_VECTOR:
                continue  # figures only, and from their pictures: add_document fills it
            side.add(
                list(texts),
                metadata=[dict(m or {}) for m in metadata],
                ids=list(ids),
                progress=False,
            )

        # Store texts for retrieval
        for id_, text in zip(ids, texts):
            self._texts[id_] = text

        # For storage backends, generate additional embeddings based on mode
        if (
            self._using_storage_backend
            if hasattr(self, "_using_storage_backend")
            else self.storage_backend is not None
        ):
            extra_embeddings: Dict[str, Any] = {}

            # Generate sparse embeddings for hybrid/ultimate/graph modes
            if self.default_mode in ("hybrid", "ultimate", "graph"):
                sparse_vectors = self._embed_sparse(texts)
                extra_embeddings["sparse_embeddings"] = sparse_vectors

            # Generate late interaction embeddings for ultimate/graph modes
            if self.default_mode in ("ultimate", "graph"):
                late_interaction_vectors = self._embed_late_interaction(texts)
                extra_embeddings["late_interaction_embeddings"] = late_interaction_vectors

            # Add to collection with all embeddings
            self._collection.add(
                ids=ids, vectors=dense_vectors, metadata=metadata, texts=texts, **extra_embeddings
            )
        else:
            # Local SQLite - just dense vectors
            self._collection.add(ids=ids, vectors=dense_vectors, metadata=metadata, texts=texts)

        # Build the knowledge graph alongside the vectors when in graph mode.
        # Extraction is the expensive half, so a failure here must not lose the
        # vectors that were already written: it is reported, not raised.
        if self.default_mode == "graph":
            try:
                self.graph.add_documents(texts, metadata=metadata, doc_ids=ids)
            except Exception as exc:
                logger.warning(
                    "Vectors for %r were stored, but graph extraction failed: %s",
                    self.name,
                    exc,
                )
            else:
                # New documents may have fixed whatever was wrong, so a reason
                # that recurs after this is worth hearing about again.
                self._graph_warnings.clear()

        self._commit_build(build)
        self._record_ingestion(build, add_started)

        self._fire(
            self.on_add,
            {
                "op": "add",
                "ms": (time.perf_counter() - add_started) * 1000,
                "count": len(texts),
                "mode": self.default_mode,
                "collection": self.name,
            },
        )

        # Persist the ANN index. Collection.save() is otherwise only reached via
        # VectrixDB.close()/flush(), which the one-line API never exposes, so the
        # usearch index was lost on exit: rows stayed in SQLite and count() kept
        # reporting them, but the index came back empty and every search returned
        # nothing. Failing to persist must not lose the write that already
        # succeeded, so a save error is reported rather than raised.
        if self.path and not getattr(self, "_deferring_saves", 0):
            try:
                self._collection.save()
            except Exception as exc:  # pragma: no cover - depends on the filesystem
                logger.warning("Could not persist the vector index for %r: %s", self.name, exc)

        return self  # Enable chaining

    @contextlib.contextmanager
    def deferred_saves(self) -> Any:
        """Save the vector index once, when the block ends, rather than after
        every ``add()`` inside it.

        ``add()`` saves the index file each time, which is what makes a single
        write durable and what makes a thousand of them slow: the file is
        rewritten a thousand times. Inside this block the rows still land in
        SQLite one by one, and the index is written when the block exits,
        whether it exits normally or not. What is given up is durability of
        the index between the first write and the end of the block: a process
        killed in between reopens with the rows and an index to rebuild with
        ``rebuild_index()``.
        """
        self._deferring_saves = getattr(self, "_deferring_saves", 0) + 1
        try:
            yield self
        finally:
            self._deferring_saves -= 1
            if not self._deferring_saves and self.path:
                try:
                    self._collection.save()
                except Exception as exc:  # pragma: no cover - depends on the filesystem
                    logger.warning("Could not persist the vector index for %r: %s", self.name, exc)

    def add_many(
        self,
        texts: Iterable[str],
        metadata: Optional[Iterable[Dict]] = None,
        ids: Optional[Iterable[str]] = None,
        batch_size: int = 256,
        progress: Optional[bool] = None,
    ) -> int:
        """Add from any iterable, a generator included, in batches. Returns the count.

        ``add()`` wants a list it can embed in one call; this feeds it batches
        so a corpus streamed from a file or a database never has to be held
        in memory at once.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        meta_iter = iter(metadata) if metadata is not None else None
        id_iter = iter(ids) if ids is not None else None
        total = 0
        batch_texts: List[str] = []
        batch_meta: List[Dict] = []
        batch_ids: List[str] = []

        def flush():
            nonlocal total
            if not batch_texts:
                return
            self.add(
                list(batch_texts),
                metadata=list(batch_meta) if meta_iter is not None else None,
                ids=list(batch_ids) if id_iter is not None else None,
                progress=progress,
            )
            total += len(batch_texts)
            batch_texts.clear()
            batch_meta.clear()
            batch_ids.clear()

        for text in texts:
            batch_texts.append(text)
            if meta_iter is not None:
                batch_meta.append(next(meta_iter))
            if id_iter is not None:
                batch_ids.append(next(id_iter))
            if len(batch_texts) >= batch_size:
                flush()
        flush()
        return total

    def revoke(
        self,
        where: Dict[str, Any],
        set: Optional[Dict[str, Any]] = None,
        unset: Optional[Iterable[str]] = None,
    ) -> "RevocationReport":
        """Change entitlement metadata on every document matching ``where``.

        The operational half of an entitlement policy. Entitlements are
        denormalised onto the chunk, which is what makes a query fast and a
        permission change expensive: one person leaving a deal team means
        every chunk of every document for that client. Doing it as a loop over
        ``update_metadata`` is doing it by hand, and the hand slips.

        ``set`` merges fields in. ``unset`` removes them, which is not the
        same as setting them to null: the filter engine tells an absent field
        from a null one, and under a policy an absent field denies while a
        null one is a value like any other.

        Administrative, so it belongs to whoever holds the collection rather
        than to a principal, the same as ``export``.

        Example:
            >>> report = db.revoke(
            ...     where={"entitlements.client_id": "CL-40219"},
            ...     unset=["entitlements.need_to_know_group"],
            ... )
            >>> report.matched, report.changed
            (412, 412)
        """
        from .core.types import Filter

        self._refuse_under_policy(
            "revoke()",
            "changing entitlements is not something a principal does to their own view.",
        )
        if not where:
            # An empty filter matches every document, so this would rewrite
            # the whole collection. The truthiness family, in the one place
            # it would be a mass edit.
            raise ValueError(
                "revoke() needs a filter. An empty one matches every document, which "
                "would rewrite the entitlements on the whole collection."
            )
        if not set and not unset:
            raise ValueError("revoke() needs something to set or unset")

        matcher = Filter.from_dict(where)
        fields = tuple(sorted({*(set or {}), *(unset or ())}))
        collection = _coll(self)
        matched = 0
        changed = 0

        for doc_id, _, metadata in list(collection._iter_documents_raw()):
            if not matcher.matches(metadata):
                continue
            matched += 1
            updated = _apply_revocation(metadata, set, unset)
            if updated == metadata:
                # Already in the state asked for. Counting it as changed
                # would overstate what this call did.
                continue
            if collection.update_metadata(doc_id, updated, merge=False):
                changed += 1

        self._stamp_build()
        return RevocationReport(matched=matched, changed=changed, at=_utcnow(), fields=fields)

    def reproduce(self, record: Any, snapshot: Optional[Union[str, Path]] = None) -> Any:
        """Restate a decision: which chunks that principal could reach.

        ``record`` is the decision record, or the dict a sink stored; find it
        with ``sink.find(decision_id)`` where the sink can read. The result is
        exact when the index build and the policy on the record are the ones
        this collection holds now, and says so when they are not. Pass
        ``snapshot`` to run against an export of the build the record names.

        The host's question, so a principal's view refuses it: which chunks a
        principal could reach is the map of the wall.

        Example:
            >>> record = audit.find(results.decision_id)
            >>> print(db.reproduce(record).report())
        """
        from .lineage import reproduce

        self._refuse_under_policy(
            "reproduce()", "which chunks a principal could reach is the map of the wall."
        )
        return reproduce(self, record, snapshot=snapshot)

    def provenance(self, ids: Union[str, Iterable[str]]) -> Any:
        """Where these chunks came from: document, version, source, chunking,
        and the build that stored them. The host's question; see ``reproduce``."""
        from .lineage import provenance_of

        self._refuse_under_policy("provenance()", "it reads chunks past the policy.")
        if isinstance(ids, str):
            ids = [ids]
        return provenance_of(self, ids)

    def evidence_pack(
        self,
        directory: Union[str, Path],
        *,
        records: Optional[Iterable[Mapping[str, Any]]] = None,
    ) -> Any:
        """Write the model risk evidence artifacts for this collection.

        What a validator asks for, generated rather than assembled by hand:
        what the model is, what it answers from, which controls are in force,
        and what the audit trail holds. Not a compliance claim; the files.
        ``records`` is the audit trail to summarise when the attached sink
        cannot read it back.
        """
        from .lineage import write_evidence_pack

        self._refuse_under_policy("evidence_pack()", "it describes the whole collection.")
        return write_evidence_pack(self, directory, records=records)

    def export(self, target: Union[str, Path]) -> Path:
        """Write the whole collection to one zip: vectors, index, texts, graph.

        Example:
            >>> db.export("backups/notes.zip")
        """
        from .snapshot import export

        self._refuse_under_policy(
            "export()",
            "a snapshot is the whole collection, which is not a per-principal thing.",
        )
        return export(self, Path(target))

    @classmethod
    def import_snapshot(
        cls, snapshot: Union[str, Path], path: Union[str, Path], name: Optional[str] = None, **kw
    ) -> "Vectrix":
        """Unpack a zip written by ``export`` under ``path`` and open it.

        Example:
            >>> db = Vectrix.import_snapshot("backups/notes.zip", path="./restored")
        """
        from .snapshot import import_snapshot

        return import_snapshot(Path(snapshot), Path(path), name=name, **kw)

    def rebuild_index(self) -> int:
        """Rebuild the ANN index from the live vectors without blocking readers.

        Deletions leave tombstones behind and recall drifts as the graph is
        edited in place; a rebuild starts clean. Returns the vector count.
        """
        rebuilt = self._collection.rebuild_index()
        self._stamp_build()
        return rebuilt

    def _refuse_graph(self, op: str) -> None:
        self._refuse_under_policy(
            op,
            "the policy decides documents by their metadata and has nothing to say about "
            "an entity or an edge, so a traversal could walk out of a principal's scope "
            "with nothing to stop it.",
        )

    def graph_path(self, a: str, b: str, max_depth: int = 4) -> Dict[str, Any]:
        """Shortest chain of relationships between two entities (graph mode).

        Example:
            >>> db.graph_path("Marie Curie", "Nobel Prize")
            {'found': True, 'entities': [...], 'relationships': [...], 'depth': 2}
        """
        self._refuse_graph("graph_path()")
        return self.graph.shortest_path(a, b, max_depth=max_depth)

    def graph_explain(self, a: str, b: Optional[str] = None, max_depth: int = 4) -> Dict[str, Any]:
        """Everything the graph knows about an entity, or about a pair (graph mode).

        Example:
            >>> db.graph_explain("Marie Curie")["communities"]
            >>> db.graph_explain("Marie Curie", "Pierre Curie")["direct"]
        """
        self._refuse_graph("graph_explain()")
        return self.graph.explain(a, b, max_depth=max_depth)

    @property
    def graph(self):
        """The knowledge graph pipeline, built on first access.

        Only available in graph mode. Extraction defaults to the NLP extractor
        rather than REBEL, because REBEL pulls a transformer download and the
        point of this package is that it works offline; pass your own
        ``GraphRAGConfig`` to choose otherwise.
        """
        if self.default_mode != "graph":
            raise ConfigurationError(
                f"Knowledge graph is only built in graph mode, not {self.default_mode!r}. "
                'Create the collection with tier="graph".'
            )

        if self._graph_pipeline is None:
            try:
                from .core.graphrag import ExtractorType, GraphRAGConfig, create_pipeline
            except ImportError as exc:  # pragma: no cover - optional subsystem
                raise DependencyError("graphrag", extra="all") from exc

            config = self._graphrag_config or GraphRAGConfig(
                enabled=True,
                extractor=ExtractorType.NLP,
            )
            graph_path = Path(self.path) / f"{self.name}_graph" if self.path else None
            self._graph_pipeline = create_pipeline(
                config=config,
                path=graph_path,
                embed_fn=lambda text: self._embed([text])[0],
            )
        return self._graph_pipeline

    def _embed_late_interaction(self, texts: Union[str, List[str]]) -> List[np.ndarray]:
        """Generate late interaction (ColBERT) embeddings."""
        if isinstance(texts, str):
            texts = [texts]

        colbert = self.late_interaction

        if hasattr(colbert, "embed_documents"):
            # HuggingFace ColBERT
            return colbert.embed_documents(texts)
        elif hasattr(colbert, "embed"):
            # FastEmbed or bundled ColBERT
            return list(colbert.embed(texts))
        elif hasattr(colbert, "encode_documents"):
            # Bundled LateInteractionEmbedder
            return colbert.encode_documents(texts)
        else:
            raise AttributeError(
                f"ColBERT embedder has no encode method. Available: {dir(colbert)}"
            )

    @traced(
        "search",
        before=lambda a: {
            "collection": a["self"].name,
            "mode": str(a["mode"] or a["self"].default_mode),
            "limit": a["limit"],
            "rerank": None if a["rerank"] is None else str(a["rerank"]),
            "filtered": a["filter"] is not None,
        },
        after=lambda found: {
            "results": len(found.items),
            "top_relevance": found.items[0].relevance if found.items else None,
            "top_relevance_kind": found.items[0].relevance_kind if found.items else None,
            "degraded": bool(found.degraded),
            "truncated": bool(found.truncated),
        },
    )
    def search(
        self,
        query: str,
        limit: int = 10,
        mode: Optional[SearchMode] = None,
        rerank: Literal[None, False, "mmr", "exact", "cross-encoder", "semantic"] = None,
        filter: Optional[Dict[str, Any]] = None,
        diversity: float = 0.7,
        token_budget: Optional[int] = None,
        score_gap: Optional[float] = None,
        explain: bool = False,
        fusion: Literal["rrf", "weighted"] = "rrf",
        alpha: float = 0.5,
        expand: Optional[Callable[[str], List[str]]] = None,
        hyde: Optional[Callable[[str], str]] = None,
        parents: bool = False,
        vectors: Optional[str] = None,
        homes: Optional[Literal["store", "local", "both"]] = None,
    ) -> Results:
        """
        Search the collection.

        Args:
            query: Search query text
            limit: Number of results (default: 10)
            mode: Search mode (defaults to mode set in constructor)
                - "dense": Semantic search only (fastest)
                - "sparse": Keyword/BM25 only
                - "hybrid": Dense + Sparse + Reranker (balanced)
                - "ultimate": Dense + Sparse + Reranker + ColBERT (best quality)
                - "graph": Ultimate + Knowledge Graph (for GraphRAG)
            rerank: Reranking. In dense and sparse modes None means no
                reranking and "mmr", "exact" or "cross-encoder" adds one.
                Hybrid, ultimate and graph modes rerank their candidates with
                the cross-encoder by default; False turns that off, which
                matters for large limits (the cross-encoder costs about
                170 ms per long document on a CPU). On Azure AI Search,
                "semantic" is the service's semantic ranker, for keyword and
                hybrid searches on an index that has a semantic
                configuration; "cross-encoder" is MiniLM here instead of it,
                and False is neither. Left out, it is how the store was opened.
            filter: Metadata filter (e.g., {"category": "tech"}); see
                docs/reference/filters.md for the grammar
            diversity: Diversity parameter for MMR (0-1, default: 0.7)
            token_budget: Stop returning results once their text would cost
                more than this many tokens. The top result always ships, and
                ``Results.truncated`` / ``Results.cut_count`` say what was cut.
            score_gap: Drop results scoring below this fraction of the top
                score, so a specific question returns one strong answer instead
                of a padded list. The right value depends on the score scale:
                cosine scores from the bundled models sit in a narrow band, so
                0.9 is typical there, while BM25 or graph scores want 0.2.
                None disables.
            explain: Put the parts of each score in ``Result.explain``: dense
                similarity, BM25, the fused score, ColBERT, the reranker and
                any graph boost. What you cannot see you cannot tune.
            fusion: How hybrid and ultimate modes combine dense and sparse:
                "rrf" (reciprocal rank fusion, rank-based, robust) or
                "weighted" (min-max normalised scores blended by ``alpha``).
            alpha: The dense share of the blend, 0 to 1. 0.5 treats both
                equally; raise it when keyword matches are dragging results.
            expand: A function from the query to alternative phrasings. Each
                is searched and the runs are fused by rank, so a question the
                corpus phrases differently still finds its document. Pass any
                LLM call; nothing is bundled.
            hyde: A function from the query to a hypothetical answer. The
                answer is embedded instead of the question, because a document
                that would answer it sits closer to real documents than the
                question does. Pass any LLM call; nothing is bundled.
            parents: Return, for each hit, the section of its document it
                belongs to rather than the chunk itself, for a document
                added with ``parent_size=``: small chunks find, the section
                answers. Several hits in one section come back as that
                section once, and a chunk from a document added without
                ``parent_size`` comes back as it is.

        Returns:
            Results object with search results

        Example:
            >>> results = db.search("python programming")
            >>> results = db.search("AI", mode="dense", explain=True)
            >>> results.top.explain

        ``vectors`` picks whose dense vectors answer, on a store that holds
        more than one: ``"vectrixdb"``, ``"azure"`` or ``"both"``. Left
        out, every vector the store holds answers. An index built with
        ``embeddings="both"`` can be asked all three ways, which is what
        makes a comparison between them fair: same chunks, same filter,
        same ranker.

        ``homes`` is for a collection kept on a store, Azure AI Search or
        OpenSearch. Every write already reaches both the store and the index
        beside this process, under the same ids, so the collection has two
        homes. ``"store"`` asks the service and raises when it does not
        answer; ``"local"`` asks the index here and never leaves the machine;
        ``"both"`` asks each and fuses the two lists by rank, and when the
        service does not answer it returns the local list alone and says so
        in ``degraded``. Left out, nothing changes. Dense, keyword and hybrid
        searches; not offered under an entitlement policy.
        """
        if homes is not None:
            again = {k: v for k, v in locals().items() if k not in ("self", "homes")}
            return self._search_homes(homes, again)
        store = self._vector_store()
        if self._named and not getattr(self._named_scope, "inside", False):
            again = {k: v for k, v in locals().items() if k not in ("self", "store", "vectors")}
            self._named_scope.inside = True
            try:
                return self._search_named(vectors, again)
            finally:
                self._named_scope.inside = False
        if vectors is not None and store is None and not self._named:
            raise ConfigurationError(
                f"vectors={vectors!r} picks between the dense vectors a store holds, and this collection "
                "has the one. A list of dense models builds more locally, and "
                "VectrixDB.with_azure_search(embeddings='both') builds an index with two."
            )
        if store is not None and not store.scoped():
            again = {k: v for k, v in locals().items() if k not in ("self", "store", "vectors")}
            with store.using_vectors(vectors, query):
                return self.search(**again)

        import time

        start = time.time()

        # Before anything else, including validation, because a policied
        # collection queried with no principal must not do work or report
        # anything that depends on the query.
        effective_filter = self._policy_filter(filter, "search")
        if effective_filter is _DENY_EVERYTHING:
            from .policy import Outcome as _Outcome

            self._record_refusal(query, _Outcome.DENIED_OUT_OF_SCOPE.value)
            return Results(
                query=query,
                items=[],
                mode=mode or self.default_mode,
                time_ms=self._pad_to_floor((time.time() - start) * 1000),
            )

        if self._policy is not None and self.on_retrieval is not None:
            return self._audited_search(
                query,
                limit,
                filter,
                {
                    "mode": mode,
                    "rerank": rerank,
                    "diversity": diversity,
                    "token_budget": token_budget,
                    "score_gap": score_gap,
                    "explain": explain,
                    "fusion": fusion,
                    "alpha": alpha,
                    "expand": expand,
                    "hyde": hyde,
                    "parents": parents,
                },
            )

        filter = effective_filter

        search_mode: str = mode or self.default_mode
        self._validate_search_mode(search_mode)
        # A limit of zero or less asked for nothing and got one result: the
        # index was handed the number unchecked and negative slicing did the
        # rest. The REST layer has always rejected it; this is the same rule
        # one level down.
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit!r}")
        if fusion not in ("rrf", "weighted"):
            raise ValueError(f"fusion must be 'rrf' or 'weighted', got {fusion!r}")
        if not 0.0 <= alpha <= 1.0:
            raise ValueError(f"alpha must be in [0, 1], got {alpha!r}")
        if rerank == "semantic":
            if search_mode not in ("sparse", "hybrid"):
                raise ConfigurationError(
                    "rerank='semantic' is Azure AI Search's semantic ranker, which reads the words: "
                    f"use it with mode='sparse' or mode='hybrid', not {search_mode!r}"
                )
            if self._semantic_store() is None:
                raise ConfigurationError(
                    "rerank='semantic' is Azure AI Search's semantic ranker, and this collection is not "
                    "stored on Azure AI Search"
                )

        embed_text = hyde(query) if hyde is not None else query
        query_vector = self._embed(embed_text)[0]

        use_backend = (
            self._using_storage_backend
            if hasattr(self, "_using_storage_backend")
            else self.storage_backend is not None
        )
        if self._home() == "local":
            use_backend = False

        notes: List[str] = []
        # Deletions are filtered after the search, not skipped during it, so a
        # heavily deleted index quietly gets slower. Say so on the result
        # rather than let the caller wonder.
        ratio = getattr(self._collection, "tombstone_ratio", 0.0)
        if ratio and ratio >= self._TOMBSTONE_NOTE_AT:
            notes.append(
                f"{ratio:.0%} of this index is deleted vectors, which slows every "
                f"search; call rebuild_index() to compact it"
            )

        options: Dict[str, Any] = {
            "filter": filter,
            "diversity": diversity,
            "use_backend": use_backend,
            "explain": explain,
            "fusion": fusion,
            "alpha": alpha,
            "rerank": rerank,
        }

        if expand is not None:
            variants = [query] + [v for v in expand(query) if v and v != query]
            runs = []
            for variant in variants:
                vector = query_vector if variant == query else self._embed(variant)[0]
                runs.append(
                    self._search_once(variant, vector, limit, search_mode, notes, **options)
                )
            results = self._fuse_runs(runs, limit, explain)
        else:
            results = self._search_once(query, query_vector, limit, search_mode, notes, **options)

        elapsed = self._pad_to_floor((time.time() - start) * 1000)

        items = [
            Result(
                id=r["id"],
                text=self._texts.get(r["id"], r.get("text", "")),
                score=r["score"],
                metadata=r.get("metadata", {}),
                explain=r.get("explain") if explain else None,
                **_verdict(r),
            )
            for r in results
        ]
        if parents:
            items = self._to_parents(items)
        items = apply_score_gap(items, score_gap, lambda r: r.score)
        items, cut, tokens = fit_to_budget(
            items, token_budget, lambda r: r.text, self.token_counter
        )
        self._fire(
            self.on_search,
            {
                "op": "search",
                "ms": elapsed,
                "count": len(items),
                "mode": search_mode,
                "collection": self.name,
            },
        )
        return Results(
            items=items,
            query=query,
            mode=search_mode,
            time_ms=elapsed,
            truncated=cut > 0,
            cut_count=cut,
            token_estimate=tokens,
            token_budget=token_budget,
            degraded=notes[0] if notes else None,
        )

    def _search_once(
        self,
        query: str,
        query_vector: "np.ndarray",
        limit: int,
        mode: str,
        notes: List[str],
        *,
        filter: Optional[Dict[str, Any]],
        diversity: float,
        use_backend: bool,
        explain: bool,
        fusion: str,
        alpha: float,
        rerank: Optional[str],
    ) -> List[Dict]:
        """One search in one mode; ``search()`` may call this per query variant."""
        rerank_candidates = rerank is not False
        # Azure's semantic ranker, for this search: asked for, turned down
        # (no reranking, or MiniLM instead), or left to how the store was opened.
        semantic: Optional[bool] = (
            True if rerank == "semantic" else False if rerank in (False, "cross-encoder") else None
        )
        if mode == "graph":
            results = self._graph_search(
                query,
                query_vector,
                limit,
                filter,
                diversity,
                use_backend=use_backend,
                notes=notes,
                explain=explain,
                rerank_candidates=rerank_candidates,
                fusion=fusion,
                alpha=alpha,
            )
        elif mode == "ultimate":
            results = self._ultimate_search(
                query,
                query_vector,
                limit,
                filter,
                diversity,
                use_backend=use_backend,
                explain=explain,
                rerank_candidates=rerank_candidates,
                fusion=fusion,
                alpha=alpha,
            )
        elif mode == "dense":
            results = self._dense_search(query_vector, limit, filter, explain=explain)
        elif mode == "sparse":
            results = self._sparse_search(
                query, limit, filter, explain=explain, use_backend=use_backend, semantic=semantic
            )
        elif mode == "hybrid":
            results = self._hybrid_search(
                query,
                query_vector,
                limit,
                filter,
                use_backend=use_backend,
                explain=explain,
                rerank_candidates=rerank_candidates,
                fusion=fusion,
                alpha=alpha,
                semantic=semantic,
            )
        else:
            raise ValueError(
                f"Unknown mode: {mode}. Use 'dense', 'sparse', 'hybrid', 'ultimate', or 'graph'"
            )

        if rerank and rerank != "semantic" and mode in ("dense", "sparse"):
            results = self._rerank(
                query, query_vector, results, rerank, limit, diversity, explain=explain
            )
        return results

    @staticmethod
    def _fire(hook: Optional[Callable[[Dict[str, Any]], None]], event: Dict[str, Any]) -> None:
        """Call a timing hook; a failing hook is logged, never raised."""
        if hook is None:
            return
        try:
            hook(event)
        except Exception as exc:  # noqa: BLE001 - the host's hook must not break the query
            logger.warning("timing hook %r failed: %s", hook, exc)

    def _to_parents(self, items: List["Result"]) -> List["Result"]:
        """Replace chunks with the sections they belong to, once each.

        A chunk without a parent (added with plain add(), or add_document
        without parent_size) is returned as it is. Several chunks of one
        section collapse into that section at the best of their scores, with
        the matching chunk's id and text kept under ``metadata["_vx_child"]``.
        """
        out: List[Result] = []
        seen: Dict[str, int] = {}
        for item in items:
            parent_id = item.metadata.get("_vx_parent") if item.metadata else None
            if not parent_id:
                out.append(item)
                continue
            if parent_id in seen:
                continue
            found = self._parents.get(parent_id)
            if found is None:
                out.append(item)
                continue
            text, meta = found
            seen[parent_id] = len(out)
            out.append(
                Result(
                    id=parent_id,
                    text=text,
                    score=item.score,
                    metadata={**meta, "_vx_child": item.id, "_vx_child_text": item.text},
                    explain=item.explain,
                    # The child is what matched, so the verdict is the child's.
                    relevance=item.relevance,
                    relevance_kind=item.relevance_kind,
                    similarity=item.similarity,
                    matched_by=item.matched_by,
                    relevances=item.relevances,
                )
            )
        return out

    @staticmethod
    def _fuse_runs(runs: List[List[Dict]], limit: int, explain: bool) -> List[Dict]:
        """Reciprocal rank fusion across the runs of a multi-query search."""
        k = 60
        fused: Dict[str, Dict] = {}
        for run in runs:
            for rank, item in enumerate(run):
                entry = fused.get(item["id"])
                if entry is None:
                    entry = dict(item)
                    entry["score"] = 0.0
                    entry["_runs"] = 0
                    fused[item["id"]] = entry
                entry["score"] += 1.0 / (k + rank + 1)
                entry["_runs"] += 1
        ordered = sorted(fused.values(), key=lambda r: r["score"], reverse=True)[:limit]
        for entry in ordered:
            runs_hit = entry.pop("_runs")
            if explain:
                entry.setdefault("explain", {})["query_variants"] = runs_hit
                entry["explain"]["fused_rrf"] = entry["score"]
        return ordered

    def _dense_search(
        self,
        query_vector: np.ndarray,
        limit: int,
        filter: Optional[Dict] = None,
        explain: bool = False,
    ) -> List[Dict]:
        """Pure dense/semantic search.

        The withheld counts come back on `self._policy_counts` when that
        attribute has been set to a dict, rather than travelling with the
        results. A decision record needs them; a caller must never be handed
        them, because the undisclosable one confirms documents exist outside
        a scope they were refused. The attribute lives on a throwaway copy
        per audited search, so searching one collection from several threads
        stays safe.
        """
        results = self._collection.search(
            query=query_vector,
            limit=limit,
            filter=filter,
            principal=self._principal,
            **self._home_kwargs(),
        )
        if self._policy_counts is not None:
            self._policy_counts.update(
                {
                    "examined": results.policy_candidates,
                    "disclosable": results.policy_withheld_disclosable,
                    "undisclosable": results.policy_withheld_undisclosable,
                }
            )
        out = []
        for r in results.results:
            item = {"id": r.id, "score": r.score, "metadata": r.metadata, "vector": r.vector}
            item.update(_judgement(r))
            if explain:
                item["explain"] = {"dense": r.score}
            out.append(item)
        return out

    def _backend_storage(self) -> Any:
        """The store's own object, Azure's or OpenSearch's, when this collection has one."""
        backend = getattr(self, "storage_backend", None)
        if backend is None or not getattr(self, "_using_storage_backend", False):
            return None
        return backend._storage if hasattr(backend, "_storage") else backend

    def _semantic_store(self) -> Any:
        """The Azure AI Search store, when that is where this collection lives."""
        storage = self._backend_storage()
        return storage if type(storage).__name__ == "AzureSearchStorage" else None

    def _keyword_store(self, use_backend: bool, semantic: Optional[bool]) -> Any:
        """The store whose own keyword search answers this one, or None for the local index.

        The local words index answers while it holds anything, the way the
        local vector index answers a dense search. A handle opened only to
        search an index another process filled holds nothing locally, and
        then the store's keyword search answers; so does a search that asks
        for Azure's semantic ranker, which only the service can run.
        """
        home = self._home()
        storage = self._backend_storage() if use_backend and home != "local" else None
        if storage is None or type(storage).__name__ not in (
            "AzureSearchStorage",
            "OpenSearchStorage",
        ):
            return None
        if not hasattr(storage, "text_search"):
            return None
        if home == "store":
            return storage
        if self._policy is not None and not self._collection._policy_pushable(storage):
            return None
        index = getattr(self._collection, "_text_index", None)
        empty = index is None or not int(getattr(index, "_doc_count", 0) or 0)
        return storage if (semantic or empty) else None

    def _sparse_search(
        self,
        query: str,
        limit: int,
        filter: Optional[Dict] = None,
        explain: bool = False,
        use_backend: bool = False,
        semantic: Optional[bool] = None,
    ) -> List[Dict]:
        """Pure sparse/keyword search.

        The principal goes through here as it does on the dense and hybrid
        paths. Leaving it off did not leak anything, because `keyword_search`
        refuses a policied collection that is handed no principal, but it
        refused a caller who had supplied one: `mode="sparse"` was unusable
        under a policy while dense and hybrid worked.

        Azure's semantic ranker reranks a keyword search only when the
        search asks for it. A store opened with ``semantic=True`` reranks
        hybrid searches by default, never keyword ones, so the same keyword
        search answers the same on a handle that holds the words index and on
        one that sends it to the service.
        """
        semantic = bool(semantic)
        storage = self._keyword_store(use_backend, semantic)
        if storage is not None:
            return self._store_keyword_search(storage, query, limit, filter, explain, semantic)
        if semantic:
            raise ConfigurationError(
                "Azure's semantic ranker runs in the service, and this search cannot be sent there: "
                "the collection's policy has rules the service cannot run"
            )
        # Highlights are left to the REST route: nothing here reads them, and
        # each is the whole document tokenized again.
        results = self._collection.keyword_search(
            query_text=query,
            limit=limit,
            filter=filter,
            principal=self._principal,
            include_highlights=False,
        )
        out = []
        for r in results.results:
            item = {"id": r.id, "score": r.score, "metadata": r.metadata}
            item.update(_judgement(r))
            if explain:
                item["explain"] = {"bm25": r.score}
            out.append(item)
        return out

    def _store_keyword_search(
        self,
        storage: Any,
        query: str,
        limit: int,
        filter: Optional[Dict],
        explain: bool,
        semantic: Optional[bool],
    ) -> List[Dict]:
        """The service's own keyword search, with Azure's semantic ranker when it is asked for."""
        engine_filter = self._collection._engine_filter(filter, self._policy, self._principal)
        extra: Dict[str, Any] = {"filter": engine_filter} if engine_filter is not None else {}
        if semantic is not None and type(storage).__name__ == "AzureSearchStorage":
            extra["semantic"] = semantic
        rows = storage.text_search(self.name, query, limit=limit, **extra)
        top = max((float(row[2]) for row in rows), default=0.0)
        candidates: List[Dict] = []
        for doc_id, data, score in rows:
            item: Dict[str, Any] = {
                "id": doc_id,
                "score": float(score),
                "metadata": _row_metadata(data),
                "text": data.get("text_content", ""),
                "found_by": ["keywords"],
            }
            if data.get("_vx_relevance") is not None:
                item["similarity"] = float(data["_vx_relevance"])
                item["similarity_kind"] = str(data.get("_vx_relevance_kind") or "reranker")
            elif top > 0:
                # As the local keyword search reports it: a share of the best hit.
                item["similarity"] = round(float(score) / top, 6)
                item["similarity_kind"] = "relative"
            if explain:
                item["explain"] = (
                    {"semantic": data["_semantic_score"]}
                    if data.get("_semantic_score") is not None
                    else {"bm25": float(score)}
                )
            candidates.append(item)
        return self._decide_backend_candidates(candidates, filter)[:limit]

    def _fuse(
        self,
        dense_results,
        sparse_results,
        fusion: str,
        alpha: float,
        explain: bool,
    ) -> Dict[str, Dict]:
        """Combine a dense and a sparse run into one score per document.

        "rrf" scores by rank, so it needs no calibration between a cosine
        similarity and a BM25 score and is the default. "weighted" min-max
        normalises each run and blends the two by ``alpha``; it lets a strong
        score count for more than a strong rank, at the cost of depending on
        the score scales. Either way, a document found by both runs gets a 15
        percent lift: agreement between two different signals is evidence.
        """
        k = 60
        dense = list(dense_results.results)
        sparse = list(sparse_results.results)
        scores: Dict[str, Dict] = {}

        def entry(r):
            if r.id not in scores:
                scores[r.id] = {
                    "rrf_dense": 0.0,
                    "rrf_sparse": 0.0,
                    "dense": None,
                    "bm25": None,
                    "metadata": r.metadata,
                    "vector": getattr(r, "vector", None),
                    "found_by": [],
                }
            return scores[r.id]

        # "Found by meaning" is being among the nearest with some likeness at
        # all. The prefetch is ten times the limit, and on a small collection
        # that is every chunk there is, which is not the same as being found.
        nearest = max(1, len(dense) // 10) if len(dense) >= 10 else len(dense)
        for rank, r in enumerate(dense):
            e = entry(r)
            e["rrf_dense"] = 1.0 / (k + rank + 1)
            e["dense"] = r.score
            e.update(_judgement(r))
            e["found_by"] = (
                ["meaning"] if rank < nearest and (r.relevance is None or r.relevance > 0.0) else []
            )
        for rank, r in enumerate(sparse):
            e = entry(r)
            e["rrf_sparse"] = 1.0 / (k + rank + 1)
            e["bm25"] = r.score
            e["found_by"] = [*e["found_by"], "keywords"]

        if fusion == "weighted":

            def minmax(values):
                present = [v for v in values if v is not None]
                lo, hi = (min(present), max(present)) if present else (0.0, 0.0)
                span = hi - lo
                return lambda v: 0.0 if v is None else (1.0 if span == 0 else (v - lo) / span)

            norm_dense = minmax(e["dense"] for e in scores.values())
            norm_bm25 = minmax(e["bm25"] for e in scores.values())
            for e in scores.values():
                e["norm_dense"] = norm_dense(e["dense"])
                e["norm_bm25"] = norm_bm25(e["bm25"])
                combined = alpha * e["norm_dense"] + (1 - alpha) * e["norm_bm25"]
                if e["dense"] is not None and e["bm25"] is not None:
                    combined *= 1.15
                e["combined"] = combined
        else:
            for e in scores.values():
                combined = alpha * e["rrf_dense"] + (1 - alpha) * e["rrf_sparse"]
                if e["rrf_dense"] > 0 and e["rrf_sparse"] > 0:
                    combined *= 1.15
                e["combined"] = combined

        if explain:
            for e in scores.values():
                parts = {
                    "dense": e["dense"],
                    "bm25": e["bm25"],
                    "rrf_dense": e["rrf_dense"],
                    "rrf_sparse": e["rrf_sparse"],
                    "fusion": fusion,
                    "alpha": alpha,
                    "fused": e["combined"],
                }
                if fusion == "weighted":
                    parts["norm_dense"] = e["norm_dense"]
                    parts["norm_bm25"] = e["norm_bm25"]
                e["explain"] = parts
        return scores

    def _candidates(
        self, scores: Dict[str, Dict], take: int, explain: bool, query_vector: Any = None
    ) -> List[Dict]:
        ordered = sorted(scores, key=lambda d: scores[d]["combined"], reverse=True)[:take]
        out = []
        for doc_id in ordered:
            e = scores[doc_id]
            item = {
                "id": doc_id,
                "score": e["combined"],
                "vector": e.get("vector"),
                "metadata": e.get("metadata", {}),
                "text": self._texts.get(doc_id, ""),
                "found_by": list(e.get("found_by") or []),
            }
            for key in ("similarity", "similarity_kind", "similarities"):
                if e.get(key) is not None:
                    item[key] = e[key]
            if item.get("similarity") is None:
                # Only the keywords found it, so nothing measured its meaning. Measure it.
                exact = (
                    self._collection._similarity_of(query_vector, doc_id)
                    if query_vector is not None
                    else None
                )
                if exact is not None:
                    item["similarity"], item["similarity_kind"] = exact, "similarity"
            if explain:
                item["explain"] = dict(e["explain"])
            out.append(item)
        return out

    def _decide_backend_candidates(
        self, candidates: List[Dict], filter: Optional[Dict]
    ) -> List[Dict]:
        """The policy's redaction rules and the caller's filter over what a
        backend handed back, with the withheld counts recorded.

        The scope rules ran inside the store, so nothing outside the scope is
        here to count; the disclosable count is exact.
        """
        from .core.types import Filter

        filter_obj = Filter.from_dict(filter) if filter else None
        if filter_obj is not None:
            candidates = [c for c in candidates if filter_obj.matches(c["metadata"])]
        if self._policy is None:
            return candidates
        kept: List[Dict] = []
        disclosable = 0
        undisclosable = 0
        for c in candidates:
            decision = self._policy.decide(self._principal or {}, c["metadata"])
            if decision.allowed:
                kept.append(c)
            elif decision.in_scope:
                disclosable += 1
            else:
                undisclosable += 1
        if self._policy_counts is not None:
            self._policy_counts.update(
                {
                    "examined": len(candidates),
                    "disclosable": disclosable,
                    "undisclosable": undisclosable,
                }
            )
        return kept

    def _cross_encode(
        self,
        query: str,
        candidates: List[Dict],
        limit: int,
        explain: bool = False,
        enabled: bool = True,
    ) -> List[Dict]:
        """Order candidates by the cross-encoder, recording its score when asked.

        A failure here returns the candidates as they were; the reranker is a
        refinement, not a requirement. ``enabled=False`` (search(rerank=False))
        skips it: the cross-encoder costs about 170 ms per long document on a
        CPU, so a hybrid search with limit=100 spent fifty seconds reranking
        three hundred candidates before this switch existed.
        """
        external = getattr(self, "_external_reranker", None)
        if not enabled or not candidates or (not self.reranker_model_name and external is None):
            return candidates[:limit]
        try:
            docs = [c["text"] for c in candidates]
            if external is not None:
                out = []
                judged = list(external.rerank(query, docs, top=limit))[:limit]
                for (idx, score), verdict in zip(
                    judged, _relevance.from_reranker(s for _, s in judged)
                ):
                    item = candidates[int(idx)]
                    item["judged"] = verdict
                    if explain:
                        item.setdefault("explain", {})["rerank"] = float(score)
                        item["explain"]["reranker"] = str(
                            getattr(external, "label", type(external).__name__)
                        )
                    out.append(item)
                return out
            reranker = self.reranker
            # The bundled RerankerEmbedder exposes score(query, documents). It
            # also has a rerank() with a different signature from the
            # HuggingFace wrapper's, and testing for "rerank" first called it
            # with an argument it does not take. That TypeError was swallowed,
            # so the reranker stage was a no-op for every bundled model.
            if hasattr(reranker, "score"):
                scores = [float(s) for s in reranker.score(query, docs)]
                ordered = sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)
            else:
                ranked = reranker.rerank(query, docs, limit=limit)
                ordered = [(candidates[idx], float(score)) for idx, score in ranked]
            out = []
            kept = ordered[:limit]
            for (item, score), verdict in zip(kept, _relevance.from_reranker(s for _, s in kept)):
                item["judged"] = verdict
                if explain:
                    item.setdefault("explain", {})["rerank"] = score
                out.append(item)
            return out
        except Exception as exc:
            # Keep the fused order, but say so once per reason: a silent
            # fallback is how the defect above went unnoticed.
            reason = f"{type(exc).__name__}: {exc}"
            emit = logger.debug if reason in self._rerank_warnings else logger.warning
            self._rerank_warnings.add(reason)
            emit("Cross-encoder reranking failed for %r, keeping fused order: %s", self.name, exc)
            return candidates[:limit]

    @staticmethod
    def _by_score(results: List[Dict]) -> List[Dict]:
        """Order by the score the caller will see.

        Graph mode reports a score and sorts by it on the path where the
        graph contributed a boost. The paths where it did not, an
        unavailable graph or a query no entity matched, returned the
        candidates in the order the reranker left them, which is not the
        order of the score beside them: a caller sorting or thresholding by
        ``score`` got a different list from the one it was handed.
        """
        return sorted(results, key=lambda r: r.get("score", 0.0), reverse=True)

    def _graph_search(
        self,
        query: str,
        query_vector: "np.ndarray",
        limit: int,
        filter: Optional[Dict] = None,
        diversity: float = 0.7,
        use_backend: bool = False,
        notes: Optional[List[str]] = None,
        explain: bool = False,
        rerank_candidates: bool = True,
        fusion: str = "rrf",
        alpha: float = 0.5,
    ) -> List[Dict]:
        """Ultimate search, reordered by what the knowledge graph knows.

        The graph is used to expand the query rather than to replace retrieval:
        entities matching the query bring in their neighbours, and documents
        mentioning any of those names are promoted. That keeps recall identical
        to ultimate mode, since nothing is filtered out, while letting a
        connection the text does not state directly change the ranking.
        """
        results = self._ultimate_search(
            query,
            query_vector,
            limit,
            filter,
            diversity,
            use_backend=use_backend,
            explain=explain,
            fusion=fusion,
            alpha=alpha,
            rerank_candidates=rerank_candidates,
        )

        try:
            graph_result = self.graph.search(query, query_vector=query_vector, k=limit)
        except GraphUnavailable as exc:
            # No graph to ask, which is ordinary: nothing added yet, or the
            # documents produced no entities. The vector results are the
            # answer, and the caller can see why in Results.degraded.
            if notes is not None:
                notes.append(f"graph unavailable: {exc}")
            logger.debug("Graph search unavailable for %r: %s", self.name, exc)
            return self._by_score(results)
        except Exception as exc:
            if notes is not None:
                notes.append(f"graph search failed: {exc}")
            # An unbuilt or empty graph is not a reason to fail a search, so the
            # fallback stays. Once per distinct reason, though, not once per
            # query: the package installs no logging handler, so
            # logging.lastResort puts warnings on stderr for anyone who never
            # configured logging.
            reason = f"{type(exc).__name__}: {exc}"
            emit = logger.debug if reason in self._graph_warnings else logger.warning
            self._graph_warnings.add(reason)
            emit(
                "Graph search unavailable for %r, using vector results only: %s",
                self.name,
                exc,
            )
            return self._by_score(results)

        related = set()
        for entity in graph_result.top_entities:
            related.add(entity.name.lower())
            related.update(alias.lower() for alias in getattr(entity, "aliases", ()) or ())

        if not related:
            return self._by_score(results)

        for item in results:
            text = (self._texts.get(item["id"], "") or "").lower()
            hits = sum(1 for name in related if name and name in text)
            # A small, bounded nudge. Retrieval still decides the candidates;
            # the graph only reorders them.
            boost = 0.05 * min(hits, 3)
            item["graph_entities"] = hits
            item["score"] = item.get("score", 0.0) + boost
            if explain:
                item.setdefault("explain", {})["graph_entities"] = hits
                item["explain"]["graph_boost"] = boost

        return self._by_score(results)

    def _hybrid_search(
        self,
        query: str,
        query_vector: np.ndarray,
        limit: int,
        filter: Optional[Dict] = None,
        use_backend: bool = False,
        explain: bool = False,
        fusion: str = "rrf",
        alpha: float = 0.5,
        rerank_candidates: bool = True,
        semantic: Optional[bool] = None,
    ) -> List[Dict]:
        """
        Hybrid search: Dense + Sparse + Reranker.

        Pipeline:
        1. Dense semantic search (vector similarity)
        2. Sparse keyword search (BM25)
        3. Fusion, by rank or by weighted score
        4. Cross-encoder reranking for final results
        """
        prefetch_limit = min(limit * 10, max(self._collection._count_raw(), 1))

        # Storage backends with their own hybrid search
        if use_backend and self._using_storage_backend:
            storage = (
                self.storage_backend._storage
                if hasattr(self.storage_backend, "_storage")
                else self.storage_backend
            )
            storage_class = storage.__class__.__name__

            # A backend's own hybrid search ranks inside the backend, where
            # no per-candidate decision happens. Under a policy it serves
            # only when it is the engine for that policy: the scope rules go
            # with the query, the redaction rules are decided here over what
            # came back. Otherwise the local path, slower and exact.
            coll = self._collection
            enforces = self._policy is None or coll._policy_pushable(storage)
            if (
                enforces
                and storage_class in ("OpenSearchStorage", "AzureSearchStorage")
                and hasattr(storage, "hybrid_search")
            ):
                engine_filter = coll._engine_filter(filter, self._policy, self._principal)
                extra: Dict[str, Any] = (
                    {"filter": engine_filter} if engine_filter is not None else {}
                )
                if semantic is not None and storage_class == "AzureSearchStorage":
                    extra["semantic"] = semantic
                # The service holds what it holds. The local count is only
                # what this process wrote, and a handle opened to search an
                # index somebody else filled counts nothing, so capping by it
                # asked the service for one candidate and a top 10 of one.
                prefetch_limit = limit * 10
                results_raw = storage.hybrid_search(
                    collection=self.name,
                    query_vector=query_vector,
                    query_text=query,
                    limit=prefetch_limit,
                    dense_weight=alpha,
                    sparse_weight=1 - alpha,
                    **extra,
                )
                # Which vector ranked what. The service fuses and does not
                # say, so an explanation asks each vector on its own.
                ranks: Dict[str, Dict[str, int]] = {}
                if explain and len(getattr(storage, "vector_names", lambda: ())()) > 1:
                    ranks = storage.vector_ranks(
                        self.name,
                        query_vector,
                        limit=prefetch_limit,
                        **({"filter": engine_filter} if engine_filter is not None else {}),
                    )
                # Backends store the caller's metadata flattened into the row
                # beside the standard fields; a nested "metadata" key is what
                # was read here before, so OpenSearch results carried none.
                candidates = []
                for r in results_raw:
                    item: Dict[str, Any] = {
                        "id": r[0],
                        "score": r[2],
                        "metadata": _row_metadata(r[1]),
                        "text": r[1].get("text_content", ""),
                    }
                    if r[1].get("_vx_relevance") is not None:
                        item["similarity"] = float(r[1]["_vx_relevance"])
                        item["similarity_kind"] = str(
                            r[1].get("_vx_relevance_kind") or "similarity"
                        )
                    if r[1].get("_vx_relevances"):
                        item["similarities"] = dict(r[1]["_vx_relevances"])
                    if r[1].get("_vx_matched_by"):
                        item["found_by"] = list(r[1]["_vx_matched_by"])
                    if explain and r[1].get("_semantic_score") is not None:
                        item["explain"] = {"semantic": r[1]["_semantic_score"]}
                    if explain and ranks.get(r[0]):
                        item.setdefault("explain", {})["vector_ranks"] = ranks[r[0]]
                    candidates.append(item)
                candidates = self._decide_backend_candidates(candidates, filter)
                # A service that reranked already has done the rerank stage;
                # the cross-encoder on top would be a second, weaker pass.
                reranked = (
                    bool(semantic)
                    if semantic is not None
                    else bool(getattr(storage, "reranks", False))
                )
                return self._cross_encode(
                    query, candidates, limit, explain, enabled=rerank_candidates and not reranked
                )
            elif self._policy is None and hasattr(self._collection, "hybrid_search"):
                query_sparse = self._embed_sparse(query)[0]
                results = self._collection.hybrid_search(
                    dense_query=query_vector,
                    sparse_query=query_sparse,
                    limit=prefetch_limit,
                    filter=filter,
                )
                candidates = [
                    {
                        "id": r.id,
                        "score": r.score,
                        "metadata": r.metadata,
                        "text": r.text or self._texts.get(r.id, ""),
                    }
                    for r in results.results
                ]
                return self._cross_encode(
                    query, candidates, limit, explain, enabled=rerank_candidates
                )

        if semantic:
            raise ConfigurationError(
                "Azure's semantic ranker runs in the service, and this search cannot be sent there: "
                "the collection's policy has rules the service cannot run"
            )

        dense_results = self._collection.search(
            query=query_vector,
            limit=prefetch_limit,
            filter=filter,
            include_vectors=True,
            principal=self._principal,
            **({"use_backend": False} if self._home() == "local" else {}),
        )
        # Ten times the limit, so no highlights: fusion reads ids and scores,
        # and making them was nine tenths of a hybrid query on SciFact.
        sparse_results = self._collection.keyword_search(
            query_text=query,
            limit=prefetch_limit,
            filter=filter,
            principal=self._principal,
            include_highlights=False,
        )
        scores = self._fuse(dense_results, sparse_results, fusion, alpha, explain)
        candidates = self._candidates(scores, min(limit * 3, len(scores)), explain, query_vector)
        return self._cross_encode(query, candidates, limit, explain, enabled=rerank_candidates)

    def _ultimate_search(
        self,
        query: str,
        query_vector: np.ndarray,
        limit: int,
        filter: Optional[Dict] = None,
        diversity: float = 0.7,
        use_backend: bool = False,
        explain: bool = False,
        fusion: str = "rrf",
        alpha: float = 0.5,
        rerank_candidates: bool = True,
    ) -> List[Dict]:
        """
        Ultimate search: Dense + Sparse + Reranker + ColBERT.

        1. Dense semantic search (vector similarity)
        2. Sparse keyword search (BM25)
        3. Fusion, by rank or by weighted score
        4. ColBERT late interaction scoring
        5. Cross-encoder reranking for final results
        """
        prefetch_limit = min(limit * 10, max(self._collection._count_raw(), 1))

        if use_backend and hasattr(self._collection, "ultimate_search"):
            query_sparse = self._embed_sparse(query)[0]
            query_late_interaction = self._embed_late_interaction([query])[0]
            results = self._collection.ultimate_search(
                dense_query=query_vector,
                sparse_query=query_sparse,
                late_interaction_query=query_late_interaction,
                limit=prefetch_limit,
                filter=filter,
            )
            candidates = [
                {
                    "id": r.id,
                    "score": r.score,
                    "metadata": r.metadata,
                    "text": r.text or self._texts.get(r.id, ""),
                }
                for r in results.results
            ]
            return self._cross_encode(query, candidates, limit, explain, enabled=rerank_candidates)

        dense_results = self._collection.search(
            query=query_vector,
            limit=prefetch_limit,
            filter=filter,
            include_vectors=True,
            principal=self._principal,
            **({"use_backend": False} if self._home() == "local" else {}),
        )
        # Ten times the limit, so no highlights: fusion reads ids and scores,
        # and making them was nine tenths of a hybrid query on SciFact.
        sparse_results = self._collection.keyword_search(
            query_text=query,
            limit=prefetch_limit,
            filter=filter,
            principal=self._principal,
            include_highlights=False,
        )
        scores = self._fuse(dense_results, sparse_results, fusion, alpha, explain)
        candidates = self._candidates(scores, min(limit * 5, len(scores)), explain)

        # ColBERT late interaction scoring
        if candidates and self.late_interaction_model_name:
            try:
                colbert = self.late_interaction
                doc_texts = [c["text"] for c in candidates]
                if hasattr(colbert, "embed_query") and hasattr(colbert, "embed_documents"):
                    query_emb = colbert.embed_query(query)
                    doc_embs = colbert.embed_documents(doc_texts)
                    colbert_scores = [colbert.score(query_emb, doc_emb) for doc_emb in doc_embs]
                elif hasattr(colbert, "embed"):
                    query_emb = list(colbert.embed([query]))[0]
                    doc_embs = list(colbert.embed(doc_texts))
                    colbert_scores = []
                    for doc_emb in doc_embs:
                        sim = np.dot(query_emb, doc_emb.T)
                        colbert_scores.append(float(np.sum(np.max(sim, axis=1))))
                else:
                    colbert_scores = colbert.score(query, doc_texts)

                top = max(colbert_scores) if colbert_scores else 0.0
                for i, c in enumerate(candidates):
                    fused = c["score"]
                    raw = colbert_scores[i] if i < len(colbert_scores) else 0.0
                    normalised = raw / top if top > 0 else 0.0
                    c["score"] = 0.6 * fused + 0.4 * normalised
                    if explain:
                        c.setdefault("explain", {})["colbert"] = float(raw)
                        c["explain"]["colbert_normalised"] = normalised
                candidates.sort(key=lambda x: x["score"], reverse=True)
            except Exception as exc:
                logger.debug("ColBERT scoring failed, keeping fused order: %s", exc)

        return self._cross_encode(query, candidates, limit, explain, enabled=rerank_candidates)

    def _rerank(
        self,
        query: str,
        query_vector: np.ndarray,
        results: List[Dict],
        method: str,
        limit: int,
        diversity: float,
        explain: bool = False,
    ) -> List[Dict]:
        """Apply reranking to results."""
        if method == "cross-encoder":
            # The bundled cross-encoder, so this works offline in dense mode
            # too. It used to reach for sentence-transformers, which is not a
            # dependency, and fail.
            if self.reranker_model_name is None:
                self.reranker_model_name = "L12"
            for r in results:
                r.setdefault("text", self._texts.get(r["id"], ""))
            return self._cross_encode(query, results, limit, explain)

        from .core.advanced_search import Reranker, RerankConfig, RerankMethod

        method_map = {"mmr": RerankMethod.MMR, "exact": RerankMethod.EXACT}
        if method not in method_map:
            raise ValueError(f"rerank must be 'mmr', 'exact' or 'cross-encoder', got {method!r}")

        # Both of these rerank by vector, and the candidates arriving here
        # carry none: the dense path does not ask for them, because paying
        # for vectors on every search to serve the few that rerank would be
        # the wrong trade. Fetching them by id for just these candidates is
        # what get_vector_fn is for, and leaving it out is what made 'exact'
        # a silent no-op and 'mmr' return nothing at all.
        def vector_for(id_: str):
            point = self._collection._get_raw(id_)
            return None if point is None else point.vector

        reranker = Reranker(RerankConfig(method=method_map[method], diversity_lambda=diversity))
        return reranker.rerank(
            query_vector=query_vector,
            candidates=results,
            query_text=None,
            limit=limit,
            get_vector_fn=vector_for,
        )

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def delete(self, ids: Union[str, List[str]]) -> Vectrix:
        """
        Delete documents by ID.

        Example:
            >>> db.delete("doc_id")
            >>> db.delete(["id1", "id2"])
        """
        if isinstance(ids, str):
            ids = [ids]

        self._collection.delete(ids=ids)

        for id_ in ids:
            self._texts.pop(id_, None)
        for side in self._named.values():
            side.delete(list(ids))

        self._stamp_build()
        return self

    def clear(self) -> Vectrix:
        """
        Clear all documents from collection.

        Example:
            >>> db.clear()
        """
        # The sources it keeps up with stay, and the next refresh writes
        # everything they give again: deleting the collection forgets them,
        # and what they had written is gone with the documents.
        from .sources import kept_sources, restore_sources

        try:
            sources_store = getattr(self._db, "sources_store", None)
            kept = kept_sources(sources_store, self.name) if sources_store is not None else []
        except Exception as exc:  # the store's own error: clearing goes ahead without them
            logger.warning(
                "the sources of %s could not be read before clearing it: %s", self.name, exc
            )
            sources_store, kept = None, []
        self._db.delete_collection(self.name)
        if kept:
            restore_sources(sources_store, self.name, kept)
        mode_tags = [self.default_mode.capitalize()]
        self._collection = self._db.create_collection(
            name=self.name,
            dimension=self.dimension,
            metric="cosine",
            description=self.description,
            enable_text_index=True,
            tags=mode_tags,
            # The same boosts and language as the collection it replaces: a
            # cleared German collection recreated with English stemming would
            # be a different index under the same name.
            text_boosts=self.text_boosts,
            shard_size=self.shard_size,
            text_language=self.text_language,
        )
        self._texts.clear()
        for side in self._named.values():
            side.clear()
        # The list of named vectors belongs to the collection, not to what was in it.
        if self._named:
            _coll(self).set_meta("named_dense_vectors", json.dumps(list(self._named)))
        # So do its model and its policy. Without them a reopen picks another
        # model, and the documents added next are open to every caller.
        _coll(self).set_meta("embedding_model", self.model_name)
        self._setup_policy_from_meta(self._policy)
        self._stamp_build()
        return self

    def _count_all(self) -> int:
        """Every document, policy or not. For the host and for internals."""
        return self._collection._count_raw()

    def count(self) -> int:
        """
        How many documents there are.

        Through a principal view this counts what that principal can see,
        because the total is a fact about documents they may not know exist:
        a walled analyst watching the number move learns that a client they
        were refused is being written to. The host's own handle still counts
        the collection.

        That costs a pass over the collection, deciding each document, where
        an unpoliced count is a single SELECT. A backend that pushes filters
        into SQL could answer it in one query; see ROADMAP.

        Example:
            >>> print(db.count())
        """
        if self._policy is None or self._principal is None:
            # No policy, or the host's own handle. A principal only ever
            # holds a view, so this branch is never a principal.
            return self._count_all()
        principal = self._principal
        return sum(1 for _ in _coll(self).iter_documents(principal=principal))

    def get(self, ids: Union[str, List[str]]) -> List[Result]:
        """
        Get documents by ID.

        Always a list, whether given one id or many, so a document that is
        not there is an empty list rather than None. For a single lookup
        that answers None on a miss, use :meth:`get_one`.

        Example:
            >>> docs = db.get(["id1", "id2"])
            >>> one = db.get_one("id1")
        """
        if isinstance(ids, str):
            ids = [ids]

        # Collection.get takes one id; this passed a list under the wrong
        # keyword and raised TypeError on every call.
        # Collection decides. A denial and a miss are the same answer,
        # because telling a caller an id exists but is not theirs confirms
        # the document.
        results = [
            p for p in self._collection.get_batch(ids, principal=self._principal) if p is not None
        ]

        return [
            Result(id=r.id, text=self._texts.get(r.id, ""), score=1.0, metadata=r.metadata)
            for r in results
        ]

    def get_one(self, id: str) -> Optional[Result]:
        """One document by id, or None if there is no such document.

        ``get()`` takes one id or many and always answers with a list, so a
        miss is ``[]`` rather than ``None`` and a hit has to be unwrapped.
        That is the right shape for a batch and the wrong one for a single
        lookup, which is what this is for.

        Example:
            >>> doc = db.get_one("doc_id")
            >>> if doc is not None:
            ...     print(doc.text)
        """
        found = self.get(id)
        return found[0] if found else None

    def similar(self, id: str, limit: int = 10) -> Results:
        """
        Find similar documents to a given document.

        Example:
            >>> similar = db.similar("doc_id", limit=5)
        """
        started = time.time()

        def elapsed_ms() -> float:
            return self._pad_to_floor((time.time() - started) * 1000)

        def empty_result() -> Results:
            return Results(items=[], query=f"similar to {id}", mode="dense", time_ms=elapsed_ms())

        # Authorization before the lookup, not after. Checking whether the id
        # exists first meant an unknown id skipped the gate, so whether this
        # raised was itself an answer to "is that a document?".
        policy = self._policy
        policy_filter = None
        principal = None
        if policy is not None:
            principal = self._require_principal("similar()")
            policy_filter = policy.compile(principal)
            if policy_filter is None:
                return empty_result()

        point = self._collection.get(id, principal=principal)
        if point is None:
            return empty_result()

        if policy is not None and principal is not None:
            # Pivoting off a document is a read of that document. Holding an
            # id is not an entitlement, so one the principal may not see
            # answers the same way as one that is not there: the neighbours of
            # a walled document are the documents most likely to be about the
            # same client.
            if not policy.decide(principal, point.metadata or {}).allowed:
                return empty_result()

        vector = point.vector
        if vector is None:
            raise ValueError(f"Document {id} has no vector")

        results = self._dense_search(vector, limit + 1, policy_filter)

        # Remove the query document itself
        results = [r for r in results if r["id"] != id][:limit]

        return Results(
            items=[
                Result(
                    id=r["id"],
                    text=self._texts.get(r["id"], ""),
                    score=r["score"],
                    metadata=r.get("metadata", {}),
                )
                for r in results
            ],
            query=f"similar to {id}",
            mode="dense",
            time_ms=elapsed_ms(),
        )

    # ------------------------------------------------------------------
    # Conversation memory
    # ------------------------------------------------------------------

    @property
    def memory(self):
        """Conversation memory over this collection.

        Recency-weighted recall, feedback that reshapes ranking, corrections
        that supersede, and a budgeted context block. See ``vectrixdb.memory``
        for the knobs; the four methods below are the everyday surface.
        """
        if self._memory is None:
            from .memory import ConversationMemory

            self._memory = ConversationMemory(self)
        return self._memory

    def remember(
        self, text: str, session: Optional[str] = None, role: str = "user", **kwargs
    ) -> str:
        """Store a conversation turn or a fact. Returns the memory id.

        Example:
            >>> db.remember("I prefer dark mode", session="u1", role="user")
            >>> db.remember("Ships to Canada only", pinned=True)
        """
        return self.memory.remember(text, session=session, role=role, **kwargs)

    def recall(self, query: str, session: Optional[str] = None, **kwargs) -> Results:
        """Memories relevant to ``query``, weighted by recency and feedback.

        Example:
            >>> db.recall("theme preference", session="u1", token_budget=300)
        """
        self._refuse_under_policy(
            "recall()",
            "conversation memory searches through its own path, which the policy does "
            "not reach, so no principal makes it safe. Keep memory in a collection of "
            "its own.",
            host_allowed=False,
        )
        return self.memory.recall(query, session=session, **kwargs)

    def feedback(
        self, id: str, outcome: str, correction: Optional[str] = None, **kwargs
    ) -> Optional[str]:
        """Grade a recalled memory: "useful", "dead_end" or "corrected".

        Example:
            >>> db.feedback(mem_id, "corrected", correction="I prefer light mode")
        """
        return self.memory.feedback(id, outcome, correction=correction, **kwargs)

    def forget(self, session: Optional[str] = None, **kwargs) -> int:
        """Delete memories: by ids, or by kind, session and age. Returns the count.

        Example:
            >>> db.forget(session="u1", older_than=90)          # turns older than 90 days
            >>> db.forget(session="u1", kinds=("fact",), superseded=True)
        """
        forgotten = self.memory.forget(session=session, **kwargs)
        if forgotten:
            self._stamp_build()
        return forgotten

    def consolidate(self, session: Optional[str], summarize, **kwargs):
        """Turn a session's old turns into standalone facts with your LLM, then drop the turns.

        Example:
            >>> report = db.consolidate("u1", summarize=my_llm_summary, keep_recent=6)
            >>> len(report.fact_ids), report.turns_removed
        """
        return self.memory.consolidate(session, summarize, **kwargs)

    def context(self, query: str, session: Optional[str] = None, **kwargs):
        """Pinned facts, recent turns and relevant memories under one token budget.

        Example:
            >>> block = db.context("what did we decide?", session="u1", token_budget=1200)
            >>> prompt = block.text + "\n\n" + user_message
        """
        self._refuse_under_policy(
            "context()",
            "conversation memory searches through its own path, which the policy does "
            "not reach, so no principal makes it safe. Keep memory in a collection of "
            "its own.",
            host_allowed=False,
        )
        return self.memory.context(query, session=session, **kwargs)

    def close(self):
        """Save and close. Safe to call twice; any use afterwards raises.

        The index is already saved on every add(), so this is about the other
        half: releasing the SQLite connections and, in graph mode, the
        pipeline. Nothing relies on __del__, which runs at an unpredictable
        time or, at interpreter exit, not at all.
        """
        if self._db is None:
            return
        try:
            if self._graph_pipeline is not None:
                self._graph_pipeline.close()
            if self.path and not isinstance(self._collection, _Closed):
                self._collection.save()
        finally:
            for side in list(getattr(self, "_named", {}).values()):
                try:
                    side.close()
                except Exception:  # pragma: no cover
                    pass
            for store in (getattr(self, "_parents", None), getattr(self, "_embed_cache", None)):
                if store is not None:
                    try:
                        store.close()
                    except Exception:  # pragma: no cover
                        pass
            # add() consults the cache before it reaches the closed collection;
            # without this it died with "closed database" instead of the
            # ConfigurationError that names the problem.
            self._embed_cache = None
            self._sources = None
            self._db.close()
            self._db = None
            self._collection = _Closed(self.name)
            self._graph_pipeline = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __len__(self):
        return self.count()

    def __repr__(self):
        if self._db is None:
            return f"Vectrix('{self.name}', closed)"
        return f"Vectrix('{self.name}', {self._count_all()} docs, model='{self.model_name}')"

    def _repr_html_(self) -> str:
        if self._db is None:
            return f"<code>Vectrix('{_html.escape(self.name)}')</code> closed"
        return (
            f"<div><code>Vectrix('{_html.escape(self.name)}')</code> "
            f"<b>{self.count()}</b> documents, mode <code>{self.default_mode}</code>, "
            f"model <code>{_html.escape(str(self.model_name))}</code>, "
            f"path <code>{_html.escape(str(self.path))}</code></div>"
        )


# ============================================================================
# CREATE AND OPEN
# ============================================================================
#
# INPUT   a name and a path
# OUTPUT  a new collection; an existing one
#
# Two functions for callers who prefer them to the constructor.


def create(name: str = "default", **kwargs) -> Vectrix:
    """
    Create a new Vectrix collection.

    Example:
        >>> db = create("my_docs")
        >>> db.add(["text 1", "text 2"])
    """
    return Vectrix(name, **kwargs)


def open(name: str = "default", path: str = "./vectrixdb_data") -> Vectrix:
    """
    Open an existing Vectrix collection.

    Example:
        >>> db = open("my_docs")
        >>> results = db.search("query")
    """
    return Vectrix(name, path=path)


# ============================================================================
# THE ONE-LINER
# ============================================================================
#
# INPUT   texts and a query
# OUTPUT  the texts indexed and searched at once
#
# For a script that wants an answer and nothing kept.


def quick_search(texts: List[str], query: str, limit: int = 5) -> Results:
    """
    One-liner: Index texts and search immediately.

    Example:
        >>> results = quick_search(
        ...     texts=["Python is great", "Java is verbose", "Rust is fast"],
        ...     query="programming language"
        ... )
        >>> print(results.top.text)
    """
    # In memory and closed after: nothing written to the working directory,
    # and two calls at once no longer clear each other's collection.
    db = Vectrix("_quick_search", path=None)  # type: ignore[arg-type]
    try:
        db.add(texts)
        return db.search(query, limit=limit)
    finally:
        db.close()
