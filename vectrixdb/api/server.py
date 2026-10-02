"""
VectrixDB API Server - FastAPI REST API.

Provides a REST interface to VectrixDB with:
- Collection management
- Vector CRUD operations
- Search endpoints (vector, keyword, hybrid)
- Cache management
- Resource monitoring
- WebSocket for real-time updates

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

import logging
import hmac
import os
import uuid
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from .._time import utcnow, utcnow_iso
from pathlib import Path
from typing import Any, Optional, Set, List

from fastapi import APIRouter
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from ..exceptions import ConfigurationError, InvalidCollectionName, PolicyError
from ..chunk_store import describe as describe_chunk_store
from ..core.database import VectrixDB
from .. import __version__
from ..core.types import DistanceMetric
from ..core.storage import StorageBackend, StorageConfig
from ..core.cache import CacheBackend, CacheConfig
from ..core.scaling import ScalingStrategy, ScalingConfig


logger = logging.getLogger(__name__)


__all__ = [
    "app",
    "router",
    "create_app",
    "run_server",
]

# Global database instance
_db: Optional[VectrixDB] = None


# ============================================================================
# THE LIVE SOCKET: connections, and events sent to the dashboard
# ============================================================================
#
# INPUT   a WebSocket the dashboard opens; an event's name and its data, from
#         anywhere in the server
# OUTPUT  the connection kept while it is open and dropped when it closes; the
#         event sent to every open dashboard
#
# So a page updates when a collection changes, without polling.


class ConnectionManager:
    """Manages WebSocket connections for real-time dashboard updates."""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        """Accept a new WebSocket connection."""
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection."""
        self.active_connections.discard(websocket)

    async def broadcast(self, event: str, data: Optional[dict] = None):
        """Broadcast an event to all connected clients."""
        message = json.dumps({"event": event, "data": data or {}, "timestamp": utcnow_iso()})

        # Send to all connections, remove dead ones
        dead_connections = set()
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                dead_connections.add(connection)

        # Clean up dead connections
        self.active_connections -= dead_connections

    @property
    def connection_count(self) -> int:
        return len(self.active_connections)


# Global connection manager
ws_manager = ConnectionManager()


async def emit_event(event: str, data: Optional[dict] = None):
    """Helper to emit WebSocket events from anywhere in the server."""
    await ws_manager.broadcast(event, data)


# ============================================================================
# THE DOOR: the keys, what is public, and what a route reads a collection as
# ============================================================================
#
# INPUT   the api-key header; the request's path and method; a collection's
#         name
# OUTPUT  the full key and the read-only key, and whether one is configured;
#         the paths anyone may reach and the methods a read-only key may use;
#         the dashboard's files, always revalidated; the collection a route is
#         for, or a refusal, and the principal to read it as; the audit sink
#         and one decision record for a search a policy judged; results
#         snipped, judged and indexed; a build id for one write
#
# Qdrant-style: one key for everything, one for reading, and with sign-in on
# the door is the sign-in module's. A collection that is private to others
# answers as one that does not exist.


# API keys - read at runtime to allow setting before server start. Each may
# also come from a file, VECTRIXDB_API_KEY_FILE, or be given as its SHA-256.
def get_api_key():
    from .signin import get_api_key as current

    return current()


def get_read_only_key():
    from .signin import get_read_only_key as current

    return current()


def _full_key_configured() -> bool:
    from .signin import full_key_configured

    return full_key_configured()


# Routes that don't require authentication
PUBLIC_PATHS = {
    "/",
    "/auth/status",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
}

# Read-only methods
READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


# The door itself is in .signin: who is asking, the forgery check, the role,
# and the access log. With sign-in off it behaves as this class always did,
# so the name stays for anything that imported it.
from . import chunk_source  # noqa: E402
from .replies import message_of, refusal  # noqa: E402
from .signin import AccessMiddleware as ApiKeyAuthMiddleware  # noqa: E402
from .signin import (
    SignInRuntime,
    caller_of,
    decide_for,
    presented_key,
    principal_of,
    reads_are_open,
    runtime_of,
    sees_content,
    session_of,
)  # noqa: E402
from .signin import _hashed as _hashed_key  # noqa: E402
from ..signin.keys import key_matches  # noqa: E402


def _ws_has_a_key(websocket: WebSocket) -> bool:
    """Whether the WebSocket carries the full or the read-only key, as a request would."""
    given = presented_key(websocket)
    if not given:
        return False
    return key_matches(
        given, get_api_key(), _hashed_key("VECTRIXDB_API_KEY_SHA256")
    ) or key_matches(given, get_read_only_key(), _hashed_key("VECTRIXDB_READ_ONLY_API_KEY_SHA256"))


from ..signin import roles  # noqa: E402


import time


class _DashboardFiles(StaticFiles):
    """The dashboard's files, always revalidated.

    Without a Cache-Control header a browser guesses how long a file is good
    for, and after an upgrade it went on showing the old page against the
    new server. ``no-cache`` does not mean never cached: the browser keeps
    its copy and asks whether it is still current, and the ETag that
    StaticFiles already sends makes the usual answer a 304 with no body.

    With a company's brand set, the page itself is sent with the brand
    already in it, so it never shows VectrixDB's name and colour first. Behind
    a gateway that publishes the server under paths of its own, it is sent
    with the map too, so each call it makes goes where its route is published.
    """

    brand: Any = None
    gateway: Any = None

    async def get_response(self, path: str, scope: Any) -> Any:
        brand, gateway = self.brand, self.gateway
        branded = path in ("", ".", "index.html") and (
            (brand is not None and brand.custom) or (gateway is not None and gateway.dressed)
        )
        asked = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if branded:
            # StaticFiles answers "is my copy current?" against the plain file,
            # which knows nothing of the brand: a browser that had the page
            # before the brand was set would be told to keep it. So it is asked
            # without the question, and the branded page answers it below.
            stale = (b"if-none-match", b"if-modified-since")
            scope = {
                **scope,
                "headers": [(k, v) for k, v in scope.get("headers", []) if k.lower() not in stale],
            }
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        if branded and getattr(response, "status_code", 200) == 200:
            page = (Path(self.directory or ".") / "index.html").read_text(encoding="utf-8")
            if brand is not None:
                page = brand.render_index(
                    page,
                    visible=gateway.visible if gateway is not None and gateway.dressed else None,
                )
            if gateway is not None:
                page = gateway.render_index(page)
            import hashlib

            tag = '"' + hashlib.sha256(page.encode("utf-8")).hexdigest()[:32] + '"'
            headers = {"Cache-Control": "no-cache", "ETag": tag}
            if asked.get("if-none-match") == tag:
                from starlette.responses import Response as Plain

                return Plain(status_code=304, headers=headers)
            from starlette.responses import HTMLResponse

            return HTMLResponse(page, headers=headers)
        return response


def _servable(db, name: str):
    """The collection this route is for, or a refusal.

    Two refusals in one place, because they were written out twenty-one times
    and a rule with twenty-one copies has somewhere to hide. A name nothing
    answers to is a 404. A collection carrying an entitlement policy is a
    403: this API resolves no principals, so there is no honest answer it can
    give about one, and that includes its size, which moves when documents it
    must not confirm are written.
    """
    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")
    if collection.policy is not None:
        raise PolicyError(f"collection {name!r} carries an entitlement policy")
    return collection


def _servable_as(db, name: str, request: Request):
    """The collection and the principal to read it as.

    ``_servable`` refuses a collection that carries a policy, because a bare
    API key is nobody in particular and the policy has nobody to judge. A
    person who signed in is somebody: their principal was settled at sign-in
    from what the identity provider vouches for, or from what an admin wrote
    beside their address. So for them the collection is served, and the
    policy decides document by document what comes back. For a collection
    with no policy this is ``_servable`` and the principal is None.
    """
    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")
    if collection.policy is None:
        return collection, None
    principal = principal_of(request)
    if principal is None:
        raise PolicyError(f"collection {name!r} carries an entitlement policy")
    return collection, principal


_decision_sink: Any = None
_decision_sink_for: Any = None


def audit_where(env: Optional[dict] = None) -> Optional[str]:
    """Where this server records its decisions: see :func:`vectrixdb.audit.audit_where`."""
    from ..audit import audit_where as where

    return where(env)


def _sink(request: Request) -> Any:
    """The audit sink the server writes decisions to, when an operator has named one."""
    global _decision_sink, _decision_sink_for
    path = audit_where()
    if not path:
        return None
    key = os.environ.get("VECTRIXDB_AUDIT_QUERY_KEY", "").strip()
    if not key:
        runtime = runtime_of(request)
        if runtime is None:
            return None
        # No key of its own: one derived from the sign-in secret, which is
        # likewise somewhere the audit file cannot reach.
        import hashlib

        key = hashlib.sha256(
            ("vectrixdb audit query key|" + runtime.config.secrets[0]).encode()
        ).hexdigest()
    if _decision_sink is None or _decision_sink_for != (path, key):
        from ..audit import DENY, audit_sink_at

        days = os.environ.get("VECTRIXDB_AUDIT_RETAIN_DAYS", "").strip()
        _decision_sink = audit_sink_at(
            path,
            query_key=key.encode(),
            on_failure=DENY,
            retain_days=int(days) if days.isdigit() else None,
        )
        _decision_sink_for = (path, key)
    return _decision_sink


def _record_decision(
    request: Request,
    collection: Any,
    principal: Optional[dict],
    query: str,
    results: Any,
    started: float,
) -> None:
    """One decision record for a search a policy judged, the same record the library writes."""
    sink = _sink(request)
    if sink is None or principal is None or collection.policy is None:
        return
    from ..audit import AuditUnavailable, RetrievalRecord

    caller = caller_of(request)
    policy = collection.policy
    disclosable = getattr(results, "policy_withheld_disclosable", None)
    undisclosable = getattr(results, "policy_withheld_undisclosable", None)
    kept = list(results.results)
    record = RetrievalRecord(
        decision_id=RetrievalRecord.new_id(),
        decided_at=utcnow(),
        collection=collection.name,
        backend=getattr(collection, "_backend", None),
        pushdown_mode=collection.pushdown_mode.value,
        index_build_id=collection.get_meta("index_build_id"),
        principal_id=caller.who if caller else None,
        principal_type="user",
        principal_snapshot=dict(principal),
        policy_fingerprint=policy.fingerprint,
        policy_version=policy.version,
        rules_evaluated=[r.describe() for r in policy.rules],
        rules_denied=[],
        query_fingerprint=sink.fingerprint_query(query),
        candidates_examined=getattr(results, "policy_candidates", None),
        candidates_exhausted=getattr(results, "policy_candidates", None) is not None,
        results_returned=len(kept),
        result_ids=[r.id for r in kept],
        withheld_disclosable=disclosable,
        withheld_undisclosable=undisclosable,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        outcome=policy.outcome(len(kept), disclosable or 0, undisclosable or 0).value,
    )
    try:
        sink.write(record)
    except AuditUnavailable as exc:
        # The library's rule, kept here: a decision that cannot be recorded is not served.
        raise HTTPException(
            status_code=503,
            detail="the audit trail cannot be written, so this search was not served",
        ) from exc


_TEXT_KEYS = ("text", "text_content", "content")


def _snipped(results_dict: dict, request: Request) -> dict:
    """With ``?snippet=N``, each result's text cut to N characters, here and not in the page.

    A result list is for choosing which chunk to open. Sent whole, every chunk
    on the list has been disclosed whether or not anybody read it, and a
    "show text" button in the page would be decoration. Cut on the server, the
    rest of a chunk is a second request, for one chunk, that is recorded.
    Without the parameter nothing changes: a program that searches wants the text.
    """
    raw = request.query_params.get("snippet")
    if not raw:
        return results_dict
    try:
        limit = max(40, min(2000, int(raw)))
    except ValueError:
        return results_dict
    for result in results_dict.get("results", []):
        cut = False
        for holder in (result, result.get("metadata") or {}, result.get("payload") or {}):
            for key in _TEXT_KEYS:
                value = holder.get(key)
                if isinstance(value, str) and len(value) > limit:
                    holder[key] = value[:limit].rstrip() + "…"
                    cut = True
        # A keyword result carries its text as highlights, and one highlight can be the whole chunk.
        marks = result.get("highlights")
        if isinstance(marks, list):
            kept = []
            for mark in marks[:3]:
                if isinstance(mark, str) and len(mark) > limit:
                    mark, cut = mark[:limit].rstrip() + "…", True
                kept.append(mark)
            cut = cut or len(marks) > 3
            result["highlights"] = kept
        result["snipped"] = cut
    return results_dict


def _judged(results_dict: dict, principal: Optional[dict]) -> dict:
    """What a policied search says back. How many were looked at is how many exist, so it goes."""
    if principal is not None:
        results_dict.pop("total_searched", None)
    return results_dict


#: What a listing may say about a chunk. None of it is the chunk's own words.
_INDEX_KEYS = (
    ("source", ("_vx_citation", "_vx_source", "source", "_vx_doc")),
    ("document", ("_vx_doc_id", "_vx_doc")),
    ("page", ("_vx_page", "page")),
)


def _index_rows(collection: Any, ids: List[str], principal: Optional[dict]) -> List[dict]:
    rows = []
    for point_id in ids:
        point = chunk_source.point(collection, point_id, principal)
        metadata = (point.metadata or {}) if point is not None else {}
        row: dict = {"id": point_id, "quality": metadata.get("_vx_quality")}
        for name, keys in _INDEX_KEYS:
            row[name] = next((metadata[k] for k in keys if metadata.get(k) not in (None, "")), None)
        rows.append(row)
    return rows


def _mint_build(metadata: List[Any]) -> str:
    """A build id for one write, stamped on every chunk it lands.

    The library's ``Vectrix.add`` does this so a chunk joins to the write that
    stored it; the server called ``Collection.add`` directly and its chunks
    carried nothing, so the lineage pages had a hole exactly where the REST
    writes were. Same format, same key, same commit after the save.
    """
    build = "build_" + uuid.uuid4().hex[:16]
    for i, m in enumerate(metadata):
        if not isinstance(m, dict):
            metadata[i] = m = {}
        m["_vx_build"] = build
    return build


def get_db() -> VectrixDB:
    """Get the database instance."""
    global _db
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


# ============================================================================
# THE REQUEST SHAPES
# ============================================================================
#
# INPUT   the JSON bodies the routes take
# OUTPUT  each as a pydantic model, so a body that is not the shape asked for
#         is 422 with the field named
#
# One model a route, v1 and v2 side by side.


class CreateCollectionRequest(BaseModel):
    """Request to create a collection."""

    name: str = Field(..., min_length=1, max_length=100)
    dimension: int = Field(..., gt=0, le=65536)
    metric: str = Field(default="cosine")
    description: Optional[str] = None
    tags: Optional[List[str]] = Field(
        default=None, description="Capability tags: Dense, Sparse, Hybrid, Ultimate, Graph"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "documents",
                "dimension": 384,
                "metric": "cosine",
                "tags": ["Dense", "Hybrid"],
            }
        }
    }


class PointData(BaseModel):
    """A single point."""

    id: str
    vector: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)


class AddPointsRequest(BaseModel):
    """Request to add points."""

    points: list[PointData]

    model_config = {
        "json_schema_extra": {
            "example": {
                "points": [
                    {"id": "doc1", "vector": [0.1, 0.2, 0.3], "metadata": {"title": "Document 1"}}
                ]
            }
        }
    }


class SearchRequest(BaseModel):
    """Search request."""

    query: list[float]
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    include_vectors: bool = False
    score_threshold: Optional[float] = None
    use_cache: bool = True

    model_config = {
        "json_schema_extra": {
            "example": {"query": [0.1, 0.2, 0.3], "limit": 10, "filter": {"category": "tech"}}
        }
    }


class HybridSearchRequest(BaseModel):
    """Hybrid search request (vector + keyword)."""

    query: list[float]
    query_text: str
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    vector_weight: float = Field(default=0.7, ge=0.0, le=1.0)
    text_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    include_vectors: bool = False
    include_highlights: bool = True

    model_config = {
        "json_schema_extra": {
            "example": {
                "query": [0.1, 0.2, 0.3],
                "query_text": "machine learning",
                "limit": 10,
                "vector_weight": 0.7,
                "text_weight": 0.3,
            }
        }
    }


class KeywordSearchRequest(BaseModel):
    """Keyword search request (full-text)."""

    query_text: str
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    include_highlights: bool = True

    model_config = {
        "json_schema_extra": {"example": {"query_text": "machine learning AI", "limit": 10}}
    }


class TextSearchRequest(BaseModel):
    """Text-based semantic search request.

    The server embeds the text with the model the library writes vectors
    with by default, bge-small-en-v1.5, so no vectors need computing.
    """

    query_text: str = Field(..., description="Search query text (auto-embedded)")
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    include_vectors: bool = False
    score_threshold: Optional[float] = None
    use_cache: bool = True
    rerank: bool = Field(
        default=False,
        description=(
            "Re-rank the top candidates with the bundled cross-encoder. The dashboard "
            "used to send this and the server ignored it, so its Rerank mode was dense search."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "query_text": "what is machine learning?",
                "limit": 10,
                "filter": {"category": "tech"},
            }
        }
    }


class SparseVectorData(BaseModel):
    """Sparse vector representation."""

    indices: list[int] = Field(..., description="Non-zero dimension indices")
    values: list[float] = Field(..., description="Corresponding values")

    model_config = {
        "json_schema_extra": {"example": {"indices": [0, 42, 100], "values": [0.5, 1.2, 0.8]}}
    }


class SparseSearchRequest(BaseModel):
    """Sparse vector search request."""

    query: SparseVectorData
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    score_threshold: Optional[float] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "query": {"indices": [0, 42, 100], "values": [0.5, 1.2, 0.8]},
                "limit": 10,
            }
        }
    }


class DenseSparseSearchRequest(BaseModel):
    """Dense + Sparse hybrid search request (Qdrant-style)."""

    dense_query: list[float]
    sparse_query: SparseVectorData
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    dense_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    sparse_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    include_vectors: bool = False

    model_config = {
        "json_schema_extra": {
            "example": {
                "dense_query": [0.1, 0.2, 0.3],
                "sparse_query": {"indices": [0, 42], "values": [0.5, 1.2]},
                "limit": 10,
                "dense_weight": 0.6,
                "sparse_weight": 0.4,
            }
        }
    }


class AddPointsWithSparseRequest(BaseModel):
    """Request to add points with sparse vectors."""

    points: list[dict[str, Any]]

    model_config = {
        "json_schema_extra": {
            "example": {
                "points": [
                    {
                        "id": "doc1",
                        "vector": [0.1, 0.2, 0.3],
                        "sparse_vector": {"indices": [0, 42], "values": [0.5, 1.2]},
                        "metadata": {"title": "Document 1"},
                    }
                ]
            }
        }
    }


class RerankSearchRequest(BaseModel):
    """Search with re-ranking (two-stage retrieval)."""

    query: list[float]
    limit: int = Field(default=10, gt=0, le=1000)
    rerank_limit: int = Field(default=100, gt=0, le=10000, description="Candidates for re-ranking")
    filter: Optional[dict[str, Any]] = None
    rerank_method: str = Field(default="exact", description="exact, mmr, cross_encoder, weighted")
    diversity_lambda: float = Field(
        default=0.5, ge=0.0, le=1.0, description="MMR diversity (0=diverse, 1=relevant)"
    )
    query_text: Optional[str] = Field(default=None, description="For cross-encoder re-ranking")

    model_config = {
        "json_schema_extra": {
            "example": {
                "query": [0.1, 0.2, 0.3],
                "limit": 10,
                "rerank_limit": 100,
                "rerank_method": "mmr",
                "diversity_lambda": 0.7,
            }
        }
    }


class FacetedSearchRequest(BaseModel):
    """Search with faceted aggregations."""

    query: list[float]
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    facets: list[str] = Field(default_factory=list, description="Fields to aggregate")
    facet_limit: int = Field(default=10, description="Max values per facet")

    model_config = {
        "json_schema_extra": {
            "example": {
                "query": [0.1, 0.2, 0.3],
                "limit": 10,
                "facets": ["category", "author", "year"],
            }
        }
    }


class ACLSearchRequest(BaseModel):
    """Search with ACL/security filtering."""

    query: list[float]
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    user_principals: list[str] = Field(
        ..., description="User's principals e.g. ['user:alice', 'group:eng']"
    )
    acl_field: str = Field(default="_acl", description="Metadata field containing ACL")
    default_allow: bool = Field(default=False, description="Allow if no ACL defined")

    model_config = {
        "json_schema_extra": {
            "example": {
                "query": [0.1, 0.2, 0.3],
                "limit": 10,
                "user_principals": ["user:alice", "group:engineering"],
            }
        }
    }


class EnterpriseSearchRequest(BaseModel):
    """Full enterprise search with all features."""

    query: list[float]
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    query_text: Optional[str] = None
    user_principals: Optional[list[str]] = None
    default_allow: bool = Field(default=False, description="Allow if no ACL defined")
    facets: Optional[list[str]] = None
    rerank: bool = Field(default=False)
    rerank_method: str = Field(default="mmr")
    rerank_limit: int = Field(default=100)

    model_config = {
        "json_schema_extra": {
            "example": {
                "query": [0.1, 0.2, 0.3],
                "limit": 10,
                "user_principals": ["user:alice", "group:engineering"],
                "facets": ["category", "author"],
                "rerank": True,
                "rerank_method": "mmr",
            }
        }
    }


class CreateCollectionRequestV2(BaseModel):
    """Enhanced request to create a collection with advanced options."""

    name: str = Field(..., min_length=1, max_length=100)
    dimension: int = Field(..., gt=0, le=65536)
    metric: str = Field(default="cosine")
    description: Optional[str] = None
    enable_text_index: bool = Field(default=True, description="Enable hybrid search")
    hnsw_m: int = Field(default=16, description="HNSW M parameter")
    hnsw_ef_construction: int = Field(default=200, description="HNSW construction parameter")
    tags: Optional[List[str]] = Field(
        default=None, description="Capability tags: Dense, Sparse, Hybrid, Ultimate, Graph"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "documents",
                "dimension": 384,
                "metric": "cosine",
                "enable_text_index": True,
                "tags": ["Dense", "Hybrid"],
            }
        }
    }


class AddPointsRequestV2(BaseModel):
    """Enhanced request to add points with text for hybrid search."""

    points: list[dict[str, Any]]

    model_config = {
        "json_schema_extra": {
            "example": {
                "points": [
                    {
                        "id": "doc1",
                        "vector": [0.1, 0.2, 0.3],
                        "metadata": {"title": "Document 1"},
                        "text": "Full text content for hybrid search",
                    }
                ]
            }
        }
    }


class DeletePointsRequest(BaseModel):
    """Request to delete points."""

    ids: list[str]


class ApiResponse(BaseModel):
    """Standard API response."""

    ok: bool = True
    message: Optional[str] = None
    data: Optional[Any] = None


# ============================================================================
# THE APPLICATION: settings, the stores, the lifespan, and create_app
# ============================================================================
#
# INPUT   the environment, or what a caller hands create_app
# OUTPUT  Azure AI Search, the chunk store and the collection store from their
#         settings; the lifespan that opens the database and closes it; the
#         root path behind a gateway; the FastAPI application with its
#         middleware, its routers, the dashboard and sign-in
#
# A malformed setting is said at start-up, not on the first call.


def _setting(*names: str, default: str = "") -> str:
    """The first of these environment settings that is set to something.

    Two spellings a setting, on purpose. ``VECTRIXDB_AZURE_SEARCH_ENDPOINT``
    is this module's own convention, the one every other backend here uses.
    ``AZURE_SEARCH_ENDPOINT`` is the name Azure's own tooling writes and the
    name a deployment already has, so a host that configured the service for
    ingestion does not configure it a second time for serving, differently
    spelled, and wonder later why the API answers from an empty database.
    """
    for name in names:
        found = os.environ.get(name, "").strip()
        if found:
            return found
    return default


def _json_setting(*names: str) -> Optional[Any]:
    """A setting that is JSON, or None. A malformed one is said at start-up."""
    raw = _setting(*names)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{names[0]} is not JSON: {exc}") from exc


def _azure_search_from_env() -> StorageConfig:
    """Azure AI Search, configured the way :meth:`VectrixDB.with_azure_search` is.

    One index a collection, held by the service, so an API server and the
    worker that writes to it are two readers of one index rather than two
    databases. The endpoint is the only setting with no sensible default; no
    key means ``DefaultAzureCredential``, which on Azure is the managed
    identity and is the better answer anyway.

    An Azure OpenAI embedding deployment, when one is named, makes the index
    hold two vectors and the service fuse them: ours and theirs, under one
    filter and one ranker. Naming it is what puts ``embeddings="both"`` on
    the index, so this matches what wrote it. It has to: an index built with
    two vectors and read as though it had one answers worse and says nothing
    about why.
    """
    endpoint = _setting("VECTRIXDB_AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_ENDPOINT")
    if not endpoint:
        raise ConfigurationError(
            "VECTRIXDB_STORAGE_BACKEND is azure_search, so AZURE_SEARCH_ENDPOINT "
            "(or VECTRIXDB_AZURE_SEARCH_ENDPOINT) is the service to serve from, "
            "like https://<service>.search.windows.net"
        )
    deployment = _setting(
        "VECTRIXDB_AZURE_OPENAI_EMBED_DEPLOYMENT", "AZURE_OPENAI_EMBED_DEPLOYMENT"
    )
    openai = _setting("VECTRIXDB_AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_ENDPOINT")
    vectorizer = None
    if deployment and openai:
        vectorizer = {
            "endpoint": openai,
            "deployment": deployment,
            "dimensions": int(_setting("AZURE_OPENAI_EMBED_DIMENSIONS", default="1536")),
        }
        key = _setting("VECTRIXDB_AZURE_OPENAI_KEY", "AZURE_OPENAI_KEY")
        if key:
            vectorizer["api_key"] = key
    return StorageConfig(
        backend=StorageBackend.AZURE_SEARCH,
        azure_search_endpoint=endpoint,
        # None, not "": None is the managed identity, "" is a key that is not one.
        azure_search_key=_setting("VECTRIXDB_AZURE_SEARCH_KEY", "AZURE_SEARCH_KEY") or None,
        # Present and empty is a choice, and a different one from absent: it
        # says the index is the collection. _setting cannot tell them apart,
        # because for every other setting empty means "not given".
        azure_search_index_prefix=next(
            (
                os.environ[name]
                for name in ("VECTRIXDB_AZURE_SEARCH_INDEX_PREFIX", "AZURE_SEARCH_INDEX_PREFIX")
                if name in os.environ
            ),
            "vectrix",
        ),
        azure_search_semantic=_setting(
            "VECTRIXDB_AZURE_SEARCH_SEMANTIC", "AZURE_SEARCH_SEMANTIC"
        ).lower()
        in ("1", "true", "yes", "on"),
        azure_search_filter_fields=_json_setting(
            "VECTRIXDB_AZURE_SEARCH_FILTER_FIELDS", "AZURE_SEARCH_FILTER_FIELDS"
        ),
        azure_search_embeddings=_setting(
            "VECTRIXDB_AZURE_SEARCH_EMBEDDINGS", default="both" if vectorizer else "vectrixdb"
        ),
        azure_search_vectorizer=vectorizer,
        azure_search_vector_weights=_json_setting("VECTRIXDB_AZURE_SEARCH_VECTOR_WEIGHTS"),
    )


def chunk_store_from_env(given: Any = None, env: Optional[dict] = None) -> Any:
    """The chunk store the server's collections share: ``given``, else ``VECTRIXDB_CHUNK_STORE``, else None.

    An address is opened with ``VECTRIXDB_CHUNK_STORE_KEY``, or the file its
    ``_FILE`` twin names, as the Cosmos account's key; with neither, the
    managed identity. A store already made is used as it is.
    """
    from ..chunk_store import open_chunk_store
    from ..signin.keys import env_secret

    source = os.environ if env is None else env
    where = (
        given if given is not None else str(source.get("VECTRIXDB_CHUNK_STORE", "") or "").strip()
    )
    if where is None or where == "":
        return None
    key = env_secret(source, "VECTRIXDB_CHUNK_STORE_KEY") if isinstance(where, str) else None
    return open_chunk_store(where, key=key)


def collection_store_from_env(given: Any = None, env: Optional[dict] = None) -> Any:
    """Where every collection's rules are kept: ``given``, else ``VECTRIXDB_COLLECTION_STORE``, else None.

    An address is opened with ``VECTRIXDB_COLLECTION_STORE_KEY``, or the file its
    ``_FILE`` twin names; with neither, a cloud store signs in as the machine.
    A store already made is used as it is.
    """
    from ..collection_records import STORE_ENV, open_collection_store
    from ..signin.keys import env_secret

    source = os.environ if env is None else env
    where = given if given is not None else str(env_secret(source, STORE_ENV) or "").strip()
    if where is None or where == "":
        return None
    key = env_secret(source, f"{STORE_ENV}_KEY") if isinstance(where, str) else None
    return open_collection_store(where, key=key)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _db

    # Startup: the path given to create_app() wins, then the environment.
    # Until 2.2 the argument was accepted and ignored, so every embedded app
    # (and every test) opened ./vectrixdb_data in the working directory.
    db_path = getattr(app.state, "db_path", None) or os.environ.get(
        "VECTRIXDB_PATH", "./vectrixdb_data"
    )

    # Storage configuration
    storage_backend = os.environ.get("VECTRIXDB_STORAGE_BACKEND", "sqlite")
    storage_config = None
    if storage_backend == "cosmosdb":
        storage_config = StorageConfig(
            backend=StorageBackend.COSMOSDB,
            cosmos_endpoint=os.environ.get("VECTRIXDB_COSMOS_ENDPOINT", ""),
            cosmos_key=os.environ.get("VECTRIXDB_COSMOS_KEY", ""),
            cosmos_database=os.environ.get("VECTRIXDB_COSMOS_DATABASE", "vectrixdb"),
        )
    elif storage_backend == "lakebase":
        storage_config = StorageConfig(
            backend=StorageBackend.LAKEBASE,
            lakebase_host=os.environ.get("VECTRIXDB_LAKEBASE_HOST", ""),
            lakebase_port=int(os.environ.get("VECTRIXDB_LAKEBASE_PORT", "5432")),
            lakebase_database=os.environ.get("VECTRIXDB_LAKEBASE_DATABASE", "vectrixdb"),
            lakebase_token=os.environ.get("VECTRIXDB_LAKEBASE_TOKEN"),
            lakebase_user=os.environ.get("VECTRIXDB_LAKEBASE_USER"),
            lakebase_password=os.environ.get("VECTRIXDB_LAKEBASE_PASSWORD"),
            lakebase_ssl=os.environ.get("VECTRIXDB_LAKEBASE_SSL", "true").lower() == "true",
        )
    elif storage_backend == "delta_lake":
        storage_config = StorageConfig(
            backend=StorageBackend.DELTA_LAKE,
            delta_workspace_url=os.environ.get("VECTRIXDB_DELTA_WORKSPACE_URL", ""),
            delta_token=os.environ.get("VECTRIXDB_DELTA_TOKEN", ""),
            delta_catalog=os.environ.get("VECTRIXDB_DELTA_CATALOG", "main"),
            delta_schema=os.environ.get("VECTRIXDB_DELTA_SCHEMA", "vectrixdb"),
            delta_warehouse_id=os.environ.get("VECTRIXDB_DELTA_WAREHOUSE_ID"),
            delta_http_path=os.environ.get("VECTRIXDB_DELTA_HTTP_PATH"),
        )
    elif storage_backend == "azure_search":
        storage_config = _azure_search_from_env()
    elif storage_backend == "memory":
        storage_config = StorageConfig(backend=StorageBackend.MEMORY)

    # Cache configuration
    cache_backend = os.environ.get("VECTRIXDB_CACHE_BACKEND", "memory")
    cache_config = CacheConfig(backend=CacheBackend.NONE)
    if cache_backend == "redis":
        cache_config = CacheConfig(
            backend=CacheBackend.REDIS,
            redis_host=os.environ.get("VECTRIXDB_REDIS_HOST", "localhost"),
            redis_port=int(os.environ.get("VECTRIXDB_REDIS_PORT", "6379")),
            redis_password=os.environ.get("VECTRIXDB_REDIS_PASSWORD"),
            redis_ssl=os.environ.get("VECTRIXDB_REDIS_SSL", "false").lower() == "true",
        )
    elif cache_backend == "memory":
        cache_config = CacheConfig(backend=CacheBackend.MEMORY)
    elif cache_backend == "hybrid":
        cache_config = CacheConfig(
            backend=CacheBackend.HYBRID,
            redis_host=os.environ.get("VECTRIXDB_REDIS_HOST", "localhost"),
            redis_port=int(os.environ.get("VECTRIXDB_REDIS_PORT", "6379")),
            redis_password=os.environ.get("VECTRIXDB_REDIS_PASSWORD"),
        )

    # Scaling configuration
    scaling_strategy = os.environ.get("VECTRIXDB_SCALING_STRATEGY", "none")
    scaling_config = ScalingConfig(
        strategy=ScalingStrategy(scaling_strategy)
        if scaling_strategy != "none"
        else ScalingStrategy.NONE,
        memory_high_watermark=float(os.environ.get("VECTRIXDB_MAX_MEMORY_PERCENT", "85")),
    )

    # Initialize database
    # The copy of every chunk the collection pages read, when more than one
    # process writes: the one create_app() was given, then the environment.
    chunk_store = chunk_store_from_env(getattr(app.state, "chunk_store", None))
    # Every collection's record, read by every server alike: the one
    # create_app() was given, then the environment. On the app for the door,
    # which reads each collection's policy before anything in it is read.
    collection_store = collection_store_from_env(getattr(app.state, "collection_store", None))
    app.state.collection_store = collection_store

    _db = VectrixDB(
        path=db_path if storage_backend != "memory" else None,
        storage_config=storage_config,
        cache_config=cache_config,
        scaling_config=scaling_config,
        chunk_store=chunk_store,
        collection_store=collection_store,
        # A server beside an ingest worker is the second reader of one
        # backend: it lists and searches what the worker made, not only what
        # it made itself.
        follow_shared=True,
    )
    # Visibility and masking go beside the policy in the same record, so one
    # record answers who may see a collection.
    signin_runtime = getattr(app.state, "signin", None)
    if signin_runtime is not None and collection_store is not None:
        signin_runtime.store.use_collection_store(collection_store)

    from ..collection_records import describe_collection_store

    logger.info("database initialized at %s", db_path)
    logger.info("storage backend: %s", storage_backend)
    logger.info("cache backend: %s", cache_backend)
    logger.info("chunk store: %s", describe_chunk_store(_db.chunk_store))
    logger.info("collection records: %s", describe_collection_store(_db.collection_store))
    logger.info("collections loaded: %d", len(_db))

    yield

    # Shutdown
    if _db:
        _db.close()
        logger.info("database closed")


def root_path_from_env(env: Optional[dict] = None) -> str:
    """The path this app is served under, behind a gateway that adds one.

    ``VECTRIXDB_ROOT_PATH`` says it outright; left unset, the path of
    ``VECTRIXDB_PUBLIC_URL`` is it, so an enterprise that publishes
    ``https://apim.company.com/vectrixdb`` sets one thing and not two. Empty
    for a server at the root of its host, which is every plain install.
    """
    from urllib.parse import urlsplit

    source = os.environ if env is None else env
    raw = (source.get("VECTRIXDB_ROOT_PATH") or "").strip()
    if not raw:
        raw = urlsplit((source.get("VECTRIXDB_PUBLIC_URL") or "").strip()).path
    path = "/" + raw.strip().strip("/")
    return "" if path == "/" else path


def served_paths() -> List[str]:
    """Every path the server declares, without building it: what a gateway path may be given to."""
    from .documents import router as documents_router
    from .evaluations import router as evaluations_router
    from .gateway import declared_paths
    from .inspection import router as inspection_router
    from .signin import router as signin_router

    routers = (router, inspection_router, documents_router, evaluations_router, signin_router)
    return declared_paths([route for each in routers for route in each.routes]) + [
        "/dashboard",
        "/docs",
        "/redoc",
        "/openapi.json",
    ]


def create_app(
    db_path: Optional[str] = None,
    enable_dashboard: bool = True,
    signin: Any = None,
    oidc_transport: Any = None,
    chunk_store: Any = None,
    collection_store: Any = None,
) -> FastAPI:
    """
    Create the FastAPI application.

    Args:
        db_path: Database path (default: ./vectrixdb_data)
        enable_dashboard: Serve dashboard static files
        signin: a ``vectrixdb.signin.SignInConfig``. Left out, it is read
            from the environment, and with ``VECTRIXDB_SIGNIN`` unset
            sign-in is off.
        oidc_transport: how the identity provider is reached, for a test
            that stands in for one.
        chunk_store: where the collection pages read every chunk from, when
            more than one process writes: a store of your own, or an
            address. Left out, ``VECTRIXDB_CHUNK_STORE``, and with that unset
            the table beside this process. See vectrixdb.chunk_store.
        collection_store: where every collection's record is kept, its
            path and its policy, shared by every server: a store
            of your own, or an address. Left out, ``VECTRIXDB_COLLECTION_STORE``,
            and with that unset each collection keeps its policy in its own
            metadata. See vectrixdb.collection_records.

    Returns:
        FastAPI application
    """
    global _db

    from .gateway import (
        DEFAULT_KEY_HEADER,
        DEFAULT_TOKEN_HEADER,
        Gateway,
        GatewayPathsMiddleware,
        declared_paths,
    )

    # The host's own path, then the prefix every route lives under, and each
    # route's own gateway path: read once, so a mistake stops the start.
    host_root = root_path_from_env()
    gateway = Gateway.from_env(root=host_root)
    app = FastAPI(
        title="VectrixDB",
        description="Where vectors come alive - A modern, visual-first vector database",
        version=__version__,
        lifespan=lifespan,
        # Behind a gateway that serves this app under a path. FastAPI then
        # routes whether or not the gateway strips the prefix, and the
        # OpenAPI document it serves carries the prefix, which is what a
        # gateway team imports to make its operations.
        root_path=host_root + gateway.prefix,
        # A redirect to add or drop a slash names the address the app sees,
        # and behind a gateway that sends the caller round it. With a prefix
        # or gateway paths, a slash too many is not found, like any wrong path.
        redirect_slashes=not gateway.shaped,
    )
    app.state.gateway = gateway
    app.state.db_path = db_path
    app.state.chunk_store = chunk_store
    app.state.collection_store = collection_store
    # Parsed once, so a typo is a start-up error rather than a header quietly
    # ignored on every request.
    from .forwarded import ENV as _PROXIES_ENV, trusted_from_env

    try:
        app.state.trusted_proxies = trusted_from_env()
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    if app.state.trusted_proxies:
        logger.info("%s: %s", _PROXIES_ENV, ", ".join(str(n) for n in app.state.trusted_proxies))

    from ..brand import Brand
    from ..signin import SignInConfig

    # A mistake in a key setting is said at start-up, not on the first request.
    get_api_key(), get_read_only_key()
    app.state.brand = Brand.from_env()
    resolved_path = db_path or os.environ.get("VECTRIXDB_PATH", "./vectrixdb_data")
    signin_config = signin if signin is not None else SignInConfig.from_env(resolved_path)
    app.state.signin = (
        SignInRuntime(
            signin_config,
            oidc_transport=oidc_transport,
            product=app.state.brand.name,
            gateway=gateway,
        )
        if signin_config.enabled
        else None
    )
    if app.state.signin is not None:
        # Not everyone in a group should sign in, so single sign-on waits for somebody to be named.
        problem = app.state.signin.needs_a_list() or app.state.signin.glass_problem()
        if problem:
            app.state.signin.close()
            raise ConfigurationError(problem)
        app.state.signin.stamp_of = _stamp_of
        # Which version this is, is for people who signed in, not for whoever finds the port.
        app.version = "shown after signing in"

    @app.exception_handler(PolicyError)
    async def _policy_refusal(request, exc):  # noqa: ANN001 - FastAPI's signature
        """A policied collection cannot be served from here, and says so.

        Every read path on Collection refuses without a principal, which is
        the behaviour that matters. Left unhandled it surfaced as a 500 and
        read like a crash; this makes the refusal legible without putting the
        library's own advice about as_principal in front of an HTTP caller,
        who is not the person who can act on it.

        A collection whose rules could not be read is not refused access: the
        store is down, and 503 says so, so it is retried rather than believed.
        """
        from ..exceptions import CollectionStoreUnavailable

        if isinstance(exc, CollectionStoreUnavailable):
            return refusal(503, str(exc))
        return refusal(
            403,
            "this collection carries an entitlement policy, and this API does "
            "not resolve principals. Serve it from a tier that does.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _route_refusal(request, exc):  # noqa: ANN001 - FastAPI's signature
        """A route's refusal, in the shape the door's refusals have.

        ``detail`` is kept as the route gave it, so a client that read it
        still does; ``message`` is the same thing as one sentence. See
        :mod:`vectrixdb.api.replies`.
        """
        return refusal(
            exc.status_code,
            message_of(exc.detail),
            detail=exc.detail,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _not_the_shape_asked_for(request, exc):  # noqa: ANN001 - FastAPI's signature
        # The field errors as FastAPI lists them, minus the caller's own input
        # and anything that is not JSON, and the same said in words.
        detail = [
            {k: v for k, v in error.items() if k in ("type", "loc", "msg")}
            for error in exc.errors()
        ]
        return refusal(422, message_of(detail), detail=detail)

    # API Key Authentication Middleware (Qdrant-style)
    app.add_middleware(ApiKeyAuthMiddleware)
    # Around the sign-in layer, so who is asking is known when a reply comes back
    # through it: a collection's identifiers masked for people, never for a key.
    if app.state.signin is not None:
        from .masked import MaskingMiddleware

        app.add_middleware(MaskingMiddleware)

    # CORS. With sign-in on the browser holds a session cookie, and "any
    # origin, with credentials" would let any page on the internet read this
    # server as whoever is signed in. So then it is the origins somebody
    # named, and none by default: the dashboard is same-origin and needs none.
    if app.state.signin is not None:
        origins = [
            o.strip() for o in os.environ.get("VECTRIXDB_CORS_ORIGINS", "").split(",") if o.strip()
        ]
        if "*" in origins:
            raise ConfigurationError(
                "VECTRIXDB_CORS_ORIGINS is * while sign-in is on, which would let any website read this server as "
                "whoever is signed in. Name the origins that may call it: https://app.company.com"
            )
        # The headers a key and a token arrive in, whatever the settings named them.
        own = [
            name
            for name in (gateway.key_header, gateway.token_header)
            if name not in (DEFAULT_KEY_HEADER, DEFAULT_TOKEN_HEADER)
        ]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["api-key", "content-type", "x-csrf-token", "x-filename", *own],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=[
                "*",
                "api-key",
                *(
                    {gateway.key_header, gateway.token_header}
                    - {DEFAULT_KEY_HEADER, DEFAULT_TOKEN_HEADER}
                ),
            ],
        )

    # Added last, so it is the outermost layer: every reply carries these
    # headers, a refusal from the layers inside included.
    from .security_headers import SecurityHeadersMiddleware, frame_ancestors_from_env

    https = bool(
        app.state.signin is not None and app.state.signin.config.secure_cookies
    ) or os.environ.get("VECTRIXDB_PUBLIC_URL", "").strip().startswith("https://")
    app.add_middleware(SecurityHeadersMiddleware, ancestors=frame_ancestors_from_env(), https=https)

    # Outside everything, because every layer below reads the path: with a
    # gateway's prefix still on it, the role table, the guest rules and the
    # masking layer all miss, and none of them says so.
    if app.root_path:
        from .rootpath import RootPathMiddleware

        app.add_middleware(RootPathMiddleware, root_path=host_root, prefix=gateway.prefix)
    # Outside that: a route's own gateway path is taken off first, when it is
    # on, so the root path layer and everything inside it see one shape.
    if gateway.paths:
        app.add_middleware(GatewayPathsMiddleware, gateway=gateway)

    # Mount dashboard if available
    if enable_dashboard:
        # Look for dashboard in multiple locations
        # 1. Bundled with package (pip install)
        package_dashboard = Path(__file__).parent.parent / "dashboard"
        # 2. Development location (dashboard/dist)
        dev_dashboard = Path(__file__).parent.parent.parent / "dashboard" / "dist"

        dashboard_path = None
        if package_dashboard.exists() and (package_dashboard / "index.html").exists():
            dashboard_path = package_dashboard
        elif dev_dashboard.exists() and (dev_dashboard / "index.html").exists():
            dashboard_path = dev_dashboard

        if dashboard_path:
            files = _DashboardFiles(directory=str(dashboard_path), html=True)
            files.brand = app.state.brand
            files.gateway = gateway
            app.mount("/dashboard", files, name="dashboard")
            logger.info("dashboard mounted from %s", dashboard_path)
            if gateway.shaped:

                @app.get("/dashboard", include_in_schema=False)
                async def _to_the_dashboard() -> Any:
                    # Relative, so it holds through the gateway: the slash redirect
                    # that is off would name the address the app sees.
                    return RedirectResponse("dashboard/", status_code=307)

    app.include_router(router)
    # The inspection routes live in their own module and import this one,
    # so they are pulled in here rather than at the top.
    from .inspection import router as inspection_router

    app.include_router(inspection_router)
    from .documents import router as documents_router

    app.include_router(documents_router)
    from .evaluations import router as evaluations_router

    app.include_router(evaluations_router)
    from .signin import router as signin_router

    app.include_router(signin_router)
    # A gateway path given to a route that is not here is a typing mistake, found now rather than by a caller.
    gateway.check(declared_paths(app.routes))
    return app


# Every route below registers on this router. create_app() includes it, so a
# fresh application gets the full route table instead of the empty one a
# second create_app() call used to come back with, when the decorators had
# been applied to the module-level singleton instead.
router = APIRouter()


# ============================================================================
# ROUTES: health and info
# ============================================================================
#
# INPUT   a request
# OUTPUT  the root, whether authentication is on, the brand's name and logo,
#         the health check, the API's root listing, and the database's
#         information
#
# With sign-in on, the version is said only to somebody signed in.


def _stamp_of(name: str) -> Optional[str]:
    """When a collection was made, which is what ties a sharing choice to it. None if there is no such collection."""
    try:
        return get_db().get_collection(name).info().created_at.isoformat()
    except Exception:  # noqa: BLE001 - not found, or not open yet: either way, not shared
        return None


def _signed_in(request: Request) -> bool:
    if runtime_of(request) is None:
        return True
    return session_of(request) is not None


@router.get("/", tags=["info"])
async def root(request: Request):
    """Root endpoint. With sign-in on, the version is said only to somebody signed in."""
    return {
        "name": "VectrixDB",
        "tagline": "Where vectors come alive",
        **({"version": __version__} if _signed_in(request) else {}),
        "docs": "/docs",
        "dashboard": "/dashboard",
        "auth_enabled": _full_key_configured(),
    }


@router.get("/auth/status", tags=["auth"])
async def auth_status(request: Request):
    """Check if authentication is enabled."""
    runtime = runtime_of(request)
    return {
        "ok": True,
        "message": None,
        "data": {
            "auth_enabled": _full_key_configured(),
            "read_only_key_enabled": bool(
                get_read_only_key()
                or os.environ.get("VECTRIXDB_READ_ONLY_API_KEY_SHA256", "").strip()
            ),
            # Present only when sign-in is on, so the reply is what it always was otherwise.
            **({"signin": sorted(runtime.methods())} if runtime is not None else {}),
        },
    }


@router.get("/brand.json", tags=["info"], include_in_schema=False)
async def brand_info(request: Request):
    """The company's name, logo and copyright line for the dashboard, when one is set."""
    return request.app.state.brand.public()


@router.get("/brand.css", tags=["info"], include_in_schema=False)
async def brand_css(request: Request):
    """The brand's colours as a stylesheet, for a dashboard some other service sends. The library's own page has them inline."""
    from starlette.responses import Response as Plain

    return Plain(
        content=request.app.state.brand.css(),
        media_type="text/css",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/v1/about", tags=["info"])
async def about_this_server():
    """Which VectrixDB this is, its licence, and the NOTICE that travels with it. For admins: About in the account menu."""
    from ..about import about

    return {"ok": True, "message": None, "data": about()}


@router.get("/api/v1/about/licence", tags=["info"])
async def about_licence():
    """The Apache License, Version 2.0, as the package carries it, in plain text."""
    from starlette.responses import PlainTextResponse

    from ..about import licence_text

    text = licence_text()
    if text is None:
        raise HTTPException(status_code=404, detail="the licence file is not in this install")
    return PlainTextResponse(text, headers={"X-Content-Type-Options": "nosniff"})


def _logo_response(request: Request, dark: bool):
    brand = request.app.state.brand
    logo = brand.logo_dark if dark else brand.logo
    if logo is None:
        raise HTTPException(status_code=404, detail="no logo is set")
    from starlette.responses import Response as Plain

    # Served as an image that may run nothing, even if somebody opens it on its own.
    return Plain(
        content=logo.data,
        media_type=logo.mime,
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )


@router.get("/brand/logo", tags=["info"], include_in_schema=False)
async def brand_logo(request: Request):
    return _logo_response(request, dark=False)


@router.get("/brand/logo-dark", tags=["info"], include_in_schema=False)
async def brand_logo_dark(request: Request):
    return _logo_response(request, dark=True)


@router.get("/health", tags=["info"])
async def health():
    """Health check."""
    return {"status": "healthy", "timestamp": utcnow().isoformat()}


# ============================================================================
# ROUTES: the live socket
# ============================================================================
#
# INPUT   a WebSocket
# OUTPUT  events as they happen, and the connection's status
#
# The dashboard's one open connection.


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard updates.

    Events emitted:
    - collection_created: When a new collection is created
    - collection_deleted: When a collection is deleted
    - points_added: When points are added to a collection
    - points_deleted: When points are deleted from a collection
    - collection_updated: When collection stats change
    - search_performed: When a search is executed

    Usage (JavaScript):
        const ws = new WebSocket('ws://localhost:7337/ws');
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log('Event:', data.event, data.data);
        };
    """
    runtime = getattr(websocket.app.state, "signin", None)
    if runtime is not None and session_of(websocket) is None:
        await websocket.close(code=4401)
        return
    if (
        runtime is None
        and _full_key_configured()
        and not reads_are_open()
        and not _ws_has_a_key(websocket)
    ):
        # Reads closed: the feed names what was written, so it needs a key too.
        await websocket.close(code=4401)
        return
    await ws_manager.connect(websocket)
    try:
        # Send initial connection confirmation
        await websocket.send_text(
            json.dumps(
                {
                    "event": "connected",
                    "data": {"message": "Connected to VectrixDB real-time updates"},
                    "timestamp": utcnow_iso(),
                }
            )
        )

        # Keep connection alive and handle incoming messages
        while True:
            try:
                # Wait for messages (ping/pong or commands)
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # Handle ping
                if data == "ping":
                    await websocket.send_text(
                        json.dumps({"event": "pong", "data": {}, "timestamp": utcnow_iso()})
                    )
            except asyncio.TimeoutError:
                # Send keepalive ping
                try:
                    await websocket.send_text(
                        json.dumps({"event": "keepalive", "data": {}, "timestamp": utcnow_iso()})
                    )
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket)


@router.get("/api/v1/ws/status", tags=["info"])
async def websocket_status():
    """Get WebSocket connection status."""
    return {"active_connections": ws_manager.connection_count, "endpoint": "/ws"}


@router.get("/api/v1", tags=["info"])
@router.get("/api/v1/", tags=["info"])
async def api_v1_root():
    """API v1 root - list available endpoints."""
    return {
        "version": "v1",
        "endpoints": {
            "info": "/api/v1/info",
            "collections": "/api/v1/collections",
            "health": "/health",
            "docs": "/docs",
            "openapi": "/openapi.json",
        },
        "description": "VectrixDB REST API v1",
    }


@router.get("/api/v1/info", tags=["info"])
@router.get("/api/info", tags=["info"], include_in_schema=False)
async def database_info():
    """Get database information."""
    db = get_db()
    info = db.info()

    # Get storage backend
    storage_backend = "sqlite"  # default
    if hasattr(db, "_storage_config") and db._storage_config:
        storage_backend = db._storage_config.backend.value

    # Documents: what the collections' chunks came from, and anything the
    # document index holds besides. The index alone said 0 for a server whose
    # documents all went into collections.
    documents_count = 0
    try:
        documents_count = sum(
            c.document_count() for c in list(getattr(db, "_collections", {}).values())
        )
    except Exception:
        pass
    try:
        if hasattr(db, "documents"):
            documents_count += len(db.documents.list_documents())
    except Exception:
        pass

    # On a deployment the chunks live in a store every instance shares, and
    # this instance's own count of them is of what it wrote itself: none.
    total_vectors, shared_store = info.total_vectors, False
    total_size: Optional[int] = info.total_size_bytes
    try:
        shared = [
            c
            for c in (db.get_collection(i.name) for i in db.list_collections())
            if chunk_source.shared(c) is not None
        ]
        if shared:
            total_vectors = sum(chunk_source.count(c) for c in shared)
            # And what is on this disk holds nothing of them, so its size says nothing.
            total_size, shared_store = None, True
    except Exception:  # noqa: BLE001 - a store that cannot be reached leaves the count the database gave
        pass

    return {
        "collections_count": info.collections_count,
        "total_vectors": total_vectors,
        "total_size_bytes": total_size,
        "storage_backend": storage_backend,
        "shared_store": shared_store,
        "documents_count": documents_count,
    }


# ============================================================================
# ROUTES: collections, who can see them, and who may retrieve
# ============================================================================
#
# INPUT   a collection's name, and a body for creating, sharing or setting the
#         policy
# OUTPUT  every collection, with the policied ones named and no more; a
#         collection made, v1 or v2; its details; it deleted; who can see
#         each, and a collection made public or kept private; who may retrieve
#         from it, in the record every server reads
#
# Nobody may retrieve until the policy is set. A sharing choice is tied to the
# collection by when it was made, so a name reused later does not inherit it.


@router.get("/api/v1/collections", tags=["collections"])
@router.get("/api/collections", tags=["collections"], include_in_schema=False)
async def list_collections(request: Request):
    """List all collections, with the policied ones named and no more.

    That a collection exists, its name and its size, is for everyone who
    reaches the server, guests included; what is in it is for the people its
    policy names. How much is in a policied collection is withheld: a size
    grows when documents are written, and this API cannot say which of those
    documents the person reading is allowed to know about, because it
    resolves no principals.
    """
    db = get_db()
    collections = []
    caller = caller_of(request)
    for info in db.list_collections():
        # A key scoped to collections lists those: what it cannot reach, it is
        # not told about.
        if caller is not None and caller.collections and info.name not in caller.collections:
            continue
        row = info.to_dict()
        collection = db.get_collection(info.name)
        if (
            chunk_source.shared(collection) is not None
            and getattr(collection, "policy", None) is None
        ):
            # What every instance wrote, not what this one did.
            _from_the_shared_store(row, collection, request)
        if getattr(collection, "policy", None) is not None:
            row = {
                "name": info.name,
                "dimension": row.get("dimension"),
                "metric": row.get("metric"),
                "entitlement_policy": True,
                "detail": "counts and size withheld: this API resolves no principals",
            }
        collections.append(row)
    return {"collections": collections, "total": len(collections)}


@router.post("/api/v1/collections", tags=["collections"])
@router.post("/api/collections", tags=["collections"], include_in_schema=False)
async def create_collection(request: CreateCollectionRequest):
    """Create a new collection."""
    db = get_db()

    try:
        metric = DistanceMetric(request.metric)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid metric: {request.metric}")

    # Auto-tagging: v1 API creates dense-only collections
    tags = list(request.tags) if request.tags else []
    if "demo" not in tags and "dense" not in tags and "hybrid" not in tags:
        tags.append("dense")

    try:
        collection = db.create_collection(
            name=request.name,
            dimension=request.dimension,
            metric=metric,
            description=request.description,
            tags=tags,
        )
        collection_info = collection.info().to_dict()

        # Emit WebSocket event
        await emit_event(
            "collection_created",
            {
                "name": request.name,
                "dimension": request.dimension,
                "metric": request.metric,
                "tags": request.tags or [],
                "collection": collection_info,
            },
        )

        return ApiResponse(
            ok=True, message=f"Collection '{request.name}' created", data=collection_info
        )
    except InvalidCollectionName as e:
        # A malformed name is a bad request, not a conflict with something
        # that already exists.
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/api/v2/collections", tags=["collections"])
async def create_collection_v2(request: CreateCollectionRequestV2):
    """Create a collection with advanced options (v2 API).

    Tier system (each tier includes features from previous tiers):
    - dense: Vector search only
    - hybrid: + BM25 text search + Rerank
    - ultimate: + Late Interaction (ColBERT)
    - graph: + Knowledge Graph (GraphRAG)
    """
    db = get_db()

    try:
        metric = DistanceMetric(request.metric)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid metric: {request.metric}")

    # Tier-based feature configuration
    tags = list(request.tags) if request.tags else []
    tags_lower = [t.lower() for t in tags]

    # Determine tier from tags
    tier = "dense"  # default
    for t in ["graph", "ultimate", "hybrid", "dense"]:
        if t in tags_lower:
            tier = t
            break

    # Auto-enable features based on tier hierarchy
    # dense < hybrid < ultimate < graph
    enable_text_index = request.enable_text_index
    if tier in ["hybrid", "ultimate", "graph"]:
        enable_text_index = True  # These tiers require text index for BM25/rerank

    # Normalize tags: keep tier + language tags (EN/ML)
    if "demo" not in tags_lower:
        normalized_tags = [tier]
        # Preserve language tags
        for t in tags:
            if t.upper() in ["EN", "ML"]:
                normalized_tags.append(t.upper())
        tags = normalized_tags

    try:
        collection = db.create_collection(
            name=request.name,
            dimension=request.dimension,
            metric=metric,
            description=request.description,
            enable_text_index=enable_text_index,
            m=request.hnsw_m,
            ef_construction=request.hnsw_ef_construction,
            tags=tags,
        )
        collection_info = collection.info().to_dict()

        # Emit WebSocket event
        await emit_event(
            "collection_created",
            {
                "name": request.name,
                "dimension": request.dimension,
                "metric": request.metric,
                "tier": tier,
                "hybrid_enabled": enable_text_index,
                "collection": collection_info,
            },
        )

        tier_features = {
            "dense": "vector search",
            "hybrid": "vector + BM25 + rerank",
            "ultimate": "vector + BM25 + rerank + late interaction",
            "graph": "vector + BM25 + rerank + late interaction + knowledge graph",
        }

        return ApiResponse(
            ok=True,
            message=f"Collection '{request.name}' created with {tier} tier ({tier_features.get(tier, tier)})",
            data=collection_info,
        )
    except InvalidCollectionName as e:
        # A malformed name is a bad request, not a conflict with something
        # that already exists.
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


def _from_the_shared_store(row: dict, collection: Any, request: Request) -> None:
    """A collection's row as every instance would give it, for one whose chunks are in a shared store.

    The count and the time of the last write are the store's. The size is
    left out: what is on this disk is the table beside this process, which
    holds nothing of them. When it was made is the collection record's, or
    left out: this process made its own handle when it started, which is
    not when the collection was made.
    """
    row["count"] = chunk_source.count(collection)
    row["updated_at"] = chunk_source.changed_at(collection)
    row["size_bytes"] = None
    row["shared_store"] = True
    made = None
    records = getattr(request.app.state, "collection_store", None)
    if records is not None:
        try:
            kept = records.get(str(row.get("name") or ""))
            made = kept.created_at if kept is not None else None
        except Exception:  # noqa: BLE001 - a record that cannot be read leaves the time out, and the page says nothing of it
            made = None
    row["created_at"] = made


@router.get("/api/v1/collections/{name}", tags=["collections"])
async def get_collection(name: str, request: Request):
    """Get collection details."""
    db = get_db()
    _servable(db, name)

    try:
        collection = db.get_collection(name)
        row = collection.info().to_dict()
        if (
            chunk_source.shared(collection) is not None
            and getattr(collection, "policy", None) is None
        ):
            # What every instance wrote, not what this one did: the same row the list gives.
            _from_the_shared_store(row, collection, request)
        return ApiResponse(ok=True, data=row)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")


@router.delete("/api/v1/collections/{name}", tags=["collections"])
@router.delete("/api/collections/{name}", tags=["collections"], include_in_schema=False)
async def delete_collection(name: str, request: Request):
    """Delete a collection."""
    try:
        db = get_db()

        if db.delete_collection(name):
            runtime = runtime_of(request)
            if runtime is not None:
                # One made again under this name starts private and unmasked, whatever this one was.
                runtime.forget(name)
            # Its graph goes with it, as its chunks and its record do.
            try:
                store = graph_store_of()
                if store is not None:
                    store.delete(name)
            except Exception:  # noqa: BLE001 - a store that cannot be reached does not undo the delete
                logger.warning("the graph of %s could not be removed from the graph store", name)
            # Emit WebSocket event
            await emit_event("collection_deleted", {"name": name})
            return ApiResponse(ok=True, message=f"Collection '{name}' deleted")
        else:
            return refusal(404, f"Collection '{name}' not found")
    except Exception as e:
        return refusal(500, f"Error deleting collection: {str(e)}")


@router.get("/api/v1/policies", tags=["collections"])
async def policies(request: Request):
    """Who may search each collection: its policy, as a count for everyone and as the list for people signed in."""
    runtime = runtime_of(request)
    records = getattr(request.app.state, "collection_store", None)
    caller = caller_of(request)
    person = caller is not None and caller.method != "guest"
    db = get_db()
    shown = {}
    for info in db.list_collections():
        record = records.get(info.name) if records is not None else None
        policy = record.policy_object() if record is not None else None
        allow = policy.allow if policy is not None else []
        token = policy is not None and policy.method == "token"
        entry: dict = {
            "method": policy.method if policy is not None else None,
            # A token policy's people are its list, beside its groups: someone must be in both.
            "people": len(policy.people)
            if policy is not None and token
            else sum(1 for e in allow if "email" in e),
            "domains": sum(1 for e in allow if "domain" in e),
            "groups": len(allow) if token else 0,
            "policied": getattr(db.get_collection(info.name), "policy", None) is not None,
        }
        if person:
            # The list itself, for the page's hover and the Policy tab: a guest gets the counts alone.
            entry["policy"] = record.policy if record is not None else None
        shown[info.name] = entry
    return {
        "ok": True,
        "data": {
            "signin": runtime is not None,
            "guests": bool(runtime and runtime.config.guests),
            "gated": records is not None,
            "collections": shown,
        },
    }


class PolicyRequest(BaseModel):
    policy: Optional[dict] = Field(
        None,
        description=(
            'Who may retrieve from the collection: {"method": "token", "allow": [{"id": ..., "name": ...}], "people": [{"email": ...}]} '
            "for security groups by object id from the sign-in token, narrowed to the people on the list, who must be in a group and on it; "
            'or {"method": "store", "allow": [{"email": ...} | {"domain": ...}]} for a list of people kept with the collection. '
            "null is nobody"
        ),
    )


@router.put("/api/v1/collections/{name}/policy", tags=["collections"])
async def set_policy(name: str, body: PolicyRequest, request: Request):
    """Who may search a collection, in the record every server reads. Nobody, until it is set."""
    from ..collection_access import AccessPolicy

    records = getattr(request.app.state, "collection_store", None)
    if records is None:
        raise HTTPException(
            status_code=404,
            detail="Nothing is gated on this server: set VECTRIXDB_COLLECTION_STORE to where each collection's record is kept",
        )
    try:
        collection = get_db().get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")
    try:
        policy = AccessPolicy.from_dict(body.policy) if body.policy is not None else None
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    caller = caller_of(request)
    by = caller.who if caller else None
    kept = records.set_policy(
        name, policy, by=by, generation=collection.info().created_at.isoformat()
    )
    runtime = runtime_of(request)
    if runtime is not None:
        runtime.access.record(
            "policy_changed",
            collection=name,
            reason=policy.describe() if policy is not None else "nobody",
            by=by,
        )
    return {"ok": True, "data": {"policy": kept.policy}}


# ============================================================================
# ROUTES: points, and the graph
# ============================================================================
#
# INPUT   a collection's name; points to add or delete; a page, with a find
# OUTPUT  points added, v1 or v2; one point; the knowledge graph for drawing,
#         and entities extracted from the documents; points deleted; a page of
#         ids with what the index says about each, and the total
#
# The listing says where each chunk came from and how clean it is, never a
# word of what it says: the text is one request per chunk, asked for and
# recorded.


@router.post("/api/v1/collections/{name}/points", tags=["points"])
async def add_points(name: str, request: AddPointsRequest):
    """Add points to a collection."""
    db = get_db()

    collection = _servable(db, name)

    try:
        ids = [p.id for p in request.points]
        vectors = [p.vector for p in request.points]
        metadata = [p.metadata for p in request.points]

        build = _mint_build(metadata)
        added = collection.add(ids=ids, vectors=vectors, metadata=metadata)
        # The library's own API saves after every write; a server that is
        # killed rather than stopped must not lose what it was sent.
        collection.save()
        collection.set_meta("index_build_id", build)
        total = collection.count()

        # Emit WebSocket event
        await emit_event(
            "points_added", {"collection": name, "added": added, "total": total, "ids": ids}
        )

        return ApiResponse(
            ok=True,
            message=f"Added {added} points",
            data={"added": added, "total": total},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v2/collections/{name}/points", tags=["points"])
async def add_points_v2(name: str, request: AddPointsRequestV2):
    """Add points with text for hybrid search (v2 API)."""
    db = get_db()

    collection = _servable(db, name)

    try:
        ids = [p["id"] for p in request.points]
        vectors = [p["vector"] for p in request.points]
        metadata = [p.get("metadata", {}) for p in request.points]
        texts: Any = [p.get("text") for p in request.points]

        build = _mint_build(metadata)
        added = collection.add(ids=ids, vectors=vectors, metadata=metadata, texts=texts)
        # The library's own API saves after every write; a server that is
        # killed rather than stopped must not lose what it was sent.
        collection.save()
        collection.set_meta("index_build_id", build)
        total = collection.count()

        # Emit WebSocket event
        await emit_event(
            "points_added",
            {
                "collection": name,
                "added": added,
                "total": total,
                "ids": ids,
                "has_text": any(texts),
            },
        )

        return ApiResponse(
            ok=True,
            message=f"Added {added} points with text",
            data={"added": added, "total": total},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")


@router.get("/api/v1/collections/{name}/points/{point_id}", tags=["points"])
async def get_point(name: str, point_id: str, req: Request):
    """Get a point by ID."""
    db = get_db()

    collection, principal = _servable_as(db, name, req)

    # Not there, and not yours, are one answer: a 403 here would confirm it exists.
    point = (
        collection.get(point_id, principal=principal)
        if principal is not None
        else collection.get(point_id)
    )
    if point is None:
        raise HTTPException(status_code=404, detail=f"Point '{point_id}' not found")

    return ApiResponse(ok=True, data=point.to_dict())


def has_graph(collection: Any) -> bool:
    """Made for graph search, which is what the Graph tag records: ``mode="graph"`` and ``--mode graph`` set it."""
    return any(str(tag).lower() == "graph" for tag in (collection.info().tags or []))


_GRAPH_STORES: dict = {}


def graph_store_of() -> Optional[Any]:
    """The graph store: the setting, or the data path's own graph folder; None on a server with no path to keep one under."""
    from ..graph_store import GRAPH_STORE_ENV, graph_store

    where = os.environ.get(GRAPH_STORE_ENV, "").strip()
    if not where:
        base = getattr(get_db(), "path", None)
        if not base:
            return None
        where = str(Path(base) / "graph")
    store = _GRAPH_STORES.get(where)
    if store is None:
        store = _GRAPH_STORES[where] = graph_store(where)
    return store


def _no_graph(name: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=(
            f"{name} was not made for graph search, so it has no knowledge graph. A collection gets one when it is "
            'made with mode="graph" in the library, --mode graph with vectrixdb ingest, or the Graph tag over the API.'
        ),
    )


@router.get("/api/v1/collections/{name}/graph", tags=["graph"])
async def get_collection_graph(name: str, limit: int = 500):
    """
    Get knowledge graph data for visualization.

    Returns nodes (entities) and edges (relationships) in Cytoscape.js format.
    Only for a collection made for graph search; the health route's
    ``capabilities.graph`` says which those are.
    """
    db = get_db()

    collection = _servable(db, name)
    if not has_graph(collection):
        raise _no_graph(name)

    # Try to get graph data from GraphRAG pipeline
    try:
        # The kept graph first: one object a collection in the graph store, the same for every server. It says
        # which build it was read from, so the page can say when the index has moved on.
        store = graph_store_of()
        kept = store.get(name) if store is not None else None
        if kept is not None:
            from ..graph_store import is_stale, nodes_and_edges, written_since

            nodes, edges = nodes_and_edges(kept, limit)
            current = collection.get_meta("index_build_id")
            stale = is_stale(kept, current)
            since = None
            if stale:
                from .inspection import _each_written

                since = written_since(kept, _each_written(collection))
            return ApiResponse(
                ok=True,
                data={
                    "nodes": nodes,
                    "edges": edges,
                    "stats": {
                        "total_entities": len(kept.get("entities") or []),
                        "total_relationships": len(kept.get("relationships") or []),
                        "total_communities": len(set((kept.get("communities") or {}).values())),
                    },
                    "extracted_at": kept.get("extracted_at"),
                    "build": kept.get("build"),
                    "model": kept.get("model"),
                    "current_build": current,
                    "stale": stale,
                    "since": since,
                    "kept_at": store.describe() if store is not None else None,
                },
            )
        # Check if collection has associated GraphRAG data
        graphrag_path = collection.path / "graphrag" if collection.path else None

        if graphrag_path and graphrag_path.exists():
            from ..core.graphrag import GraphStorage, KnowledgeGraph

            storage = GraphStorage(str(graphrag_path / "graph.db"))
            graph = storage.load_graph()

            if graph and not graph.is_empty():
                entities = graph.get_all_entities()[:limit]
                relationships = graph.get_all_relationships(include_superseded=True)[: limit * 2]
                # Level-0 community per entity, for colouring.
                community_of = {}
                try:
                    hierarchy = storage.load_hierarchy()
                    if hierarchy is not None:
                        for entity_id, levels in hierarchy.entity_to_community.items():
                            if 0 in levels:
                                community_of[entity_id] = levels[0]
                except Exception:  # noqa: BLE001 - colouring is optional
                    community_of = {}

                # Convert to Cytoscape.js format
                nodes = []
                for entity in entities:
                    node_type = entity.type.lower() if hasattr(entity, "type") else "concept"
                    nodes.append(
                        {
                            "data": {
                                "id": entity.id,
                                "label": entity.name,
                                "type": node_type,
                                "description": getattr(entity, "description", ""),
                                "importance": getattr(entity, "importance", 0.5),
                                "community": community_of.get(entity.id),
                            }
                        }
                    )

                edges = []
                for rel in relationships:
                    edges.append(
                        {
                            "data": {
                                "id": rel.id,
                                "source": rel.source_id,
                                "target": rel.target_id,
                                "label": rel.type if hasattr(rel, "type") else "RELATED_TO",
                                "description": getattr(rel, "description", ""),
                                "strength": getattr(rel, "strength", 0.5),
                                "superseded": bool(getattr(rel, "superseded_by", None)),
                                "valid_from": getattr(rel, "valid_from", None),
                                "valid_to": getattr(rel, "valid_to", None),
                            }
                        }
                    )

                return ApiResponse(
                    ok=True,
                    data={
                        "nodes": nodes,
                        "edges": edges,
                        "stats": {
                            "total_entities": len(entities),
                            "total_relationships": len(edges),
                            "total_communities": len(set(community_of.values())),
                        },
                    },
                )

        # No graph data found - return empty with message
        return ApiResponse(
            ok=True,
            data={
                "nodes": [],
                "edges": [],
                "stats": {"total_entities": 0, "total_relationships": 0},
                "message": "No graph data found. Add documents with GraphRAG enabled to build the knowledge graph.",
            },
        )

    except ImportError:
        raise HTTPException(status_code=500, detail="GraphRAG module not available")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading graph: {str(e)}")


@router.post("/api/v1/collections/{name}/graph/extract", tags=["graph"])
async def extract_graph_entities(name: str):
    """
    Extract entities and relationships from collection documents.

    Runs the GraphRAG pipeline to extract entities using mREBEL model
    and build a knowledge graph for visualization.
    """
    db = get_db()

    collection = _servable(db, name)
    if not has_graph(collection):
        raise _no_graph(name)

    try:
        from ..core.graphrag import GraphRAGPipeline, GraphRAGConfig
        from ..core.graphrag.graph.storage import GraphStorage
        from ..core.graphrag.graph.knowledge_graph import KnowledgeGraph

        # Get all documents from collection
        all_ids = collection.list_ids(limit=1000)
        if not all_ids:
            return ApiResponse(ok=False, message="No documents in collection")

        documents = []
        for point_id in all_ids:
            point = collection.get(point_id)
            if point:
                # Point is a dataclass, access metadata directly
                metadata = getattr(point, "metadata", {}) or {}
                text = metadata.get("text", "") or getattr(point, "text", "")
                if text:
                    documents.append(text)

        if not documents:
            return ApiResponse(ok=False, message="No text content found in documents")

        # Initialize GraphRAG pipeline
        graphrag_path = collection.path / "graphrag" if collection.path else None
        if graphrag_path:
            graphrag_path.mkdir(parents=True, exist_ok=True)

        from ..core.graphrag.config import ExtractorType

        config = GraphRAGConfig(
            enabled=True,
            extractor=ExtractorType.NLP,  # spaCy: faster and more reliable than an LLM here
        )

        pipeline = GraphRAGPipeline(config, path=graphrag_path)

        # Process documents
        stats = pipeline.add_documents(documents)

        # The finished graph goes to the graph store, one object a collection, so every server shows the same one
        # and the page can tell when the index has moved on since.
        store = graph_store_of()
        if store is not None and graphrag_path is not None:
            from ..graph_store import graph_json

            store.put(
                name,
                graph_json(
                    name,
                    GraphStorage(str(graphrag_path / "graph.db")),
                    build=collection.get_meta("index_build_id"),
                    model="spaCy",
                ),
            )

        # Emit WebSocket event
        await emit_event(
            "graph_extracted",
            {
                "collection": name,
                "entities": stats.entities_extracted,
                "relationships": stats.relationships_extracted,
            },
        )

        return ApiResponse(
            ok=True,
            message=f"Extracted {stats.entities_extracted} entities and {stats.relationships_extracted} relationships",
            data={
                "documents_processed": stats.documents_processed,
                "entities_extracted": stats.entities_extracted,
                "relationships_extracted": stats.relationships_extracted,
                "processing_time_ms": stats.processing_time_ms,
            },
        )

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"GraphRAG module not available: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error extracting entities: {str(e)}")


@router.delete("/api/v1/collections/{name}/points", tags=["points"])
async def delete_points(name: str, request: DeletePointsRequest):
    """Delete points from a collection."""
    db = get_db()

    collection = _servable(db, name)

    deleted = collection.delete(request.ids)
    # The library's own API saves after every write; a server that is
    # killed rather than stopped must not lose what it was sent.
    collection.save()
    collection.set_meta("index_build_id", "build_" + uuid.uuid4().hex[:16])
    total = collection.count()

    # Emit WebSocket event
    await emit_event(
        "points_deleted",
        {"collection": name, "deleted": deleted, "total": total, "ids": request.ids},
    )

    return ApiResponse(
        ok=True,
        message=f"Deleted {deleted} points",
        data={"deleted": deleted, "total": total},
    )


@router.get("/api/v1/collections/{name}/points", tags=["points"])
async def list_points(
    name: str,
    req: Request,
    limit: int = Query(default=100, gt=0, le=1000),
    offset: int = Query(default=0, ge=0),
    index: bool = Query(default=False),
    q: Optional[str] = Query(
        default=None,
        description="A find: points whose id, or source when the chunks are kept, holds this.",
    ),
):
    """List points in a collection, a page at a time.

    With ``index=true`` each id comes with where the chunk came from and how
    clean its text is, and never the text. That is what a table of chunks
    needs, and it means a page can list a collection without having been sent
    a word of it. The text is one request per chunk, asked for and recorded.
    ``q`` finds by id, and by source where the chunks are kept; ``total`` is
    then how many match.
    """
    db = get_db()

    collection, principal = _servable_as(db, name, req)

    ids, total = (
        chunk_source.find(collection, q or "", limit, offset, principal)
        if (q or "").strip()
        else chunk_source.page(collection, limit, offset, principal)
    )
    return ApiResponse(
        ok=True,
        data={
            "ids": ids,
            "limit": limit,
            "offset": offset,
            "total": total,
            **({"rows": _index_rows(collection, ids, principal)} if index else {}),
            # Ids and nothing more is all a viewer gets; the page says so rather than showing empty rows.
            **({"text_hidden": True} if not sees_content(req) else {}),
        },
    )


# ============================================================================
# ROUTES: search
# ============================================================================
#
# INPUT   a collection's name and a search body: vectors, text, keywords,
#         sparse vectors, or both
# OUTPUT  results, judged by the policy when there is one, redacted for a
#         read-only key, snipped when asked
#
# Query text is embedded here with the model the collection was written with;
# a text upsert embeds on the way in.


def is_authenticated(request: Request) -> bool:
    """Check if request has valid full API key."""
    if runtime_of(request) is not None:
        return sees_content(request)
    if not _full_key_configured():
        return True  # No key configured = everyone authenticated
    from ..signin.keys import key_matches

    return key_matches(
        presented_key(request),
        get_api_key(),
        os.environ.get("VECTRIXDB_API_KEY_SHA256", "").strip() or None,
    )


def redact_search_results(results_dict: dict) -> dict:
    """Redact sensitive data from search results for read-only users."""
    if "results" in results_dict:
        for result in results_dict["results"]:
            # Hide vector values for read-only users
            if "vector" in result:
                result["vector"] = (
                    f"[{len(result['vector'])} dimensions - hidden]"
                    if isinstance(result.get("vector"), list)
                    else result.get("vector")
                )

            # Partially mask IDs
            if "id" in result and result["id"]:
                id_str = str(result["id"])
                if len(id_str) > 8:
                    result["id"] = id_str[:4] + "***" + id_str[-4:]

    results_dict["_redacted"] = True
    return results_dict


@router.post("/api/v1/collections/{name}/search", tags=["search"])
@router.post("/api/collections/{name}/search", tags=["search"], include_in_schema=False)
async def search(name: str, request: SearchRequest, req: Request):
    """Search for similar vectors."""
    db = get_db()

    collection, principal = _servable_as(db, name, req)
    started = time.perf_counter()

    try:
        results = collection.search(
            query=request.query,
            limit=request.limit,
            filter=request.filter,
            include_vectors=request.include_vectors,
            score_threshold=request.score_threshold,
            use_cache=request.use_cache,
            **({"principal": principal} if principal is not None else {}),
        )
        _record_decision(
            req,
            collection,
            principal,
            json.dumps(list(map(float, request.query))),
            results,
            started,
        )
        results_dict = _snipped(_judged(results.to_dict(), principal), req)

        # Auto-redact for read-only users
        if not is_authenticated(req):
            results_dict = redact_search_results(results_dict)

        return ApiResponse(ok=True, data=results_dict)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Cached embedder instance for text search
_text_embedder = None


def _default_query_model() -> str:
    """The embedder key the library writes vectors with by default.

    Derived, never restated. This was the literal "dense_en", which is a
    second copy of a fact that lives in ``Vectrix``; when the default model
    changed the copy did not, and the API went back to embedding queries in
    a different space from the documents. Reading it from ``Vectrix`` means
    that cannot happen again.
    """
    from ..easy import _EMBEDDED_MODEL_KEYS, Vectrix

    return _EMBEDDED_MODEL_KEYS.get(Vectrix._default_model, Vectrix._default_model)


#: The model the library writes vectors with by default. Query embedding has to
#: happen in the same space, so this is the default for incoming queries too.
DEFAULT_QUERY_MODEL = _default_query_model()


def _default_model_name() -> str:
    """The name the library records for its default model, which is what a
    collection written here records too. Recording the key instead made the
    library warn, on opening it, that it was built with another model."""
    from ..easy import Vectrix

    return Vectrix._default_model


def get_text_embedder(model: Optional[str] = None):
    """Get the cached text embedder used to embed incoming query text.

    This must be the same model that produced the stored vectors. It previously
    called ``DenseEmbedder()`` with no argument, which defaults to the
    multilingual model, while the library writes vectors with the English
    default (``bge_small_en`` from 2.2, ``dense_en`` before it). Embedding a query in a different space from the documents
    gives meaningless similarities: the same sentence embedded by both scored
    0.0148 against itself, so dashboard rankings were effectively random.
    """
    global _text_embedder
    wanted = model or DEFAULT_QUERY_MODEL
    if _text_embedder is not None and getattr(_text_embedder, "_vectrix_model", None) != wanted:
        _text_embedder = None
    if _text_embedder is None:
        try:
            from ..models import DenseEmbedder

            embedder: Any = DenseEmbedder(model=wanted)
            embedder._vectrix_model = wanted  # remembered so a model change is noticed
            _text_embedder = embedder
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"Text embedder not available. Run: vectrixdb download-models. Error: {str(e)}",
            )
    return _text_embedder


@router.post("/api/v1/collections/{name}/text-search", tags=["search"])
@router.post("/api/collections/{name}/text-search", tags=["search"], include_in_schema=False)
async def text_search(name: str, request: TextSearchRequest, req: Request):
    """
    Semantic search using text query.

    The server embeds the query with the model the library writes vectors
    with by default, bge-small-en-v1.5, the English model in the wheel, so the
    query and the documents are in the same space.

    This is the recommended search endpoint for the dashboard and most use cases.
    """
    db = get_db()

    collection, principal = _servable_as(db, name, req)
    started = time.perf_counter()

    # Get embedder and embed query text
    try:
        embedder = get_text_embedder()
        query_vector = embedder.embed(request.query_text)[0].tolist()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to embed query: {str(e)}")

    try:
        # The reranking path takes no principal, so under a policy the plain
        # search runs: a slightly worse order is a fair price for never
        # scoring a document the person may not see.
        if request.rerank and principal is None:
            results = collection.search_with_rerank(
                query=query_vector,
                limit=request.limit,
                rerank_limit=max(50, request.limit * 5),
                filter=request.filter,
                rerank_method="cross_encoder",
                query_text=request.query_text,
            )
        else:
            results = collection.search(
                query=query_vector,
                limit=request.limit,
                filter=request.filter,
                include_vectors=request.include_vectors,
                score_threshold=request.score_threshold,
                use_cache=request.use_cache,
                **({"principal": principal} if principal is not None else {}),
            )
        _record_decision(req, collection, principal, request.query_text, results, started)
        results_dict = _snipped(_judged(results.to_dict(), principal), req)

        # Auto-redact for read-only users
        if not is_authenticated(req):
            results_dict = redact_search_results(results_dict)

        return ApiResponse(ok=True, data=results_dict)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class TextUpsertPoint(BaseModel):
    """Point with text for auto-embedding."""

    id: str = Field(..., description="Point ID")
    text: str = Field(..., description="Text to embed and store")
    payload: Optional[dict] = Field(default=None, description="Additional metadata")


class TextUpsertRequest(BaseModel):
    """Request for text upsert endpoint."""

    points: List[TextUpsertPoint] = Field(..., description="Points with text to embed")


@router.post("/api/v1/collections/{name}/text-upsert", tags=["points"])
@router.post("/api/collections/{name}/text-upsert", tags=["points"], include_in_schema=False)
async def text_upsert(name: str, request: TextUpsertRequest):
    """
    Insert points with automatic text embedding.

    The server embeds each text with the model the library writes vectors
    with by default, bge-small-en-v1.5, and stores the vector and the text,
    so keyword and hybrid search work on it too.
    """
    db = get_db()

    collection = _servable(db, name)

    # Get embedder
    try:
        embedder = get_text_embedder()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Text embedder not available: {str(e)}")

    try:
        ids = []
        vectors = []
        metadata_list = []
        texts = []

        # Embed all texts
        all_texts = [p.text for p in request.points]
        all_embeddings = embedder.embed(all_texts)

        for i, point in enumerate(request.points):
            ids.append(point.id)
            vectors.append(all_embeddings[i].tolist())
            texts.append(point.text)
            # Merge text into payload
            payload = point.payload or {}
            if "text" not in payload:
                payload["text"] = point.text
            metadata_list.append(payload)

        build = _mint_build(metadata_list)
        if collection.get_meta("embedding_model") is None:
            collection.set_meta("embedding_model", _default_model_name())
        added = collection.add(ids=ids, vectors=vectors, metadata=metadata_list, texts=texts)
        # The library's own API saves after every write; a server that is
        # killed rather than stopped must not lose what it was sent.
        collection.save()
        collection.set_meta("index_build_id", build)
        total = collection.count()

        # Emit WebSocket event
        await emit_event(
            "points_added",
            {"collection": name, "added": added, "total": total, "ids": ids, "has_text": True},
        )

        return ApiResponse(
            ok=True,
            message=f"Added {added} points with auto-embedded text",
            data={"added": added, "total": total},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to embed and insert: {str(e)}")


@router.post("/api/v1/collections/{name}/hybrid-search", tags=["search"])
async def hybrid_search(name: str, request: HybridSearchRequest):
    """
    Hybrid search combining vector similarity and keyword matching.

    Uses Reciprocal Rank Fusion (RRF) to combine results from vector
    search and BM25 keyword search for improved relevance.
    """
    db = get_db()

    collection = _servable(db, name)

    try:
        results = collection.hybrid_search(
            query=request.query,
            query_text=request.query_text,
            limit=request.limit,
            filter=request.filter,
            vector_weight=request.vector_weight,
            text_weight=request.text_weight,
            include_vectors=request.include_vectors,
            include_highlights=request.include_highlights,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v1/collections/{name}/text-hybrid-search", tags=["search"])
@router.post("/api/collections/{name}/text-hybrid-search", tags=["search"], include_in_schema=False)
async def text_hybrid_search(name: str, request: TextSearchRequest, req: Request):
    """
    Hybrid search with automatic text embedding.

    Automatically embeds the query text and performs hybrid search
    combining vector similarity and keyword matching (RRF fusion).
    """
    db = get_db()

    collection = _servable(db, name)

    # Check if collection has text index
    info = collection.info()
    if not info.has_text_index:
        raise HTTPException(
            status_code=400,
            detail="Hybrid search requires text index. Create collection with enable_text_index=True",
        )

    # Get embedder and embed query text
    try:
        embedder = get_text_embedder()
        query_vector = embedder.embed(request.query_text)[0].tolist()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to embed query: {str(e)}")

    try:
        results = collection.hybrid_search(
            query=query_vector,
            query_text=request.query_text,
            limit=request.limit,
            filter=request.filter,
            include_vectors=request.include_vectors,
            include_highlights=True,
        )
        return ApiResponse(ok=True, data=_snipped(results.to_dict(), req))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v1/collections/{name}/keyword-search", tags=["search"])
async def keyword_search(name: str, request: KeywordSearchRequest, req: Request):
    """
    Full-text keyword search using BM25 ranking.

    Searches through text content indexed with the vectors.
    """
    db = get_db()

    collection = _servable(db, name)

    try:
        results = collection.keyword_search(
            query_text=request.query_text,
            limit=request.limit,
            filter=request.filter,
            include_highlights=request.include_highlights,
        )
        return ApiResponse(ok=True, data=_snipped(results.to_dict(), req))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v1/collections/{name}/sparse-search", tags=["search"])
async def sparse_search(name: str, request: SparseSearchRequest):
    """
    Search using sparse vectors only.

    Use this for SPLADE, learned sparse embeddings, or custom sparse representations.
    """
    db = get_db()

    collection = _servable(db, name)

    try:
        from ..core.types import SparseVector

        sparse_query = SparseVector(
            indices=request.query.indices,
            values=request.query.values,
        )
        results = collection.sparse_search(
            query=sparse_query,
            limit=request.limit,
            filter=request.filter,
            score_threshold=request.score_threshold,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v1/collections/{name}/dense-sparse-search", tags=["search"])
async def dense_sparse_search(name: str, request: DenseSparseSearchRequest):
    """
    Hybrid search combining dense and sparse vectors (Qdrant-style).

    This is the most powerful search mode, combining:
    - Dense vectors (e.g., sentence-transformers embeddings)
    - Sparse vectors (e.g., SPLADE, learned sparse embeddings)

    Uses Reciprocal Rank Fusion (RRF) to combine results.
    """
    db = get_db()

    collection = _servable(db, name)

    try:
        from ..core.types import SparseVector

        sparse_query = SparseVector(
            indices=request.sparse_query.indices,
            values=request.sparse_query.values,
        )
        results = collection.dense_sparse_search(
            dense_query=request.dense_query,
            sparse_query=sparse_query,
            limit=request.limit,
            filter=request.filter,
            dense_weight=request.dense_weight,
            sparse_weight=request.sparse_weight,
            include_vectors=request.include_vectors,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v2/collections/{name}/points/sparse", tags=["points"])
async def add_points_with_sparse(name: str, request: AddPointsWithSparseRequest):
    """Add points with sparse vectors for hybrid dense+sparse search."""
    db = get_db()

    collection = _servable(db, name)

    try:
        from ..core.types import SparseVector

        ids = [p["id"] for p in request.points]
        vectors = [p["vector"] for p in request.points]
        metadata = [p.get("metadata", {}) for p in request.points]

        # Parse sparse vectors
        sparse_vectors: List[Any] = []
        for p in request.points:
            sv = p.get("sparse_vector")
            if sv:
                sparse_vectors.append(
                    SparseVector(
                        indices=sv["indices"],
                        values=sv["values"],
                    )
                )
            else:
                sparse_vectors.append(None)

        build = _mint_build(metadata)
        added = collection.add(
            ids=ids,
            vectors=vectors,
            metadata=metadata,
            sparse_vectors=sparse_vectors,
        )
        # The library's own API saves after every write; a server that is
        # killed rather than stopped must not lose what it was sent.
        collection.save()
        collection.set_meta("index_build_id", build)
        total = collection.count()

        # Emit WebSocket event
        await emit_event(
            "points_added",
            {"collection": name, "added": added, "total": total, "ids": ids, "has_sparse": True},
        )

        return ApiResponse(
            ok=True,
            message=f"Added {added} points with sparse vectors",
            data={"added": added, "total": total},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")


# ============================================================================
# ROUTES: enterprise search
# ============================================================================
#
# INPUT   a search body with reranking, facets, an ACL, or all of them
# OUTPUT  two-stage retrieval, faceted counts, ACL-filtered results, or the
#         whole set at once
#
# The same search, with each feature switched on by its body.


@router.post("/api/v1/collections/{name}/search/rerank", tags=["enterprise-search"])
async def search_with_rerank(name: str, request: RerankSearchRequest):
    """
    Two-stage retrieval: fast ANN search followed by precise re-ranking.

    Methods:
    - exact: Recalculate exact distances
    - mmr: Maximal Marginal Relevance (balances relevance + diversity)
    - cross_encoder: Neural re-ranking (requires query_text)
    - weighted: Weighted combination of scores
    """
    db = get_db()

    collection = _servable(db, name)

    try:
        results = collection.search_with_rerank(
            query=request.query,
            limit=request.limit,
            rerank_limit=request.rerank_limit,
            filter=request.filter,
            rerank_method=request.rerank_method,
            diversity_lambda=request.diversity_lambda,
            query_text=request.query_text,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v1/collections/{name}/search/facets", tags=["enterprise-search"])
async def search_with_facets(name: str, request: FacetedSearchRequest):
    """
    Search with faceted aggregations.

    Returns search results plus counts/aggregations for specified fields.
    Useful for building filter UIs, category browsers, etc.
    """
    db = get_db()

    collection = _servable(db, name)

    try:
        facet_fields: List[Any] = list(request.facets)
        results = collection.search_with_facets(
            query=request.query,
            limit=request.limit,
            filter=request.filter,
            facets=facet_fields,
            facet_limit=request.facet_limit,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v1/collections/{name}/search/acl", tags=["enterprise-search"])
async def search_with_acl(name: str, request: ACLSearchRequest):
    """
    Search with ACL-based security filtering.

    Only returns results the user is authorized to see based on their principals.

    Documents should have ACL metadata like:
    {"_acl": ["user:alice", "group:engineering"]}
    """
    db = get_db()

    collection = _servable(db, name)

    try:
        results = collection.search_with_acl(
            query=request.query,
            user_principals=request.user_principals,
            limit=request.limit,
            filter=request.filter,
            acl_field=request.acl_field,
            default_allow=request.default_allow,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/v1/collections/{name}/search/enterprise", tags=["enterprise-search"])
async def enterprise_search(name: str, request: EnterpriseSearchRequest):
    """
    Full enterprise search combining all advanced features:
    - Vector similarity search
    - ACL-based security filtering
    - Faceted aggregations
    - Re-ranking for higher precision

    This is the most feature-complete search endpoint.
    """
    db = get_db()

    collection = _servable(db, name)

    try:
        results = collection.enterprise_search(
            query=request.query,
            limit=request.limit,
            filter=request.filter,
            query_text=request.query_text,
            user_principals=request.user_principals,
            default_allow=request.default_allow,
            facets=request.facets,
            rerank=request.rerank,
            rerank_method=request.rerank_method,
            rerank_limit=request.rerank_limit,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# ROUTES: the cache
# ============================================================================
#
# INPUT   nothing
# OUTPUT  the cache's statistics; the cache cleared
#
# Two routes, for an operator.


@router.get("/api/v1/cache/stats", tags=["cache"])
async def get_cache_stats():
    """Get cache statistics."""
    db = get_db()
    stats = db.get_cache_stats()
    return ApiResponse(ok=True, data=stats)


@router.delete("/api/v1/cache", tags=["cache"])
async def clear_cache():
    """Clear all cached data."""
    db = get_db()
    db.clear_cache()
    return ApiResponse(ok=True, message="Cache cleared")


# ============================================================================
# ROUTES: resources
# ============================================================================
#
# INPUT   nothing
# OUTPUT  resource utilisation now, and the extended information with storage,
#         cache and scaling
#
# What the Overview's tiles and the Console read.


@router.get("/api/v1/resources", tags=["monitoring"])
async def get_resource_stats():
    """Get current resource utilization stats."""
    db = get_db()
    stats = db.get_resource_stats()
    return ApiResponse(ok=True, data=stats)


@router.get("/api/v1/info/extended", tags=["info"])
async def get_extended_info():
    """
    Get extended database information including storage, cache, and scaling stats.
    """
    db = get_db()
    info = db.extended_info()
    return ApiResponse(ok=True, data=info)


# ============================================================================
# ROUTES: the document index
# ============================================================================
#
# INPUT   a document to index; a document's id; a page, with a find
# OUTPUT  the index or a page of it; a document with its tree; a document
#         indexed, deleted, or cut into chunks for embedding
#
# The document index is the library's, and these are its face over REST.


class IndexDocumentRequest(BaseModel):
    """Request to index a document."""

    text: str = Field(..., min_length=1)
    title: Optional[str] = None
    doc_type: str = Field(default="text", description="Document type: text, markdown, pdf")
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/v1/documents", tags=["documents"])
async def list_documents(
    q: Optional[str] = None,
    limit: int = Query(default=0, ge=0),
    offset: int = Query(default=0, ge=0),
):
    """The document index, or a page of it: ``q`` finds by title, id or type; ``limit`` 0 is every document; ``total`` is how many match."""
    db = get_db()
    try:
        docs = db.documents.list_documents()
        needle = (q or "").strip().lower()
        if needle:
            docs = [
                d
                for d in docs
                if needle
                in f"{d.title} {d.doc_id} {getattr(d.doc_type, 'value', d.doc_type)}".lower()
            ]
        total = len(docs)
        docs = docs[offset:] if not limit else docs[offset : offset + limit]
        return {
            "total": total,
            "documents": [
                {
                    "doc_id": doc.doc_id,
                    "title": doc.title,
                    "doc_type": doc.doc_type.value
                    if hasattr(doc.doc_type, "value")
                    else str(doc.doc_type),
                    "page_count": doc.page_count,
                    "section_count": doc.section_count,
                    "node_count": doc.node_count,
                    "indexed_at": doc.indexed_at,
                    "metadata": doc.metadata,
                }
                for doc in docs
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list documents: {str(e)}")


@router.get("/api/v1/documents/{doc_id}", tags=["documents"])
async def get_document(doc_id: str):
    """Get a document with its tree structure."""
    db = get_db()
    try:
        doc = db.documents.get_document(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")

        nodes = db.documents.get_document_nodes(doc_id)
        return {
            "document": {
                "doc_id": doc.doc_id,
                "title": doc.title,
                "doc_type": doc.doc_type.value
                if hasattr(doc.doc_type, "value")
                else str(doc.doc_type),
                "page_count": doc.page_count,
                "section_count": doc.section_count,
                "node_count": doc.node_count,
                "indexed_at": doc.indexed_at,
                "metadata": doc.metadata,
            },
            "nodes": [
                {
                    "node_id": node.node_id,
                    "parent_id": node.parent_id,
                    "level": node.level,
                    "title": node.title,
                    "text": node.text[:200] + "..." if len(node.text) > 200 else node.text,
                    "page_num": node.page_num,
                    "position": node.position,
                }
                for node in nodes
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get document: {str(e)}")


@router.post("/api/v1/documents", tags=["documents"])
async def index_document(request: IndexDocumentRequest):
    """Index a new document."""
    db = get_db()
    try:
        # index_text takes the document id and its content by position; this
        # route passed ``text=`` and no id and raised TypeError on every call.
        doc_info = db.documents.index_text(
            uuid.uuid4().hex,
            request.text,
            title=request.title,
            doc_type=request.doc_type,
            metadata=request.metadata,
        )
        await emit_event("document_indexed", {"doc_id": doc_info.doc_id, "title": doc_info.title})
        return {
            "ok": True,
            "document": {
                "doc_id": doc_info.doc_id,
                "title": doc_info.title,
                "node_count": doc_info.node_count,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to index document: {str(e)}")


@router.delete("/api/v1/documents/{doc_id}", tags=["documents"])
async def delete_document(doc_id: str):
    """Delete an indexed document."""
    db = get_db()
    try:
        success = db.documents.delete_document(doc_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
        await emit_event("document_deleted", {"doc_id": doc_id})
        return {"ok": True, "deleted": doc_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete document: {str(e)}")


@router.get("/api/v1/documents/{doc_id}/chunks", tags=["documents"])
async def get_document_chunks(doc_id: str, chunk_size: int = Query(default=512, ge=100, le=2000)):
    """Get chunks from a document for embedding."""
    db = get_db()
    try:
        chunks = db.documents.get_chunks(doc_id, chunk_size=chunk_size)
        if not chunks:
            raise HTTPException(
                status_code=404, detail=f"Document '{doc_id}' not found or has no chunks"
            )
        return {
            "doc_id": doc_id,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "node_id": chunk.node_id,
                    "text": chunk.text,
                    "heading": chunk.heading,
                    "level": chunk.level,
                    "page_num": chunk.page_num,
                    "position": chunk.position,
                }
                for chunk in chunks
            ],
            "total": len(chunks),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get chunks: {str(e)}")


# ============================================================================
# MAIN SCRIPT: running the server
# ============================================================================
#
# INPUT   host, port, the data path, a key, and the options
# OUTPUT  uvicorn serving the app; a refusal to listen beyond this machine
#         with no key and no sign-in
#
# An open server on a network address is the one mistake that cannot be undone
# from here, so it is refused unless VECTRIXDB_ALLOW_OPEN says otherwise.

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def refuse_open_server(host: str, *, api_key: Optional[str] = None) -> None:
    """Refuse to listen beyond this machine with no key and no sign-in.

    An open server is right for a laptop. Bound to every interface it is
    every chunk of every collection, readable by whoever finds the port, and
    the default host used to make that the out-of-the-box behaviour. Somebody
    who really means it, behind a gateway that does the asking, says so with
    ``VECTRIXDB_ALLOW_OPEN=1``.
    """
    if host in _LOOPBACK or api_key or os.environ.get("VECTRIXDB_SIGNIN", "").strip():
        return
    if os.environ.get("VECTRIXDB_ALLOW_OPEN", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.warning(
            "listening on %s with no API key and no sign-in, because VECTRIXDB_ALLOW_OPEN is set",
            host,
        )
        return
    raise ConfigurationError(
        f"refusing to listen on {host} with no API key and no sign-in: anybody who can reach the port could read "
        "every collection. Set VECTRIXDB_API_KEY, or turn sign-in on with VECTRIXDB_SIGNIN, or listen on 127.0.0.1. "
        "If something in front of this server does the asking, set VECTRIXDB_ALLOW_OPEN=1."
    )


def run_server(
    host: str = "127.0.0.1",
    port: int = 7337,
    db_path: str = "./vectrixdb_data",
    reload: bool = False,
    api_key: Optional[str] = None,
    read_only_key: Optional[str] = None,
    enable_dashboard: bool = True,
):
    """Run the VectrixDB server."""
    import uvicorn

    refuse_open_server(
        host,
        api_key=api_key
        or get_api_key()
        or os.environ.get("VECTRIXDB_API_KEY_SHA256", "").strip()
        or None,
    )
    os.environ["VECTRIXDB_PATH"] = db_path
    os.environ["VECTRIXDB_DASHBOARD"] = "1" if enable_dashboard else "0"
    if api_key:
        os.environ["VECTRIXDB_API_KEY"] = api_key
    if read_only_key:
        os.environ["VECTRIXDB_READ_ONLY_API_KEY"] = read_only_key

    uvicorn.run(
        "vectrixdb.api.server:app",
        host=host,
        port=port,
        reload=reload,
        # "server: uvicorn" tells whoever finds the port what to look up.
        server_header=False,
    )


if __name__ == "__main__":
    run_server()


# The default application. run_server starts it by import string, so it cannot
# pass arguments: the database path and the dashboard switch travel in the
# environment, which run_server sets after this module is imported. So the
# application is built the first time something asks for it, not at import.
# Built at import, it opened the sign-in file under the default path, in
# whatever folder the server was started from, and ignored --no-dashboard.
app: FastAPI


def __getattr__(name: str) -> Any:
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global app
    app = create_app(
        enable_dashboard=os.environ.get("VECTRIXDB_DASHBOARD", "1").lower()
        not in ("0", "false", "no")
    )
    return app
