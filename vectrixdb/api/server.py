"""
VectrixDB API Server - FastAPI REST API.

Provides a REST interface to VectrixDB with:
- Collection management
- Vector CRUD operations
- Search endpoints (vector, keyword, hybrid)
- Cache management
- Resource monitoring
- WebSocket for real-time updates

Author: Daddy Nyame Owusu - Boakye
"""

import os
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Set, List

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.database import VectrixDB
from .. import __version__
from ..core.types import DistanceMetric
from ..core.storage import StorageBackend, StorageConfig
from ..core.cache import CacheBackend, CacheConfig
from ..core.scaling import ScalingStrategy, ScalingConfig

# Global database instance
_db: Optional[VectrixDB] = None


# =============================================================================
# WebSocket Connection Manager for Real-Time Updates
# =============================================================================

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

    async def broadcast(self, event: str, data: dict = None):
        """Broadcast an event to all connected clients."""
        message = json.dumps({
            "event": event,
            "data": data or {},
            "timestamp": datetime.now().isoformat()
        })

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


async def emit_event(event: str, data: dict = None):
    """Helper to emit WebSocket events from anywhere in the server."""
    await ws_manager.broadcast(event, data)


# =============================================================================
# API Key Authentication (Qdrant-style)
# =============================================================================

# API keys - read at runtime to allow setting before server start
def get_api_key():
    return os.environ.get("VECTRIXDB_API_KEY") or None

def get_read_only_key():
    return os.environ.get("VECTRIXDB_READ_ONLY_API_KEY") or None

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


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """
    Qdrant-style API key authentication middleware.

    Behavior:
    - No API key configured: Full access to everyone
    - API key configured:
      - No key provided: Read-only access (GET, HEAD, OPTIONS only)
      - Read-only key: Read-only access
      - Full API key: Full access (all methods)

    This allows viewing collections/data without a key, but requires
    authentication for write operations (create, update, delete).
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        # Skip auth for public paths (docs, health, etc.)
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Skip auth for dashboard static files
        if path.startswith("/dashboard"):
            return await call_next(request)

        # Get configured keys at runtime
        configured_key = get_api_key()
        readonly_key = get_read_only_key()

        # If no API key configured, allow everything
        if not configured_key:
            return await call_next(request)

        # Get API key from header
        api_key = request.headers.get("api-key")

        # Full API key = full access
        if api_key == configured_key:
            return await call_next(request)

        # Read-only API key = read-only access
        if api_key == readonly_key and readonly_key:
            if method in READ_ONLY_METHODS:
                return await call_next(request)
            else:
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "message": "Read-only API key cannot perform write operations",
                        "data": None
                    }
                )

        # No key provided = read-only access (Qdrant-style)
        if not api_key:
            if method in READ_ONLY_METHODS:
                return await call_next(request)
            else:
                return JSONResponse(
                    status_code=401,
                    content={
                        "ok": False,
                        "message": "API key required for write operations. Provide api-key header.",
                        "data": None
                    },
                    headers={"WWW-Authenticate": "ApiKey"}
                )

        # Invalid key provided
        return JSONResponse(
            status_code=401,
            content={
                "ok": False,
                "message": "Invalid API key",
                "data": None
            },
            headers={"WWW-Authenticate": "ApiKey"}
        )


import time


def get_db() -> VectrixDB:
    """Get the database instance."""
    global _db
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


# =============================================================================
# Pydantic Models
# =============================================================================


class CreateCollectionRequest(BaseModel):
    """Request to create a collection."""

    name: str = Field(..., min_length=1, max_length=100)
    dimension: int = Field(..., gt=0, le=65536)
    metric: str = Field(default="cosine")
    description: Optional[str] = None
    tags: Optional[List[str]] = Field(default=None, description="Capability tags: Dense, Sparse, Hybrid, Ultimate, Graph")

    model_config = {"json_schema_extra": {"example": {"name": "documents", "dimension": 384, "metric": "cosine", "tags": ["Dense", "Hybrid"]}}}


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
        "json_schema_extra": {"example": {"query": [0.1, 0.2, 0.3], "limit": 10, "filter": {"category": "tech"}}}
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

    Automatically embeds text using the bundled multilingual-e5-small model.
    No need to compute vectors manually - just provide the query text.
    """

    query_text: str = Field(..., description="Search query text (auto-embedded)")
    limit: int = Field(default=10, gt=0, le=1000)
    filter: Optional[dict[str, Any]] = None
    include_vectors: bool = False
    score_threshold: Optional[float] = None
    use_cache: bool = True

    model_config = {
        "json_schema_extra": {
            "example": {
                "query_text": "what is machine learning?",
                "limit": 10,
                "filter": {"category": "tech"}
            }
        }
    }


class SparseVectorData(BaseModel):
    """Sparse vector representation."""

    indices: list[int] = Field(..., description="Non-zero dimension indices")
    values: list[float] = Field(..., description="Corresponding values")

    model_config = {
        "json_schema_extra": {
            "example": {"indices": [0, 42, 100], "values": [0.5, 1.2, 0.8]}
        }
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
    diversity_lambda: float = Field(default=0.5, ge=0.0, le=1.0, description="MMR diversity (0=diverse, 1=relevant)")
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
    user_principals: list[str] = Field(..., description="User's principals e.g. ['user:alice', 'group:eng']")
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
    tags: Optional[List[str]] = Field(default=None, description="Capability tags: Dense, Sparse, Hybrid, Ultimate, Graph")

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


# =============================================================================
# Application Setup
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _db

    # Startup - Get configuration from environment
    db_path = os.environ.get("VECTRIXDB_PATH", "./vectrixdb_data")

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
        strategy=ScalingStrategy(scaling_strategy) if scaling_strategy != "none" else ScalingStrategy.NONE,
        memory_high_watermark=float(os.environ.get("VECTRIXDB_MAX_MEMORY_PERCENT", "85")),
    )

    # Initialize database
    _db = VectrixDB(
        path=db_path if storage_backend != "memory" else None,
        storage_config=storage_config,
        cache_config=cache_config,
        scaling_config=scaling_config,
    )

    print(f"[VectrixDB] Database initialized at: {db_path}")
    print(f"[VectrixDB] Storage: {storage_backend}")
    print(f"[VectrixDB] Cache: {cache_backend}")
    print(f"[VectrixDB] Collections: {len(_db)}")

    yield

    # Shutdown
    if _db:
        _db.close()
        print("[VectrixDB] Database closed")


def create_app(
    db_path: Optional[str] = None,
    enable_dashboard: bool = True,
) -> FastAPI:
    """
    Create the FastAPI application.

    Args:
        db_path: Database path (default: ./vectrixdb_data)
        enable_dashboard: Serve dashboard static files

    Returns:
        FastAPI application
    """
    global _db

    app = FastAPI(
        title="VectrixDB",
        description="Where vectors come alive - A modern, visual-first vector database",
        version=__version__,
        lifespan=lifespan,
    )

    # API Key Authentication Middleware (Qdrant-style)
    app.add_middleware(ApiKeyAuthMiddleware)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", "api-key"],
    )

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
            app.mount("/dashboard", StaticFiles(directory=str(dashboard_path), html=True), name="dashboard")
            print(f"[VectrixDB] Dashboard mounted from: {dashboard_path}")

    return app


# Create default app
app = create_app()


# =============================================================================
# Routes - Health & Info
# =============================================================================


@app.get("/", tags=["info"])
async def root():
    """Root endpoint."""
    return {
        "name": "VectrixDB",
        "tagline": "Where vectors come alive",
        "version": __version__,
        "docs": "/docs",
        "dashboard": "/dashboard",
        "auth_enabled": get_api_key() is not None,
    }


@app.get("/auth/status", tags=["auth"])
async def auth_status():
    """Check if authentication is enabled."""
    return {
        "ok": True,
        "message": None,
        "data": {
            "auth_enabled": get_api_key() is not None,
            "read_only_key_enabled": get_read_only_key() is not None,
        }
    }


@app.get("/health", tags=["info"])
async def health():
    """Health check."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


# =============================================================================
# WebSocket for Real-Time Updates
# =============================================================================

@app.websocket("/ws")
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
    await ws_manager.connect(websocket)
    try:
        # Send initial connection confirmation
        await websocket.send_text(json.dumps({
            "event": "connected",
            "data": {"message": "Connected to VectrixDB real-time updates"},
            "timestamp": datetime.now().isoformat()
        }))

        # Keep connection alive and handle incoming messages
        while True:
            try:
                # Wait for messages (ping/pong or commands)
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # Handle ping
                if data == "ping":
                    await websocket.send_text(json.dumps({
                        "event": "pong",
                        "data": {},
                        "timestamp": datetime.now().isoformat()
                    }))
            except asyncio.TimeoutError:
                # Send keepalive ping
                try:
                    await websocket.send_text(json.dumps({
                        "event": "keepalive",
                        "data": {},
                        "timestamp": datetime.now().isoformat()
                    }))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket)


@app.get("/api/v1/ws/status", tags=["info"])
async def websocket_status():
    """Get WebSocket connection status."""
    return {
        "active_connections": ws_manager.connection_count,
        "endpoint": "/ws"
    }


@app.get("/api/v1", tags=["info"])
@app.get("/api/v1/", tags=["info"])
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


@app.get("/api/v1/info", tags=["info"])
@app.get("/api/info", tags=["info"], include_in_schema=False)
async def database_info():
    """Get database information."""
    db = get_db()
    info = db.info()
    return {
        "collections_count": info.collections_count,
        "total_vectors": info.total_vectors,
        "total_size_bytes": info.total_size_bytes,
    }


# =============================================================================
# Routes - Collections
# =============================================================================


@app.get("/api/v1/collections", tags=["collections"])
@app.get("/api/collections", tags=["collections"], include_in_schema=False)
async def list_collections():
    """List all collections."""
    db = get_db()
    collections = [c.to_dict() for c in db.list_collections()]
    return {"collections": collections, "total": len(collections)}


@app.post("/api/v1/collections", tags=["collections"])
@app.post("/api/collections", tags=["collections"], include_in_schema=False)
async def create_collection(request: CreateCollectionRequest):
    """Create a new collection."""
    db = get_db()

    try:
        metric = DistanceMetric(request.metric)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid metric: {request.metric}")

    # Auto-tagging: v1 API creates dense-only collections
    tags = list(request.tags) if request.tags else []
    if 'demo' not in tags and 'dense' not in tags and 'hybrid' not in tags:
        tags.append('dense')

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
        await emit_event("collection_created", {
            "name": request.name,
            "dimension": request.dimension,
            "metric": request.metric,
            "tags": request.tags or [],
            "collection": collection_info
        })

        return ApiResponse(ok=True, message=f"Collection '{request.name}' created", data=collection_info)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v2/collections", tags=["collections"])
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
    tier = 'dense'  # default
    for t in ['graph', 'ultimate', 'hybrid', 'dense']:
        if t in tags_lower:
            tier = t
            break

    # Auto-enable features based on tier hierarchy
    # dense < hybrid < ultimate < graph
    enable_text_index = request.enable_text_index
    if tier in ['hybrid', 'ultimate', 'graph']:
        enable_text_index = True  # These tiers require text index for BM25/rerank

    # Normalize tags to just the tier (remove redundant tags)
    if 'demo' not in tags_lower:
        # Keep only the tier tag and demo if present
        tags = [tier]

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
        await emit_event("collection_created", {
            "name": request.name,
            "dimension": request.dimension,
            "metric": request.metric,
            "tier": tier,
            "hybrid_enabled": enable_text_index,
            "collection": collection_info
        })

        tier_features = {
            'dense': 'vector search',
            'hybrid': 'vector + BM25 + rerank',
            'ultimate': 'vector + BM25 + rerank + late interaction',
            'graph': 'vector + BM25 + rerank + late interaction + knowledge graph'
        }

        return ApiResponse(
            ok=True,
            message=f"Collection '{request.name}' created with {tier} tier ({tier_features.get(tier, tier)})",
            data=collection_info,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/v1/collections/{name}", tags=["collections"])
async def get_collection(name: str):
    """Get collection details."""
    db = get_db()

    try:
        collection = db.get_collection(name)
        return ApiResponse(ok=True, data=collection.info().to_dict())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")


@app.delete("/api/v1/collections/{name}", tags=["collections"])
@app.delete("/api/collections/{name}", tags=["collections"], include_in_schema=False)
async def delete_collection(name: str):
    """Delete a collection."""
    try:
        db = get_db()

        if db.delete_collection(name):
            # Emit WebSocket event
            await emit_event("collection_deleted", {"name": name})
            return ApiResponse(ok=True, message=f"Collection '{name}' deleted")
        else:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "message": f"Collection '{name}' not found", "data": None}
            )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": f"Error deleting collection: {str(e)}", "data": None}
        )


# =============================================================================
# Routes - Points
# =============================================================================


@app.post("/api/v1/collections/{name}/points", tags=["points"])
async def add_points(name: str, request: AddPointsRequest):
    """Add points to a collection."""
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    try:
        ids = [p.id for p in request.points]
        vectors = [p.vector for p in request.points]
        metadata = [p.metadata for p in request.points]

        added = collection.add(ids=ids, vectors=vectors, metadata=metadata)
        total = collection.count()

        # Emit WebSocket event
        await emit_event("points_added", {
            "collection": name,
            "added": added,
            "total": total,
            "ids": ids
        })

        return ApiResponse(
            ok=True,
            message=f"Added {added} points",
            data={"added": added, "total": total},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v2/collections/{name}/points", tags=["points"])
async def add_points_v2(name: str, request: AddPointsRequestV2):
    """Add points with text for hybrid search (v2 API)."""
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    try:
        ids = [p["id"] for p in request.points]
        vectors = [p["vector"] for p in request.points]
        metadata = [p.get("metadata", {}) for p in request.points]
        texts = [p.get("text") for p in request.points]

        added = collection.add(ids=ids, vectors=vectors, metadata=metadata, texts=texts)
        total = collection.count()

        # Emit WebSocket event
        await emit_event("points_added", {
            "collection": name,
            "added": added,
            "total": total,
            "ids": ids,
            "has_text": any(texts)
        })

        return ApiResponse(
            ok=True,
            message=f"Added {added} points with text",
            data={"added": added, "total": total},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")


@app.get("/api/v1/collections/{name}/points/{point_id}", tags=["points"])
async def get_point(name: str, point_id: str):
    """Get a point by ID."""
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    point = collection.get(point_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"Point '{point_id}' not found")

    return ApiResponse(ok=True, data=point.to_dict())


@app.get("/api/v1/collections/{name}/graph", tags=["graph"])
async def get_collection_graph(name: str, limit: int = 500):
    """
    Get knowledge graph data for visualization.

    Returns nodes (entities) and edges (relationships) in Cytoscape.js format.
    Only works for collections with GraphRAG enabled (Graph tag).
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    # Check if collection has Graph tag
    info = collection.info()
    if "Graph" not in (info.tags or []) and "graph" not in [t.lower() for t in (info.tags or [])]:
        raise HTTPException(
            status_code=400,
            detail="Collection does not have GraphRAG enabled. Add 'Graph' tag to enable."
        )

    # Try to get graph data from GraphRAG pipeline
    try:
        # Check if collection has associated GraphRAG data
        graphrag_path = collection.path / "graphrag" if collection.path else None

        if graphrag_path and graphrag_path.exists():
            from ..core.graphrag import GraphStorage, KnowledgeGraph

            storage = GraphStorage(graphrag_path / "graph.db")
            graph = storage.load_graph()

            if graph and not graph.is_empty():
                entities = graph.get_all_entities()[:limit]
                relationships = graph.get_all_relationships()[:limit * 2]

                # Convert to Cytoscape.js format
                nodes = []
                for entity in entities:
                    node_type = entity.type.lower() if hasattr(entity, 'type') else 'concept'
                    nodes.append({
                        "data": {
                            "id": entity.id,
                            "label": entity.name,
                            "type": node_type,
                            "description": getattr(entity, 'description', ''),
                            "importance": getattr(entity, 'importance', 0.5),
                        }
                    })

                edges = []
                for rel in relationships:
                    edges.append({
                        "data": {
                            "id": rel.id,
                            "source": rel.source_id,
                            "target": rel.target_id,
                            "label": rel.type if hasattr(rel, 'type') else 'RELATED_TO',
                            "description": getattr(rel, 'description', ''),
                            "strength": getattr(rel, 'strength', 0.5),
                        }
                    })

                return ApiResponse(
                    ok=True,
                    data={
                        "nodes": nodes,
                        "edges": edges,
                        "stats": {
                            "total_entities": len(entities),
                            "total_relationships": len(edges),
                        }
                    }
                )

        # No graph data found - return empty with message
        return ApiResponse(
            ok=True,
            data={
                "nodes": [],
                "edges": [],
                "stats": {"total_entities": 0, "total_relationships": 0},
                "message": "No graph data found. Add documents with GraphRAG enabled to build the knowledge graph."
            }
        )

    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="GraphRAG module not available"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error loading graph: {str(e)}"
        )


@app.post("/api/v1/collections/{name}/graph/extract", tags=["graph"])
async def extract_graph_entities(name: str):
    """
    Extract entities and relationships from collection documents.

    Runs the GraphRAG pipeline to extract entities using mREBEL model
    and build a knowledge graph for visualization.
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    # Check if collection has Graph tag
    info = collection.info()
    if "Graph" not in (info.tags or []) and "graph" not in [t.lower() for t in (info.tags or [])]:
        raise HTTPException(
            status_code=400,
            detail="Collection must have 'graph' tag to enable GraphRAG"
        )

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
                metadata = getattr(point, 'metadata', {}) or {}
                text = metadata.get('text', '') or getattr(point, 'text', '')
                if text:
                    documents.append(text)

        if not documents:
            return ApiResponse(ok=False, message="No text content found in documents")

        # Initialize GraphRAG pipeline
        graphrag_path = collection.path / "graphrag" if collection.path else None
        if graphrag_path:
            graphrag_path.mkdir(parents=True, exist_ok=True)

        config = GraphRAGConfig(
            enabled=True,
            extractor="nlp",  # Use spaCy NLP for entity extraction (faster, more reliable)
        )

        pipeline = GraphRAGPipeline(config, path=graphrag_path)

        # Process documents
        stats = pipeline.add_documents(documents)

        # Emit WebSocket event
        await emit_event("graph_extracted", {
            "collection": name,
            "entities": stats.entities_extracted,
            "relationships": stats.relationships_extracted
        })

        return ApiResponse(
            ok=True,
            message=f"Extracted {stats.entities_extracted} entities and {stats.relationships_extracted} relationships",
            data={
                "documents_processed": stats.documents_processed,
                "entities_extracted": stats.entities_extracted,
                "relationships_extracted": stats.relationships_extracted,
                "processing_time_ms": stats.processing_time_ms
            }
        )

    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"GraphRAG module not available: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error extracting entities: {str(e)}"
        )


@app.delete("/api/v1/collections/{name}/points", tags=["points"])
async def delete_points(name: str, request: DeletePointsRequest):
    """Delete points from a collection."""
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    deleted = collection.delete(request.ids)
    total = collection.count()

    # Emit WebSocket event
    await emit_event("points_deleted", {
        "collection": name,
        "deleted": deleted,
        "total": total,
        "ids": request.ids
    })

    return ApiResponse(
        ok=True,
        message=f"Deleted {deleted} points",
        data={"deleted": deleted, "total": total},
    )


@app.get("/api/v1/collections/{name}/points", tags=["points"])
async def list_points(
    name: str,
    limit: int = Query(default=100, gt=0, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """List points in a collection."""
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    ids = collection.list_ids(limit=limit, offset=offset)
    return ApiResponse(
        ok=True,
        data={
            "ids": ids,
            "limit": limit,
            "offset": offset,
            "total": collection.count(),
        },
    )


# =============================================================================
# Routes - Search
# =============================================================================


def is_authenticated(request: Request) -> bool:
    """Check if request has valid full API key."""
    configured_key = get_api_key()
    if not configured_key:
        return True  # No key configured = everyone authenticated
    api_key = request.headers.get("api-key")
    return api_key == configured_key


def redact_search_results(results_dict: dict) -> dict:
    """Redact sensitive data from search results for read-only users."""
    if "results" in results_dict:
        for result in results_dict["results"]:
            # Hide vector values for read-only users
            if "vector" in result:
                result["vector"] = f"[{len(result['vector'])} dimensions - hidden]" if isinstance(result.get("vector"), list) else result.get("vector")

            # Partially mask IDs
            if "id" in result and result["id"]:
                id_str = str(result["id"])
                if len(id_str) > 8:
                    result["id"] = id_str[:4] + "***" + id_str[-4:]

    results_dict["_redacted"] = True
    return results_dict


@app.post("/api/v1/collections/{name}/search", tags=["search"])
@app.post("/api/collections/{name}/search", tags=["search"], include_in_schema=False)
async def search(name: str, request: SearchRequest, req: Request):
    """Search for similar vectors."""
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    try:
        results = collection.search(
            query=request.query,
            limit=request.limit,
            filter=request.filter,
            include_vectors=request.include_vectors,
            score_threshold=request.score_threshold,
            use_cache=request.use_cache,
        )
        results_dict = results.to_dict()

        # Auto-redact for read-only users
        if not is_authenticated(req):
            results_dict = redact_search_results(results_dict)

        return ApiResponse(ok=True, data=results_dict)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Cached embedder instance for text search
_text_embedder = None

def get_text_embedder():
    """Get cached text embedder (multilingual-e5-small)."""
    global _text_embedder
    if _text_embedder is None:
        try:
            from ..models import DenseEmbedder
            _text_embedder = DenseEmbedder()
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"Text embedder not available. Run: vectrixdb download-models. Error: {str(e)}"
            )
    return _text_embedder


@app.post("/api/v1/collections/{name}/text-search", tags=["search"])
@app.post("/api/collections/{name}/text-search", tags=["search"], include_in_schema=False)
async def text_search(name: str, request: TextSearchRequest, req: Request):
    """
    Semantic search using text query.

    Automatically embeds the query text using the bundled multilingual-e5-small model
    (100+ languages supported). No need to compute vectors manually.

    This is the recommended search endpoint for the dashboard and most use cases.
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    # Get embedder and embed query text
    try:
        embedder = get_text_embedder()
        query_vector = embedder.embed(request.query_text)[0].tolist()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to embed query: {str(e)}")

    try:
        results = collection.search(
            query=query_vector,
            limit=request.limit,
            filter=request.filter,
            include_vectors=request.include_vectors,
            score_threshold=request.score_threshold,
            use_cache=request.use_cache,
        )
        results_dict = results.to_dict()

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


@app.post("/api/v1/collections/{name}/text-upsert", tags=["points"])
@app.post("/api/collections/{name}/text-upsert", tags=["points"], include_in_schema=False)
async def text_upsert(name: str, request: TextUpsertRequest):
    """
    Insert points with automatic text embedding.

    Automatically embeds text using the bundled multilingual-e5-small model
    and stores both the vector and text for hybrid search support.
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

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

        added = collection.add(ids=ids, vectors=vectors, metadata=metadata_list, texts=texts)
        total = collection.count()

        # Emit WebSocket event
        await emit_event("points_added", {
            "collection": name,
            "added": added,
            "total": total,
            "ids": ids,
            "has_text": True
        })

        return ApiResponse(
            ok=True,
            message=f"Added {added} points with auto-embedded text",
            data={"added": added, "total": total},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to embed and insert: {str(e)}")


@app.post("/api/v1/collections/{name}/hybrid-search", tags=["search"])
async def hybrid_search(name: str, request: HybridSearchRequest):
    """
    Hybrid search combining vector similarity and keyword matching.

    Uses Reciprocal Rank Fusion (RRF) to combine results from vector
    search and BM25 keyword search for improved relevance.
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

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


@app.post("/api/v1/collections/{name}/text-hybrid-search", tags=["search"])
@app.post("/api/collections/{name}/text-hybrid-search", tags=["search"], include_in_schema=False)
async def text_hybrid_search(name: str, request: TextSearchRequest):
    """
    Hybrid search with automatic text embedding.

    Automatically embeds the query text and performs hybrid search
    combining vector similarity and keyword matching (RRF fusion).
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    # Check if collection has text index
    info = collection.info()
    if not info.has_text_index:
        raise HTTPException(
            status_code=400,
            detail="Hybrid search requires text index. Create collection with enable_text_index=True"
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
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/collections/{name}/keyword-search", tags=["search"])
async def keyword_search(name: str, request: KeywordSearchRequest):
    """
    Full-text keyword search using BM25 ranking.

    Searches through text content indexed with the vectors.
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    try:
        results = collection.keyword_search(
            query_text=request.query_text,
            limit=request.limit,
            filter=request.filter,
            include_highlights=request.include_highlights,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/collections/{name}/sparse-search", tags=["search"])
async def sparse_search(name: str, request: SparseSearchRequest):
    """
    Search using sparse vectors only.

    Use this for SPLADE, learned sparse embeddings, or custom sparse representations.
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

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


@app.post("/api/v1/collections/{name}/dense-sparse-search", tags=["search"])
async def dense_sparse_search(name: str, request: DenseSparseSearchRequest):
    """
    Hybrid search combining dense and sparse vectors (Qdrant-style).

    This is the most powerful search mode, combining:
    - Dense vectors (e.g., sentence-transformers embeddings)
    - Sparse vectors (e.g., SPLADE, learned sparse embeddings)

    Uses Reciprocal Rank Fusion (RRF) to combine results.
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

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


@app.post("/api/v2/collections/{name}/points/sparse", tags=["points"])
async def add_points_with_sparse(name: str, request: AddPointsWithSparseRequest):
    """Add points with sparse vectors for hybrid dense+sparse search."""
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    try:
        from ..core.types import SparseVector

        ids = [p["id"] for p in request.points]
        vectors = [p["vector"] for p in request.points]
        metadata = [p.get("metadata", {}) for p in request.points]

        # Parse sparse vectors
        sparse_vectors = []
        for p in request.points:
            sv = p.get("sparse_vector")
            if sv:
                sparse_vectors.append(SparseVector(
                    indices=sv["indices"],
                    values=sv["values"],
                ))
            else:
                sparse_vectors.append(None)

        added = collection.add(
            ids=ids,
            vectors=vectors,
            metadata=metadata,
            sparse_vectors=sparse_vectors,
        )
        total = collection.count()

        # Emit WebSocket event
        await emit_event("points_added", {
            "collection": name,
            "added": added,
            "total": total,
            "ids": ids,
            "has_sparse": True
        })

        return ApiResponse(
            ok=True,
            message=f"Added {added} points with sparse vectors",
            data={"added": added, "total": total},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e}")


# =============================================================================
# Routes - Enterprise Search Features
# =============================================================================


@app.post("/api/v1/collections/{name}/search/rerank", tags=["enterprise-search"])
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

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

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


@app.post("/api/v1/collections/{name}/search/facets", tags=["enterprise-search"])
async def search_with_facets(name: str, request: FacetedSearchRequest):
    """
    Search with faceted aggregations.

    Returns search results plus counts/aggregations for specified fields.
    Useful for building filter UIs, category browsers, etc.
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    try:
        results = collection.search_with_facets(
            query=request.query,
            limit=request.limit,
            filter=request.filter,
            facets=request.facets,
            facet_limit=request.facet_limit,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/collections/{name}/search/acl", tags=["enterprise-search"])
async def search_with_acl(name: str, request: ACLSearchRequest):
    """
    Search with ACL-based security filtering.

    Only returns results the user is authorized to see based on their principals.

    Documents should have ACL metadata like:
    {"_acl": ["user:alice", "group:engineering"]}
    """
    db = get_db()

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

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


@app.post("/api/v1/collections/{name}/search/enterprise", tags=["enterprise-search"])
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

    try:
        collection = db.get_collection(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")

    try:
        results = collection.enterprise_search(
            query=request.query,
            limit=request.limit,
            filter=request.filter,
            query_text=request.query_text,
            user_principals=request.user_principals,
            facets=request.facets,
            rerank=request.rerank,
            rerank_method=request.rerank_method,
            rerank_limit=request.rerank_limit,
        )
        return ApiResponse(ok=True, data=results.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Routes - Cache Management
# =============================================================================


@app.get("/api/v1/cache/stats", tags=["cache"])
async def get_cache_stats():
    """Get cache statistics."""
    db = get_db()
    stats = db.get_cache_stats()
    return ApiResponse(ok=True, data=stats)


@app.delete("/api/v1/cache", tags=["cache"])
async def clear_cache():
    """Clear all cached data."""
    db = get_db()
    db.clear_cache()
    return ApiResponse(ok=True, message="Cache cleared")


# =============================================================================
# Routes - Resource Monitoring
# =============================================================================


@app.get("/api/v1/resources", tags=["monitoring"])
async def get_resource_stats():
    """Get current resource utilization stats."""
    db = get_db()
    stats = db.get_resource_stats()
    return ApiResponse(ok=True, data=stats)


@app.get("/api/v1/info/extended", tags=["info"])
async def get_extended_info():
    """
    Get extended database information including storage, cache, and scaling stats.
    """
    db = get_db()
    info = db.extended_info()
    return ApiResponse(ok=True, data=info)


# =============================================================================
# Run Server
# =============================================================================


def run_server(
    host: str = "0.0.0.0",
    port: int = 7337,
    db_path: str = "./vectrixdb_data",
    reload: bool = False,
    api_key: str = None,
    read_only_key: str = None,
):
    """Run the VectrixDB server."""
    import uvicorn

    os.environ["VECTRIXDB_PATH"] = db_path
    if api_key:
        os.environ["VECTRIXDB_API_KEY"] = api_key
    if read_only_key:
        os.environ["VECTRIXDB_READ_ONLY_API_KEY"] = read_only_key

    uvicorn.run(
        "vectrixdb.api.server:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    run_server()
