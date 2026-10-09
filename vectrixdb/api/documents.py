"""Documents over REST: send a file, get it read, cut, embedded and written.

    POST   /api/v1/collections/{name}/documents      the file is the request body
    GET    /api/v1/collections/{name}/documents      what is kept
    GET    /api/v1/collections/{name}/documents/{id} the Markdown a document was indexed from
    DELETE /api/v1/collections/{name}/documents/{id} its chunks, and its kept copy moved aside
    GET    /api/v1/extractors                        who reads which file type here

The file travels as the raw request body with its name in ``X-Filename``,
which needs no form parser on either side; a multipart form with a ``file``
field is accepted too when ``python-multipart`` is installed. The chunking
options are query parameters and ``metadata`` is a JSON object in one.

What reads a file is the same registry the library uses. Two settings hand
file types to a service the operator runs:

    VECTRIXDB_EXTRACTOR_URL      https://ai.internal:9001
    VECTRIXDB_EXTRACTOR_ROUTES   {".pdf": "/extract/pdf", ".wav": "/transcribe/audio"}
    VECTRIXDB_EXTRACTOR_BODY     raw (default) or multipart

and ``VECTRIXDB_KEEP_SOURCE=1`` keeps the Markdown beside each collection,
which is what the two GET routes serve. Given a folder, ``s3://bucket/prefix``
or a Blob address instead, they serve what another process keeps there, one
folder a collection.

Like every data route, these refuse a policied collection: a document route
that served one would hand out whole documents with nobody's entitlements
checked. And these routes never fetch an address a request names: the
sources routes do, for operators, through the guard in vectrixdb._fetch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .. import tracing
from ..exceptions import DependencyError, ExtractionError, ExtractionQualityError
from . import chunk_source

__all__ = [
    "router",
    "MAX_UPLOAD_BYTES",
    "configured_extractors",
    "keeps_source",
    "store_for",
    "write_document",
]


# ============================================================================
# SETTINGS: the router, the upload limit, and the default routes
# ============================================================================
#
# The most a file may weigh, and the extraction routes a server uses when the
# environment names none.

router = APIRouter()

#: The largest file the route reads. A request that says it is bigger is
#: refused before its body is read.
MAX_UPLOAD_BYTES = int(os.environ.get("VECTRIXDB_MAX_UPLOAD_BYTES") or 100 * 1024 * 1024)

DEFAULT_ROUTES = {
    ".pdf": "/extract/pdf",
    ".docx": "/extract/docx",
    ".doc": "/extract/doc",
    ".png": "/transcribe/image",
    ".jpg": "/transcribe/image",
    ".jpeg": "/transcribe/image",
    ".wav": "/transcribe/audio",
    ".mp3": "/transcribe/audio",
    ".m4a": "/transcribe/audio",
    ".mp4": "/transcribe/video",
}


# ============================================================================
# WHO READS, AND WHERE DOCUMENTS ARE KEPT
# ============================================================================
#
# INPUT   the environment
# OUTPUT  the extractors the environment asks for, or None for the built-in
#         readers; whether this server keeps the Markdown documents were
#         indexed from; a collection's document store, or None; which file
#         types are read, and by whom
#
# Nothing is opened to answer whether documents are kept, and a client is made
# once a process: a client is not free to make.


def configured_extractors() -> Any:
    """The extractors the environment asks for, or None for the built-in readers."""
    from ..exceptions import ConfigurationError
    from ..extract import HttpExtractor

    try:
        return HttpExtractor.from_environment(routes=dict(DEFAULT_ROUTES))
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


#: What VECTRIXDB_KEEP_SOURCE says to keep nothing, and to keep it beside each collection.
_OFF = ("", "0", "false", "no", "off")
_BESIDE = ("1", "true", "yes", "on")


def _kept_where() -> str:
    return (os.environ.get("VECTRIXDB_KEEP_SOURCE") or "").strip()


def keeps_source() -> bool:
    """Does this server keep the Markdown documents were indexed from? Nothing is opened to answer."""
    where = _kept_where()
    if where.lower() in _OFF:
        return False
    if where.lower() in _BESIDE:
        from .server import get_db

        return getattr(get_db(), "path", None) is not None
    return True


def store_for(name: str) -> Any:
    """This collection's document store, or None when the server keeps none.

    ``VECTRIXDB_KEEP_SOURCE`` says where. ``1`` keeps it beside each
    collection under the server's data path. A folder, ``s3://bucket/prefix``
    or a Blob address, ``https://<account>.blob.core.windows.net/<container>/<prefix>``,
    is where another process keeps it too, one folder a collection inside it:
    the Markdown a function ingesting files wrote is then what this serves.
    """
    where = _kept_where()
    if where.lower() in _OFF:
        return None
    from ..documents import DocumentStore, LocalFiles

    if where.lower() in _BESIDE:
        from .server import get_db

        base = getattr(get_db(), "path", None)
        if base is None:
            return None
        return DocumentStore(LocalFiles(Path(base) / f"{name}.documents"))
    return DocumentStore(_files_at(where, name))


_files: Dict[tuple, Any] = {}


def _files_at(where: str, name: str) -> Any:
    """One collection's folder inside the address ``VECTRIXDB_KEEP_SOURCE`` gives. Made once a process: a client is not free to make."""
    key = (where, name)
    if key not in _files:
        from urllib.parse import urlparse

        from ..documents import BlobFiles, LocalFiles, S3Files
        from ..evaluation import _blob_account, _blob_client, _s3_client

        account = _blob_account(where)
        if where.startswith("s3://"):
            parsed = urlparse(where)
            _files[key] = S3Files(
                _s3_client(), parsed.netloc, f"{parsed.path.strip('/')}/{name}".lstrip("/")
            )
        elif account:
            container, _, prefix = urlparse(where).path.lstrip("/").partition("/")
            if not container:
                raise HTTPException(
                    status_code=500,
                    detail="VECTRIXDB_KEEP_SOURCE is a Blob address with no container in it",
                )
            _files[key] = BlobFiles(
                _blob_client(account), container, f"{prefix.strip('/')}/{name}".lstrip("/")
            )
        else:
            _files[key] = LocalFiles(Path(where) / name)
    return _files[key]


def _readers() -> Dict[str, Any]:
    from ..extract import resolve
    from ..extract.engines import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES

    built_in = [
        ".md",
        ".markdown",
        ".txt",
        ".html",
        ".htm",
        ".csv",
        ".pptx",
        ".pdf",
        ".docx",
        ".doc",
        ".xlsx",
        ".xlsm",
    ]
    local = sorted(IMAGE_SUFFIXES + AUDIO_SUFFIXES + VIDEO_SUFFIXES)
    extractors = configured_extractors()
    routed = resolve(extractors).suffixes() if extractors is not None else resolve(None).suffixes()
    return {"built_in": built_in, "local_engines": local, "extractors": routed}


@router.get("/api/v1/extractors", tags=["documents"])
async def extractors():
    """Which file types this server reads, and who reads them. The Ingest
    page asks, so its drop zone takes what the server takes."""
    readers = _readers()
    accepted = sorted(
        set(readers["built_in"])
        | set(readers["local_engines"])
        | {s for s in readers["extractors"] if s != "*"}
    )
    return {
        **readers,
        "accepted": accepted,
        "extractor_url": bool(os.environ.get("VECTRIXDB_EXTRACTOR_URL")),
        "keeps_source": keeps_source(),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


# ============================================================================
# THE UPLOAD, AND THE ROUTES
# ============================================================================
#
# INPUT   a file as the request body with its name in X-Filename, or a
#         multipart form; a collection's name; a document's id
# OUTPUT  the file read, cut, embedded and written, as add_document does; what
#         is kept, listed; the Markdown a document was indexed from, as text;
#         the document deleted
#
# A file too big is 413, and a multipart form on a server without python-
# multipart says to send the body with X-Filename instead, which needs
# nothing.


async def _upload(request: Request) -> tuple:
    """``(bytes, name)`` from a raw body with X-Filename, or a multipart form."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"The file is larger than {MAX_UPLOAD_BYTES} bytes"
        )
    if (request.headers.get("content-type") or "").lower().startswith("multipart/form-data"):
        try:
            form = await request.form()
        except (AssertionError, RuntimeError, ImportError):
            raise HTTPException(
                status_code=415,
                detail="A multipart form needs python-multipart on the server. Send the file as the request body "
                "with its name in X-Filename instead, which needs nothing.",
            )
        part = form.get("file")
        if part is None or not hasattr(part, "read"):
            raise HTTPException(status_code=400, detail="The form has no file field")
        data = await part.read()
        name = getattr(part, "filename", None) or "document.txt"
    else:
        data = await request.body()
        name = unquote(request.headers.get("x-filename") or "")
    if not data:
        raise HTTPException(status_code=400, detail="The request has no file in it")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"The file is larger than {MAX_UPLOAD_BYTES} bytes"
        )
    name = Path(name.replace("\\", "/")).name
    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name the file in X-Filename, for example X-Filename: report.pdf",
        )
    return data, name


@router.post("/api/v1/collections/{name}/documents", tags=["documents"])
async def add_document(
    name: str,
    request: Request,
    doc_id: Optional[str] = Query(
        None, description="The document's id. The file's name when left out."
    ),
    chunk: str = Query("markdown"),
    chunk_size: int = Query(1000, ge=50, le=20000),
    overlap: int = Query(200, ge=0),
    embed_heading: bool = Query(False),
    metadata: Optional[str] = Query(None, description="A JSON object put on every chunk."),
    on_low_quality: str = Query("warn", pattern="^(warn|reject|allow)$"),
):
    """Read a file, cut it, embed it and write it, as ``add_document`` does.

    Sending the same id again replaces the document. The reply says how many
    chunks were written, how the extraction scored, and each chunk's citation.
    """
    from ..ingest import STRATEGIES, SUFFIX_KINDS, load_bytes
    from .server import _servable, emit_event, get_db

    collection = _servable(get_db(), name)
    if chunk not in STRATEGIES or chunk in ("semantic", "llm"):
        raise HTTPException(
            status_code=400, detail="chunk is recursive, sentence, markdown or fixed"
        )
    try:
        extra = json.loads(metadata) if metadata else {}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"metadata is not JSON: {exc}")
    if not isinstance(extra, dict):
        raise HTTPException(status_code=400, detail="metadata is a JSON object")
    # The span the SDK's add_document makes, with the same attributes: never
    # the file's name, its text or its metadata.
    with tracing.span("add_document", collection=name, chunking=chunk) as span:
        data, filename = await _upload(request)
        span.set(kind=SUFFIX_KINDS.get(Path(filename).suffix.lower()))

        try:
            doc = load_bytes(data, filename, extractors=configured_extractors(), source=filename)
        except ExtractionError as exc:
            raise HTTPException(status_code=502 if exc.route else 422, detail=str(exc))
        except DependencyError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        front_id = doc.metadata.get("doc_id")
        document_id = (
            doc_id or (front_id if isinstance(front_id, str) and front_id else None) or filename
        )
        reply, ids = write_document(
            name,
            collection,
            doc,
            document_id,
            chunk=chunk,
            chunk_size=chunk_size,
            overlap=overlap,
            embed_heading=embed_heading,
            metadata=extra,
            on_low_quality=on_low_quality,
            what=filename,
        )
        await emit_event(
            "points_added",
            {"collection": name, "added": reply["chunks"], "total": collection.count(), "ids": ids},
        )
        span.set(chunks=reply["chunks"])
        return reply


def write_document(
    name: str,
    collection: Any,
    doc: Any,
    document_id: str,
    *,
    chunk: str = "markdown",
    chunk_size: int = 1000,
    overlap: int = 200,
    embed_heading: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
    on_low_quality: str = "warn",
    source_version: Optional[str] = None,
    what: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """A document already read, cut, embedded and written in place of the one under its id.

    What the upload route does once the file is read, and what a source the
    server refreshes writes through, so a document is the same chunks,
    citations and lineage however it arrived. Returns the reply the route
    sends and the ids of the chunks written. Raises HTTPException as the
    route answers.
    """
    from ..ingest import prepare_document, texts_to_embed
    from ..quality import DEFAULT_THRESHOLD
    from .server import _mint_build, get_text_embedder

    extra = dict(metadata or {})
    prepared = prepare_document(
        doc,
        document_id,
        chunk=chunk,
        chunk_size=chunk_size,
        overlap=overlap,
        metadata=extra,
        embed_heading=embed_heading,
    )
    if prepared is None:
        raise HTTPException(
            status_code=422, detail=f"Nothing could be read from {what or document_id}"
        )
    if prepared.quality < DEFAULT_THRESHOLD and on_low_quality == "reject":
        raise HTTPException(
            status_code=422,
            detail=str(ExtractionQualityError(document_id, prepared.quality, DEFAULT_THRESHOLD)),
        )

    try:
        embedder = get_text_embedder()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Text embedder not available: {exc}")
    try:
        vectors = embedder.embed(texts_to_embed(prepared.texts, prepared.metadata))
        metas = [
            dict(m, text=t) if "text" not in m else dict(m)
            for m, t in zip(prepared.metadata, prepared.texts)
        ]
        build = _mint_build(metas)
        stale = chunk_source.document_chunks(collection, document_id)
        if stale:
            collection.delete(stale)
        if collection.get_meta("embedding_model") is None:
            from .server import _default_model_name

            collection.set_meta("embedding_model", _default_model_name())
        added = collection.add(
            ids=prepared.ids,
            vectors=[v.tolist() for v in vectors],
            metadata=metas,
            texts=prepared.texts,
        )
        collection.save()
        collection.set_meta("index_build_id", build)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    store = store_for(name)
    if store is not None:
        store.put(
            document_id,
            doc,
            source_version=source_version,
            chunking={
                "chunk": chunk,
                "chunk_size": chunk_size,
                "overlap": overlap,
                "embed_heading": embed_heading,
            },
            user_metadata=extra,
        )
    reply = {
        "ok": True,
        "doc_id": document_id,
        "replaced": len(stale),
        "chunks": added,
        "quality": prepared.quality,
        "low_quality": prepared.quality < DEFAULT_THRESHOLD,
        "quality_threshold": DEFAULT_THRESHOLD,
        "extractor": doc.metadata.get("extractor") or "built-in",
        "pages": len(doc.pages),
        "figures": sum(1 for m in prepared.metadata if m.get("figure")),
        "citations": [m["_vx_citation"] for m in prepared.metadata],
        "build": build,
        "kept": store is not None,
    }
    return reply, list(prepared.ids)


def _kept(name: str) -> Any:
    from .server import _servable, get_db

    _servable(get_db(), name)
    store = store_for(name)
    if store is None:
        raise HTTPException(
            status_code=404,
            detail="This server does not keep documents. Start it with VECTRIXDB_KEEP_SOURCE=1 and the "
            "Markdown each document was indexed from is kept and served here.",
        )
    return store


@router.get("/api/v1/collections/{name}/documents", tags=["documents"])
async def list_documents(name: str):
    store = _kept(name)
    return {
        "documents": [
            {"doc_id": doc_id, **entry} for doc_id, entry in sorted(store.entries().items())
        ]
    }


@router.get("/api/v1/collections/{name}/documents/{doc_id:path}", tags=["documents"])
async def get_document(
    name: str, doc_id: str, raw: bool = Query(False, description="With the front matter.")
):
    """The Markdown a document was indexed from, as text."""
    from ..exceptions import DocumentNotFoundError
    from ..ingest import split_front_matter

    store = _kept(name)
    try:
        text = store.markdown(doc_id)
    except DocumentNotFoundError:
        raise HTTPException(status_code=404, detail=f"No kept document {doc_id!r}")
    return PlainTextResponse(
        text if raw else split_front_matter(text)[1], media_type="text/markdown; charset=utf-8"
    )


@router.delete("/api/v1/collections/{name}/documents/{doc_id:path}", tags=["documents"])
async def delete_document(name: str, doc_id: str):
    from .server import _servable, get_db

    collection = _servable(get_db(), name)
    ids = chunk_source.document_chunks(collection, doc_id)
    if ids:
        collection.delete(ids)
        collection.save()
    store = store_for(name)
    moved = store.delete(doc_id) if store is not None else False
    if not ids and not moved:
        raise HTTPException(status_code=404, detail=f"No document {doc_id!r}")
    return {
        "ok": True,
        "doc_id": doc_id,
        "chunks_removed": len(ids),
        "kept_copy_moved_aside": bool(moved),
    }
