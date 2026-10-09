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

Behind a company's gateway, proxy or private CA:

    db = connect(
        "https://gateway.example.com",
        key=KEY,
        key_header="Ocp-Apim-Subscription-Key",
        prefix="/acme",
        gateway_paths="api/v1=/files/search, auth=/files/auth",
        verify="/etc/ssl/company-ca.pem",
    )

A key never travels over plain HTTP to another machine (``allow_http=True``
says it may), and no redirect is followed, so a key goes only to the address
it was given for. ``HTTPS_PROXY`` and ``NO_PROXY`` are honoured.

Every language's client offers this surface; ``sdk/CONTRACT.md`` in the
repository is the shared description.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Union
from urllib.parse import quote, urlsplit

from . import __version__
from .easy import Result, Results
from .exceptions import ConfigurationError, DependencyError, VectrixError

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
    "read_gateway_paths",
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
    return f"/api/v1/collections/{_segment(name, 'a collection name')}"


def _segment(value: str, what: str = "an id") -> str:
    # A document or source id may contain "/", which the route reads back
    # from one encoded segment. "." and ".." are refused: HTTP stacks collapse
    # dot segments, so a document id of ".." would delete its collection.
    text = str(value)
    if text in ("", ".", ".."):
        raise ValueError(f"{what} cannot be {text!r}: it would name another route")
    return quote(text, safe="")


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


# ============================================================================
# THE CONNECTION: where the key goes, and how a gateway publishes the routes
# ============================================================================
#
# INPUT   the address, the key or token, and the options for a gateway, a
#         proxy and a private CA
# OUTPUT  the base address, the headers every request carries, the path each
#         route is sent to, and the TLS settings; or a ConfigurationError
#
# The gateway paths are read the way the server reads its own
# VECTRIXDB_GATEWAY_PATHS (vectrixdb/api/gateway.py), so one list serves both
# sides. That module needs the server's packages, so the reading is here too,
# and a test holds the two to the same answers.

#: RFC 9110 token characters: what an HTTP header's name may be made of.
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
#: What a header value may not hold: controls, which would split or end it.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _names(value: Any, what: str) -> str:
    """``/one/two``, or ``""``: a path of names, whatever it was written as."""
    parts = [part for part in str(value or "").strip().split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ConfigurationError(f"{what} is a path of names, not {value!r}")
    return "/" + "/".join(parts) if parts else ""


def read_gateway_paths(value: Union[None, str, Mapping[str, str]]) -> Dict[str, str]:
    """Each route's own gateway path, ``{"api/v1": "/files/search"}``, from ``api/v1=/files/search, auth=/files/auth``."""
    if not value:
        return {}
    if isinstance(value, Mapping):
        pairs: List[Tuple[Any, Any]] = list(value.items())
    else:
        pairs = []
        for entry in str(value).split(","):
            if not entry.strip():
                continue
            route, equals, path = entry.partition("=")
            if not equals:
                raise ConfigurationError(f"a gateway path is route=path, not {entry.strip()!r}")
            pairs.append((route, path))
    paths: Dict[str, str] = {}
    for route, path in pairs:
        name, where = _names(route, "a route")[1:], _names(path, "a gateway path")
        if not name or not where:
            given = f"{str(route or '').strip()}={str(path or '').strip()}"
            raise ConfigurationError(f"a gateway path is route=path with both given, not {given!r}")
        if name in paths:
            raise ConfigurationError(f"{name} is given two gateway paths")
        paths[name] = where
    return paths


def _route_path(route: str, prefix: str, paths: Mapping[str, str]) -> str:
    """The path a request for ``route`` goes to: its gateway path, the prefix, the route."""
    bare = route.split("?", 1)[0].split("#", 1)[0].strip("/")
    best: Optional[str] = None
    for name in paths:
        if (bare == name or bare.startswith(name + "/")) and (
            best is None or len(name) > len(best)
        ):
            best = name
    return f"{paths[best] if best else ''}{prefix}{route}"


def _loopback(host: str) -> bool:
    """Whether ``host`` is this machine: ``localhost``, ``127.0.0.0/8`` or ``::1``."""
    name = host.strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _header_name(value: str, what: str) -> str:
    name = str(value or "").strip()
    if not _HEADER_NAME.match(name):
        raise ConfigurationError(f"{what} is {value!r}, which cannot be the name of an HTTP header")
    return name.lower()


def _tls(verify: Any, cert: Any) -> Any:
    """What httpx is given as ``verify``: its own default, or a context with the CA bundle and the client certificate."""
    if verify is False:
        raise ConfigurationError(
            "Certificates are always checked. For a private CA, give its bundle: verify='/path/to/ca.pem'"
        )
    if verify == "system":
        # The operating system's own store, where a managed machine keeps the
        # company's authority and a TLS-inspecting proxy's.
        try:
            import truststore
        except ImportError as exc:
            raise ConfigurationError(
                'verify="system" needs truststore, which the client extra brings on Python 3.10 '
                'and later: pip install "vectrixdb[client]"'
            ) from exc
        system = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if cert:
            certfile, keyfile = (
                (cert, None) if isinstance(cert, (str, os.PathLike)) else tuple(cert)
            )
            system.load_cert_chain(str(certfile), str(keyfile) if keyfile else None)
        return system
    if isinstance(verify, ssl.SSLContext):
        if cert:
            raise ConfigurationError(
                "give the client certificate in the SSLContext passed as verify"
            )
        return verify
    if verify in (None, True) and not cert:
        return True
    bundle = verify if isinstance(verify, (str, os.PathLike)) else os.environ.get("SSL_CERT_FILE")
    if not bundle:
        try:
            import certifi

            bundle = certifi.where()
        except ImportError:  # pragma: no cover - httpx brings certifi
            bundle = None
    context = ssl.create_default_context(cafile=str(bundle) if bundle else None)
    if cert:
        certfile, keyfile = (cert, None) if isinstance(cert, (str, os.PathLike)) else tuple(cert)
        context.load_cert_chain(str(certfile), str(keyfile) if keyfile else None)
    return context


@dataclass
class _Wire:
    """How every request is made: the base address, the headers, the route map, the TLS settings."""

    url: str
    headers: Dict[str, str] = field(repr=False)
    prefix: str
    paths: Dict[str, str]
    verify: Any

    @classmethod
    def of(
        cls,
        url: str,
        key: Optional[str],
        token: Optional[str],
        *,
        allow_http: bool,
        key_header: str,
        token_header: str,
        headers: Optional[Mapping[str, str]],
        prefix: Optional[str],
        gateway_paths: Union[None, str, Mapping[str, str]],
        verify: Any,
        cert: Any,
        user_agent: Optional[str] = None,
    ) -> "_Wire":
        base = str(url or "").strip().rstrip("/")
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigurationError(f"{url!r} is not an address: https://vectors.example.com")
        if parts.username or parts.password:
            raise ConfigurationError(
                "Put the key in key= or token=, not in the address, where logs and printed forms keep it"
            )
        for what, value in (
            ("The key", key),
            ("The token", token),
            *((f"The header {name}", v) for name, v in (headers or {}).items()),
            ("The user agent", user_agent),
        ):
            if value is not None and _CONTROL.search(str(value)):
                # The value is not repeated: it is likely a secret with a stray line break.
                raise ConfigurationError(
                    f"{what} has a line break or another control character in it. Copy it again"
                )
        if (
            (key or token)
            and parts.scheme == "http"
            and not allow_http
            and not _loopback(parts.hostname)
        ):
            raise ConfigurationError(
                f"{base} is plain HTTP, and the key or token would cross the network readable. "
                "Use its https:// address, or pass allow_http=True on a network you trust"
            )
        sent: Dict[str, str] = {}
        for name, value in (headers or {}).items():
            sent[_header_name(name, "a header")] = str(value)
        # A wrapper's name goes first, so a gateway's log says which tool called, and this client's after it.
        sent["user-agent"] = f"{user_agent.strip()} {USER_AGENT}" if user_agent else USER_AGENT
        sent.setdefault("accept", "application/json")
        if token:
            sent[_header_name(token_header, "token_header")] = f"Bearer {token}"
        elif key:
            sent[_header_name(key_header, "key_header")] = key
        return cls(
            url=base,
            headers=sent,
            prefix=_names(prefix, "the prefix"),
            paths=read_gateway_paths(gateway_paths),
            verify=_tls(verify, cert),
        )

    def path(self, route: str) -> str:
        return _route_path(route, self.prefix, self.paths)


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
    if 300 <= response.status_code < 400:
        # Never followed: the key would go with it, to wherever it points.
        where = response.headers.get("location") or "nowhere"
        raise RequestError(
            response.status_code,
            f"{response.status_code} from {url}, pointing to {where}. Redirects are not "
            "followed, so a key goes only to the address it was given for: use the address it names",
            None,
            url,
        )
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
        url: Where the server is, ``https://vectors.example.com``. Behind a
            gateway that publishes each part under a path of its own, the
            gateway's address, with ``prefix`` and ``gateway_paths``.
        key: An API key, sent in the ``api-key`` header.
        token: A company sign-in token, sent as a bearer token instead of a key.
        timeout: Seconds to wait for each request.
        transport: An httpx transport, for tests that talk to an app in-process.
        allow_http: Send the key or token over plain HTTP to another machine.
            Off, only ``https://`` or this machine.
        key_header: The header the key goes in, for a gateway with its own:
            ``Ocp-Apim-Subscription-Key``. The server's ``VECTRIXDB_KEY_HEADER``.
        token_header: The header the token goes in, as ``Bearer <token>``.
        headers: More headers for every request, such as a gateway's
            subscription key alongside a person's token.
        prefix: The path every route lives under, as the server's ``VECTRIXDB_PREFIX``.
        gateway_paths: Each route's gateway path, as the server's
            ``VECTRIXDB_GATEWAY_PATHS``: ``api/v1=/files/search, auth=/files/auth``.
        verify: A CA bundle to trust, for a private CA; ``"system"`` for the
            operating system's own store, where a managed machine keeps the
            company's authority; or an ``ssl.SSLContext``. Certificates are
            always checked.
        cert: A client certificate for a gateway that asks for one: a path,
            or ``(certificate, key)``.
        user_agent: A wrapper's name and version, ``acme-vectors/1.4``, put
            before this client's own in ``User-Agent``, so a gateway's log says
            which tool made each call.
    """

    def __init__(
        self,
        url: str,
        key: Optional[str] = None,
        token: Optional[str] = None,
        *,
        timeout: float = 30.0,
        transport: Any = None,
        allow_http: bool = False,
        key_header: str = "api-key",
        token_header: str = "authorization",
        headers: Optional[Mapping[str, str]] = None,
        prefix: Optional[str] = None,
        gateway_paths: Union[None, str, Mapping[str, str]] = None,
        verify: Any = True,
        cert: Any = None,
        user_agent: Optional[str] = None,
    ) -> None:
        httpx = _httpx()
        self._wire = _Wire.of(
            url,
            key,
            token,
            allow_http=allow_http,
            key_header=key_header,
            token_header=token_header,
            headers=headers,
            prefix=prefix,
            gateway_paths=gateway_paths,
            verify=verify,
            cert=cert,
            user_agent=user_agent,
        )
        self.url = self._wire.url
        self._http = httpx.Client(
            base_url=self.url,
            headers=self._wire.headers,
            timeout=timeout,
            transport=transport,
            verify=self._wire.verify,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"VectrixClient({self.url!r})"

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
                    self._wire.path(call.path),
                    params=call.params,
                    json=call.json,
                    content=call.content,
                    headers=call.headers,
                )
            except httpx.ConnectError as exc:
                raise ConnectionFailed(f"{self.url} could not be reached: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise ConnectionFailed(f"{self.url} did not answer in time: {exc}") from exc
            except httpx.HTTPError as exc:
                # Its text may quote a request header, the key's included, so only its kind is said.
                raise ConnectionFailed(
                    f"{self.url} could not be asked: {type(exc).__name__}"
                ) from None
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
        allow_http: bool = False,
        key_header: str = "api-key",
        token_header: str = "authorization",
        headers: Optional[Mapping[str, str]] = None,
        prefix: Optional[str] = None,
        gateway_paths: Union[None, str, Mapping[str, str]] = None,
        verify: Any = True,
        cert: Any = None,
        user_agent: Optional[str] = None,
    ) -> None:
        httpx = _httpx()
        self._wire = _Wire.of(
            url,
            key,
            token,
            allow_http=allow_http,
            key_header=key_header,
            token_header=token_header,
            headers=headers,
            prefix=prefix,
            gateway_paths=gateway_paths,
            verify=verify,
            cert=cert,
            user_agent=user_agent,
        )
        self.url = self._wire.url
        self._http = httpx.AsyncClient(
            base_url=self.url,
            headers=self._wire.headers,
            timeout=timeout,
            transport=transport,
            verify=self._wire.verify,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"AsyncVectrixClient({self.url!r})"

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
                    self._wire.path(call.path),
                    params=call.params,
                    json=call.json,
                    content=call.content,
                    headers=call.headers,
                )
            except httpx.ConnectError as exc:
                raise ConnectionFailed(f"{self.url} could not be reached: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise ConnectionFailed(f"{self.url} did not answer in time: {exc}") from exc
            except httpx.HTTPError as exc:
                # Its text may quote a request header, the key's included, so only its kind is said.
                raise ConnectionFailed(
                    f"{self.url} could not be asked: {type(exc).__name__}"
                ) from None
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
    **options: Any,
) -> VectrixClient:
    """A server, by address and key: ``connect("https://vectors.example.com", key="...")``.

    ``options`` are ``VectrixClient``'s: ``key_header``, ``prefix``,
    ``gateway_paths``, ``verify``, ``cert``, ``headers``, ``allow_http``,
    ``user_agent``.
    """
    return VectrixClient(url, key=key, token=token, timeout=timeout, **options)
