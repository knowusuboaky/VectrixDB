"""Extraction: whoever is best placed turns a file's bytes into text.

The library reads what it can read offline: PDF, DOCX, PPTX, XLSX, CSV, HTML,
Markdown and text. A scanned page, an hour of audio or a format nobody here
has heard of needs somebody else, and that somebody is a callable:

    def ocr_pdf(data: bytes, name: str) -> LoadedDocument: ...

    extract.register(".pdf", ocr_pdf)                     # for the process
    Vectrix("docs", extractors={".pdf": ocr_pdf})         # for one collection
    Vectrix("docs", extractors=HttpExtractor(url, routes={".pdf": "/extract/pdf"}))

An extractor takes the bytes and the file's name and returns a
:class:`~vectrixdb.ingest.LoadedDocument`, a string of Markdown, or a mapping
with ``text`` and optionally ``pages``, ``headings`` and ``metadata``. It may
take a third argument, ``source``, to be told where the bytes came from. A
suffix nobody registered falls through to the built-in readers, so
registering one extractor changes one format and nothing else.

What this module does not do: decide what a document is entitled to. An
extractor returns text and positions. The fields a policy decides by come
from the ingestion, never from a service that was only asked to read a file.
"""

from __future__ import annotations

import inspect
import json
import random
import time
import uuid
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

from ..exceptions import ExtractionError
from ..ingest import LoadedDocument, _labels_from, markdown_document

__all__ = [
    "ChatDescriber",
    "ExtractionError",
    "Extractor",
    "ExtractorRegistry",
    "Fallback",
    "HttpDescriber",
    "HttpExtractor",
    "PageReader",
    "batched",
    "coerce",
    "load_url",
    "load_youtube",
    "is_youtube",
    "register",
    "registered",
    "resolve",
    "unregister",
    "WordsOnly",
    "describer_from_environment",
]


# ============================================================================
# SETTINGS: the extractor type, and the wildcard
# ============================================================================
#
# What an extractor is, a callable from bytes and a name to a document, and
# the suffix that stands for any file.

Extractor = Callable[..., Any]
ANY = "*"


# ============================================================================
# SUFFIXES, AND WHAT AN EXTRACTOR RETURNED
# ============================================================================
#
# INPUT   a name, a path, a URI or a bare suffix; whatever an extractor
#         returned
# OUTPUT  the suffix lowercased; the key a caller is registering; a
#         LoadedDocument, whatever shape came back
#
# An extractor may return a string, a document or a dict; all reach the
# collection as one shape.


def _suffix_of(name: str) -> str:
    """``.pdf`` from a name, a path, a URI or a bare suffix, lowercased."""
    text = str(name).strip().lower()
    if text == ANY:
        return ANY
    if text.startswith(".") and "/" not in text and text.count(".") == 1:
        return text
    return PurePosixPath(text.split("?")[0].replace("\\", "/")).suffix


def _suffix_key(raw: str) -> str:
    """The suffix a caller is registering: ``.pdf``, or ``pdf`` for the same."""
    text = str(raw).strip().lower()
    if text.isalnum():
        return "." + text
    return _suffix_of(text)


def _positions(raw: Any, width: int, what: str, length: int) -> List[tuple]:
    out: List[tuple] = []
    last = -1
    for entry in raw or []:
        if not isinstance(entry, (list, tuple)) or len(entry) != width:
            raise ExtractionError(f"{what} entries are lists of {width}, got {entry!r}")
        offset = entry[0]
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= length:
            raise ExtractionError(f"a {what} offset is an integer inside the text, got {offset!r}")
        if offset < last:
            raise ExtractionError(f"{what} offsets ascend, and {offset} follows {last}")
        last = offset
        out.append(tuple(entry))
    return out


def coerce(result: Any, name: str = "") -> LoadedDocument:
    """Whatever an extractor returned, as a :class:`LoadedDocument`.

    A string is read as Markdown: its headings are found, its images become
    figure lines and its pipe tables become rows, exactly as a ``.md`` file
    is read. A mapping is the structured reply, where the extractor knows
    the page breaks; its offsets are checked, because an offset past the end
    of the text would give every chunk after it the wrong page in silence.
    """
    if isinstance(result, LoadedDocument):
        return result
    if isinstance(result, (bytes, bytearray)):
        result = bytes(result).decode("utf-8", errors="replace")
    if isinstance(result, str):
        return markdown_document(result)
    if isinstance(result, Mapping):
        text = result.get("text")
        if not isinstance(text, str):
            raise ExtractionError(f"the reply for {name or 'a document'} has no text")
        pages = [
            (int(o), int(n)) for o, n in _positions(result.get("pages"), 2, "pages", len(text))
        ]
        headings = [
            (int(o), str(h), int(level))
            for o, h, level in _positions(result.get("headings"), 3, "headings", len(text))
        ]
        metadata = result.get("metadata") or {}
        # A recording's phrases with their times, from a service that sends
        # them, such as vectrixdb's own extraction service. Not offsets into
        # the text, so they are taken as they come rather than checked
        # against its length the way pages and headings are.
        segments = [(float(a), float(b), str(t)) for a, b, t in result.get("segments") or []]
        if not isinstance(metadata, Mapping):
            raise ExtractionError("the reply's metadata is an object")
        # The figure lines the reply's text holds, with what the service knew about each.
        figures = [
            (int(o), dict(info))
            for o, info in _positions(result.get("figures"), 2, "figures", len(text))
            if isinstance(info, Mapping)
        ]
        doc = markdown_document(
            text, pages=pages, headings=headings or None, metadata=dict(metadata), figures=figures
        )
        doc.segments = segments
        # A PDF's printed page numbers, from a service that sends them.
        doc.page_labels = _labels_from(result.get("page_labels"))
        return doc
    raise ExtractionError(
        f"an extractor returns a LoadedDocument, a string or a mapping, not {type(result).__name__}"
    )


# ============================================================================
# THE REGISTRY: suffix to extractor, with a parent to fall back on
# ============================================================================
#
# INPUT   suffixes and an extractor; what a caller passed
# OUTPUT  an extractor used for these suffixes everywhere in this process,
#         registered and unregistered; the suffixes somebody registered; a
#         registry from nothing, a mapping, a registry or a list
#
# Built-in readers are not listed; they are the parent every registry falls
# back on.


class ExtractorRegistry:
    """Suffix to extractor, with a parent to fall back on.

    A collection's registry sits on top of the process-wide one, so
    ``extractors={".pdf": fn}`` overrides PDF for that collection and still
    sees whatever ``register()`` added for everything else. ``"*"`` matches
    any suffix nothing more specific claimed.
    """

    def __init__(
        self,
        extractors: Optional[Mapping[str, Extractor]] = None,
        *,
        parent: Optional["ExtractorRegistry"] = None,
    ) -> None:
        self._own: Dict[str, Extractor] = {}
        self._parent = parent
        for suffix, fn in (extractors or {}).items():
            self.register(suffix, fn)

    def register(
        self, suffixes: Union[str, Iterable[str]], extractor: Extractor
    ) -> "ExtractorRegistry":
        if not callable(extractor):
            raise TypeError(f"an extractor is callable, got {type(extractor).__name__}")
        for raw in [suffixes] if isinstance(suffixes, str) else list(suffixes):
            suffix = _suffix_key(raw)
            if not suffix:
                raise ValueError(f"{raw!r} is not a suffix; write it like '.pdf'")
            self._own[suffix] = extractor
        return self

    def unregister(self, suffix: str) -> bool:
        return self._own.pop(_suffix_key(suffix), None) is not None

    def get(self, name: str) -> Optional[Extractor]:
        suffix = _suffix_of(name)
        node: Optional[ExtractorRegistry] = self
        while node is not None:
            if suffix and suffix in node._own:
                return node._own[suffix]
            node = node._parent
        node = self
        while node is not None:
            if ANY in node._own:
                return node._own[ANY]
            node = node._parent
        return None

    def suffixes(self) -> List[str]:
        seen: Dict[str, None] = {}
        node: Optional[ExtractorRegistry] = self
        while node is not None:
            for suffix in node._own:
                seen.setdefault(suffix)
            node = node._parent
        return sorted(seen)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def extract(
        self, data: bytes, name: str, source: Optional[str] = None
    ) -> Optional[LoadedDocument]:
        """Run the extractor for ``name``, or return None when there is none."""
        fn = self.get(name)
        if fn is None:
            return None
        try:
            result = fn(data, name, source=source) if _takes_source(fn) else fn(data, name)
        except ExtractionError:
            raise
        except Exception as exc:
            # One exception type for a failed extraction, whoever raised:
            # the worker and the REST route catch this and nothing else.
            raise ExtractionError(f"extracting {name} failed: {exc}") from exc
        doc = coerce(result, name)
        doc.metadata.setdefault("extractor", _label(fn))
        return doc


def _takes_source(fn: Extractor) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "source" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _label(fn: Extractor) -> str:
    label = getattr(fn, "label", None)
    if isinstance(label, str) and label:
        return label
    return getattr(fn, "__qualname__", None) or type(fn).__name__


_DEFAULT = ExtractorRegistry()


def register(suffixes: Union[str, Iterable[str]], extractor: Extractor) -> None:
    """Use ``extractor`` for these suffixes everywhere in this process."""
    _DEFAULT.register(suffixes, extractor)


def unregister(suffix: str) -> bool:
    return _DEFAULT.unregister(suffix)


def registered() -> List[str]:
    """The suffixes somebody registered. Built-in readers are not listed."""
    return _DEFAULT.suffixes()


def resolve(extractors: Any) -> ExtractorRegistry:
    """A registry from what a caller passed: nothing, a mapping, a registry,
    or one object that serves several suffixes, as :class:`HttpExtractor` does."""
    if extractors is None:
        return _DEFAULT
    if isinstance(extractors, ExtractorRegistry):
        return extractors
    if isinstance(extractors, Mapping):
        return ExtractorRegistry(extractors, parent=_DEFAULT)
    suffixes = getattr(extractors, "suffixes", None)
    if callable(extractors) and suffixes is not None:
        found = suffixes() if callable(suffixes) else suffixes
        return ExtractorRegistry({s: extractors for s in found}, parent=_DEFAULT)
    raise TypeError(
        "extractors is a mapping of suffix to callable, an ExtractorRegistry, or an "
        f"extractor that names its suffixes, not {type(extractors).__name__}"
    )


# ============================================================================
# OVER HTTP: an endpoint you run
# ============================================================================
#
# INPUT   a file, a name and an endpoint; a figure; an address
# OUTPUT  the document the service read, masked when asked, with the masking
#         summary kept and never the offsets; a figure described by the
#         service; a document fetched by its address and read like a file
#
# The transport is a function, so the tests hand one in and nothing here opens
# a socket. Whoever is best placed turns a file's bytes into text: the library
# reads what it can read offline, and hands the rest to a service.

Transport = Callable[[str, str, Dict[str, str], bytes, float], Tuple[int, Mapping[str, str], bytes]]


#: What is worth asking again: the network, not the file. A plain error from a transport is final.
_TRANSIENT_ERRORS: Tuple[type[BaseException], ...] = (OSError, TimeoutError, ConnectionError)
_TRANSIENT_STATUSES = frozenset({408, 429, 502, 503, 504})


def _urllib_transport(method: str, url: str, headers: Dict[str, str], body: bytes, timeout: float):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310 - scheme checked at construction
            return reply.status, dict(reply.headers.items()), reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


def _multipart(field: str, name: str, data: bytes) -> Tuple[bytes, str]:
    boundary = "vx" + uuid.uuid4().hex
    safe = name.replace('"', "").replace("\r", "").replace("\n", "")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{safe}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    return (
        head + data + f"\r\n--{boundary}--\r\n".encode(),
        f"multipart/form-data; boundary={boundary}",
    )


class HttpExtractor:
    """Extraction by an endpoint you run.

    ``routes`` maps a suffix to a path on ``base_url``; a route may carry a
    query string. The file goes up as a multipart field, or with
    ``body="raw"`` as the request body with its name in ``filename_header``,
    which is how a service that reads ``await request.body()`` wants it.

    The reply is plain text, read as Markdown, or JSON::

        {"text": "...", "pages": [[0, 1], [1842, 2]],
         "headings": [[0, "Late fees", 1]], "metadata": {"ocr": true}}

    Plain text gives a document cited by name; ``pages`` is what makes
    ``scan.pdf#page=4`` possible. Anything but a 2xx raises
    :class:`ExtractionError` naming the route and the status.

    The bytes of every file with a routed suffix are sent to ``base_url``,
    so it is an address the host chose. Only ``http`` and ``https`` are
    accepted. ``headers`` is where a key for the service goes, if it has one.

    ``mask`` asks the service to mask identifiers as it reads, the way
    VectrixDB's own extraction service does with ``?mask=1``: ``True`` for
    the identifiers, ``"all"`` for everything its engine knows, or the types,
    a list or a comma-separated string. What the service reports it masked
    comes back in the document's metadata as ``masking``, counts and a risk
    score, so what is kept says what was taken out of it.
    """

    def __init__(
        self,
        base_url: str,
        routes: Mapping[str, str],
        *,
        body: str = "multipart",
        field: str = "file",
        filename_header: str = "X-Filename",
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 60.0,
        transport: Optional[Transport] = None,
        mask: Union[bool, str, Sequence[str], None] = False,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"base_url is an http or https address, got {base_url!r}")
        if body not in ("multipart", "raw"):
            raise ValueError(f"body is 'multipart' or 'raw', got {body!r}")
        if not routes:
            raise ValueError("routes maps at least one suffix to a path")
        self.base_url = base_url.rstrip("/")
        self.routes: Dict[str, str] = {}
        for suffix, route in routes.items():
            key = _suffix_key(suffix)
            if not key:
                raise ValueError(f"{suffix!r} is not a suffix; write it like '.pdf'")
            self.routes[key] = route if route.startswith("/") else "/" + route
        self.body = body
        self.field = field
        self.filename_header = filename_header
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self._transport: Transport = transport or _urllib_transport
        self.label = self.base_url
        self.mask = _mask_query(mask)
        # A service that timed out, could not be reached, or answered 408, 429, 502, 503 or 504 is
        # asked again, waiting longer each time with jitter and honouring Retry-After; up to retries more times.
        self.retries = max(0, int(retries))
        self.backoff = max(0.0, float(backoff))
        self._sleep = sleep

    @classmethod
    def from_environment(
        cls, env: Optional[Mapping[str, str]] = None, *, routes: Optional[Mapping[str, str]] = None
    ) -> Optional["HttpExtractor"]:
        """The service ``VECTRIXDB_EXTRACTOR_URL`` names, or None when it names none.

        ``VECTRIXDB_EXTRACTOR_KEY`` goes in the header ``VECTRIXDB_EXTRACTOR_KEY_HEADER``
        names, ``x-api-key`` by default; ``VECTRIXDB_EXTRACTOR_BODY`` is ``raw``
        (the default) or ``multipart``; ``VECTRIXDB_EXTRACTOR_TIMEOUT`` is in
        seconds, 300 by default. ``VECTRIXDB_EXTRACTOR_ROUTES``, a JSON mapping
        of suffix to route, wins over ``routes``, which is what the caller
        routes by when the setting is not there: for VectrixDB's own extraction
        service, :func:`vectrixdb.api.extraction.extraction_routes`.
        ``VECTRIXDB_EXTRACTOR_MASK`` asks the service to mask as it reads:
        ``1`` for the identifiers, ``all``, or the types, comma separated.
        """
        import os

        from ..exceptions import ConfigurationError

        found = os.environ if env is None else env
        url = str(found.get("VECTRIXDB_EXTRACTOR_URL") or "").strip()
        if not url:
            return None
        raw = str(found.get("VECTRIXDB_EXTRACTOR_ROUTES") or "").strip()
        if raw:
            try:
                routes = json.loads(raw)
            except ValueError as exc:
                raise ConfigurationError(f"VECTRIXDB_EXTRACTOR_ROUTES is not JSON: {exc}") from exc
            if not isinstance(routes, Mapping):
                raise ConfigurationError(
                    'VECTRIXDB_EXTRACTOR_ROUTES is a mapping of suffix to route: {".pdf": "/extract/pdf"}'
                )
        if not routes:
            raise ConfigurationError(
                f"VECTRIXDB_EXTRACTOR_URL is {url} and nothing says which route reads what"
            )
        key = str(found.get("VECTRIXDB_EXTRACTOR_KEY") or "").strip()
        header = str(found.get("VECTRIXDB_EXTRACTOR_KEY_HEADER") or "").strip() or "x-api-key"
        try:
            return cls(
                url,
                routes,
                body=str(found.get("VECTRIXDB_EXTRACTOR_BODY") or "").strip() or "raw",
                retries=int(found.get("VECTRIXDB_EXTRACTOR_RETRIES") or 3),
                headers={header: key} if key else None,
                timeout=float(found.get("VECTRIXDB_EXTRACTOR_TIMEOUT") or 300),
                mask=str(found.get("VECTRIXDB_EXTRACTOR_MASK") or "").strip() or False,
            )
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc

    def suffixes(self) -> List[str]:
        return sorted(self.routes)

    def route_for(self, name: str) -> Optional[str]:
        return self.routes.get(_suffix_of(name)) or self.routes.get(ANY)

    def _wait(self, attempt: int, reply_headers: Optional[Mapping[str, Any]]) -> None:
        """Before the next try: Retry-After when the service says, else the backoff doubled each time, with jitter, at most a minute."""
        asked = None
        for key, value in (reply_headers or {}).items():
            if str(key).lower() == "retry-after":
                try:
                    asked = float(str(value).strip())
                except ValueError:
                    asked = None
        wait = (
            min(60.0, asked)
            if asked is not None and asked >= 0
            else min(60.0, self.backoff * (2**attempt)) * (0.5 + random.random())
        )
        if wait > 0:
            self._sleep(wait)

    def __call__(self, data: bytes, name: str, source: Optional[str] = None) -> LoadedDocument:
        route = self.route_for(name)
        if route is None:
            raise ExtractionError(f"no route for {name}", route=None)
        url = self.base_url + route
        if self.mask:
            url += ("&" if "?" in url else "?") + self.mask
        headers = dict(self.headers)
        if self.body == "raw":
            payload = bytes(data)
            headers.setdefault("Content-Type", "application/octet-stream")
            # A header is latin-1 on the wire; a name that is not is sent
            # percent-encoded rather than dropped.
            try:
                name.encode("latin-1")
                headers[self.filename_header] = name
            except UnicodeEncodeError:
                from urllib.parse import quote

                headers[self.filename_header] = quote(name)
        else:
            payload, content_type = _multipart(self.field, name, bytes(data))
            headers["Content-Type"] = content_type
        headers.setdefault("Accept", "application/json, text/plain;q=0.9, */*;q=0.1")
        attempt = 0
        while True:
            try:
                status, reply_headers, reply = self._transport(
                    "POST", url, headers, payload, self.timeout
                )
            except ExtractionError:
                raise
            except _TRANSIENT_ERRORS as exc:
                if attempt < self.retries:
                    self._wait(attempt, None)
                    attempt += 1
                    continue
                raise ExtractionError(
                    f"{route} could not be reached after {attempt + 1} tries: {exc}", route=route
                ) from exc
            except Exception as exc:
                raise ExtractionError(f"{route} could not be reached: {exc}", route=route) from exc
            if int(status) in _TRANSIENT_STATUSES and attempt < self.retries:
                self._wait(attempt, reply_headers)
                attempt += 1
                continue
            break
        if not 200 <= int(status) < 300:
            detail = bytes(reply or b"")[:300].decode("utf-8", errors="replace").strip()
            raise ExtractionError(
                f"{route} answered {status} for {name}"
                + (f": {detail}" if detail else "")
                + (f" ({attempt + 1} tries)" if attempt else ""),
                route=route,
                status=int(status),
            )
        content_type = ""
        for key, value in (reply_headers or {}).items():
            if str(key).lower() == "content-type":
                content_type = str(value).lower()
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            text = bytes(reply).decode(charset, errors="replace")
        except LookupError:
            text = bytes(reply).decode("utf-8", errors="replace")
        if "json" in content_type:
            try:
                parsed: Any = json.loads(text)
            except ValueError as exc:
                raise ExtractionError(
                    f"{route} said JSON and sent something else", route=route
                ) from exc
            # A service that wraps plain text in a JSON string is still plain text.
            doc = coerce(parsed if isinstance(parsed, (Mapping, str)) else {"text": None}, name)
            if isinstance(parsed, Mapping) and isinstance(parsed.get("masking"), Mapping):
                doc.metadata["masking"] = _masking_summary(parsed["masking"])
        else:
            doc = coerce(text, name)
            said = next(
                (
                    str(value)
                    for key, value in (reply_headers or {}).items()
                    if str(key).lower() == "x-masking"
                ),
                "",
            )
            if said:
                try:
                    doc.metadata["masking"] = _masking_summary(json.loads(said))
                except ValueError:
                    pass
        doc.metadata.setdefault("extractor", url.split("?")[0])
        return doc


def _mask_query(mask: Union[bool, str, Sequence[str], None]) -> str:
    """The query that asks the service to mask: nothing, ``mask=1``, or ``mask=1&types=...``."""
    if mask is None or mask is False:
        return ""
    if mask is True:
        return "mask=1"
    if isinstance(mask, str):
        word = mask.strip().lower()
        if not word or word in ("0", "no", "false", "off"):
            return ""
        if word in ("1", "yes", "true", "on"):
            return "mask=1"
        return f"mask=1&types={word.replace(' ', '')}"
    named = ",".join(str(part).strip().lower() for part in mask if str(part).strip())
    return f"mask=1&types={named}" if named else "mask=1"


def _masking_summary(said: Mapping[str, Any]) -> Dict[str, Any]:
    """What the service reported, without the text it already handed back, and without the offsets nobody keeps."""
    return {
        key: value
        for key, value in said.items()
        if key in ("counts", "score", "engine", "language", "regex_only")
    }


class HttpDescriber:
    """A figure described by an endpoint you run.

    What ``Vectrix(describe_figures=...)`` wants is a callable of the image
    bytes and a context; this is one that posts the image to ``url``. With
    ``body="raw"`` the image is the request body, its name goes in
    ``filename_header`` and the context, the caption, the page and the text
    either side, goes percent-encoded in ``X-Context``. With the default,
    multipart, the image is the ``file`` field and the context a ``context``
    field of JSON.

    The reply is plain text, the description, or JSON with any of
    ``caption``, ``description``, ``table`` (rows) and ``decorative``.
    """

    def __init__(
        self,
        url: str,
        *,
        body: str = "multipart",
        field: str = "file",
        filename_header: str = "X-Filename",
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 120.0,
        transport: Optional[Transport] = None,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"url is an http or https address, got {url!r}")
        if body not in ("multipart", "raw"):
            raise ValueError(f"body is 'multipart' or 'raw', got {body!r}")
        self.url = url
        self.body = body
        self.field = field
        self.filename_header = filename_header
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self._transport: Transport = transport or _urllib_transport

    def __call__(self, image: bytes, context: Mapping[str, Any]) -> Any:
        from urllib.parse import quote

        name = str(context.get("name") or "figure") + ".png"
        packed = json.dumps(
            {k: v for k, v in context.items() if v not in (None, "")}, ensure_ascii=False
        )
        headers = dict(self.headers)
        if self.body == "raw":
            payload = bytes(image)
            headers.setdefault("Content-Type", "application/octet-stream")
            headers[self.filename_header] = quote(name)
            headers["X-Context"] = quote(packed)[:6000]
        else:
            boundary = "vx" + uuid.uuid4().hex
            crlf = chr(13) + chr(10)
            context_part = crlf.join(
                [
                    f"--{boundary}",
                    'Content-Disposition: form-data; name="context"',
                    "Content-Type: application/json",
                    "",
                    packed,
                ]
            )
            file_head = crlf.join(
                [
                    f"--{boundary}",
                    f'Content-Disposition: form-data; name="{self.field}"; filename="{quote(name)}"',
                    "Content-Type: application/octet-stream",
                    "",
                    "",
                ]
            )
            payload = (
                (context_part + crlf + file_head).encode("utf-8")
                + bytes(image)
                + (crlf + f"--{boundary}--" + crlf).encode()
            )
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        try:
            status, reply_headers, reply = self._transport(
                "POST", self.url, headers, payload, self.timeout
            )
        except Exception as exc:
            raise ExtractionError(
                f"the figure describer could not be reached: {exc}", route=self.url
            ) from exc
        if not 200 <= int(status) < 300:
            detail = bytes(reply or b"")[:300].decode("utf-8", errors="replace").strip()
            raise ExtractionError(
                f"the figure describer answered {status}: {detail}",
                route=self.url,
                status=int(status),
            )
        kind = ""
        for key, value in (reply_headers or {}).items():
            if str(key).lower() == "content-type":
                kind = str(value).lower()
        text = bytes(reply).decode("utf-8", errors="replace")
        if "json" in kind:
            try:
                parsed_reply = json.loads(text)
            except ValueError as exc:
                raise ExtractionError(
                    "the figure describer said JSON and sent something else", route=self.url
                ) from exc
            return parsed_reply if isinstance(parsed_reply, (Mapping, str)) else None
        return text.strip() or None


def load_url(
    url: str,
    *,
    extractors: Any = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    transport: Optional[Transport] = None,
    images: bool = False,
) -> LoadedDocument:
    """A document fetched by its address and read like a file.

    What it is called decides who reads it: ``report.pdf`` at the end of the
    address goes to the PDF reader or to an extractor registered for
    ``.pdf``. An address with no suffix that answers with HTML is read as a
    page. Only ``http`` and ``https``. This fetches whatever address it is
    given, so it is for addresses the host chose, not ones a request named.
    """
    from ..ingest import load_bytes

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"url is an http or https address, got {url!r}")
    send = dict(headers or {})
    send.setdefault("Accept", "text/html, application/pdf;q=0.9, */*;q=0.5")
    try:
        status, reply_headers, body = (transport or _urllib_transport)(
            "GET", url, send, b"", float(timeout)
        )
    except Exception as exc:
        raise ExtractionError(f"{url} could not be reached: {exc}") from exc
    if not 200 <= int(status) < 300:
        raise ExtractionError(f"{url} answered {status}", status=int(status))
    name = PurePosixPath(parsed.path).name
    content_type = ""
    for key, value in (reply_headers or {}).items():
        if str(key).lower() == "content-type":
            content_type = str(value).lower()
    if not PurePosixPath(name).suffix:
        name = (name or parsed.netloc) + (
            ".html" if "html" in content_type or not content_type else ".txt"
        )
    return load_bytes(bytes(body), name, extractors=extractors, source=url, images=images)


# ============================================================================
# LAZY: youtube and batches
# ============================================================================
#
# INPUT   a name
# OUTPUT  load_youtube, is_youtube and batched, imported when first asked for
#
# yt-dlp and ffmpeg are not everybody's, so they load on demand.


def __getattr__(name: str) -> Any:
    """load_youtube, is_youtube and batched, imported when first asked for.

    Lazily, because the module they live in is only wanted by a caller that
    fetches videos, and importing it with the rest of this package would
    cost everybody who does not.
    """
    if name in ("load_youtube", "is_youtube"):
        from . import youtube

        return getattr(youtube, name)
    if name == "batched":
        from .batches import batched

        return batched
    if name in ("ChatDescriber", "Fallback", "WordsOnly", "describer_from_environment"):
        from . import describers

        return getattr(describers, name)
    if name == "PageReader":
        from .page_reader import PageReader

        return PageReader
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
