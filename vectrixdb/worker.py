"""One ingestion worker that every event source can call.

A file lands in a bucket, a blob container or a directory, and something has
to fetch it, cut it up, embed it, stamp its lineage and write the ingestion
record. That something is the same whichever cloud raised the event, so it
is written once here and the cloud-specific part is a parser for the event
shape and a fetcher for the bytes, each a few lines.

    worker = IngestWorker(db, fetcher=S3Fetcher(boto3.client("s3")))
    for event in events_from_s3(lambda_event):
        worker.handle(event)

``handle`` is idempotent on the document's version hash. An event delivered
twice, which every queue promises to do sooner or later, finds the same
bytes already ingested and does nothing. A changed file replaces its chunks
under the same document id, so a stale chunk never outlives the file it
came from. A delete event removes the document, and removal mints a build
like any other write, so lineage records that the document was there and
then was not.

What the worker does not do: authenticate. A fetcher is built around a
client the host already holds, with the host's credentials, and the worker
never sees a key.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Union
from urllib.parse import unquote, unquote_plus, urlparse

from ._web import redact_url
from .exceptions import ConfigurationError


# ============================================================================
# SETTINGS: the logger
# ============================================================================
#
# One logger for the worker's lines.

logger = logging.getLogger(__name__)

__all__ = [
    "BlobFetcher",
    "IngestEvent",
    "IngestOutcome",
    "IngestWorker",
    "LocalFetcher",
    "LocalWatcher",
    "S3Fetcher",
    "events_from_event_grid",
    "events_from_s3",
]


# ============================================================================
# THE EVENTS
# ============================================================================
#
# INPUT   an S3 notification, direct or via SQS; an Event Grid delivery, in
#         either schema
# OUTPUT  something happened to one object somewhere, one event each
#
# Both clouds' shapes read into one.


@dataclass(frozen=True)
class IngestEvent:
    """Something happened to one object somewhere.

    ``kind`` is ``created`` or ``deleted``; a modification is a ``created``,
    since the worker tells a new file from a changed one by its bytes.
    ``uri`` names the object the way its store does, and is the document
    id unless the worker is given another way to derive one. ``version``
    is whatever the store offers (an ETag, a version id) and is recorded on
    the ingestion; the worker does not trust it for idempotency, since two
    stores spell it differently and one of them may not spell it at all.
    """

    kind: str
    uri: str
    version: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ("created", "deleted"):
            raise ValueError(f"event kind must be created or deleted, not {self.kind!r}")


def events_from_s3(payload: Mapping[str, Any]) -> List[IngestEvent]:
    """The events in an S3 notification, whether it arrived directly or via SQS.

    Lambda hands an S3 notification as ``{"Records": [...]}`` with
    ``eventName`` ``ObjectCreated:*`` or ``ObjectRemoved:*``. Through SQS the
    same JSON sits as a string in each SQS record's ``body``. Both shapes
    are read; anything else is skipped rather than raised on, because a
    queue that also carries other messages is normal.
    """
    out: List[IngestEvent] = []
    for record in payload.get("Records", []) or []:
        if "s3" in record:
            name = str(record.get("eventName", ""))
            kind = (
                "created"
                if name.startswith("ObjectCreated")
                else "deleted"
                if name.startswith("ObjectRemoved")
                else None
            )
            if kind is None:
                continue
            s3 = record["s3"]
            bucket = s3.get("bucket", {}).get("name")
            key = unquote_plus(str(s3.get("object", {}).get("key", "")))
            if not bucket or not key:
                continue
            obj = s3.get("object", {})
            out.append(
                IngestEvent(
                    kind=kind,
                    uri=f"s3://{bucket}/{key}",
                    version=obj.get("versionId") or obj.get("eTag"),
                    metadata={"bucket": bucket, "key": key, "event": name},
                )
            )
        elif "body" in record:
            try:
                inner = json.loads(record["body"])
            except (TypeError, ValueError):
                continue
            if isinstance(inner, dict):
                out.extend(events_from_s3(inner))
    return out


def events_from_event_grid(
    payload: Union[Mapping[str, Any], Iterable[Mapping[str, Any]]],
) -> List[IngestEvent]:
    """The events in an Event Grid delivery, in the Event Grid or the CloudEvents schema.

    Blob Storage raises ``Microsoft.Storage.BlobCreated`` and
    ``Microsoft.Storage.BlobDeleted`` with the blob's URL and ETag under
    ``data``. The subscription validation handshake and anything else that
    is not a blob event is skipped.
    """
    items = [payload] if isinstance(payload, Mapping) else list(payload)
    out: List[IngestEvent] = []
    for item in items:
        etype = str(item.get("eventType") or item.get("type") or "")
        data = item.get("data") or {}
        url = data.get("url") or data.get("blobUrl")
        if not url:
            continue
        if etype.endswith("BlobCreated"):
            kind = "created"
        elif etype.endswith("BlobDeleted"):
            kind = "deleted"
        else:
            continue
        out.append(
            IngestEvent(
                kind=kind,
                uri=str(url),
                version=data.get("eTag") or data.get("etag"),
                metadata={"event": etype, "api": data.get("api")},
            )
        )
    return out


# ============================================================================
# THE FETCHERS
# ============================================================================
#
# INPUT   a URI
# OUTPUT  the bytes and the name to load them under: from this machine, from
#         S3 through a boto3 client, or from Azure Storage through a
#         BlobServiceClient
#
# The name is what the loader picks a parser by, so it is kept with the bytes.


class Fetcher(Protocol):
    """Bytes for a URI, plus the name the bytes should be loaded under."""

    def fetch(self, uri: str) -> bytes: ...


def _name_of(uri: str) -> str:
    """The file name a URI ends in, so the loader picks the right parser."""
    parsed = urlparse(uri)
    path = parsed.path if parsed.scheme else uri
    return PurePosixPath(unquote(path.replace("\\", "/"))).name or "document"


class LocalFetcher:
    """Files on this machine. A URI is a path, or ``file://`` plus a path."""

    def fetch(self, uri: str) -> bytes:
        if not uri.startswith("file://"):
            return Path(uri).read_bytes()
        rest = uri[7:]
        plain = Path(rest)
        if plain.is_file():
            return plain.read_bytes()
        from urllib.request import url2pathname

        # A real file URI: /C:/docs/a%20b.pdf in the address is the file
        # C:/docs/a b.pdf on a Windows disk. url2pathname knows both halves.
        return Path(url2pathname(rest)).read_bytes()


class S3Fetcher:
    """Objects in S3, through a boto3 client the host built."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch(self, uri: str) -> bytes:
        parsed = urlparse(uri)
        if parsed.scheme != "s3":
            raise ValueError(f"not an s3 URI: {uri}")
        body = self._client.get_object(Bucket=parsed.netloc, Key=unquote(parsed.path.lstrip("/")))[
            "Body"
        ]
        return body.read()


class BlobFetcher:
    """Blobs in Azure Storage, through a BlobServiceClient the host built."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch(self, uri: str) -> bytes:
        parsed = urlparse(uri)
        parts = unquote(parsed.path.lstrip("/")).split("/", 1)
        if len(parts) != 2 or not parts[1]:
            raise ValueError(f"not a blob URL with a container and a name: {uri}")
        container, name = parts
        blob = self._client.get_blob_client(container=container, blob=name)
        return blob.download_blob().readall()


# ============================================================================
# THE OUTCOME, AND THE WORKER
# ============================================================================
#
# INPUT   object events
# OUTPUT  what one event did; add_document and delete_document calls, one an
#         event
#
# A file lands in a bucket, a blob container or a directory, and something has
# to fetch it, cut it up, embed it and write it: this is that something,
# called by whatever noticed.


@dataclass
class IngestOutcome:
    """What one event did.

    ``action`` is ``created``, ``updated``, ``unchanged``, ``deleted``,
    ``absent``, ``failed``, ``ignored`` or ``superseded``. ``unchanged`` is the idempotent case: the same bytes were
    already in. ``absent`` is a delete for a document that was not there,
    which is also a no-op and also fine. ``failed`` is an extraction that
    did not produce text, recorded only when the worker was built with
    ``on_error="record"``; ``error`` says why, and nothing was written.
    ``ignored`` is an event for a file inside the collection's own document
    store, which is the library's output and never its input. ``superseded``
    is an event in a batch that a later event for the same object overrode.
    """

    action: str
    doc_id: str
    uri: str
    version: Optional[str] = None
    chunks: int = 0
    build_id: Optional[str] = None
    error: Optional[str] = None


class TokenBucket:
    """A pace: ``rate`` tokens a second, a burst of two seconds' worth, and ``take`` waits for what it has not got.

    ``clock`` and ``sleep`` are injectable, so a test needs no real second.
    """

    def __init__(
        self,
        rate: float,
        burst: Optional[float] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"rate is tokens a second, above zero, got {rate!r}")
        self.rate = float(rate)
        self.burst = float(burst) if burst else max(1.0, self.rate * 2)
        self.clock = clock
        self.sleep = sleep
        self.tokens = self.burst
        self.at = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now

    def take(self, n: int = 1) -> float:
        """Spend ``n`` tokens, waiting for them first when they are not there. How long it waited."""
        self._refill()
        waited = 0.0
        if self.tokens < n:
            short = n - self.tokens
            wait = short / self.rate
            self.sleep(wait)
            waited = wait
            self._refill()
        self.tokens -= n
        return waited


class IngestWorker:
    """Turn object events into ``add_document`` and ``delete_document`` calls.

    ``db`` is an open ``Vectrix``; whatever it was opened with, the policy
    and its metadata contract, the quality gate, the audit sink and the
    build stamping, applies to every event. ``doc_id_of`` maps a URI to a
    document id; the default is the URI itself, without any signature, token
    or key it carries, which is also what a chunk keeps as its ``source``. ``metadata_of`` adds
    metadata to every chunk of a document, which is where a policy's fields
    come from when the store does not carry them.

    ``extractors`` says who reads which file type, as on ``Vectrix``; left
    out, the worker uses the ones the collection was opened with. A failed
    extraction raises :class:`~vectrixdb.exceptions.ExtractionError` by
    default, because on a queue an exception is what asks for the retry
    and, in the end, the dead letter. ``on_error="record"`` returns a
    ``failed`` outcome instead, for a batch that should not stop at one bad
    file. Either way the document's existing chunks are untouched: the old
    ones are only replaced once there is new text to replace them with.
    """

    def __init__(
        self,
        db: Any,
        fetcher: Fetcher,
        *,
        doc_id_of: Optional[Callable[[str], str]] = None,
        metadata_of: Optional[Callable[[IngestEvent], Dict[str, Any]]] = None,
        extractors: Any = None,
        on_error: str = "raise",
        chunks_per_second: Optional[float] = None,
        **add_document_options: Any,
    ) -> None:
        if on_error not in ("raise", "record"):
            raise ValueError(f"on_error is 'raise' or 'record', got {on_error!r}")
        self.db = db
        self.fetcher = fetcher
        self.on_error = on_error
        # A pace on what is written to the index: a burst of files then waits between documents
        # rather than flooding the store or the model that embeds for it.
        self.pace: Optional[TokenBucket] = (
            TokenBucket(chunks_per_second) if chunks_per_second else None
        )
        if extractors is not None:
            from .extract import resolve

            self.extractors: Any = resolve(extractors)
        else:
            self.extractors = getattr(db, "_extractors", None)
        # A signed link's credential stays in the fetch, and out of every id,
        # chunk and citation; an address with none is its own id, as always.
        self.doc_id_of = doc_id_of or redact_url
        self.metadata_of = metadata_of
        self.options = add_document_options

    # -- one event ----------------------------------------------------------

    def handle(self, event: IngestEvent) -> IngestOutcome:
        doc_id = self.doc_id_of(event.uri)
        store = getattr(self.db, "documents", None)
        if store is not None and store.holds(event.uri):
            # The store's own files. Ingesting one would write another, and
            # that one would raise an event too.
            logger.warning(
                "ignored %s: it is inside the document store %r", redact_url(event.uri), store
            )
            return IngestOutcome(
                action="ignored",
                doc_id=doc_id,
                uri=event.uri,
                version=event.version,
                error="inside the collection's document store",
            )
        chunks = getattr(self.db, "kept_chunks", None)
        if chunks is not None and chunks.holds(event.uri):
            logger.warning(
                "ignored %s: it is inside the chunk store %r", redact_url(event.uri), chunks
            )
            return IngestOutcome(
                action="ignored",
                doc_id=doc_id,
                uri=event.uri,
                version=event.version,
                error="inside the collection's chunk store",
            )
        if event.kind == "deleted":
            removed = self.db.delete_document(doc_id)
            return IngestOutcome(
                action="deleted" if removed else "absent",
                doc_id=doc_id,
                uri=event.uri,
                version=event.version,
                chunks=removed,
                build_id=self.db.index_build_id if removed else None,
            )

        from .exceptions import ExtractionError
        from .ingest import load_bytes

        # Stage one: has the original changed? The store's version when the
        # event carries one, an ETag, else a hash of the bytes. The same
        # original is not read again, which is what an OCR bill wants.
        kept = store.entry(doc_id) if store is not None else None
        if (
            kept
            and event.version
            and kept.get("source_version") == event.version
            and self._current_version(doc_id)
        ):
            return IngestOutcome(
                action="unchanged",
                doc_id=doc_id,
                uri=event.uri,
                version=event.version,
                build_id=self.db.index_build_id,
            )
        # Kept and not indexed. With the Markdown written first, that is a
        # later step that failed last time, and this try starts from the
        # Markdown: the original is not fetched or read again, which is the
        # part that costs money when reading is OCR, a vision model or speech.
        first = bool(getattr(self.db, "markdown_first", False))
        digest: Optional[str] = None
        if (
            first
            and store is not None
            and kept
            and event.version
            and kept.get("source_version") == event.version
        ):
            doc = store.get(doc_id, images=True)
            source_version = event.version
        else:
            data = self.fetcher.fetch(event.uri)
            digest = hashlib.sha256(data).hexdigest()[:16]
            source_version = event.version or digest
            if (
                kept
                and (
                    kept.get("source_version") == source_version or kept.get("source_sha") == digest
                )
                and self._current_version(doc_id)
            ):
                if kept.get("source_version") != source_version and store is not None:
                    # The same bytes under a new ETag, a file saved again unchanged: noted, and not read again.
                    store.touch(doc_id, source_version=source_version, source_sha=digest)
                return IngestOutcome(
                    action="unchanged",
                    doc_id=doc_id,
                    uri=event.uri,
                    version=event.version,
                    build_id=self.db.index_build_id,
                )
            if (
                first
                and store is not None
                and kept
                and kept.get("source_version") == source_version
            ):
                doc = store.get(doc_id, images=True)
            else:
                name = _name_of(event.uri)
                try:
                    # The collection decides: a describer or an image embedder means the
                    # pictures are kept, exactly as add_document keeps them from a file.
                    wants = getattr(self.db, "wants_images", None)
                    doc = load_bytes(
                        data,
                        name,
                        extractors=self.extractors,
                        source=redact_url(event.uri),
                        images=bool(wants and wants()),
                        # Whatever reads a scanned page for add_document reads one
                        # here too, or a blob and a file are two different documents.
                        ocr=getattr(self.db, "page_ocr", None),
                    )
                except ExtractionError as exc:
                    if self.on_error == "raise":
                        raise
                    return IngestOutcome(
                        action="failed",
                        doc_id=doc_id,
                        uri=event.uri,
                        version=event.version,
                        error=str(exc),
                    )

        # Stage two: has the text changed? A file saved again with the same
        # words costs the extraction and no write.
        version = self._version_of(doc.text)
        current = self._current_version(doc_id)
        if current == version:
            if store is not None and kept:
                store.touch(doc_id, source_version=source_version)
            return IngestOutcome(
                action="unchanged",
                doc_id=doc_id,
                uri=event.uri,
                version=event.version,
                build_id=self.db.index_build_id,
            )

        extra: Dict[str, Any] = {"object_version": event.version} if event.version else {}
        if self.metadata_of is not None:
            extra.update(self.metadata_of(event))
        extra.update(event.metadata.get("metadata") or {})

        if current is not None:
            self.db.delete_document(doc_id)
        written = self.db.add_document(
            doc, doc_id=doc_id, metadata=extra, source_version=source_version, **self.options
        )
        if digest and store is not None:
            # The bytes' own hash beside the version, so the same file saved again is known without being read.
            store.touch(doc_id, source_version=source_version, source_sha=digest)
        if self.pace is not None and written:
            self.pace.take(int(written))
        return IngestOutcome(
            action="updated" if current is not None else "created",
            doc_id=doc_id,
            uri=event.uri,
            version=event.version,
            chunks=written,
            build_id=self.db.index_build_id,
        )

    def handle_all(self, events: Iterable[IngestEvent]) -> List[IngestOutcome]:
        """Handle a batch, in order, with a burst in mind.

        A bucket that receives a thousand files sends a thousand events, and
        often more than one for the same object. Only the last event for an
        object is acted on, since it is the one that says how things stand;
        the earlier ones are answered ``superseded``. And the vector index is
        saved once, at the end, instead of after every document. Each
        document is still its own ingestion, with its own record and build,
        because an ingestion record names one document.
        """
        batch = list(events)
        last = {event.uri: index for index, event in enumerate(batch)}
        defer = getattr(self.db, "deferred_saves", None)
        scope = defer() if callable(defer) else contextlib.nullcontext()
        outcomes: List[IngestOutcome] = []
        with scope:
            for index, event in enumerate(batch):
                if last[event.uri] != index:
                    outcomes.append(
                        IngestOutcome(
                            action="superseded",
                            doc_id=self.doc_id_of(event.uri),
                            uri=event.uri,
                            version=event.version,
                        )
                    )
                    continue
                outcomes.append(self.handle(event))
        return outcomes

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _version_of(text: str) -> str:
        import hashlib

        # The same hash add_document stamps as _vx_doc_version.
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _current_version(self, doc_id: str) -> Optional[str]:
        """The version stamped on this document's chunks, or None if it has none."""
        coll = getattr(self.db, "_collection", None)
        if coll is None:
            return None
        for _, _, meta in coll._iter_documents_raw():
            if meta.get("_vx_doc") == doc_id:
                return meta.get("_vx_doc_version")
        return None


# ============================================================================
# A DIRECTORY AS AN EVENT SOURCE
# ============================================================================
#
# INPUT   a directory
# OUTPUT  events for the files that appear in it, for machines without a queue
#
# The same worker, fed by a watcher.


class LocalWatcher:
    """A directory as an event source, for machines without a queue.

    ``poll()`` compares the directory with what it saw last time and hands
    the worker a created event for every new or changed file and a deleted
    event for every file that is gone. The snapshot is size and mtime, so
    a touched file reaches the worker, and the worker's own version check
    makes that a no-op. Recursion follows the glob.
    """

    def __init__(
        self, root: Union[str, Path], worker: IngestWorker, *, pattern: str = "**/*"
    ) -> None:
        self.root = Path(root)
        self.worker = worker
        self.pattern = pattern
        self._seen: Dict[str, tuple] = {}
        store = getattr(worker.db, "documents", None)
        files_root = getattr(getattr(store, "files", None), "root", None)
        if files_root is not None:
            watched, kept = self.root.resolve(), Path(files_root).resolve()
            if kept == watched or watched in kept.parents or kept in watched.parents:
                raise ConfigurationError(
                    f"The watched folder {watched} and the document store {kept} overlap. The store is "
                    "what the library writes and the watched folder is what it reads; one inside the "
                    "other ingests its own output for ever. Give keep_source a folder elsewhere."
                )

    def _snapshot(self) -> Dict[str, tuple]:
        out: Dict[str, tuple] = {}
        for path in sorted(self.root.glob(self.pattern)):
            if path.is_file() and not path.name.endswith(".tmp"):
                st = path.stat()
                out[str(path)] = (st.st_size, st.st_mtime_ns)
        return out

    def poll(self) -> List[IngestOutcome]:
        now = self._snapshot()
        events: List[IngestEvent] = []
        for path, sig in now.items():
            if self._seen.get(path) != sig:
                events.append(IngestEvent(kind="created", uri=path))
        for path in self._seen:
            if path not in now:
                events.append(IngestEvent(kind="deleted", uri=path))
        self._seen = now
        return self.worker.handle_all(events)
