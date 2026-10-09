"""Talk to a VectrixDB server from Python, with the same calls as ``Vectrix``.

    from vectrixdb import connect

    db = connect("https://vectors.example.com", key="...")
    db.add_document("handbook", open("handbook.pdf", "rb").read(), "handbook.pdf")
    for hit in db.search("handbook", "how long do refunds take"):
        print(hit.score, hit.citation, hit.text)

Code written for a local ``Vectrix`` moves to a server by changing the line
that opens it: ``search`` returns the same ``Results`` with the same
``Result`` objects, citations included. ``AsyncVectrixClient`` is the same
surface with ``await``.

The client needs ``httpx`` (``pip install "vectrixdb[client]"``). It sends
the key in the ``api-key`` header, or a company sign-in token as a bearer
token, retries a 429 or a 503 up to three times honouring ``Retry-After``,
and raises one refusal type per status, all of them ``RequestError``:

    try:
        db.describe("nope")
    except NotFoundError as refused:
        print(refused.status, refused.message)

Every language's client offers this surface; ``sdk/CONTRACT.md`` in the
repository is the shared description.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Union
from urllib.parse import quote

from . import __version__
from .easy import Result, Results
from .exceptions import DependencyError, VectrixError

__all__ = [
    "Added",
    "AsyncVectrixClient",
    "AuthError",
    "BusyError",
    "Collection",
    "ConflictError",
    "ConnectionFailed",
    "Document",
    "ForbiddenError",
    "InvalidError",
    "NotFoundError",
    "Refreshed",
    "RequestError",
    "Source",
    "TooLargeError",
    "VectrixClient",
    "connect",
]

USER_AGENT = f"vectrixdb-python/{__version__}"
RETRIES = 3
BACKOFF = (1.0, 2.0, 4.0)
MODES = {"meaning": "text-search", "hybrid": "text-hybrid-search"}


# ============================================================================
# REFUSALS
# ============================================================================
#
# INPUT   a response the server refused with
# OUTPUT  one exception per status, each carrying status, message and detail


class RequestError(VectrixError):
    """The server refused a request. ``status`` says how, ``message`` says why."""

    def __init__(self, status: int, message: str, detail: Any = None, url: str = "") -> None:
        self.status = status
        self.message = message
        self.detail = detail
        self.url = url
        super().__init__(f"{status}: {message}")


class AuthError(RequestError):
    """401: no key, or a wrong one."""


class ForbiddenError(RequestError):
    """403: the key's role or scope says no."""


class NotFoundError(RequestError):
    """404: no such collection, document or source."""


class ConflictError(RequestError):
    """409: it already exists."""


class TooLargeError(RequestError):
    """413: the upload is bigger than the server takes."""


class InvalidError(RequestError):
    """422: a field is wrong; ``detail`` lists which."""


class BusyError(RequestError):
    """429 or 503, still, after the retries."""


class ConnectionFailed(VectrixError):
    """The server could not be reached at all."""


_KINDS: Dict[int, type] = {
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    413: TooLargeError,
    422: InvalidError,
    429: BusyError,
    503: BusyError,
}


def _refusal(status: int, body: Any, url: str) -> RequestError:
    message, detail = f"{status} from {url}", None
    if isinstance(body, dict):
        said = body.get("message") or body.get("detail")
        if isinstance(said, str) and said:
            message = said
        detail = body.get("detail")
    return _KINDS.get(status, RequestError)(status, message, detail, url)


# ============================================================================
# WHAT THE SERVER SENDS BACK
# ============================================================================
#
# INPUT   the JSON of a reply, in one of the server's two envelopes
# OUTPUT  plain objects, with the whole reply kept on ``raw``


@dataclass
class Collection:
    """A collection as the server describes it."""

    name: str
    dimension: int = 0
    metric: str = "cosine"
    count: int = 0
    size_bytes: int = 0
    description: Optional[str] = None
    has_text_index: bool = False
    tags: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    indexed_fields: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> "Collection":
        return cls(
            name=str(row.get("name", "")),
            dimension=int(row.get("dimension") or 0),
            metric=str(row.get("metric") or "cosine"),
            count=int(row.get("count") or 0),
            size_bytes=int(row.get("size_bytes") or 0),
            description=row.get("description"),
            has_text_index=bool(row.get("has_text_index")),
            tags=list(row.get("tags") or []),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            indexed_fields=list(row.get("indexed_fields") or []),
            raw=dict(row),
        )


@dataclass
class Document:
    """A document the server keeps, as ``documents()`` lists it."""

    doc_id: str
    filename: Optional[str] = None
    kind: Optional[str] = None
    source: Optional[str] = None
    version: Optional[str] = None
    extracted_at: Optional[str] = None
    chunking: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> "Document":
        return cls(
            doc_id=str(row.get("doc_id", "")),
            filename=row.get("filename"),
            kind=row.get("kind"),
            source=row.get("source"),
            version=row.get("version"),
            extracted_at=row.get("extracted_at"),
            chunking=dict(row.get("chunking") or {}),
            raw=dict(row),
        )


@dataclass
class Added:
    """What ``add_document`` did."""

    doc_id: str
    chunks: int = 0
    replaced: int = 0
    quality: Optional[float] = None
    low_quality: bool = False
    citations: List[str] = field(default_factory=list)
    kept: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> "Added":
        return cls(
            doc_id=str(row.get("doc_id", "")),
            chunks=int(row.get("chunks") or 0),
            replaced=int(row.get("replaced") or 0),
            quality=row.get("quality"),
            low_quality=bool(row.get("low_quality")),
            citations=list(row.get("citations") or []),
            kept=bool(row.get("kept")),
            raw=dict(row),
        )


@dataclass
class Source:
    """A place a collection keeps in sync with."""

    id: str
    address: str = ""
    kind: Optional[str] = None
    every: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> "Source":
        return cls(
            id=str(row.get("id") or row.get("source_id") or ""),
            address=str(row.get("address") or ""),
            kind=row.get("kind"),
            every=row.get("every"),
            raw=dict(row),
        )


@dataclass
class Refreshed:
    """What ``refresh_sources`` did."""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: List[Any] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> "Refreshed":
        return cls(
            added=int(row.get("added") or 0),
            updated=int(row.get("updated") or 0),
            unchanged=int(row.get("unchanged") or 0),
            removed=int(row.get("removed") or 0),
            failed=list(row.get("failed") or []),
            raw=dict(row),
        )


def _data(body: Any) -> Any:
    """The payload: ``data`` from the ``{ok, message, data}`` envelope, else the body."""
    if isinstance(body, dict) and "data" in body and "ok" in body:
        return body["data"]
    return body


def _hits(body: Any, query: str, mode: str, started: float) -> Results:
    payload = _data(body)
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    items = [
        Result(
            id=str(row.get("id", "")),
            text=str(row.get("text") or (row.get("metadata") or {}).get("text") or ""),
            score=float(row.get("score") or 0.0),
            metadata=dict(row.get("metadata") or {}),
        )
        for row in rows
    ]
    return Results(
        items=items, query=query, mode=mode, time_ms=(time.perf_counter() - started) * 1000
    )


# ============================================================================
# THE CALLS, DESCRIBED ONCE
# ============================================================================
#
# INPUT   the arguments of one call
# OUTPUT  what to send, and how to read the answer
#
# The sync and async clients share these, so each call's wire shape is
# written in one place.


@dataclass
class _Call:
    method: str
    path: str
    read: Callable[[Any, Any], Any]
    params: Optional[Dict[str, Any]] = None
    json: Optional[Any] = None
    content: Optional[bytes] = None
    headers: Optional[Dict[str, str]] = None
    text: bool = False


def _json_of(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _ok(_response: Any, _body: Any) -> None:
    return None


def _collection_path(name: str) -> str:
    return f"/api/v1/collections/{quote(str(name), safe='')}"


def _segment(value: str) -> str:
    # A document or source id may contain "/", which the route reads back
    # from one encoded segment.
    return quote(str(value), safe="")


def _bytes_of(data: Union[bytes, bytearray, str, Path, Any]) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, Path):
        return data.read_bytes()
    if isinstance(data, str):
        return data.encode("utf-8")
    if hasattr(data, "read"):
        read = data.read()
        return read if isinstance(read, bytes) else str(read).encode("utf-8")
    raise TypeError("add_document takes bytes, a str, a Path or an open file")


class _Calls:
    """Every call's wire shape. Subclasses decide how to send."""

    @staticmethod
    def _health() -> _Call:
        return _Call("GET", "/health", lambda r, b: r.status_code == 200)

    @staticmethod
    def _ready() -> _Call:
        return _Call("GET", "/ready", lambda r, b: r.status_code == 200)

    @staticmethod
    def _whoami() -> _Call:
        return _Call("GET", "/auth/me", lambda r, b: _data(b))

    @staticmethod
    def _collections() -> _Call:
        def read(_r: Any, body: Any) -> List[Collection]:
            payload = _data(body)
            rows = payload.get("collections", []) if isinstance(payload, dict) else []
            return [Collection.of(row) for row in rows]

        return _Call("GET", "/api/v1/collections", read)

    @staticmethod
    def _describe(name: str) -> _Call:
        return _Call("GET", _collection_path(name), lambda r, b: Collection.of(_data(b) or {}))

    @staticmethod
    def _create_collection(
        name: str,
        dimension: int,
        text_index: bool,
        metric: str,
        description: Optional[str],
    ) -> _Call:
        body: Dict[str, Any] = {
            "name": name,
            "dimension": dimension,
            "enable_text_index": text_index,
            "metric": metric,
        }
        if description is not None:
            body["description"] = description
        return _Call(
            "POST", "/api/v2/collections", lambda r, b: Collection.of(_data(b) or {}), json=body
        )

    @staticmethod
    def _delete_collection(name: str) -> _Call:
        return _Call("DELETE", _collection_path(name), _ok)

    @staticmethod
    def _add_document(
        collection: str,
        data: Any,
        filename: Optional[str],
        doc_id: Optional[str],
        metadata: Optional[Mapping[str, Any]],
        chunk: Optional[str],
        chunk_size: Optional[int],
        overlap: Optional[int],
    ) -> _Call:
        if filename is None:
            filename = data.name if isinstance(data, Path) else getattr(data, "name", None)
            filename = Path(str(filename)).name if filename else "document.md"
        params: Dict[str, Any] = {}
        if doc_id is not None:
            params["doc_id"] = doc_id
        if metadata:
            params["metadata"] = json.dumps(dict(metadata))
        if chunk is not None:
            params["chunk"] = chunk
        if chunk_size is not None:
            params["chunk_size"] = chunk_size
        if overlap is not None:
            params["overlap"] = overlap
        return _Call(
            "POST",
            f"{_collection_path(collection)}/documents",
            lambda r, b: Added.of(b if isinstance(b, dict) else {}),
            params=params,
            content=_bytes_of(data),
            headers={"content-type": "application/octet-stream", "x-filename": quote(filename)},
        )

    @staticmethod
    def _add_texts(collection: str, points: Iterable[Any]) -> _Call:
        rows = []
        for point in points:
            if isinstance(point, Mapping):
                # The route's field for metadata is ``payload``.
                rows.append(
                    {
                        "id": str(point["id"]),
                        "text": str(point["text"]),
                        "payload": dict(point.get("metadata") or point.get("payload") or {}),
                    }
                )
            else:
                pid, text, *rest = point
                rows.append(
                    {"id": str(pid), "text": str(text), "payload": dict(rest[0] if rest else {})}
                )

        def read(_r: Any, body: Any) -> int:
            payload = _data(body)
            return int(payload.get("added", len(rows))) if isinstance(payload, dict) else len(rows)

        return _Call(
            "POST", f"{_collection_path(collection)}/text-upsert", read, json={"points": rows}
        )

    @staticmethod
    def _search(
        collection: str,
        query: str,
        limit: int,
        filter: Optional[Mapping[str, Any]],
        rerank: bool,
        mode: str,
    ) -> _Call:
        if mode not in MODES:
            raise ValueError(f"mode is one of {', '.join(MODES)}, not {mode!r}")
        body: Dict[str, Any] = {"query_text": query, "limit": int(limit), "rerank": bool(rerank)}
        if filter:
            body["filter"] = dict(filter)
        started = time.perf_counter()
        return _Call(
            "POST",
            f"{_collection_path(collection)}/{MODES[mode]}",
            lambda r, b: _hits(b, query, mode, started),
            json=body,
        )

    @staticmethod
    def _documents(collection: str) -> _Call:
        def read(_r: Any, body: Any) -> List[Document]:
            payload = _data(body)
            rows = payload.get("documents", []) if isinstance(payload, dict) else []
            return [Document.of(row) for row in rows]

        return _Call("GET", f"{_collection_path(collection)}/documents", read)

    @staticmethod
    def _open_document(collection: str, doc_id: str) -> _Call:
        return _Call(
            "GET",
            f"{_collection_path(collection)}/documents/{_segment(doc_id)}",
            lambda r, b: r.text,
            text=True,
        )

    @staticmethod
    def _delete_document(collection: str, doc_id: str) -> _Call:
        def read(_r: Any, body: Any) -> int:
            payload = _data(body)
            return int(payload.get("chunks_removed", 0)) if isinstance(payload, dict) else 0

        return _Call("DELETE", f"{_collection_path(collection)}/documents/{_segment(doc_id)}", read)

    @staticmethod
    def _sources(collection: str) -> _Call:
        def read(_r: Any, body: Any) -> List[Source]:
            payload = _data(body)
            rows = payload.get("sources", []) if isinstance(payload, dict) else []
            return [Source.of(row) for row in rows]

        return _Call("GET", f"{_collection_path(collection)}/sources", read)

    @staticmethod
    def _add_source(
        collection: str, address: str, kind: Optional[str], every: Optional[str]
    ) -> _Call:
        body: Dict[str, Any] = {"address": address}
        if kind is not None:
            body["kind"] = kind
        if every is not None:
            body["every"] = every

        def read(_r: Any, reply: Any) -> Source:
            payload = _data(reply)
            if isinstance(payload, dict) and isinstance(payload.get("source"), dict):
                payload = payload["source"]
            return Source.of(payload if isinstance(payload, dict) else {})

        return _Call("POST", f"{_collection_path(collection)}/sources", read, json=body)

    @staticmethod
    def _refresh_sources(collection: str) -> _Call:
        return _Call(
            "POST",
            f"{_collection_path(collection)}/sources/refresh",
            lambda r, b: Refreshed.of(b if isinstance(b, dict) else {}),
            json={},
        )

    @staticmethod
    def _delete_source(collection: str, source_id: str, delete_documents: bool) -> _Call:
        return _Call(
            "DELETE",
            f"{_collection_path(collection)}/sources/{_segment(source_id)}",
            _ok,
            params={"delete_documents": "true" if delete_documents else "false"},
        )


# ============================================================================
# SENDING
# ============================================================================
#
# INPUT   a described call
# OUTPUT  its answer, after the retries the contract allows


def _headers(key: Optional[str], token: Optional[str]) -> Dict[str, str]:
    sent = {"user-agent": USER_AGENT, "accept": "application/json"}
    if token:
        sent["authorization"] = f"Bearer {token}"
    elif key:
        sent["api-key"] = key
    return sent


def _wait_for(response: Any, attempt: int) -> float:
    said = response.headers.get("retry-after")
    if said:
        try:
            return max(0.0, float(said))
        except ValueError:
            pass
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]


def _retry(response: Any, attempt: int) -> bool:
    return response.status_code in (429, 503) and attempt < RETRIES


def _finish(call: _Call, response: Any, url: str) -> Any:
    body = None if call.text and response.status_code < 400 else _json_of(response)
    if response.status_code >= 400:
        raise _refusal(response.status_code, body, url)
    return call.read(response, body)


def _httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise DependencyError("httpx", extra="client") from exc
    return httpx


class VectrixClient(_Calls):
    """A VectrixDB server, used from Python.

    Args:
        url: Where the server is, ``https://vectors.example.com``.
        key: An API key, sent in the ``api-key`` header.
        token: A company sign-in token, sent as a bearer token instead of a key.
        timeout: Seconds to wait for each request.
        transport: An httpx transport, for tests that talk to an app in-process.
    """

    def __init__(
        self,
        url: str,
        key: Optional[str] = None,
        token: Optional[str] = None,
        *,
        timeout: float = 30.0,
        transport: Any = None,
    ) -> None:
        httpx = _httpx()
        self.url = url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.url,
            headers=_headers(key, token),
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "VectrixClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _send(self, call: _Call) -> Any:
        httpx = _httpx()
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    call.method,
                    call.path,
                    params=call.params,
                    json=call.json,
                    content=call.content,
                    headers=call.headers,
                )
            except httpx.ConnectError as exc:
                raise ConnectionFailed(f"{self.url} could not be reached: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise ConnectionFailed(f"{self.url} did not answer in time: {exc}") from exc
            if _retry(response, attempt):
                time.sleep(_wait_for(response, attempt))
                attempt += 1
                continue
            return _finish(call, response, str(response.url))

    # -- the surface -------------------------------------------------------

    def health(self) -> bool:
        """Whether the process answers."""
        return bool(self._send(self._health()))

    def ready(self) -> bool:
        """Whether the models are loaded; ``health()`` on a server with no ``/ready``."""
        try:
            return bool(self._send(self._ready()))
        except NotFoundError:
            return self.health()
        except BusyError:
            return False

    def whoami(self) -> Dict[str, Any]:
        """Who the server takes this client for."""
        answer = self._send(self._whoami())
        return dict(answer) if isinstance(answer, dict) else {}

    def collections(self) -> List[Collection]:
        """The collections this key may see."""
        return list(self._send(self._collections()))

    def describe(self, name: str) -> Collection:
        """One collection."""
        return Collection.of(self._send(self._describe(name)).raw)

    def create_collection(
        self,
        name: str,
        dimension: int = 384,
        text_index: bool = True,
        metric: str = "cosine",
        description: Optional[str] = None,
    ) -> Collection:
        """A new collection; ``text_index`` makes hybrid search possible."""
        return Collection.of(
            self._send(
                self._create_collection(name, dimension, text_index, metric, description)
            ).raw
        )

    def delete_collection(self, name: str) -> None:
        self._send(self._delete_collection(name))

    def add_document(
        self,
        collection: str,
        data: Union[bytes, str, Path, Any],
        filename: Optional[str] = None,
        *,
        doc_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        chunk: Optional[str] = None,
        chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> Added:
        """Send a file's bytes; the server reads, cuts and embeds it."""
        return Added.of(
            self._send(
                self._add_document(
                    collection, data, filename, doc_id, metadata, chunk, chunk_size, overlap
                )
            ).raw
        )

    def add_texts(self, collection: str, points: Iterable[Any]) -> int:
        """Add texts the server embeds: ``[{"id": ..., "text": ..., "metadata": {...}}]``."""
        return int(self._send(self._add_texts(collection, points)))

    def search(
        self,
        collection: str,
        query: str,
        limit: int = 10,
        filter: Optional[Mapping[str, Any]] = None,
        rerank: bool = False,
        mode: str = "meaning",
    ) -> Results:
        """Search by meaning, or ``mode="hybrid"`` for meaning and exact words."""
        return self._send(self._search(collection, query, limit, filter, rerank, mode))

    def documents(self, collection: str) -> List[Document]:
        """The documents the server keeps for a collection."""
        return list(self._send(self._documents(collection)))

    def open_document(self, collection: str, doc_id: str) -> str:
        """The Markdown a document was indexed from."""
        return str(self._send(self._open_document(collection, doc_id)))

    def delete_document(self, collection: str, doc_id: str) -> int:
        """Remove a document; returns how many chunks went."""
        return int(self._send(self._delete_document(collection, doc_id)))

    def sources(self, collection: str) -> List[Source]:
        return list(self._send(self._sources(collection)))

    def add_source(
        self,
        collection: str,
        address: str,
        kind: Optional[str] = None,
        every: Optional[str] = None,
    ) -> Source:
        """A page or feed the collection keeps in sync with."""
        return self._send(self._add_source(collection, address, kind, every))

    def refresh_sources(self, collection: str) -> Refreshed:
        return self._send(self._refresh_sources(collection))

    def delete_source(
        self, collection: str, source_id: str, delete_documents: bool = False
    ) -> None:
        self._send(self._delete_source(collection, source_id, delete_documents))


class AsyncVectrixClient(_Calls):
    """``VectrixClient`` with ``await``; see that class for the arguments."""

    def __init__(
        self,
        url: str,
        key: Optional[str] = None,
        token: Optional[str] = None,
        *,
        timeout: float = 30.0,
        transport: Any = None,
    ) -> None:
        httpx = _httpx()
        self.url = url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.url,
            headers=_headers(key, token),
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncVectrixClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def _send(self, call: _Call) -> Any:
        import asyncio

        httpx = _httpx()
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    call.method,
                    call.path,
                    params=call.params,
                    json=call.json,
                    content=call.content,
                    headers=call.headers,
                )
            except httpx.ConnectError as exc:
                raise ConnectionFailed(f"{self.url} could not be reached: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise ConnectionFailed(f"{self.url} did not answer in time: {exc}") from exc
            if _retry(response, attempt):
                await asyncio.sleep(_wait_for(response, attempt))
                attempt += 1
                continue
            return _finish(call, response, str(response.url))

    async def health(self) -> bool:
        return bool(await self._send(self._health()))

    async def ready(self) -> bool:
        try:
            return bool(await self._send(self._ready()))
        except NotFoundError:
            return await self.health()
        except BusyError:
            return False

    async def whoami(self) -> Dict[str, Any]:
        answer = await self._send(self._whoami())
        return dict(answer) if isinstance(answer, dict) else {}

    async def collections(self) -> List[Collection]:
        return list(await self._send(self._collections()))

    async def describe(self, name: str) -> Collection:
        return Collection.of((await self._send(self._describe(name))).raw)

    async def create_collection(
        self,
        name: str,
        dimension: int = 384,
        text_index: bool = True,
        metric: str = "cosine",
        description: Optional[str] = None,
    ) -> Collection:
        made = await self._send(
            self._create_collection(name, dimension, text_index, metric, description)
        )
        return Collection.of(made.raw)

    async def delete_collection(self, name: str) -> None:
        await self._send(self._delete_collection(name))

    async def add_document(
        self,
        collection: str,
        data: Union[bytes, str, Path, Any],
        filename: Optional[str] = None,
        *,
        doc_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        chunk: Optional[str] = None,
        chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> Added:
        call = self._add_document(
            collection, data, filename, doc_id, metadata, chunk, chunk_size, overlap
        )
        return Added.of((await self._send(call)).raw)

    async def add_texts(self, collection: str, points: Iterable[Any]) -> int:
        return int(await self._send(self._add_texts(collection, points)))

    async def search(
        self,
        collection: str,
        query: str,
        limit: int = 10,
        filter: Optional[Mapping[str, Any]] = None,
        rerank: bool = False,
        mode: str = "meaning",
    ) -> Results:
        return await self._send(self._search(collection, query, limit, filter, rerank, mode))

    async def documents(self, collection: str) -> List[Document]:
        return list(await self._send(self._documents(collection)))

    async def open_document(self, collection: str, doc_id: str) -> str:
        return str(await self._send(self._open_document(collection, doc_id)))

    async def delete_document(self, collection: str, doc_id: str) -> int:
        return int(await self._send(self._delete_document(collection, doc_id)))

    async def sources(self, collection: str) -> List[Source]:
        return list(await self._send(self._sources(collection)))

    async def add_source(
        self,
        collection: str,
        address: str,
        kind: Optional[str] = None,
        every: Optional[str] = None,
    ) -> Source:
        return await self._send(self._add_source(collection, address, kind, every))

    async def refresh_sources(self, collection: str) -> Refreshed:
        return await self._send(self._refresh_sources(collection))

    async def delete_source(
        self, collection: str, source_id: str, delete_documents: bool = False
    ) -> None:
        await self._send(self._delete_source(collection, source_id, delete_documents))


def connect(
    url: str,
    key: Optional[str] = None,
    token: Optional[str] = None,
    *,
    timeout: float = 30.0,
) -> VectrixClient:
    """A server, by address and key: ``connect("https://vectors.example.com", key="...")``."""
    return VectrixClient(url, key=key, token=token, timeout=timeout)
