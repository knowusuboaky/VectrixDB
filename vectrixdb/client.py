"""A VectrixDB server, from Python: the same calls as a local ``Vectrix``, over its REST API.

    import vectrixdb

    db = vectrixdb.connect("https://vectors.company.com", key=KEY, collection="handbook")
    for result in db.search("how long do refunds take", limit=5):
        print(result.readable_citation, result.text)

    db = vectrixdb.Vectrix("handbook", path="./data")        # the same code, on this machine

A search answers with the same ``Results`` and ``Result`` a local collection
gives: ``.top``, ``citation``, ``readable_citation``, ``relevance``. So code
written against a collection on a laptop moves to a server by changing the
line that opens it.

Who calls is a key, ``key=``, or a person's or an app's access token from the
company's identity provider, ``token=``: a string, or a function that
returns a fresh one, called before each request. Every call is judged by the
server as that caller, and its refusals come back as the exceptions in
:mod:`vectrixdb.exceptions`: :class:`~vectrixdb.exceptions.ServerRefused` and
the kinds under it.

A busy server's 429 and 503, and a connection that drops, are tried again,
waiting what ``Retry-After`` asks or a little longer each time; anything else
is the answer. :func:`connect` gives the blocking client; :func:`connect_async`
the same calls as coroutines. Both need ``httpx``: ``pip install
"vectrixdb[client]"``.

A key or a token is only sent over ``https://``, or to this machine over
``http://``; ``allow_http=True`` lifts that for a network you trust. A
redirect is never followed, so a key never goes to a second host. The
proxy settings and certificate files of the environment are honoured
(``HTTPS_PROXY``, ``NO_PROXY``, ``SSL_CERT_FILE``); ``verify=`` takes a
company's own certificate authority as a file, or ``"system"`` for the
operating system's certificates.

``vectrixdb.connect()`` with no address goes to ``VECTRIXDB_URL``, else to
the company's server from :mod:`vectrixdb.company`, a wrapper package's
defaults or the machine's defaults file; the company's headers, certificate
authority and key header go with every call to that server, and to no
other.
"""

from __future__ import annotations

import json as _json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    overload,
)
from urllib.parse import quote, urlsplit

from .easy import Result, Results
from .exceptions import (
    DependencyError,
    ServerBusy,
    ServerNotFound,
    ServerPermissionDenied,
    ServerRefused,
    ServerRejected,
    ServerSignInRequired,
)

__all__ = [
    "connect",
    "connect_async",
    "Client",
    "AsyncClient",
    "RemoteCollection",
    "AsyncRemoteCollection",
    "OPERATIONS",
]

# ============================================================================
# SETTINGS: the modes, the operations, and how hard to try again
# ============================================================================
#
# Each search mode is the route the server answers it on, the same table the
# MCP tools use; every request the client makes is one of OPERATIONS, which a
# test holds to docs/reference/openapi.json.

Token = Union[str, Callable[[], str]]

#: A search mode, and the route and body it is on the server.
MODES: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "hybrid": ("text-hybrid-search", {}),
    "dense": ("text-search", {}),
    "keyword": ("keyword-search", {}),
    "rerank": ("text-search", {"rerank": True}),
}

#: Every request this client makes, as (method, path) in the server's OpenAPI document.
OPERATIONS: Tuple[Tuple[str, str], ...] = (
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/api/v1/whoami"),
    ("GET", "/api/v1/collections"),
    ("POST", "/api/v2/collections"),
    ("GET", "/api/v1/collections/{name}"),
    ("DELETE", "/api/v1/collections/{name}"),
    ("POST", "/api/v1/collections/{name}/text-search"),
    ("POST", "/api/v1/collections/{name}/text-hybrid-search"),
    ("POST", "/api/v1/collections/{name}/keyword-search"),
    ("POST", "/api/v1/collections/{name}/similar"),
    ("POST", "/api/v1/collections/{name}/text-upsert"),
    ("POST", "/api/v1/collections/{name}/documents"),
    ("GET", "/api/v1/collections/{name}/documents"),
    ("GET", "/api/v1/collections/{name}/documents/{doc_id}"),
    ("DELETE", "/api/v1/collections/{name}/documents/{doc_id}"),
    ("GET", "/api/v1/collections/{name}/sources"),
    ("POST", "/api/v1/collections/{name}/sources"),
    ("POST", "/api/v1/collections/{name}/sources/refresh"),
)

#: Answers worth asking again: the server or the way to it was busy.
_AGAIN = frozenset({429, 502, 503, 504})


def _user_agent() -> str:
    try:
        from importlib.metadata import version

        return f"vectrixdb-python/{version('vectrixdb')}"
    except Exception:  # pragma: no cover - running from a source tree
        return "vectrixdb-python"


#: How the client names itself in the server's and the proxy's logs.
_USER_AGENT = _user_agent()


# ============================================================================
# A REQUEST, AND ITS ANSWER
# ============================================================================
#
# INPUT   a method, a path, a body; the caller's key or token
# OUTPUT  the answer's data; or the server's refusal as an exception
#
# One place for both clients, so the blocking and the async one cannot
# drift: each only sends.


@dataclass(frozen=True)
class _Request:
    method: str
    path: str
    json: Any = None
    content: Optional[bytes] = None
    headers: Optional[Mapping[str, str]] = None
    params: Optional[Mapping[str, Any]] = None


def _httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - the extra is missing
        raise DependencyError("httpx", "client") from exc
    return httpx


#: Hosts a plain http:// request never leaves this machine for.
_THIS_MACHINE = frozenset({"localhost", "127.0.0.1", "::1"})


def _on_this_machine(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in _THIS_MACHINE or host.endswith(".localhost") or host.startswith("127.")


def _check_url(url: str, sends_a_caller: bool, allow_http: bool) -> str:
    """The server's address, refused if it is not http(s), or if a key would cross a network in clear text."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"the server's address must start https:// (or http://): {url!r}")
    if parts.username or parts.password:
        raise ValueError("put the key in key=, not in the address, where logs and history keep it")
    if (
        parts.scheme == "http"
        and sends_a_caller
        and not allow_http
        and not _on_this_machine(parts.hostname)
    ):
        raise ValueError(
            f"{parts.hostname} is reached over http://, which would send the key in clear text: "
            "use https://, or allow_http=True on a network you trust"
        )
    return url.rstrip("/")


def tls(verify: Any = True) -> Any:
    """What to check a server's certificate against: True, False, a CA file or folder, ``"system"``, or an SSLContext."""
    import ssl

    if isinstance(verify, (ssl.SSLContext, bool)):
        return verify
    if verify == "system":
        try:
            import truststore
        except ImportError as exc:
            raise DependencyError("truststore", "client") from exc
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    path = Path(str(verify)).expanduser()
    if not path.exists():
        raise ValueError(f"no certificate file or folder at {path}")
    if path.is_dir():
        return ssl.create_default_context(capath=str(path))
    return ssl.create_default_context(cafile=str(path))


def _extra_headers(
    headers: Optional[Mapping[str, str]], user_agent: Optional[str], key_header: str
) -> Dict[str, str]:
    """The headers every request carries besides the caller: never one that names the caller."""
    extra = {str(k): str(v) for k, v in (headers or {}).items()}
    taken = {k.lower() for k in extra} & {"authorization", key_header.lower()}
    if taken:
        raise ValueError(
            f"{', '.join(sorted(taken))} names the caller: give it as key= or token=, not in headers="
        )
    if user_agent:
        extra["User-Agent"] = f"{user_agent} {_USER_AGENT}"
    return extra


def _auth_headers(key: Optional[str], token: Optional[Token], key_header: str) -> Dict[str, str]:
    if key and token:
        raise ValueError("give key= or token=, not both: the server takes one caller per request")
    if key:
        return {key_header: key}
    if token:
        value = token() if callable(token) else token
        return {"Authorization": f"Bearer {value}"}
    return {}


def _said(response: Any) -> str:
    """The server's own words for a refusal: its message, its detail, or the start of its body."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list) and value:
                return "; ".join(
                    str(v.get("msg", v)) if isinstance(v, dict) else str(v) for v in value
                )
    return str(body)[:300]


def _refusal(response: Any) -> ServerRefused:
    status, said = int(response.status_code), _said(response)
    if 300 <= status < 400:
        # Never followed, so a key never goes to a second host: the caller is told where instead.
        where = response.headers.get("location") or "another address"
        return ServerRefused(
            status, f"the server sent this request to {where}; connect to that address instead"
        )
    kind = {
        401: ServerSignInRequired,
        403: ServerPermissionDenied,
        404: ServerNotFound,
        429: ServerBusy,
        503: ServerBusy,
    }.get(status)
    if kind is None:
        kind = ServerRejected if 400 <= status < 500 else ServerRefused
    return kind(status, said)


def _data(response: Any) -> Any:
    """An answer's payload: the ``data`` of the server's envelope, or the body as it came."""
    if not response.content:
        return None
    try:
        body = response.json()
    except ValueError:
        return response.text
    if (
        isinstance(body, dict)
        and "data" in body
        and set(body) <= {"ok", "data", "message", "error"}
    ):
        return body["data"]
    return body


def _wait(response: Any, attempt: int) -> float:
    asked = response.headers.get("retry-after") if response is not None else None
    try:
        seconds = float(asked) if asked else 0.0
    except ValueError:
        seconds = 0.0
    return min(30.0, max(seconds, 0.5 * (2**attempt)))


def _path(template: str, **parts: str) -> str:
    return template.format(
        **{k: quote(str(v), safe="/" if k == "doc_id" else "") for k, v in parts.items()}
    )


def _results(data: Any, query: str, mode: str, started: float) -> Results:
    payload = data if isinstance(data, dict) else {}
    found = payload.get("results", []) if isinstance(payload, dict) else []
    items = []
    for hit in found:
        result = Result.from_dict(hit)
        result.relevance = hit.get("relevance")
        result.relevance_kind = hit.get("relevance_kind")
        if not result.text:
            result.text = str((hit.get("metadata") or {}).get("text") or "")
        items.append(result)
    return Results(
        items=items,
        query=query,
        mode=mode,
        time_ms=(time.perf_counter() - started) * 1000,
        decision_id=payload.get("decision_id") if isinstance(payload, dict) else None,
    )


def _search_request(
    name: str, query: str, mode: str, limit: int, filter: Optional[Mapping[str, Any]]
) -> _Request:
    if mode not in MODES:
        raise ValueError(f"mode is one of {', '.join(MODES)}, not {mode!r}")
    route, extra = MODES[mode]
    body: Dict[str, Any] = {"query_text": query, "limit": int(limit), **extra}
    if filter:
        body["filter"] = dict(filter)
    return _Request("POST", _path("/api/v1/collections/{name}/" + route, name=name), json=body)


def _upsert_request(
    name: str,
    texts: Sequence[str],
    ids: Optional[Sequence[str]],
    metadata: Optional[Sequence[Mapping[str, Any]]],
) -> _Request:
    texts = list(texts)
    if ids is not None and len(ids) != len(texts):
        raise ValueError("ids has to have one id a text")
    if metadata is not None and len(metadata) != len(texts):
        raise ValueError("metadata has to have one entry a text")
    points = []
    for i, text in enumerate(texts):
        point: Dict[str, Any] = {
            "id": str(ids[i]) if ids is not None else uuid.uuid4().hex,
            "text": text,
        }
        if metadata is not None and metadata[i]:
            point["payload"] = dict(metadata[i])
        points.append(point)
    return _Request(
        "POST", _path("/api/v1/collections/{name}/text-upsert", name=name), json={"points": points}
    )


def _document_request(
    name: str,
    source: Union[str, Path, bytes],
    doc_id: Optional[str],
    filename: Optional[str],
    metadata: Optional[Mapping[str, Any]],
    chunk: Optional[str],
    chunk_size: Optional[int],
    overlap: Optional[int],
) -> _Request:
    """A file by its path, its bytes, or a string of Markdown, sent as the upload route reads it."""
    if isinstance(source, bytes):
        data, named = source, filename or doc_id or "document.md"
    elif isinstance(source, Path) or (isinstance(source, str) and _looks_like_a_file(source)):
        path = Path(source)
        data, named = path.read_bytes(), filename or path.name
    else:
        data, named = (
            str(source).encode("utf-8"),
            filename
            or (doc_id or "document")
            + ("" if str(doc_id or "").endswith((".md", ".txt")) else ".md"),
        )
    params: Dict[str, Any] = {}
    if doc_id:
        params["doc_id"] = doc_id
    if metadata:
        params["metadata"] = _json.dumps(dict(metadata))
    for key, value in (("chunk", chunk), ("chunk_size", chunk_size), ("overlap", overlap)):
        if value is not None:
            params[key] = value
    return _Request(
        "POST",
        _path("/api/v1/collections/{name}/documents", name=name),
        content=data,
        headers={"content-type": "application/octet-stream", "x-filename": quote(named)},
        params=params,
    )


def _looks_like_a_file(text: str) -> bool:
    if "\n" in text or len(text) > 1024:
        return False
    try:
        return Path(text).is_file()
    except OSError:
        return False


# ============================================================================
# THE BLOCKING CLIENT
# ============================================================================
#
# INPUT   a server's address and a caller
# OUTPUT  the server's collections, and each collection's calls
#
# One httpx.Client a client, kept open, so a program's requests reuse their
# connections.


class Client:
    """A VectrixDB server, as one caller. Made by :func:`connect`."""

    def __init__(
        self,
        url: str,
        *,
        key: Optional[str] = None,
        token: Optional[Token] = None,
        key_header: str = "api-key",
        timeout: float = 60.0,
        retries: int = 3,
        http: Any = None,
        verify: Any = True,
        allow_http: bool = False,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        if key and token:
            # Refused now, not at the first call, and without asking a token function for a token.
            raise ValueError(
                "give key= or token=, not both: the server takes one caller per request"
            )
        # An httpx client of the caller's own is theirs to secure: a proxy, a test.
        self.url = _check_url(url, bool(key or token) and http is None, allow_http)
        self._key, self._token, self._key_header = key, token, key_header
        self._extra = _extra_headers(headers, user_agent, key_header)
        self.retries = max(0, int(retries))
        self._own = http is None
        self._http = (
            http
            if http is not None
            else _httpx().Client(
                base_url=self.url,
                timeout=timeout,
                verify=tls(verify),
                follow_redirects=False,
                headers={"User-Agent": _USER_AGENT},
            )
        )

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._own:
            self._http.close()

    def _send(self, request: _Request) -> Any:
        httpx = _httpx()
        attempt = 0
        while True:
            headers = {
                **self._extra,
                **_auth_headers(self._key, self._token, self._key_header),
                **dict(request.headers or {}),
            }
            try:
                response = self._http.request(
                    request.method,
                    request.path,
                    json=request.json,
                    content=request.content,
                    headers=headers,
                    params=dict(request.params or {}) or None,
                )
            except httpx.TransportError:
                if attempt >= self.retries:
                    raise
                time.sleep(_wait(None, attempt))
                attempt += 1
                continue
            if response.status_code in _AGAIN and attempt < self.retries:
                time.sleep(_wait(response, attempt))
                attempt += 1
                continue
            if response.status_code >= 300:
                raise _refusal(response)
            return response

    # -- the server

    def health(self) -> Dict[str, Any]:
        """Whether the server is up: ``/health``, which needs no caller."""
        return _data(self._send(_Request("GET", "/health")))

    def ready(self) -> bool:
        """Whether its models are loaded and it takes searches: ``/ready``."""
        try:
            self._send(_Request("GET", "/ready"))
        except ServerBusy:
            return False
        return True

    def whoami(self) -> Dict[str, Any]:
        """Who this client is on the server: how it came in, its role, what it may do, what it reaches."""
        return _data(self._send(_Request("GET", "/api/v1/whoami")))

    def collections(self) -> List[Dict[str, Any]]:
        """The collections this caller reaches, each with its size."""
        data = _data(self._send(_Request("GET", "/api/v1/collections")))
        return list((data or {}).get("collections", []) if isinstance(data, dict) else data or [])

    def create_collection(
        self,
        name: str,
        *,
        hybrid: bool = True,
        description: Optional[str] = None,
        dimension: int = 384,
        metric: str = "cosine",
    ) -> "RemoteCollection":
        """Make a collection, searchable by meaning and exact words unless ``hybrid=False``."""
        body: Dict[str, Any] = {
            "name": name,
            "dimension": dimension,
            "metric": metric,
            "enable_text_index": bool(hybrid),
            "tags": ["hybrid"] if hybrid else ["dense"],
        }
        if description:
            body["description"] = description
        self._send(_Request("POST", "/api/v2/collections", json=body))
        return self.collection(name)

    def delete_collection(self, name: str) -> None:
        """Delete a collection and everything in it, for good."""
        self._send(_Request("DELETE", _path("/api/v1/collections/{name}", name=name)))

    def collection(self, name: str) -> "RemoteCollection":
        """One collection, with the calls a local ``Vectrix`` has."""
        return RemoteCollection(self, name)


class RemoteCollection:
    """One collection on a server: search it, add to it, read what it holds."""

    def __init__(self, client: Client, name: str) -> None:
        self.client, self.name = client, name

    def __repr__(self) -> str:
        return f"RemoteCollection({self.name!r} at {self.client.url})"

    def describe(self) -> Dict[str, Any]:
        """Its size, how it is searched, and the metadata fields it can be filtered on."""
        return _data(
            self.client._send(_Request("GET", _path("/api/v1/collections/{name}", name=self.name)))
        )

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        mode: str = "hybrid",
        filter: Optional[Mapping[str, Any]] = None,
    ) -> Results:
        """Search as this caller: ``hybrid`` (the default), ``dense``, ``keyword`` or ``rerank``."""
        started = time.perf_counter()
        response = self.client._send(_search_request(self.name, query, mode, limit, filter))
        return _results(_data(response), query, mode, started)

    def similar(
        self, id: str, limit: int = 10, *, filter: Optional[Mapping[str, Any]] = None
    ) -> Results:
        """The chunks most like one, by the id a search result gives."""
        started = time.perf_counter()
        body: Dict[str, Any] = {"id": id, "limit": int(limit)}
        if filter:
            body["filter"] = dict(filter)
        response = self.client._send(
            _Request("POST", _path("/api/v1/collections/{name}/similar", name=self.name), json=body)
        )
        return _results(_data(response), id, "similar", started)

    def add(
        self,
        texts: Union[str, Sequence[str]],
        ids: Optional[Sequence[str]] = None,
        metadata: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> int:
        """Add records, the server embedding each. Returns how many were written."""
        texts = [texts] if isinstance(texts, str) else list(texts)
        data = _data(self.client._send(_upsert_request(self.name, texts, ids, metadata)))
        return int(data.get("added", len(texts))) if isinstance(data, dict) else len(texts)

    def add_document(
        self,
        source: Union[str, Path, bytes],
        *,
        doc_id: Optional[str] = None,
        filename: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        chunk: Optional[str] = None,
        chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Read, cut and index a document: a file's path, its bytes, or a string of Markdown. Returns the server's account."""
        request = _document_request(
            self.name, source, doc_id, filename, metadata, chunk, chunk_size, overlap
        )
        return _data(self.client._send(request))

    def documents(self) -> List[Dict[str, Any]]:
        """The documents it holds, on a server that keeps them."""
        data = _data(
            self.client._send(
                _Request("GET", _path("/api/v1/collections/{name}/documents", name=self.name))
            )
        )
        return (
            list((data or {}).get("documents", [])) if isinstance(data, dict) else list(data or [])
        )

    def document(self, doc_id: str) -> str:
        """A document's Markdown, as it was indexed."""
        response = self.client._send(
            _Request(
                "GET",
                _path(
                    "/api/v1/collections/{name}/documents/{doc_id}", name=self.name, doc_id=doc_id
                ),
            )
        )
        return response.text

    def delete_document(self, doc_id: str) -> int:
        """Delete a document and every chunk of it, for good. Returns how many chunks went."""
        data = _data(
            self.client._send(
                _Request(
                    "DELETE",
                    _path(
                        "/api/v1/collections/{name}/documents/{doc_id}",
                        name=self.name,
                        doc_id=doc_id,
                    ),
                )
            )
        )
        return int((data or {}).get("chunks_removed", 0)) if isinstance(data, dict) else 0

    def sources(self) -> List[Dict[str, Any]]:
        """The feeds and pages it keeps up with."""
        data = _data(
            self.client._send(
                _Request("GET", _path("/api/v1/collections/{name}/sources", name=self.name))
            )
        )
        return list((data or {}).get("sources", [])) if isinstance(data, dict) else list(data or [])

    def add_source(
        self, address: str, *, every: str = "6h", kind: Optional[str] = None
    ) -> Dict[str, Any]:
        """Keep up with a feed or a page, read every ``every``. Nothing is written until a refresh."""
        body: Dict[str, Any] = {"address": address, "every": every}
        if kind:
            body["kind"] = kind
        data = _data(
            self.client._send(
                _Request(
                    "POST", _path("/api/v1/collections/{name}/sources", name=self.name), json=body
                )
            )
        )
        return dict((data or {}).get("source", data or {})) if isinstance(data, dict) else {}

    def refresh_sources(
        self, source: Optional[str] = None, *, force: bool = False
    ) -> Dict[str, Any]:
        """Read its sources now: one, or every one that is due; ``force`` reads every one."""
        body: Dict[str, Any] = {"force": bool(force)}
        if source:
            body["source"] = source
        return _data(
            self.client._send(
                _Request(
                    "POST",
                    _path("/api/v1/collections/{name}/sources/refresh", name=self.name),
                    json=body,
                )
            )
        )


# ============================================================================
# THE ASYNC CLIENT
# ============================================================================
#
# INPUT   the same as the blocking client
# OUTPUT  the same calls, as coroutines, over one httpx.AsyncClient


class AsyncClient:
    """A VectrixDB server, as one caller, for asyncio. Made by :func:`connect_async`."""

    def __init__(
        self,
        url: str,
        *,
        key: Optional[str] = None,
        token: Optional[Union[Token, Callable[[], Awaitable[str]]]] = None,
        key_header: str = "api-key",
        timeout: float = 60.0,
        retries: int = 3,
        http: Any = None,
        verify: Any = True,
        allow_http: bool = False,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        if key and token:
            raise ValueError(
                "give key= or token=, not both: the server takes one caller per request"
            )
        self.url = _check_url(url, bool(key or token) and http is None, allow_http)
        self._key, self._token, self._key_header = key, token, key_header
        self._extra = _extra_headers(headers, user_agent, key_header)
        self.retries = max(0, int(retries))
        self._own = http is None
        self._http = (
            http
            if http is not None
            else _httpx().AsyncClient(
                base_url=self.url,
                timeout=timeout,
                verify=tls(verify),
                follow_redirects=False,
                headers={"User-Agent": _USER_AGENT},
            )
        )

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own:
            await self._http.aclose()

    async def _headers(self) -> Dict[str, str]:
        if self._key:
            return {self._key_header: self._key}
        if self._token:
            value = self._token() if callable(self._token) else self._token
            if hasattr(value, "__await__"):
                value = await value  # type: ignore[misc,unused-ignore]
            return {"Authorization": f"Bearer {value}"}
        return {}

    async def _send(self, request: _Request) -> Any:
        import asyncio

        httpx = _httpx()
        attempt = 0
        while True:
            headers = {
                **self._extra,
                **(await self._headers()),
                **dict(request.headers or {}),
            }
            try:
                response = await self._http.request(
                    request.method,
                    request.path,
                    json=request.json,
                    content=request.content,
                    headers=headers,
                    params=dict(request.params or {}) or None,
                )
            except httpx.TransportError:
                if attempt >= self.retries:
                    raise
                await asyncio.sleep(_wait(None, attempt))
                attempt += 1
                continue
            if response.status_code in _AGAIN and attempt < self.retries:
                await asyncio.sleep(_wait(response, attempt))
                attempt += 1
                continue
            if response.status_code >= 300:
                raise _refusal(response)
            return response

    async def health(self) -> Dict[str, Any]:
        return _data(await self._send(_Request("GET", "/health")))

    async def ready(self) -> bool:
        try:
            await self._send(_Request("GET", "/ready"))
        except ServerBusy:
            return False
        return True

    async def whoami(self) -> Dict[str, Any]:
        return _data(await self._send(_Request("GET", "/api/v1/whoami")))

    async def collections(self) -> List[Dict[str, Any]]:
        data = _data(await self._send(_Request("GET", "/api/v1/collections")))
        return list((data or {}).get("collections", []) if isinstance(data, dict) else data or [])

    async def create_collection(
        self,
        name: str,
        *,
        hybrid: bool = True,
        description: Optional[str] = None,
        dimension: int = 384,
        metric: str = "cosine",
    ) -> "AsyncRemoteCollection":
        body: Dict[str, Any] = {
            "name": name,
            "dimension": dimension,
            "metric": metric,
            "enable_text_index": bool(hybrid),
            "tags": ["hybrid"] if hybrid else ["dense"],
        }
        if description:
            body["description"] = description
        await self._send(_Request("POST", "/api/v2/collections", json=body))
        return self.collection(name)

    async def delete_collection(self, name: str) -> None:
        await self._send(_Request("DELETE", _path("/api/v1/collections/{name}", name=name)))

    def collection(self, name: str) -> "AsyncRemoteCollection":
        return AsyncRemoteCollection(self, name)


class AsyncRemoteCollection:
    """One collection on a server, for asyncio: the calls of :class:`RemoteCollection`, awaited."""

    def __init__(self, client: AsyncClient, name: str) -> None:
        self.client, self.name = client, name

    def __repr__(self) -> str:
        return f"AsyncRemoteCollection({self.name!r} at {self.client.url})"

    async def describe(self) -> Dict[str, Any]:
        return _data(
            await self.client._send(
                _Request("GET", _path("/api/v1/collections/{name}", name=self.name))
            )
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        *,
        mode: str = "hybrid",
        filter: Optional[Mapping[str, Any]] = None,
    ) -> Results:
        started = time.perf_counter()
        response = await self.client._send(_search_request(self.name, query, mode, limit, filter))
        return _results(_data(response), query, mode, started)

    async def similar(
        self, id: str, limit: int = 10, *, filter: Optional[Mapping[str, Any]] = None
    ) -> Results:
        started = time.perf_counter()
        body: Dict[str, Any] = {"id": id, "limit": int(limit)}
        if filter:
            body["filter"] = dict(filter)
        response = await self.client._send(
            _Request("POST", _path("/api/v1/collections/{name}/similar", name=self.name), json=body)
        )
        return _results(_data(response), id, "similar", started)

    async def add(
        self,
        texts: Union[str, Sequence[str]],
        ids: Optional[Sequence[str]] = None,
        metadata: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> int:
        texts = [texts] if isinstance(texts, str) else list(texts)
        data = _data(await self.client._send(_upsert_request(self.name, texts, ids, metadata)))
        return int(data.get("added", len(texts))) if isinstance(data, dict) else len(texts)

    async def add_document(
        self,
        source: Union[str, Path, bytes],
        *,
        doc_id: Optional[str] = None,
        filename: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        chunk: Optional[str] = None,
        chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> Dict[str, Any]:
        request = _document_request(
            self.name, source, doc_id, filename, metadata, chunk, chunk_size, overlap
        )
        return _data(await self.client._send(request))

    async def documents(self) -> List[Dict[str, Any]]:
        data = _data(
            await self.client._send(
                _Request("GET", _path("/api/v1/collections/{name}/documents", name=self.name))
            )
        )
        return (
            list((data or {}).get("documents", [])) if isinstance(data, dict) else list(data or [])
        )

    async def document(self, doc_id: str) -> str:
        response = await self.client._send(
            _Request(
                "GET",
                _path(
                    "/api/v1/collections/{name}/documents/{doc_id}", name=self.name, doc_id=doc_id
                ),
            )
        )
        return response.text

    async def delete_document(self, doc_id: str) -> int:
        data = _data(
            await self.client._send(
                _Request(
                    "DELETE",
                    _path(
                        "/api/v1/collections/{name}/documents/{doc_id}",
                        name=self.name,
                        doc_id=doc_id,
                    ),
                )
            )
        )
        return int((data or {}).get("chunks_removed", 0)) if isinstance(data, dict) else 0

    async def sources(self) -> List[Dict[str, Any]]:
        data = _data(
            await self.client._send(
                _Request("GET", _path("/api/v1/collections/{name}/sources", name=self.name))
            )
        )
        return list((data or {}).get("sources", [])) if isinstance(data, dict) else list(data or [])

    async def add_source(
        self, address: str, *, every: str = "6h", kind: Optional[str] = None
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"address": address, "every": every}
        if kind:
            body["kind"] = kind
        data = _data(
            await self.client._send(
                _Request(
                    "POST", _path("/api/v1/collections/{name}/sources", name=self.name), json=body
                )
            )
        )
        return dict((data or {}).get("source", data or {})) if isinstance(data, dict) else {}

    async def refresh_sources(
        self, source: Optional[str] = None, *, force: bool = False
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"force": bool(force)}
        if source:
            body["source"] = source
        return _data(
            await self.client._send(
                _Request(
                    "POST",
                    _path("/api/v1/collections/{name}/sources/refresh", name=self.name),
                    json=body,
                )
            )
        )


# ============================================================================
# CONNECT
# ============================================================================


def _with_company(url: Optional[str], options: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """The address, else VECTRIXDB_URL, else the company's server; the company's options under the caller's own."""
    import os

    from . import company

    found = company.load()
    address = (url or os.environ.get("VECTRIXDB_URL", "") or found.server).strip()
    if not address:
        raise ValueError(
            "which server? connect(url), VECTRIXDB_URL, or a company's defaults: see vectrixdb.company"
        )
    merged = {**found.for_server(address), **options}
    if "headers" in options and "headers" in found.for_server(address):
        merged["headers"] = {**found.for_server(address)["headers"], **options["headers"]}
    return address, merged


@overload
def connect(url: Optional[str] = None, *, collection: None = None, **options: Any) -> Client: ...
@overload
def connect(url: Optional[str] = None, *, collection: str, **options: Any) -> RemoteCollection: ...
def connect(
    url: Optional[str] = None, *, collection: Optional[str] = None, **options: Any
) -> Union[Client, RemoteCollection]:
    """A VectrixDB server at ``url``, as ``key=`` or ``token=``. With ``collection=``, that collection.

    Options: ``key_header`` (the header a key goes in, ``api-key``),
    ``timeout`` (seconds, 60), ``retries`` (3), ``verify`` (``True``, a
    company's certificate authority as a file or folder, ``"system"`` for the
    operating system's certificates, or an ``ssl.SSLContext``),
    ``allow_http`` (send the key over ``http://`` to another machine; off),
    ``headers`` (sent with every request, such as a gateway's subscription
    key; never the caller's own), ``user_agent`` (a wrapper's name and
    version, put before the client's), and ``http``, an ``httpx.Client`` of
    your own, for a test or anything else, which is then yours to secure.

    With no ``url``, ``VECTRIXDB_URL``, else the company's server
    (:mod:`vectrixdb.company`). The company's headers, certificate authority
    and key header go with calls to its own server only.
    """
    address, merged = _with_company(url, options)
    client = Client(address, **merged)
    return client.collection(collection) if collection else client


@overload
def connect_async(
    url: Optional[str] = None, *, collection: None = None, **options: Any
) -> AsyncClient: ...
@overload
def connect_async(
    url: Optional[str] = None, *, collection: str, **options: Any
) -> AsyncRemoteCollection: ...
def connect_async(
    url: Optional[str] = None, *, collection: Optional[str] = None, **options: Any
) -> Union[AsyncClient, AsyncRemoteCollection]:
    """The async client: the same calls as :func:`connect`, as coroutines."""
    address, merged = _with_company(url, options)
    client = AsyncClient(address, **merged)
    return client.collection(collection) if collection else client
