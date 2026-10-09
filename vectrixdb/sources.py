"""Feeds and pages a collection keeps up with: read again on a schedule, and only what changed written again.

    db = Vectrix("news")
    db.sources.add("https://example.com/feed.xml", every="6h")   # RSS, Atom or JSON Feed
    db.sources.add("https://example.com/pricing", every="1d")    # a page, or a PDF at an address
    report = db.sources.refresh()                                 # the sources that are due
    print(report)    # 2 sources: 12 added, 1 updated, 40 unchanged, 0 removed, 0 failed

A feed gives one document an entry, with its title, link, author and dates on
every chunk. ``Feed(address, articles=True)`` reads the article each entry
links to rather than the feed's summary of it. A podcast's episodes are
transcribed when the collection has an audio engine registered for their
files, and are their show notes otherwise. A page gives one document, its
main text, so a change to the menu around it is not a change.

Only what changed is written again. A feed or page that answers 304 to the
ETag and date it gave last time costs one request; an entry whose text is the
same is left alone; one that changed replaces its document in place, under the
same id, so nothing is indexed twice and lineage still walks back to the write.
An entry that drops out of a feed keeps its documents. A page that answers 404
or 410 is marked gone, and its chunks are removed only when the source was
added with ``delete_when_gone=True``.

Every fetch goes through :mod:`vectrixdb._fetch`: http and https only, never a
private, loopback or metadata address however a name resolves, every redirect
checked, size and time capped, robots.txt obeyed, a second at least between
two requests to one host, and a bot check refused rather than indexed. An
address with a password, a token or a signature written into it is refused
when it is added, because it would be kept where every operator can list it:
write ``${NAME}`` in its place and the value is read from the environment each
time the source is fetched, and never shown or stored.

A licensed news feed, an internal system or anything else is a :class:`Source`
of your own: subclass it, give it a ``kind``, and :func:`register_source` it,
or declare it under the ``vectrixdb.sources`` entry point, so a scheduled
refresh in another process can build it again from what was kept.

Two refreshes never work on one source at once, here or on another server: a
refresh holds a lease on each source while it reads it, and one that died lets
go when the lease runs out. Sources are kept beside the collection records when
the database has a store for them (``VECTRIXDB_COLLECTION_STORE``: Cosmos DB,
PostgreSQL, DynamoDB), so every server sees the same list, and in the
database's own file otherwise.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import re
import socket
import sqlite3
import time
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._web import redact_url, site_of, status_message
from .exceptions import ConfigurationError, DependencyError, ExtractionError

__all__ = [
    "AUDIO_MAX_BYTES",
    "DEFAULT_EVERY",
    "MAX_ITEMS",
    "MIN_EVERY",
    "Feed",
    "Item",
    "Page",
    "RefreshReport",
    "Source",
    "SourceContext",
    "SourceInfo",
    "SourceOutcome",
    "Sources",
    "every_text",
    "parse_every",
    "register_source",
]

log = logging.getLogger("vectrixdb.sources")


# ============================================================================
# SETTINGS: how often, how much, how long, and where it is kept
# ============================================================================
#
# How often a source is read when nobody says, and how often at the most; how
# many entries one refresh writes for one source; how long a refresh holds a
# source; how much a podcast episode may weigh; the kinds of record kept.

#: How often a source is read when nobody says: four times a day.
DEFAULT_EVERY = 6 * 3600.0
#: The most often a source may be read. A feed read every minute is a load on somebody else's server.
MIN_EVERY = 5 * 60.0
#: The least often: once a year. A source read less often than that is not being kept up with.
MAX_EVERY = 365 * 86400.0
#: Entries written for one source in one refresh, at the most. The rest wait
#: for the next refresh, so a feed of a thousand entries does not hold one up for an hour.
MAX_ITEMS = 50
#: Entries read from one feed, at the most. A feed is the latest few; one with more is an archive.
MAX_ENTRIES = 500
#: How long one refresh holds a source. A refresh that died lets go when this runs out.
LEASE_FOR = 30 * 60.0
#: The most one podcast episode's sound may weigh.
AUDIO_MAX_BYTES = 300 * 1024 * 1024
#: How long an episode may take to arrive, in seconds: a page has thirty.
AUDIO_TIMEOUT = 600.0

#: The kinds of record a collection's sources are kept as, in the records layer.
SOURCE = "source"
ITEM = "source_item"
LEASE = "source_lease"
#: The table in the database's own file, when there is no collection store.
TABLE = "vectrixdb_sources"

_FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/feed+json, application/rdf+xml;q=0.9, "
    "application/xml;q=0.8, text/xml;q=0.8, application/json;q=0.7, */*;q=0.5"
)
_PAGE_ACCEPT = "text/html, application/xhtml+xml;q=0.9, application/pdf;q=0.8, */*;q=0.5"
#: Query parameters that only say where a click came from: not part of what a link is.
_TRACKING = re.compile(
    r"^(?:utm_[a-z_]+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_hsenc|_hsmi)$", re.I
)


# ============================================================================
# HOW OFTEN
# ============================================================================
#
# INPUT   90, "90s", "30m", "6h", "1d", "1w", "1h30m" or a timedelta
# OUTPUT  seconds, five minutes at the least and a year at the most; and
#         seconds written back the short way
#
# Written the way a cron line or a person says it.

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PART = re.compile(r"(\d+(?:\.\d+)?)([smhdw])")


def parse_every(every: Union[str, int, float, timedelta]) -> float:
    """Seconds between two reads of a source, from ``"6h"``, ``"1d"``, ``"30m"``, ``"1h30m"``, a number or a timedelta."""
    if isinstance(every, timedelta):
        seconds = every.total_seconds()
    elif isinstance(every, (int, float)) and not isinstance(every, bool):
        seconds = float(every)
    elif isinstance(every, str):
        text = every.strip().lower().replace(" ", "")
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            seconds = float(text)
        else:
            parts = _PART.findall(text)
            if not parts or "".join(n + u for n, u in parts) != text:
                raise ConfigurationError(
                    f"every is a number of seconds or a length such as 30m, 6h, 1d or 1w, not {every!r}"
                )
            seconds = sum(float(n) * _UNITS[u] for n, u in parts)
    else:
        raise ConfigurationError(
            f"every is seconds, a length such as 6h, or a timedelta, not {every!r}"
        )
    if not math.isfinite(seconds) or seconds > MAX_EVERY:
        raise ConfigurationError(
            f"every is {every!r}: a source is read once a year at the least often. Say 52w or less."
        )
    if seconds < MIN_EVERY:
        raise ConfigurationError(
            f"every is {every_text(seconds)}: a source is read every five minutes at the most often, "
            "which is already often for somebody else's server. Say 5m or more."
        )
    return float(seconds)


def every_text(seconds: float) -> str:
    """Seconds the short way: ``21600`` is ``6h``, ``5400`` is ``1h30m``."""
    left = int(round(float(seconds)))
    out = []
    for unit in ("w", "d", "h", "m", "s"):
        size = _UNITS[unit]
        if left >= size:
            out.append(f"{left // size}{unit}")
            left %= size
    return "".join(out) or "0s"


# ============================================================================
# ADDRESSES: kept without secrets, fetched whole
# ============================================================================
#
# INPUT   an address as somebody gave it, with ${NAME} where a secret goes
# OUTPUT  the address as kept, or a refusal naming the parameter that holds a
#         secret; the address to fetch, the secrets read from the
#         environment; the address to show; a message with every secret value
#         taken out of it
#
# What is kept is listed to every operator and written on every chunk, so a
# secret is never in it. A value read from the environment is put into the
# source's own address and nowhere else: never into an address a feed names.

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _secrets_in(address: str) -> List[str]:
    """The parameters of ``address`` that carry a secret written out rather than as ``${NAME}``.

    Wherever :func:`redact_url` looks: the query, a path parameter such as
    ``;jsessionid=``, the fragment, and the query of a single-page app's
    route in it, ``#/callback?code=``.
    """
    parts = urlsplit(address)
    route, mark, tail = parts.fragment.rpartition("?")
    pieces = [parts.query, tail, route] if mark else [parts.query, parts.fragment]
    pairs = [pair for piece in pieces for pair in re.split(r"[&;]", piece)]
    pairs += re.findall(r";([^;/=?#]*=[^;/?#]*)", parts.path)
    named: List[str] = []
    for pair in pairs:
        if not pair or _PLACEHOLDER.fullmatch(pair):
            continue
        name, _, value = pair.partition("=")
        if _PLACEHOLDER.fullmatch(value):
            continue
        # As a parameter, and a piece with no name as the fragment it can be, a token on its own.
        probe = f"https://x.invalid/?{pair}" if "=" in pair else f"https://x.invalid/#{pair}"
        if redact_url(probe) != probe:
            named.append(f"{name}=" if "=" in pair else "a value")
    return named


def placeholders(address: str) -> List[str]:
    """The environment variables an address reads, in order."""
    return [m.group(1) for m in _PLACEHOLDER.finditer(str(address or ""))]


def _kept_address(address: Any, *, web: bool) -> str:
    """The address as it is kept, or ConfigurationError saying why it cannot be."""
    text = str(address or "").strip()
    if not text:
        raise ConfigurationError("a source needs an address")
    if len(text) > 2048:
        raise ConfigurationError("a source's address is 2,048 characters at the most")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ConfigurationError(f"{redact_url(text)} is not an address: {exc}") from None
    if web and (parts.scheme.lower() not in ("http", "https") or not parts.netloc):
        raise ConfigurationError(f"{redact_url(text)} is not an http or https address")
    if "@" in parts.netloc:
        raise ConfigurationError(
            f"{redact_url(text)} has a name or a password in it, which would be kept where every "
            "operator can list it. Write ${NAME} in its place, and set NAME in the environment."
        )
    secrets = _secrets_in(text)
    if secrets:
        raise ConfigurationError(
            f"{redact_url(text)} carries a secret in {', '.join(secrets)}, which would be kept where every "
            "operator can list it and written on every chunk. Write ${NAME} in its place, "
            f"{secrets[0]}${{FEED_TOKEN}} for instance, and set FEED_TOKEN in the environment: it is read "
            "when the source is fetched, and never shown or stored."
        )
    return text


def _expand(address: str, env: Optional[Mapping[str, str]] = None) -> str:
    """The address to fetch: every ``${NAME}`` replaced by its value from the environment."""
    found = os.environ if env is None else env

    def value(match: "re.Match[str]") -> str:
        name = match.group(1)
        said = found.get(name)
        if not said:
            raise ConfigurationError(
                f"{name} is not set, and this source's address reads it from the environment"
            )
        return str(said)

    return _PLACEHOLDER.sub(value, address)


def public(address: str) -> str:
    """An address as it may be shown: no credentials, and a parameter that is only ``${NAME}`` left out."""
    text = redact_url(str(address or ""))
    if "${" not in text:
        return text
    parts = urlsplit(text)

    def kept(piece: str) -> str:
        pairs = [p for p in re.split(r"[&;]", piece) if p and not _PLACEHOLDER.fullmatch(p)]
        pairs = [p for p in pairs if not _PLACEHOLDER.fullmatch(p.partition("=")[2])]
        return "&".join(pairs)

    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, kept(parts.query), kept(parts.fragment))
    )


def _scrub(text: Any, address: str, env: Optional[Mapping[str, str]] = None) -> str:
    """A message with every value this address reads from the environment taken back out of it."""
    said = str(text)
    found = os.environ if env is None else env
    for name in placeholders(address):
        value = found.get(name)
        if value and len(value) >= 4:
            said = said.replace(value, "${" + name + "}")
    return said


def _doc_id_of(link: str) -> str:
    """A link as a document's id: without credentials, without click tracking, without a fragment."""
    shown = redact_url(link)
    try:
        parts = urlsplit(shown)
    except ValueError:
        return shown
    if not parts.scheme:
        return shown
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), "")
    )


def _key_part(key: str) -> str:
    """An item's key as the end of a document id: as it is when it is short and holds nothing secret, else a digest of it."""
    if len(key) <= 200 and redact_url(key) == key:
        return key
    return "k-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _version(text: str) -> str:
    """The hash ``add_document`` stamps as ``_vx_doc_version``."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _now_text(at: Optional[float] = None) -> str:
    moment = time.time() if at is None else float(at)
    try:
        when = datetime.fromtimestamp(moment, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        # Past what a date holds: kept by an older version, or by a source of somebody else's.
        when = (datetime.max if moment > 0 else datetime.min).replace(tzinfo=timezone.utc)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


# ============================================================================
# WHAT A SOURCE GIVES, AND WHAT IT IS GIVEN
# ============================================================================
#
# INPUT   a source, reading
# OUTPUT  items, each a document to be, with what says whether it changed;
#         the context a source reads with: the guarded fetch, what it kept
#         last time, which items are already indexed, and the collection's
#         own readers
#
# The plug-in point: a source of your own needs nothing else.


@dataclass
class Item:
    """One document a source gives: a feed's entry, a page, an article from a licensed feed.

    ``key`` is the item's id within its source, the same every time it is
    read. Give its text as ``text`` (Markdown), ``document`` (a
    :class:`~vectrixdb.ingest.LoadedDocument`), or ``read``, a function
    called only when the item is new or changed: that is how an item that
    costs something to read, an article to fetch, an episode to transcribe,
    is not read again for nothing. ``fingerprint`` is what says it changed,
    anything that does when it does, a revision number, an updated date; left
    out, the item is read every time and its text compared.

    ``gone`` says it no longer exists, a page that answers 410: its
    documents stay unless its source deletes what is gone.
    """

    key: str
    text: Optional[str] = None
    document: Any = None
    read: Optional[Callable[[], Any]] = None
    fingerprint: Optional[str] = None
    title: Optional[str] = None
    link: Optional[str] = None
    author: Optional[str] = None
    published: Optional[str] = None
    updated: Optional[str] = None
    #: More metadata for every chunk of its document.
    metadata: Dict[str, Any] = field(default_factory=dict)
    #: Its document's id. Left out, its link, else its source's address and its key.
    doc_id: Optional[str] = None
    gone: bool = False


class SourceContext:
    """What a source reads with: the guarded fetch, what it kept last time, and the collection's readers.

    ``address`` is the source's own address to fetch, any ``${NAME}`` in it
    read from the environment, and ``shown`` is the same address as it may
    be shown. ``state`` is a small dict kept between refreshes, an ETag or
    a cursor; it is saved when the source has been read without an error,
    and an ETag and a date only once every item it gave has been written.
    """

    def __init__(
        self,
        *,
        address: str,
        shown: str,
        fetcher: Any,
        state: Dict[str, Any],
        known: Mapping[str, Mapping[str, Any]],
        extractors: Any,
        collection: str,
        scrub: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.address = address
        self.shown = shown
        self.state = state
        self.collection = collection
        self._fetcher = fetcher
        self._known = known
        self._extractors = extractors
        self._scrub = scrub
        self.notes: List[str] = []

    def get(
        self,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        *,
        max_bytes: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """What ``url`` answers, through every check of :class:`vectrixdb._fetch.Fetcher`, whatever the status.

        ``max_bytes`` and ``timeout`` change the caps for this one request.
        """
        if timeout is None:
            return self._fetcher.get(url, headers, max_bytes=max_bytes)
        return self._fetcher.get(url, headers, max_bytes=max_bytes, timeout=timeout)

    def known(self, key: str) -> Optional[str]:
        """The fingerprint recorded when the item with this key was last written, or None."""
        held = self._known.get(key)
        return None if held is None else held.get("fingerprint")

    def has(self, key: str) -> bool:
        """Whether an item with this key has been written before."""
        return key in self._known

    def reads(self, name: str) -> bool:
        """Whether the collection has an extractor of its own for a file of this name: an audio engine for ``.mp3``."""
        from .extract import resolve

        return resolve(self._extractors).get(name) is not None

    def read_bytes(self, data: bytes, name: str, *, source: str) -> Any:
        """Bytes read into a document the way the collection reads a file of this name."""
        from .ingest import load_bytes

        return load_bytes(bytes(data), name, extractors=self._extractors, source=source)

    def note(self, text: str) -> None:
        """A line for the refresh's report: something worth knowing that is not a failure."""
        self.notes.append(str(text))

    def scrub(self, text: str) -> str:
        """``text`` with every value the source's address reads from the environment taken back out, as ``${NAME}``."""
        return self._scrub(str(text)) if self._scrub is not None else str(text)


# ============================================================================
# A SOURCE, AND THE KINDS THERE ARE
# ============================================================================
#
# INPUT   an address and settings; a kind's name and its class
# OUTPUT  a source, kept as its kind, its address and its settings and built
#         again from them; a kind registered, in code or as an entry point
#
# A refresh in another process builds every source again from what was kept,
# so a kind it cannot find is a failure it names, not a silent skip.


class Source:
    """What a collection keeps up with. Subclass it for a source the built-ins do not read.

    A subclass names its ``kind``, reads in :meth:`read`, and says in
    :meth:`settings` what besides its address builds it again; the default
    :meth:`from_settings` passes them back as keyword arguments. Keep
    secrets out of both: a licensed feed's key belongs in the environment,
    read when :meth:`read` runs, or in the address as ``${NAME}``, which
    ``context.address`` has filled in and ``self.address`` does not. Then
    :func:`register_source` it::

        class Wire(Source):
            kind = "wire"

            def read(self, context):
                for story in wire_client(os.environ["WIRE_KEY"]).latest(desk=self.address):
                    yield Item(key=story.id, fingerprint=story.revision, title=story.headline,
                               link=story.url, published=story.time, read=lambda s=story: s.body_markdown())

        register_source("wire", Wire)
        db.sources.add(Wire("energy"), every="1h")
    """

    #: The name it is kept and registered under.
    kind: ClassVar[str] = ""
    #: Whether its items that are gone take their documents with them.
    delete_when_gone: bool = False

    def __init__(self, address: str) -> None:
        self.address = _kept_address(address, web=False)

    def settings(self) -> Dict[str, Any]:
        """What, besides the address, builds this source again: JSON, and never a secret."""
        return {}

    @classmethod
    def from_settings(cls, address: str, settings: Mapping[str, Any]) -> "Source":
        """The source again, from its address and :meth:`settings`."""
        return cls(address, **dict(settings))

    def read(self, context: SourceContext) -> Optional[Iterable[Item]]:
        """The items as they stand, or None when nothing changed since last time, a 304."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}({public(self.address)!r})"


def register_source(kind: str, cls: Type[Source]) -> Callable[[], None]:
    """Make a kind of source of your own known here; returns a function that forgets it.

    A package does the same with an entry point in the ``vectrixdb.sources``
    group, which is what a scheduled refresh in another process finds.
    """
    from . import plugins

    if not isinstance(cls, type) or not issubclass(cls, Source):
        raise TypeError("a source kind is a subclass of vectrixdb.sources.Source")
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", str(kind or "")):
        raise ConfigurationError(
            f"a source kind is lower-case letters, digits, '.', '-' and '_', not {kind!r}"
        )
    if kind in _BUILT_IN:
        raise ConfigurationError(f"{kind!r} is a built-in kind of source")
    if cls.kind and cls.kind != kind:
        raise ConfigurationError(
            f"{cls.__name__}.kind is {cls.kind!r}, so it is registered as that"
        )
    cls.kind = kind
    return plugins.register("sources", kind, cls)


def _class_for(kind: str) -> Type[Source]:
    """The class a kind of source is built with, or ConfigurationError naming what is missing."""
    if kind in _BUILT_IN:
        return _BUILT_IN[kind]
    from . import plugins

    try:
        found = plugins.load("sources", kind)
    except plugins.PluginNotFound as exc:
        raise ConfigurationError(
            f"no kind of source called {kind!r} is known here ({exc}). Install the package that "
            "provides it, or register_source() it, in every process that refreshes this collection."
        ) from None
    if not (isinstance(found, type) and issubclass(found, Source)):
        raise ConfigurationError(f"the {kind!r} source plugin is not a vectrixdb.sources.Source")
    return found


# ============================================================================
# FEEDS: RSS, ATOM, RSS 1.0 AND JSON FEED
# ============================================================================
#
# INPUT   a feed's bytes
# OUTPUT  its title and its entries, each as one shape whatever the format:
#         id, link, title, author, dates, its HTML and its enclosures; an
#         item an entry, its document read only when it is new or changed
#
# feedparser reads the XML formats and is handed bytes only, never an
# address or a file name, so it never fetches anything itself. JSON Feed is
# JSON, and is read here.


def _iso(value: Any) -> Optional[str]:
    """A date as ISO 8601 in UTC, from a struct_time or an RFC 3339 string; None when it is neither."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (time.struct_time, tuple)):
            year, month, day, hour, minute, second = (int(v) for v in tuple(value)[:6])
            when = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        else:
            text = str(value).strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            when = datetime.fromisoformat(text)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return None


def _json_feed(data: bytes, shown: str) -> Tuple[str, List[Dict[str, Any]]]:
    try:
        feed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExtractionError(f"{shown} is not a feed: {exc}") from None
    if not isinstance(feed, dict) or not str(feed.get("version") or "").startswith(
        "https://jsonfeed.org/version/"
    ):
        raise ExtractionError(f"{shown} answered JSON that is not a JSON Feed")
    entries: List[Dict[str, Any]] = []
    for item in feed.get("items") or []:
        if not isinstance(item, dict):
            continue
        authors = item.get("authors") or ([item["author"]] if item.get("author") else [])
        names = [str(a.get("name")) for a in authors if isinstance(a, dict) and a.get("name")]
        html = item.get("content_html")
        if not html and item.get("content_text"):
            html = _escape(str(item["content_text"]))
        if not html and item.get("summary"):
            html = _escape(str(item["summary"]))
        entries.append(
            {
                "id": str(item.get("id") or "") or None,
                "link": item.get("url") or item.get("external_url"),
                "title": item.get("title"),
                "author": ", ".join(names) or None,
                "published": _iso(item.get("date_published")),
                "updated": _iso(item.get("date_modified")),
                "html": html or "",
                "enclosures": [
                    {
                        "href": a.get("url"),
                        "type": a.get("mime_type"),
                        "length": a.get("size_in_bytes"),
                    }
                    for a in item.get("attachments") or []
                    if isinstance(a, dict) and a.get("url")
                ],
                "extra": {},
            }
        )
    return str(feed.get("title") or ""), entries


def _escape(text: str) -> str:
    import html

    return "<p>" + html.escape(text).replace("\n\n", "</p><p>") + "</p>"


def _xml_feed(reply: Any, base: str, shown: str) -> Tuple[str, List[Dict[str, Any]]]:
    try:
        import feedparser
    except ImportError:
        raise DependencyError("feedparser", "feeds") from None
    parsed = feedparser.parse(
        io.BytesIO(reply.body),
        response_headers={
            "content-type": reply.header("content-type") or "application/xml",
            "content-location": base,
        },
    )
    if not parsed.get("version") and not parsed.entries:
        why = parsed.get("bozo_exception")
        raise ExtractionError(
            f"{shown} is not a feed: RSS, Atom or JSON Feed was expected"
            + (f" ({type(why).__name__}: {why})" if why else "")
        )
    entries: List[Dict[str, Any]] = []
    for e in parsed.entries:
        content = e.get("content") or []
        html = (content[0].get("value") if content else None) or e.get("summary") or ""
        enclosures = [
            {"href": x.get("href"), "type": x.get("type"), "length": x.get("length")}
            for x in (e.get("enclosures") or [])
            if x.get("href")
        ]
        extra: Dict[str, Any] = {}
        if e.get("yt_videoid"):
            extra["video_id"] = str(e.get("yt_videoid"))
        entries.append(
            {
                "id": e.get("id") or None,
                "link": e.get("link") or None,
                "title": e.get("title") or None,
                "author": e.get("author") or None,
                "published": _iso(e.get("published_parsed")),
                # Asked for by name, feedparser hands back the published date
                # when an entry has no updated one, with a warning.
                "updated": _iso(e["updated_parsed"]) if "updated_parsed" in e else None,
                "html": html,
                "enclosures": enclosures,
                "extra": extra,
            }
        )
    return str(parsed.feed.get("title") or ""), entries


def _audio_of(entry: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """An entry's sound, a podcast episode's: its first audio enclosure."""
    from .extract.engines import AUDIO_SUFFIXES

    for enclosure in entry.get("enclosures") or []:
        href = str(enclosure.get("href") or "")
        kind = str(enclosure.get("type") or "").lower()
        suffix = PurePosixPath(urlsplit(href).path).suffix.lower()
        if kind.startswith("audio/") or suffix in AUDIO_SUFFIXES:
            return {
                "href": href,
                "type": kind,
                "suffix": suffix if suffix in AUDIO_SUFFIXES else "",
            }
    return None


def _html_text(html: str) -> str:
    """An entry's HTML as the text the page reader makes of it: headings, lists and tables kept."""
    if not html.strip():
        return ""
    if "<" not in html and "&" not in html:
        return html.strip()
    from .ingest import load_bytes

    return load_bytes(html.encode("utf-8"), "entry.html", kind="html").text.strip()


def _remember(state: Dict[str, Any], reply: Any) -> None:
    """The validators a reply gave, for the next conditional request."""
    for header, key in (("etag", "etag"), ("last-modified", "modified")):
        value = reply.header(header)
        if value:
            state[key] = value[:512]
        else:
            state.pop(key, None)


def _conditional(state: Mapping[str, Any], accept: str) -> Dict[str, str]:
    headers = {"Accept": accept}
    if state.get("etag"):
        headers["If-None-Match"] = str(state["etag"])
    if state.get("modified"):
        headers["If-Modified-Since"] = str(state["modified"])
    return headers


def _name_for(reply: Any, fallback: str = "page", shown: Optional[str] = None) -> str:
    """A file name for what an address answered, its suffix saying who reads it, as load_url names one.

    ``shown`` names it in place of the address that answered: the source's
    own address as it may be shown, so a segment it reads from the
    environment never becomes the name every citation carries.
    """
    parts = urlsplit(shown or reply.url)
    name = PurePosixPath(parts.path).name
    if _PLACEHOLDER.search(name):
        name = ""
    kind = reply.header("content-type").split(";", 1)[0].strip().lower()
    if PurePosixPath(name).suffix:
        return name
    stem = name or parts.hostname or fallback
    if not kind or "html" in kind:
        return stem + ".html"
    if kind in ("text/markdown", "text/x-markdown"):
        return stem + ".md"
    guessed = mimetypes.guess_extension(kind) or ".txt"
    return stem + (".txt" if guessed in (".xml", ".json", ".bin") else guessed)


def _ok(status: int) -> bool:
    return 200 <= int(status) < 300


def _failure(reply: Any, shown: str, more: str = "") -> Exception:
    """What an answer that is not a success means for a refresh.

    A 429 or a 503 is the site asking for time: the source is put off to the
    next refresh, or to when the site said, and not counted as failed. Any
    other answer fails it, with the status.
    """
    from ._fetch import Deferred

    said = status_message(shown, reply.status, reply.headers)
    if reply.status in (429, 503):
        wait = getattr(reply, "retry_after", None)
        return Deferred(
            f"{said}; the next refresh tries again", until=time.time() + wait if wait else None
        )
    return ExtractionError(said + more, status=reply.status)


class Feed(Source):
    """An RSS 2.0, RSS 1.0, Atom or JSON Feed: one document an entry.

    ``articles=True`` reads the article an entry links to, through the same
    checks as the feed, and indexes that rather than the feed's summary; a
    site that refuses it, by robots.txt or a bot check, gets the feed's own
    text and a note in the report. ``transcribe`` turns a podcast's episodes
    into transcripts when the collection has an audio engine registered for
    their files; without one, or with ``transcribe=False``, an episode is its
    show notes. Nothing is downloaded to find out.
    """

    kind = "feed"

    def __init__(
        self,
        address: str,
        *,
        articles: bool = False,
        transcribe: bool = True,
        max_audio_bytes: int = AUDIO_MAX_BYTES,
    ) -> None:
        self.address = _kept_address(address, web=True)
        self.articles = bool(articles)
        self.transcribe = bool(transcribe)
        self.max_audio_bytes = int(max_audio_bytes)

    def settings(self) -> Dict[str, Any]:
        said: Dict[str, Any] = {"articles": self.articles, "transcribe": self.transcribe}
        if self.max_audio_bytes != AUDIO_MAX_BYTES:
            said["max_audio_bytes"] = self.max_audio_bytes
        return said

    def read(self, context: SourceContext) -> Optional[Iterable[Item]]:
        reply = context.get(context.address, _conditional(context.state, _FEED_ACCEPT))
        if reply.status == 304:
            return None
        if not _ok(reply.status):
            gone = (
                ". If the feed has moved, remove this source and add the new address"
                if reply.status in (404, 410)
                else ""
            )
            raise _failure(reply, context.shown, gone)
        kind = reply.header("content-type").split(";", 1)[0].strip().lower()
        # Relative links are read against the address as it may be shown when
        # it reads the environment: against the one fetched, they would carry
        # the value into every entry's link and id.
        base = context.shown if placeholders(self.address) else redact_url(reply.url)
        if "json" in kind or reply.body.lstrip()[:1] == b"{":
            title, entries = _json_feed(reply.body, context.shown)
        else:
            title, entries = _xml_feed(reply, base, context.shown)
        _remember(context.state, reply)
        return self._items(context, title, entries[:MAX_ENTRIES])

    def _items(
        self, context: SourceContext, title: str, entries: Sequence[Dict[str, Any]]
    ) -> Iterator[Item]:
        for entry in entries:
            link = str(entry.get("link") or "") or None
            key = str(
                entry.get("id") or link or _digest(entry.get("title"), entry.get("published"))
            )
            audio = _audio_of(entry)
            audio_name = None
            if audio is not None:
                stem = PurePosixPath(urlsplit(audio["href"]).path).stem or "episode"
                audio_name = stem + (audio["suffix"] or ".mp3")
            transcribing = bool(
                audio is not None and self.transcribe and audio_name and context.reads(audio_name)
            )
            fingerprint = _digest(
                entry.get("title"),
                link and redact_url(link),
                entry.get("published"),
                entry.get("updated"),
                entry.get("html"),
                audio and redact_url(audio["href"]),
                self.articles,
                transcribing,
            )
            metadata: Dict[str, Any] = dict(entry.get("extra") or {})
            if title:
                metadata["feed"] = title
            if audio is not None:
                metadata["audio"] = redact_url(audio["href"])
            yield Item(
                key=key[:2048],
                fingerprint=fingerprint,
                title=entry.get("title"),
                link=redact_url(link) if link else None,
                author=entry.get("author"),
                published=entry.get("published"),
                updated=entry.get("updated"),
                metadata=metadata,
                read=_bind(self._document, context, entry, link, audio, audio_name, transcribing),
            )

    def _document(
        self,
        context: SourceContext,
        entry: Mapping[str, Any],
        link: Optional[str],
        audio: Optional[Mapping[str, Any]],
        audio_name: Optional[str],
        transcribing: bool,
    ) -> Any:
        from .ingest import markdown_document

        heading = str(entry.get("title") or "").strip()
        notes = _html_text(str(entry.get("html") or ""))
        shown_link = redact_url(link) if link else context.shown
        if transcribing and audio is not None and audio_name:
            reply = context.get(
                audio["href"],
                {"Accept": audio.get("type") or "audio/*"},
                max_bytes=self.max_audio_bytes,
                timeout=AUDIO_TIMEOUT,
            )
            if not _ok(reply.status):
                raise _failure(reply, redact_url(audio["href"]))
            doc = context.read_bytes(reply.body, audio_name, source=redact_url(audio["href"]))
            doc.metadata["filename"] = heading or audio_name
            if notes:
                doc.metadata["show_notes"] = notes[:4000]
            return doc
        if self.articles and link:
            from ._fetch import Refused

            try:
                reply = context.get(link, {"Accept": _PAGE_ACCEPT})
            except Refused as exc:
                context.note(f"{shown_link}: {exc} The feed's own text was indexed instead.")
            else:
                if _ok(reply.status):
                    doc = context.read_bytes(reply.body, _name_for(reply), source=shown_link)
                    if doc.text.strip():
                        return doc
                    context.note(
                        f"{shown_link} had no text in it; the feed's own text was indexed instead."
                    )
                else:
                    context.note(
                        f"{shown_link} answered {reply.status}; the feed's own text was indexed instead."
                    )
        # The title as the first line, so it is searched with the rest, and
        # not as a heading, which a citation would repeat after the title.
        text = f"{heading}\n\n{notes}".strip() if heading else notes
        metadata = {"source": shown_link}
        if heading:
            metadata["filename"] = heading
        return markdown_document(text, metadata=metadata)


def _bind(fn: Callable[..., Any], *args: Any) -> Callable[[], Any]:
    return lambda: fn(*args)


# ============================================================================
# PAGES
# ============================================================================
#
# INPUT   a page's address
# OUTPUT  its main text as one document, or nothing when it answers 304, or
#         a gone item when it answers 404 or 410 after it was indexed
#
# Read by the reader the collection reads that kind of file with, so a page,
# a PDF at an address, a Markdown file, all come in as they would from disk.


class Page(Source):
    """One address, read again on a schedule: a web page's main text, or a file such as a PDF.

    It is written again only when its text changed. A page that answers 404
    or 410 after it was indexed is reported gone and its chunks kept;
    ``delete_when_gone=True`` removes them instead.
    """

    kind = "page"

    def __init__(self, address: str, *, delete_when_gone: bool = False) -> None:
        self.address = _kept_address(address, web=True)
        self.delete_when_gone = bool(delete_when_gone)

    def settings(self) -> Dict[str, Any]:
        return {"delete_when_gone": True} if self.delete_when_gone else {}

    def read(self, context: SourceContext) -> Optional[Iterable[Item]]:
        reply = context.get(context.address, _conditional(context.state, _PAGE_ACCEPT))
        if reply.status == 304:
            return None
        if reply.status in (404, 410) and context.has(self.address):
            return [Item(key=self.address, link=context.shown, gone=True)]
        if not _ok(reply.status):
            raise _failure(reply, context.shown)
        _remember(context.state, reply)
        named = _name_for(reply, shown=context.shown if reply.url == context.address else None)
        doc = context.read_bytes(reply.body, named, source=context.shown)
        return [
            Item(
                key=self.address,
                document=doc,
                title=doc.metadata.get("title"),
                link=context.shown,
            )
        ]


_BUILT_IN: Dict[str, Type[Source]] = {"feed": Feed, "page": Page}


def _is_feed(reply: Any) -> bool:
    """Whether what an address answered is a feed, by its type, else by how it starts."""
    from ._fetch import is_feed_type

    kind = reply.header("content-type")
    if is_feed_type(kind):
        return True
    head = reply.body.lstrip()[:4096]
    if head[:1] == b"{":
        return b"jsonfeed.org/version/" in head
    return re.search(rb"<(?:rss|feed|rdf:RDF)[\s>]", head) is not None


# ============================================================================
# WHAT A REFRESH SAYS
# ============================================================================
#
# INPUT   what happened to each source and each item
# OUTPUT  per source: added, updated, unchanged, removed, gone, failed with
#         reasons, waiting for the next refresh; for the refresh, the same
#         summed, as a dict and as a line
#
# Every reason has had the secrets read from the environment taken out.


@dataclass
class SourceOutcome:
    """What one refresh did with one source."""

    id: str
    address: str
    kind: str
    #: refreshed, not modified, failed, deferred, busy or not due.
    status: str = "refreshed"
    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    unchanged: int = 0
    removed: List[str] = field(default_factory=list)
    gone: List[str] = field(default_factory=list)
    #: Items that could not be read or written, each {"item", "reason"}; tried again next time.
    failed: List[Dict[str, str]] = field(default_factory=list)
    #: Items left for the next refresh, past this one's limit.
    waiting: int = 0
    #: Why the source failed or was put off.
    reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "address": self.address,
            "kind": self.kind,
            "status": self.status,
            "added": list(self.added),
            "updated": list(self.updated),
            "unchanged": self.unchanged,
            "removed": list(self.removed),
            "gone": list(self.gone),
            "failed": [dict(f) for f in self.failed],
            "waiting": self.waiting,
            "reason": self.reason,
            "notes": list(self.notes),
        }


@dataclass
class RefreshReport:
    """What one refresh did: each source, and the totals."""

    sources: List[SourceOutcome] = field(default_factory=list)
    #: Sources left alone because they are not due yet.
    not_due: int = 0

    @property
    def added(self) -> List[str]:
        return [d for s in self.sources for d in s.added]

    @property
    def updated(self) -> List[str]:
        return [d for s in self.sources for d in s.updated]

    @property
    def unchanged(self) -> int:
        return sum(s.unchanged for s in self.sources)

    @property
    def removed(self) -> List[str]:
        return [d for s in self.sources for d in s.removed]

    @property
    def gone(self) -> List[str]:
        return [d for s in self.sources for d in s.gone]

    @property
    def waiting(self) -> int:
        return sum(s.waiting for s in self.sources)

    @property
    def failed(self) -> List[Dict[str, str]]:
        """Every failure, a source's or an item's, each with the source's address and the reason."""
        out: List[Dict[str, str]] = []
        for s in self.sources:
            if s.status == "failed":
                out.append({"source": s.address, "reason": s.reason or "failed"})
            out.extend({"source": s.address, **f} for f in s.failed)
        return out

    @property
    def deferred(self) -> List[Dict[str, str]]:
        """Sources put off to the next refresh, each with the reason: a site that asked for time, robots.txt unreadable."""
        return [
            {"source": s.address, "reason": s.reason or ""}
            for s in self.sources
            if s.status == "deferred"
        ]

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not self.failed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": len(self.added),
            "updated": len(self.updated),
            "unchanged": self.unchanged,
            "removed": len(self.removed),
            "gone": len(self.gone),
            "failed": self.failed,
            "deferred": self.deferred,
            "waiting": self.waiting,
            "not_due": self.not_due,
            "sources": [s.to_dict() for s in self.sources],
        }

    def __str__(self) -> str:
        if not self.sources and self.not_due:
            return (
                f"nothing due: {self.not_due} source{'s' if self.not_due != 1 else ''} not due yet"
            )
        line = (
            f"{len(self.sources)} source{'s' if len(self.sources) != 1 else ''}: "
            f"{len(self.added)} added, {len(self.updated)} updated, {self.unchanged} unchanged, "
            f"{len(self.removed)} removed, {len(self.failed)} failed"
        )
        if self.deferred:
            line += f", {len(self.deferred)} put off"
        if self.waiting:
            line += f", {self.waiting} waiting for the next refresh"
        if self.not_due:
            line += f", {self.not_due} not due yet"
        return line


@dataclass
class SourceInfo:
    """One source as it is kept: never a secret."""

    id: str
    collection: str
    kind: str
    address: str
    every: float
    settings: Dict[str, Any] = field(default_factory=dict)
    added_at: Optional[str] = None
    added_by: Optional[str] = None
    last_refresh: Optional[str] = None
    next_due: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    #: Documents it has written that are still there.
    documents: int = 0

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> "SourceInfo":
        due = data.get("next_due")
        return cls(
            id=str(data.get("id")),
            collection=str(data.get("collection")),
            kind=str(data.get("type")),
            address=public(str(data.get("address") or "")),
            every=float(data.get("every") or DEFAULT_EVERY),
            settings=dict(data.get("settings") or {}),
            added_at=data.get("added_at"),
            added_by=data.get("added_by"),
            last_refresh=data.get("last_refresh"),
            next_due=_now_text(float(due)) if due is not None else None,
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            documents=int(data.get("documents") or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "collection": self.collection,
            "kind": self.kind,
            "address": self.address,
            "every": every_text(self.every),
            "every_seconds": self.every,
            "settings": dict(self.settings),
            "added_at": self.added_at,
            "added_by": self.added_by,
            "last_refresh": self.last_refresh,
            "next_due": self.next_due,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "documents": self.documents,
        }


# ============================================================================
# WHERE DOCUMENTS ARE WRITTEN, AND WHERE SOURCES ARE KEPT
# ============================================================================
#
# INPUT   a collection opened in Python, or a server's own writer; a database
# OUTPUT  a document written in place of its last version, or deleted; the
#         records store a collection's sources live in
#
# The library writes through add_document, the server through its own route's
# code: either way the same chunks, citations and lineage.


class _LibraryWriter:
    """Writes through a :class:`~vectrixdb.easy.Vectrix`: delete_document, then add_document."""

    def __init__(self, db: Any) -> None:
        self._db = db

    @property
    def extractors(self) -> Any:
        return getattr(self._db, "_extractors", None)

    def batch(self) -> Any:
        defer = getattr(self._db, "deferred_saves", None)
        return defer() if callable(defer) else contextlib.nullcontext()

    def write(self, doc: Any, doc_id: str, metadata: Dict[str, Any], version: str) -> int:
        # Gone first, so a version cut into fewer chunks leaves none of the last one behind.
        self._db.delete_document(doc_id)
        return int(
            self._db.add_document(doc, doc_id=doc_id, metadata=metadata, source_version=version)
            or 0
        )

    def delete(self, doc_id: str) -> int:
        return int(self._db.delete_document(doc_id) or 0)


def local_store(path: Any) -> Any:
    """A database's own sources table: in its ``_vectrixdb.db``, or in memory for a database with no path."""
    from .signin.records import SqlRecords

    if not path:
        return SqlRecords(
            sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None),
            table=TABLE,
            where="SQLite in memory",
        )
    file = Path(path) / "_vectrixdb.db"
    file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(file), check_same_thread=False, timeout=30, isolation_level=None
    )
    return SqlRecords(connection, table=TABLE, where=f"SQLite file {file}", path=file)


def _key(collection: str, sid: str) -> str:
    return f"{collection}:{sid}"


def _item_key(collection: str, sid: str, key: str) -> str:
    return f"{collection}:{sid}:{hashlib.sha256(key.encode()).hexdigest()[:24]}"


def _doc_index(collection: str, doc_id: str) -> str:
    return f"{collection}:{hashlib.sha256(doc_id.encode()).hexdigest()[:24]}"


def forget_collection(records: Any, collection: str) -> int:
    """Every source of a collection forgotten, with what each had written and any lease. How many sources went."""
    gone = 0
    for record in records.query(SOURCE, ix1=collection):
        sid = str(record.data.get("id") or "")
        for item in records.query(ITEM, ix1=_key(collection, sid)):
            records.delete(ITEM, item.key)
        records.delete(LEASE, _key(collection, sid))
        if records.delete(SOURCE, record.key):
            gone += 1
    return gone


def kept_sources(records: Any, collection: str) -> List[Dict[str, Any]]:
    """What builds a collection's sources again, without what they had written: what clear() carries over."""
    return [
        {k: v for k, v in record.data.items() if k in _DEFINITION}
        for record in records.query(SOURCE, ix1=collection)
    ]


def restore_sources(records: Any, collection: str, kept: Sequence[Mapping[str, Any]]) -> None:
    """Sources put back as :func:`kept_sources` had them, due at once and with nothing written yet."""
    from .signin.records import Record

    for data in kept:
        sid = str(data.get("id") or "")
        if sid:
            records.put(Record(SOURCE, _key(collection, sid), dict(data), ix1=collection))


#: What a kept source is, as distinct from what its refreshes found.
_DEFINITION = ("id", "collection", "type", "address", "settings", "every", "added_at", "added_by")

# One fetcher for each set of host settings, for as long as the process runs:
# robots.txt is read once a day a site, and the pace between two requests to a
# site, and a site's Retry-After, hold across refreshes however often a
# scheduler or a person calls one.
_FETCHERS: Dict[Tuple[str, str], Any] = {}
_FETCHERS_LOCK = threading.Lock()


def _shared_fetcher(env: Optional[Mapping[str, str]] = None) -> Any:
    """The process's fetcher for the hosts ``VECTRIXDB_SOURCES_HOSTS`` and ``VECTRIXDB_SOURCES_INTERNAL_HOSTS`` name."""
    from ._fetch import Fetcher

    found = os.environ if env is None else env
    key = (
        str(found.get("VECTRIXDB_SOURCES_HOSTS") or ""),
        str(found.get("VECTRIXDB_SOURCES_INTERNAL_HOSTS") or ""),
    )
    with _FETCHERS_LOCK:
        fetcher = _FETCHERS.get(key)
        if fetcher is None:
            fetcher = _FETCHERS[key] = Fetcher.from_environment(env)
        return fetcher


# ============================================================================
# THE SOURCES OF ONE COLLECTION
# ============================================================================
#
# INPUT   a source to add, list, remove or refresh
# OUTPUT  it kept; the list; it forgotten, with its documents when asked;
#         every source that is due read, through the guarded fetcher, with
#         only what changed written, and a report of it all
#
# Each source is refreshed under a lease, so a cron job and a request, or two
# servers, never read one source at once.


class _Held:
    """A refresh's lease on one source, made longer while the refresh is still at work on it."""

    def __init__(self, records: Any, lease: Any) -> None:
        self._records = records
        self.lease = lease
        self.lost = False

    def keep(self) -> bool:
        """Whether the lease is still this refresh's: made longer once a third of it has gone."""
        from .signin.records import Record

        if self.lost:
            return False
        if self.lease.version is None or self.lease.expires is None:
            return True
        if float(self.lease.expires) - time.time() > LEASE_FOR * 2 / 3:
            return True
        longer = Record(
            LEASE,
            self.lease.key,
            dict(self.lease.data),
            expires=time.time() + LEASE_FOR,
            version=self.lease.version,
        )
        try:
            kept = bool(self._records.replace(longer))
        except Exception as exc:  # the store's own error: stop here rather than risk two writers
            log.warning("the lease on %s could not be made longer: %s", self.lease.key, exc)
            kept = False
        if kept:
            self.lease = longer
        else:
            self.lost = True
        return kept


class Sources:
    """The feeds and pages one collection keeps up with. ``db.sources`` on a :class:`~vectrixdb.easy.Vectrix`.

    ``records`` is where they are kept (see :func:`Sources.of`), ``writer``
    what writes their documents. ``fetcher`` is a
    :class:`vectrixdb._fetch.Fetcher`, made from the environment for each
    refresh when left out: ``VECTRIXDB_SOURCES_HOSTS`` limits the hosts,
    ``VECTRIXDB_SOURCES_INTERNAL_HOSTS`` names intranet hosts allowed to be
    private, and ``HTTPS_PROXY`` with ``NO_PROXY`` are read as everywhere.
    """

    def __init__(
        self,
        collection: str,
        records: Any,
        writer: Any,
        *,
        fetcher: Any = None,
        env: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.collection = str(collection)
        self._records = records
        self._writer = writer
        self.fetcher = fetcher
        self._env = env
        self._clock = clock

    @classmethod
    def of(cls, db: Any, **given: Any) -> "Sources":
        """The sources of a :class:`~vectrixdb.easy.Vectrix`: kept where its database keeps them, written with add_document."""
        database = getattr(db, "_db", None)
        records = given.pop("records", None) or getattr(database, "sources_store", None)
        if records is None:
            raise ConfigurationError(
                f"collection {db.name!r} is on a database that keeps no sources: open it with a path, "
                "or pass records= to Sources.of()"
            )
        return cls(db.name, records, _LibraryWriter(db), **given)

    # ------------------------------------------------------------- adding ---

    def add(
        self,
        source: Union[str, Source],
        every: Union[str, int, float, timedelta] = DEFAULT_EVERY,
        *,
        kind: Optional[str] = None,
        by: Optional[str] = None,
        **options: Any,
    ) -> SourceInfo:
        """Keep this collection up with ``source``: an address, or a :class:`Feed`, :class:`Page` or a Source of your own.

        An address is fetched once to tell a feed from a page, unless
        ``kind`` says which; ``options`` go to that class, ``articles=True``
        to a feed, ``delete_when_gone=True`` to a page. Adding an address
        that is already here changes how often it is read and its options,
        and keeps what it has written. Nothing is written until
        :meth:`refresh`.
        """
        seconds = parse_every(every)
        if isinstance(source, Source):
            if options or kind:
                raise ConfigurationError(
                    "give options to the source itself, Feed(..., articles=True)"
                )
            built = source
        else:
            built = self._build(str(source), kind, options)
        cls = _class_for(built.kind)
        if type(built) is not cls:
            raise ConfigurationError(
                f"{type(built).__name__} says it is a {built.kind!r} source, which is {cls.__name__} here: "
                "register_source() it under a kind of its own"
            )
        try:
            json.dumps(built.settings())
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"a source's settings are kept as JSON: {exc}") from None
        self._check(built)
        from .signin.records import Record

        sid = _digest(built.kind, built.address)[:12]
        key = _key(self.collection, sid)
        for _attempt in range(5):
            held = self._records.get(SOURCE, key)
            data = dict(held.data) if held is not None else {}
            data.update(
                id=sid,
                collection=self.collection,
                type=built.kind,
                address=built.address,
                settings=built.settings(),
                every=seconds,
            )
            data.setdefault("added_at", _now_text())
            if by is not None or held is None:
                data["added_by"] = by
            if held is None:
                if self._records.create(Record(SOURCE, key, data, ix1=self.collection)):
                    return SourceInfo.from_data(data)
            elif self._records.replace(
                Record(SOURCE, key, data, ix1=self.collection, version=held.version)
            ):
                return SourceInfo.from_data(data)
        raise ConfigurationError(
            f"{public(built.address)} is being changed by somebody else; try again"
        )

    def _build(self, address: str, kind: Optional[str], options: Mapping[str, Any]) -> Source:
        if kind is None:
            _kept_address(address, web=True)
            kind = "feed" if self._looks_like_feed(address) else "page"
        cls = _class_for(kind)
        try:
            return cls(address, **dict(options))
        except TypeError as exc:
            raise ConfigurationError(f"a {kind} source takes no such option: {exc}") from None

    def _looks_like_feed(self, address: str) -> bool:
        fetcher = self.fetcher or self._new_fetcher()
        try:
            reply = fetcher.get(_expand(address, self._env), {"Accept": _FEED_ACCEPT})
        except (ExtractionError, ConfigurationError) as exc:
            raise ExtractionError(
                _scrub(
                    f"{public(address)} could not be read to tell a feed from a page: {exc}",
                    address,
                    self._env,
                )
            ) from None
        except Exception as exc:  # Deferred, and anything a fetcher of somebody else's raised
            raise ExtractionError(
                _scrub(
                    f"{public(address)} could not be read to tell a feed from a page: {exc}. Say kind='feed' or kind='page'.",
                    address,
                    self._env,
                )
            ) from None
        if not _ok(reply.status):
            raise ExtractionError(
                f"{public(address)} answered {reply.status}; check the address, or say kind='feed' or kind='page'",
                status=reply.status,
            )
        return _is_feed(reply)

    def _new_fetcher(self) -> Any:
        return _shared_fetcher(self._env)

    def _check(self, built: Source) -> None:
        """A feed's or a page's address refused now, when it would be refused at every refresh.

        Nothing is sent: the scheme, the host and every address it resolves
        to are judged as a fetch judges them. An address whose ``${NAME}`` is
        not set here is judged when it is fetched, where it is set.
        """
        if not isinstance(built, (Feed, Page)):
            return
        try:
            address = _expand(built.address, self._env)
        except ConfigurationError:
            return
        from ._fetch import Deferred

        fetcher = self.fetcher or self._new_fetcher()
        try:
            fetcher.check(address)
        except Deferred:
            # Offline: nothing is looked up now. It is checked when it is fetched.
            return
        except ExtractionError as exc:
            raise ExtractionError(_scrub(str(exc), built.address, self._env)) from None

    # ------------------------------------------------------------ reading ---

    def list(self) -> List[SourceInfo]:
        """Every source of this collection, in the order they were added."""
        found = [
            SourceInfo.from_data(r.data) for r in self._records.query(SOURCE, ix1=self.collection)
        ]
        return sorted(found, key=lambda s: (s.added_at or "", s.address))

    def get(self, source: Union[str, Source]) -> Optional[SourceInfo]:
        """One source, by its id, its address or the source itself; None when it is not here."""
        record = self._find(source)
        return SourceInfo.from_data(record.data) if record is not None else None

    def _find(self, source: Union[str, Source]) -> Any:
        if isinstance(source, Source):
            return self._records.get(
                SOURCE, _key(self.collection, _digest(source.kind, source.address)[:12])
            )
        wanted = str(source or "").strip()
        held = self._records.get(SOURCE, _key(self.collection, wanted))
        if held is not None:
            return held
        for record in self._records.query(SOURCE, ix1=self.collection):
            address = str(record.data.get("address") or "")
            if wanted in (address, public(address)):
                return record
        return None

    # ----------------------------------------------------------- removing ---

    def remove(self, source: Union[str, Source], *, delete_documents: bool = False) -> bool:
        """Stop keeping up with a source. Whether there was one.

        Its documents stay, unless ``delete_documents``; then every document
        it wrote goes, except one another source of this collection wrote too.
        """
        record = self._find(source)
        if record is None:
            return False
        sid = str(record.data.get("id"))
        for item in self._records.query(ITEM, ix1=_key(self.collection, sid)):
            doc_id = str(item.data.get("doc") or "")
            if delete_documents and doc_id and not self._written_by_another(doc_id, sid):
                self._writer.delete(doc_id)
            self._records.delete(ITEM, item.key)
        self._records.delete(LEASE, _key(self.collection, sid))
        return bool(self._records.delete(SOURCE, record.key))

    def reset(self, source: Union[str, Source, None] = None) -> int:
        """Forget what a source has written, or every source's, so the next refresh writes it all again.

        For a collection cut again in a new way, or emptied by hand. The
        documents stay where they are until the refresh replaces them.
        Returns how many sources were reset.
        """
        records = (
            [self._find(source)]
            if source is not None
            else list(self._records.query(SOURCE, ix1=self.collection))
        )
        count = 0
        for record in records:
            if record is None:
                continue
            sid = str(record.data.get("id"))
            for item in self._records.query(ITEM, ix1=_key(self.collection, sid)):
                self._records.delete(ITEM, item.key)
            self._update(sid, lambda data: data.update(state={}, next_due=None, documents=0))
            count += 1
        return count

    def _written_by_another(self, doc_id: str, sid: str) -> bool:
        return any(
            str(item.data.get("source")) != sid
            for item in self._records.query(ITEM, ix2=_doc_index(self.collection, doc_id))
        )

    # --------------------------------------------------------- refreshing ---

    def refresh(
        self,
        force: bool = False,
        *,
        only: Union[str, Source, None] = None,
        max_items: int = MAX_ITEMS,
    ) -> RefreshReport:
        """Read every source that is due, or every one with ``force``, and write only what changed.

        ``only`` refreshes one source, due or not. ``max_items`` is the most
        entries written for one source this time; the rest wait for the next
        refresh. Never raises for a source that fails: the report says which,
        and why, and the next refresh tries again.
        """
        report = RefreshReport()
        if only is not None:
            found = self._find(only)
            if found is None:
                raise ConfigurationError(f"{only!r} is not a source of {self.collection!r}")
            chosen = [found]
        else:
            chosen = sorted(
                self._records.query(SOURCE, ix1=self.collection),
                key=lambda r: (r.data.get("added_at") or "", r.key),
            )
        fetcher = self.fetcher or self._new_fetcher()
        now = self._clock()
        with self._writer.batch():
            for record in chosen:
                due = record.data.get("next_due")
                asked = record.data.get("wait_until")
                if asked is not None and float(asked) > now:
                    # The site asked for this long, by Retry-After or Crawl-delay,
                    # and force does not ask it sooner.
                    report.sources.append(self._put_off(record, float(asked)))
                    continue
                if not force and only is None and due is not None and float(due) > now:
                    report.not_due += 1
                    continue
                report.sources.append(self._refresh_one(record, fetcher, max(int(max_items), 1)))
        return report

    def _put_off(self, record: Any, until: float) -> SourceOutcome:
        template = str(record.data.get("address") or "")
        shown = public(template)
        return SourceOutcome(
            id=str(record.data.get("id")),
            address=shown,
            kind=str(record.data.get("type")),
            status="deferred",
            reason=f"{site_of(shown)} asked to be left alone until {_now_text(until)}, and is asked again then",
        )

    def _refresh_one(self, record: Any, fetcher: Any, max_items: int) -> SourceOutcome:
        from ._fetch import Deferred

        data = dict(record.data)
        sid = str(data.get("id"))
        template = str(data.get("address") or "")
        outcome = SourceOutcome(id=sid, address=public(template), kind=str(data.get("type")))
        taken = self._take_lease(sid)
        if taken is None:
            outcome.status = "busy"
            outcome.reason = "another refresh is reading this source now"
            return outcome
        lease = _Held(self._records, taken)
        started = self._clock()
        every = float(data.get("every") or DEFAULT_EVERY)
        state = dict(data.get("state") or {})
        next_due = started + every
        # When the site itself said, by Retry-After or Crawl-delay.
        asked: Optional[float] = None
        try:
            try:
                source = _class_for(outcome.kind).from_settings(
                    template, data.get("settings") or {}
                )
                address = _expand(template, self._env)
            except (ConfigurationError, TypeError) as exc:
                outcome.status, outcome.reason = "failed", str(exc)
            else:
                known = {
                    str(item.data.get("key")): {**item.data, "_record": item}
                    for item in self._records.query(ITEM, ix1=_key(self.collection, sid))
                }
                context = SourceContext(
                    address=address,
                    shown=outcome.address,
                    fetcher=fetcher,
                    state=state,
                    known=known,
                    extractors=self._writer.extractors,
                    collection=self.collection,
                    scrub=lambda text: _scrub(text, template, self._env),
                )
                try:
                    found = source.read(context)
                    if found is None:
                        outcome.status = "not modified"
                        outcome.unchanged = sum(1 for k in known.values() if not k.get("gone"))
                    else:
                        self._take(source, sid, found, known, context, outcome, max_items, lease)
                except Deferred as exc:
                    outcome.status, outcome.reason = "deferred", str(exc)
                    asked = _asked_until(getattr(exc, "until", None), started)
                    next_due = asked if asked is not None else started + min(every, 3600.0)
                except (
                    ExtractionError,
                    ConfigurationError,
                    DependencyError,
                    OSError,
                    ValueError,
                ) as exc:
                    outcome.status, outcome.reason = "failed", str(exc)
                except Exception as exc:  # a source of somebody else's: reported, never raised
                    log.exception("source %s of %s failed", outcome.address, self.collection)
                    outcome.status, outcome.reason = "failed", f"{type(exc).__name__}: {exc}"
                outcome.notes.extend(context.notes)
        finally:
            outcome.reason = _scrub(outcome.reason, template, self._env) if outcome.reason else None
            outcome.notes = [_scrub(n, template, self._env) for n in outcome.notes]
            for failure in outcome.failed:
                failure["reason"] = _scrub(failure["reason"], template, self._env)
            if outcome.status != "busy":
                self._settle(sid, outcome, state, next_due, asked)
            self._release(lease.lease)
        return outcome

    def _take(
        self,
        source: Source,
        sid: str,
        found: Iterable[Item],
        known: Mapping[str, Mapping[str, Any]],
        context: SourceContext,
        outcome: SourceOutcome,
        max_items: int,
        lease: Optional["_Held"] = None,
    ) -> None:
        """Every item compared with what was written, and only the new and the changed written.

        Before each item it writes, the lease on the source is made longer
        when a third of it has gone. One that was lost, to a refresh that
        took it after it ran out, leaves the rest for the next refresh.

        An item's document is its link, unless another item of this source
        has that link already, a podcast whose every episode links the show,
        a changelog whose entries link one page: then it is the link and its
        own key. Nothing a source writes or reports carries a value its
        address reads from the environment.
        """
        from ._fetch import Deferred

        scrub = context.scrub
        # The document each link is already, and which item it is for.
        claimed: Dict[str, str] = {}
        for held_key, held_data in known.items():
            if held_data.get("doc"):
                claimed.setdefault(str(held_data["doc"]), held_key)
        seen = set()
        writes = 0
        for item in found:
            if not isinstance(item, Item) or not str(item.key or ""):
                continue
            item = replace(
                item,
                key=scrub(str(item.key)),
                link=scrub(item.link) if item.link else item.link,
                title=scrub(item.title) if item.title else item.title,
                metadata={
                    k: scrub(v) if isinstance(v, str) else v
                    for k, v in (item.metadata or {}).items()
                },
                doc_id=scrub(item.doc_id) if item.doc_id else item.doc_id,
            )
            if item.key in seen:
                continue
            seen.add(item.key)
            held = known.get(item.key)
            # A source of somebody else's may hand over a signed link: it is
            # fetched as given, and kept and shown without its credential.
            label = redact_url(item.link) if item.link else (item.title or item.key)
            if item.doc_id:
                doc_id = item.doc_id
            elif item.link:
                doc_id = _doc_id_of(item.link)
                if claimed.setdefault(doc_id, item.key) != item.key:
                    doc_id = f"{doc_id}#{_key_part(item.key)}"
            else:
                doc_id = f"{outcome.address}#{_key_part(item.key)}"
            if item.gone:
                self._gone(source, sid, item, held, outcome)
                continue
            if (
                held is not None
                and not held.get("gone")
                and item.fingerprint is not None
                and held.get("fingerprint") == item.fingerprint
            ):
                outcome.unchanged += 1
                continue
            if writes >= max_items:
                outcome.waiting += 1
                continue
            if lease is not None and not lease.keep():
                outcome.waiting += 1
                continue
            try:
                doc = _document_of(item)
            except Deferred as exc:
                outcome.waiting += 1
                context.note(f"{label}: {exc}")
                continue
            except Exception as exc:  # one item that cannot be read does not stop the rest
                outcome.failed.append({"item": label, "reason": _reason(exc)})
                continue
            if doc is None or not str(getattr(doc, "text", "") or "").strip():
                outcome.failed.append({"item": label, "reason": "there is no text in it"})
                continue
            # Its name, its source and its title go onto every chunk and citation.
            metadata_of = getattr(doc, "metadata", None)
            if isinstance(metadata_of, dict):
                for name, value in list(metadata_of.items()):
                    if isinstance(value, str):
                        metadata_of[name] = scrub(value)
            version = _version(doc.text)
            if (
                held is not None
                and not held.get("gone")
                and held.get("version") == version
                and held.get("doc") == doc_id
            ):
                outcome.unchanged += 1
                self._remember_item(sid, item, doc_id, version, held)
                continue
            if held is None and self._indexed_elsewhere(doc_id, version):
                outcome.unchanged += 1
                self._remember_item(sid, item, doc_id, version, None)
                continue
            try:
                self._writer.write(doc, doc_id, self._metadata(source, sid, item, outcome), version)
            except Exception as exc:
                outcome.failed.append({"item": label, "reason": _reason(exc)})
                continue
            writes += 1
            (outcome.updated if held is not None else outcome.added).append(doc_id)
            if held is not None and held.get("doc") and held.get("doc") != doc_id:
                previous = str(held.get("doc"))
                if not self._written_by_another(previous, sid):
                    self._writer.delete(previous)
            self._remember_item(sid, item, doc_id, version, held)
        if lease is not None and lease.lost:
            context.note(
                "this refresh ran past its lease on the source and another may have taken it, so what "
                "was left waits for the next refresh"
            )

    def _gone(
        self,
        source: Source,
        sid: str,
        item: Item,
        held: Optional[Mapping[str, Any]],
        outcome: SourceOutcome,
    ) -> None:
        if held is None or held.get("gone"):
            return
        from .signin.records import Record

        doc_id = str(held.get("doc") or "")
        if source.delete_when_gone and doc_id:
            if not self._written_by_another(doc_id, sid):
                self._writer.delete(doc_id)
            outcome.removed.append(doc_id)
        else:
            outcome.gone.append(doc_id)
        record = held.get("_record")
        data = {k: v for k, v in held.items() if k != "_record"}
        data.update(gone=True, changed_at=_now_text())
        self._records.put(
            Record(ITEM, record.key, data, ix1=record.ix1, ix2=record.ix2)
            if record is not None
            else Record(ITEM, _item_key(self.collection, sid, item.key), data)
        )

    def _indexed_elsewhere(self, doc_id: str, version: str) -> bool:
        """Whether another source of this collection already wrote this document, as it is now."""
        return any(
            item.data.get("version") == version and not item.data.get("gone")
            for item in self._records.query(ITEM, ix2=_doc_index(self.collection, doc_id))
        )

    def _remember_item(
        self, sid: str, item: Item, doc_id: str, version: str, held: Optional[Mapping[str, Any]]
    ) -> None:
        from .signin.records import Record

        now = _now_text()
        data = {
            "key": item.key,
            "source": sid,
            "doc": doc_id,
            "version": version,
            "fingerprint": item.fingerprint,
            "gone": False,
            "first_seen": (held or {}).get("first_seen") or now,
            "changed_at": now,
        }
        self._records.put(
            Record(
                ITEM,
                _item_key(self.collection, sid, item.key),
                data,
                ix1=_key(self.collection, sid),
                ix2=_doc_index(self.collection, doc_id),
            )
        )

    def _metadata(
        self, source: Source, sid: str, item: Item, outcome: SourceOutcome
    ) -> Dict[str, Any]:
        said: Dict[str, Any] = {
            "title": item.title,
            "link": redact_url(item.link) if item.link else None,
            "author": item.author,
            "published": item.published,
            "updated": item.updated,
        }
        said.update(item.metadata or {})
        said.update(source_id=sid, source_kind=source.kind, source_address=outcome.address)
        return {k: v for k, v in said.items() if v is not None and v != ""}

    # ----------------------------------------------------- the bookkeeping ---

    def _take_lease(self, sid: str) -> Any:
        from .signin.records import Record

        key = _key(self.collection, sid)
        who = f"{socket.gethostname()}:{os.getpid()}"
        if not self._records.create(
            Record(LEASE, key, {"by": who, "at": _now_text()}, expires=time.time() + LEASE_FOR)
        ):
            return None
        return self._records.get(LEASE, key) or Record(LEASE, key)

    def _release(self, lease: Any) -> None:
        try:
            self._records.delete(LEASE, lease.key, version=lease.version)
        except Exception:  # the lease runs out by itself
            log.warning("the lease on %s could not be let go; it runs out by itself", lease.key)

    def _settle(
        self,
        sid: str,
        outcome: SourceOutcome,
        state: Dict[str, Any],
        next_due: float,
        asked: Optional[float] = None,
    ) -> None:
        """What this refresh found, written back to the source, unless it was removed meanwhile."""
        documents = sum(
            1
            for item in self._records.query(ITEM, ix1=_key(self.collection, sid))
            if not item.data.get("gone")
        )

        kept = dict(state)
        if outcome.waiting or outcome.failed:
            # Not all of what it gave is written: the next refresh asks for
            # it whole, rather than hear it has not changed and never write
            # the rest.
            kept.pop("etag", None)
            kept.pop("modified", None)

        def change(data: Dict[str, Any]) -> None:
            data.update(
                last_refresh=_now_text(),
                next_due=next_due,
                last_status=outcome.status,
                last_error=outcome.reason if outcome.status in ("failed", "deferred") else None,
                documents=documents,
            )
            if asked is not None:
                data["wait_until"] = asked
            else:
                data.pop("wait_until", None)
            if outcome.status in ("refreshed", "not modified"):
                data["state"] = kept

        if not self._update(sid, change):
            # Removed while it was being read: what this refresh wrote down goes too.
            for item in self._records.query(ITEM, ix1=_key(self.collection, sid)):
                self._records.delete(ITEM, item.key)

    def _update(self, sid: str, change: Callable[[Dict[str, Any]], None]) -> bool:
        from .signin.records import Record

        key = _key(self.collection, sid)
        for _attempt in range(5):
            held = self._records.get(SOURCE, key)
            if held is None:
                return False
            data = dict(held.data)
            change(data)
            if self._records.replace(
                Record(SOURCE, key, data, ix1=self.collection, version=held.version)
            ):
                return True
        log.warning(
            "source %s of %s changed under five tries in a row; left as it was",
            sid,
            self.collection,
        )
        return True


def _asked_until(until: Any, now: float) -> Optional[float]:
    """When a site that put a source off said to come back, a week from now at the most; None when it did not say."""
    from ._fetch import MAX_DEFER

    try:
        said = float(until) if until else None
    except (TypeError, ValueError, OverflowError):
        return None
    if said is None or math.isnan(said):
        return None
    return min(max(said, now), now + MAX_DEFER)


def _document_of(item: Item) -> Any:
    """An item's document: the one it carries, or what ``read`` gives, or its text as Markdown."""
    from .ingest import LoadedDocument, markdown_document

    if item.document is not None:
        return item.document
    found: Any = item.read() if item.read is not None else item.text
    if found is None:
        return None
    if isinstance(found, LoadedDocument):
        return found
    metadata = {"source": redact_url(item.link)} if item.link else {}
    if item.title:
        metadata["filename"] = item.title
    return markdown_document(str(found), metadata=metadata)


def _reason(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    said = str(detail if detail else exc) or type(exc).__name__
    if isinstance(exc, (ExtractionError, ConfigurationError, DependencyError)):
        return said
    return f"{type(exc).__name__}: {said}"
