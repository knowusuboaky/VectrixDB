"""Sources over REST: the feeds and pages a collection keeps up with, and the refresh a scheduler calls.

    GET    /api/v1/collections/{name}/sources                the sources, as kept
    POST   /api/v1/collections/{name}/sources                {"address": "https://example.com/feed.xml", "every": "6h"}
    DELETE /api/v1/collections/{name}/sources/{id}           ?delete_documents=true takes its documents too
    POST   /api/v1/collections/{name}/sources/refresh        {"force": false, "max_items": 50}

Listing is for anybody who may list where a collection's chunks came from;
adding, removing and refreshing are writes, an operator's or an admin's, and
every one of them is in the access log.

These are the only routes that fetch an address a request names, so every
fetch goes through the guard in :mod:`vectrixdb._fetch`: http and https only,
never a private, loopback or metadata address however a name resolves, every
redirect checked again, size and time capped, robots.txt obeyed and requests
to one host spaced. ``VECTRIXDB_SOURCES_HOSTS`` narrows it to the hosts named;
``VECTRIXDB_SOURCES_INTERNAL_HOSTS`` names the intranet hosts that may be
private. An address that reads ``${NAME}`` from the environment is refused
here: a request could otherwise have the server send its own settings to any
host. Add one of those from Python or the command line on the server.

A scheduler calls the refresh route, cron with curl, a Kubernetes CronJob, an
Azure Functions timer, and a refresh that overlaps another leaves the sources
the other is reading alone, on this server or any other: see
docs/how-to/sources.md. Like the document routes, these refuse a collection
that carries an entitlement policy.
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import tracing
from ..exceptions import ConfigurationError, DependencyError, ExtractionError
from ..sources import MAX_ITEMS, Sources, placeholders
from . import chunk_source

__all__ = ["router"]


# ============================================================================
# SETTINGS: the router, and what a request sends
# ============================================================================
#
# The router, and the two bodies: a source to add, a refresh to run.

router = APIRouter()


class AddSourceRequest(BaseModel):
    address: str = Field(..., max_length=2048, description="The feed's or the page's address.")
    every: Union[str, float] = Field(
        "6h", description="How often it is read: 30m, 6h, 1d, or seconds."
    )
    kind: Optional[str] = Field(
        None,
        pattern="^(feed|page)$",
        description="feed or page. Left out, it is fetched once to tell.",
    )
    articles: Optional[bool] = Field(
        None, description="A feed: index the article each entry links to."
    )
    transcribe: Optional[bool] = Field(
        None, description="A podcast: transcribe episodes when an audio extractor is configured."
    )
    delete_when_gone: Optional[bool] = Field(
        None, description="A page: remove its chunks when it answers 404 or 410."
    )


class RefreshRequest(BaseModel):
    force: bool = Field(False, description="Every source, not only the ones that are due.")
    source: Optional[str] = Field(None, description="One source's id or address.")
    max_items: int = Field(MAX_ITEMS, ge=1, le=500, description="Entries written per source.")


# ============================================================================
# THE SERVER'S WRITER
# ============================================================================
#
# INPUT   a document a source gave, its id, its metadata and its version
# OUTPUT  it cut, embedded and written in place of its last version through
#         the upload route's own code; or its chunks deleted
#
# The same chunks, citations and lineage as a file sent to the documents
# route, and the same document store.


class _ServerWriter:
    def __init__(self, name: str, collection: Any) -> None:
        from .documents import configured_extractors

        self.name = name
        self.collection = collection
        self.extractors = configured_extractors()

    def batch(self) -> Any:
        return contextlib.nullcontext()

    def write(self, doc: Any, doc_id: str, metadata: Dict[str, Any], version: str) -> int:
        from .documents import write_document

        # The span the library's add_document makes for each document a source writes.
        with tracing.span("add_document", collection=self.name, chunking="recursive") as span:
            reply, _ids = write_document(
                self.name,
                self.collection,
                doc,
                doc_id,
                chunk="recursive",
                metadata=metadata,
                source_version=version,
            )
            span.set(chunks=int(reply["chunks"]))
        return int(reply["chunks"])

    def delete(self, doc_id: str) -> int:
        from .documents import store_for

        ids = chunk_source.document_chunks(self.collection, doc_id)
        if ids:
            self.collection.delete(ids)
            self.collection.save()
        store = store_for(self.name)
        if store is not None:
            store.delete(doc_id)
        return len(ids)


def _sources(request: Request, name: str) -> Sources:
    from .server import _servable, get_db

    db = get_db()
    collection = _servable(db, name)
    return Sources(
        name,
        db.sources_store,
        _ServerWriter(name, collection),
        # The tests hand in a fetcher with a fake network; a server makes one
        # from the environment for every refresh.
        fetcher=getattr(request.app.state, "sources_fetcher", None),
    )


def _who(request: Request) -> Optional[str]:
    from .signin import caller_of

    caller = caller_of(request)
    return getattr(caller, "who", None) if caller is not None else None


# ============================================================================
# THE ROUTES
# ============================================================================
#
# INPUT   a collection's name; a source to add; a source's id; a refresh
# OUTPUT  the sources as kept, never a secret; the source added; the source
#         removed; the refresh's report
#
# Fetching and writing run in a worker thread, so a refresh that waits on a
# slow site holds no other request up.


@router.get("/api/v1/collections/{name}/sources", tags=["sources"])
async def list_sources(name: str, request: Request):
    """The feeds and pages this collection keeps up with, when each was read and what it found."""
    sources = _sources(request, name)
    found = await run_in_threadpool(sources.list)
    return {"sources": [s.to_dict() for s in found]}


@router.post("/api/v1/collections/{name}/sources", tags=["sources"])
async def add_source(name: str, body: AddSourceRequest, request: Request):
    """Keep this collection up with a feed or a page. Nothing is written until a refresh."""
    if placeholders(body.address):
        raise HTTPException(
            status_code=400,
            detail="An address that reads ${NAME} from the environment is added from Python or the command "
            "line on the server, not over the API: a request could otherwise have the server send its own "
            "settings to any host.",
        )
    sources = _sources(request, name)
    options = {
        key: value
        for key, value in (
            ("articles", body.articles),
            ("transcribe", body.transcribe),
            ("delete_when_gone", body.delete_when_gone),
        )
        if value is not None
    }
    try:
        info = await run_in_threadpool(
            lambda: sources.add(
                body.address, body.every, kind=body.kind, by=_who(request), **options
            )
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ExtractionError, DependencyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "source": info.to_dict()}


@router.delete("/api/v1/collections/{name}/sources/{source_id}", tags=["sources"])
async def remove_source(
    name: str,
    source_id: str,
    request: Request,
    delete_documents: bool = Query(
        False, description="Take the documents it wrote too, unless another source wrote them."
    ),
):
    """Stop keeping up with a source. Its documents stay unless delete_documents is true."""
    sources = _sources(request, name)
    removed = await run_in_threadpool(
        lambda: sources.remove(source_id, delete_documents=delete_documents)
    )
    if not removed:
        raise HTTPException(status_code=404, detail=f"No source {source_id!r} in {name!r}")
    return {"ok": True, "id": source_id, "documents_deleted": bool(delete_documents)}


@router.post("/api/v1/collections/{name}/sources/refresh", tags=["sources"])
async def refresh_sources(name: str, request: Request, body: Optional[RefreshRequest] = None):
    """Read the sources that are due, every one with force, and write only what changed."""
    from .server import emit_event

    asked = body or RefreshRequest(force=False, source=None, max_items=MAX_ITEMS)
    sources = _sources(request, name)
    try:
        report = await run_in_threadpool(
            lambda: sources.refresh(asked.force, only=asked.source, max_items=asked.max_items)
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if report.added or report.updated or report.removed:
        await emit_event(
            "sources_refreshed",
            {
                "collection": name,
                "added": len(report.added),
                "updated": len(report.updated),
                "removed": len(report.removed),
            },
        )
    return {"ok": report.ok, **report.to_dict()}
