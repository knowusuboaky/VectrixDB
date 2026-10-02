"""The document store: the Markdown a document was indexed from, kept.

Chunks are what the index holds. The document they were cut from is gone the
moment it is chunked, unless it is kept, and then three things stop being
possible: cutting it a different way without extracting it again, which
costs money when extraction is OCR; showing the page a citation points at;
and saying what, exactly, was indexed. ``Vectrix(keep_source=True)`` keeps
it, one Markdown file per document with front matter that reads back
exactly, and the images its figures name beside it.

The store is a root of its own. It is never the folder or the container the
originals live in, for two reasons: an original can itself be ``notes.md``,
and a watcher on the originals would see the store's own files land and
ingest them, for ever. :meth:`DocumentStore.holds` is what a watcher and a
worker ask before they touch a path.

Where the files go is a small interface, :class:`LocalFiles`,
:class:`S3Files`, :class:`BlobFiles`, each built around a client the host
made. The front matter of each file is the record; ``_index.json`` is a
listing for people and for speed, rewritten on every change and rebuilt from
the files when it is missing. Two processes writing one store at once can
lose a line of that listing and never a document.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote, urlparse

from .exceptions import ConfigurationError, DocumentNotFoundError
from .ingest import LoadedDocument, split_front_matter

__all__ = ["BlobFiles", "ChunkStore", "DocumentStore", "LocalFiles", "S3Files", "chunks_name", "stored_name"]


# ============================================================================
# SETTINGS: the index file, and the deleted marker
# ============================================================================
#
# The index every store keeps beside its documents, and what marks a document
# as deleted rather than gone.

INDEX = "_index.json"
DELETED = "_deleted"


# ============================================================================
# A DOCUMENT'S NAME
# ============================================================================
#
# INPUT   a document's id
# OUTPUT  where its Markdown goes: the id's own path when it is one, a safe
#         name otherwise
#
# Names are derived, never chosen, so two stores agree on where a document is.


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def stored_name(doc_id: str) -> str:
    """Where a document's Markdown goes, from its id: the id's own path,
    made safe, plus ``.md``. ``s3://inbox/acme/scan.pdf`` is
    ``inbox/acme/scan.pdf.md`` and ``notes.md`` is ``notes.md.md``, so the
    original's type stays readable and an original that is Markdown can
    never be mistaken for its own extraction."""
    text = str(doc_id).replace("\\", "/")
    parsed = urlparse(text)
    if parsed.scheme and "://" in text:
        text = parsed.netloc + parsed.path
    parts = [p for p in text.split("/") if p and p not in (".", "..")]
    safe = [quote(p, safe=" ()+,=@!~'") .replace("%20", " ") for p in parts] or ["document"]
    # A segment must not end in a dot or a space on Windows.
    safe = [s.rstrip(". ") or "_" for s in safe]
    return "/".join(safe) + ".md"


# ============================================================================
# THE FILES: a folder, a bucket, or a container
# ============================================================================
#
# INPUT   a folder; an S3 bucket or a prefix in one, through a boto3 client
#         the host built; a Blob container or a prefix in one, through a
#         BlobServiceClient
# OUTPUT  the same reads, writes, lists and deletes on each
#
# The store above does not know which it has.


class LocalFiles:
    """A folder."""

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root)

    def _path(self, rel: str) -> Path:
        target = (self.root / rel).resolve()
        target.relative_to(self.root.resolve())  # a name never leaves the root
        return target

    def read(self, rel: str) -> bytes:
        return self._path(rel).read_bytes()

    def write(self, rel: str, data: bytes) -> None:
        path = self._path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def exists(self, rel: str) -> bool:
        return self._path(rel).is_file()

    def delete(self, rel: str) -> None:
        try:
            self._path(rel).unlink()
        except FileNotFoundError:
            pass

    def list(self, prefix: str = "") -> Iterator[str]:
        base = self.root.resolve()
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(base).as_posix()
                if rel.startswith(prefix):
                    yield rel

    def holds(self, uri: str) -> bool:
        text = str(uri)
        if text.startswith("file://"):
            from urllib.request import url2pathname

            text = url2pathname(text[7:])
        elif "://" in text:
            return False
        try:
            Path(text).resolve().relative_to(self.root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def describe(self) -> str:
        return str(self.root)


class S3Files:
    """A bucket, or a prefix in one, through a boto3 S3 client the host built."""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _key(self, rel: str) -> str:
        return self.prefix + rel

    def read(self, rel: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(rel))["Body"].read()

    def write(self, rel: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(rel), Body=data)

    def exists(self, rel: str) -> bool:
        return any(True for key in self.list(rel) if key == rel)

    def delete(self, rel: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(rel))

    def list(self, prefix: str = "") -> Iterator[str]:
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": self._key(prefix)}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for item in page.get("Contents") or []:
                yield str(item["Key"])[len(self.prefix) :]
            token = page.get("NextContinuationToken")
            if not token:
                return

    def holds(self, uri: str) -> bool:
        parsed = urlparse(str(uri))
        if parsed.scheme != "s3" or parsed.netloc != self.bucket:
            return False
        return parsed.path.lstrip("/").startswith(self.prefix)

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"


class BlobFiles:
    """A container, or a prefix in one, through a ``BlobServiceClient``."""

    def __init__(self, client: Any, container: str, prefix: str = "") -> None:
        self.client = client
        self.container = container
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _blob(self, rel: str) -> Any:
        return self.client.get_blob_client(container=self.container, blob=self.prefix + rel)

    def read(self, rel: str) -> bytes:
        return self._blob(rel).download_blob().readall()

    def write(self, rel: str, data: bytes) -> None:
        self._blob(rel).upload_blob(data, overwrite=True)

    def exists(self, rel: str) -> bool:
        return bool(self._blob(rel).exists())

    def delete(self, rel: str) -> None:
        blob = self._blob(rel)
        if blob.exists():
            blob.delete_blob()

    def list(self, prefix: str = "") -> Iterator[str]:
        container = self.client.get_container_client(self.container)
        for item in container.list_blobs(name_starts_with=self.prefix + prefix):
            name = item["name"] if isinstance(item, Mapping) else item.name
            yield str(name)[len(self.prefix) :]

    def holds(self, uri: str) -> bool:
        parsed = urlparse(str(uri))
        if parsed.scheme not in ("http", "https"):
            return False
        parts = PurePosixPath(parsed.path.lstrip("/")).parts
        if not parts or parts[0] != self.container:
            return False
        return "/".join(parts[1:]).startswith(self.prefix)

    def describe(self) -> str:
        return f"blob:{self.container}/{self.prefix}"


# ============================================================================
# THE STORE: one Markdown file a document, and the figures it names
# ============================================================================
#
# INPUT   a document and its figures; an id
# OUTPUT  the Markdown kept with its front matter, the index entry, and the
#         figures beside it; a document read back with its pages; a document
#         deleted, a marker left
#
# Chunks are what the index holds. The document they were cut from is gone the
# moment it is chunked, unless it is kept here, and then it can be cut again
# without reading it twice.


class DocumentStore:
    """One Markdown file per document, and the figures it names.

    ``files`` is a :class:`LocalFiles`, :class:`S3Files` or :class:`BlobFiles`,
    or anything with their six methods. ``retain_days`` is how long a deleted
    document stays under ``_deleted`` before :meth:`purge_deleted` removes
    it; ``None`` keeps it for ever, which is the default, because deciding
    that an extraction nobody can repeat may be thrown away is not a
    decision for a library. ``keep_deleted=False`` is that decision made: a
    deleted document is removed at once, its Markdown and its figures, and no
    copy is kept under ``_deleted``, for a host whose rule is that a document
    deleted at its source is gone everywhere.
    """

    def __init__(self, files: Any, retain_days: Optional[int] = None, *, keep_deleted: bool = True) -> None:
        for method in ("read", "write", "exists", "delete", "list", "holds"):
            if not callable(getattr(files, method, None)):
                raise ConfigurationError(f"a document store needs files with a {method}() method")
        self.files = files
        self.retain_days = retain_days
        self.keep_deleted = bool(keep_deleted)
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        where = self.files.describe() if callable(getattr(self.files, "describe", None)) else type(self.files).__name__
        return f"DocumentStore({where})"

    # -- the listing

    def _load_index(self) -> Dict[str, Any]:
        try:
            index = json.loads(self.files.read(INDEX).decode("utf-8"))
            if isinstance(index, dict) and isinstance(index.get("documents"), dict):
                index.setdefault("deleted", [])
                return index
        except Exception:
            pass
        return self.reindex(write=False)

    def _save_index(self, index: Dict[str, Any]) -> None:
        index["version"] = 1
        self.files.write(INDEX, json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    def reindex(self, write: bool = True) -> Dict[str, Any]:
        """The listing, rebuilt from the front matter of the files."""
        documents: Dict[str, Any] = {}
        for rel in self.files.list(""):
            if not rel.endswith(".md") or rel.startswith(DELETED + "/") or ".figures/" in rel:
                continue
            try:
                front, _body = split_front_matter(self.files.read(rel).decode("utf-8", errors="replace"))
            except Exception:
                continue
            doc_id = front.get("doc_id")
            if front.get("vectrixdb") != "extracted" or not isinstance(doc_id, str):
                continue
            documents[doc_id] = self._entry_from(front, rel)
        index = {"documents": documents, "deleted": []}
        if write:
            with self._lock:
                self._save_index(index)
        return index

    @staticmethod
    def _entry_from(front: Mapping[str, Any], rel: str) -> Dict[str, Any]:
        keys = ("source", "filename", "kind", "source_version", "source_sha", "version", "extractor", "extracted_at", "chunking", "user_metadata", "figure_files", "masking")
        entry = {k: front.get(k) for k in keys if front.get(k) is not None}
        entry["file"] = rel
        return entry

    # -- writing

    def put(
        self,
        doc_id: str,
        doc: LoadedDocument,
        *,
        source_version: Optional[str] = None,
        chunking: Optional[Dict[str, Any]] = None,
        user_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Keep ``doc`` under ``doc_id``, replacing whatever was kept before."""
        import hashlib

        with self._lock:
            index = self._load_index()
            previous = index["documents"].get(doc_id)
            rel = (previous or {}).get("file") or self._free_name(doc_id, doc, index)
            figure_files: Dict[str, str] = {}
            folder = rel[: -len(".md")] + ".figures/"
            for number, (src, data) in enumerate(sorted(doc.images.items()), start=1):
                # The image keeps the name its document gave it, p4-fig1.png,
                # unless two figures share one.
                base = quote(PurePosixPath(src.split("?")[0]).name, safe="()+,=@!~'") or f"fig{number}.png"
                name = folder + base
                if name in figure_files.values():
                    name = folder + f"{number}-{base}"
                self.files.write(name, data)
                figure_files[src] = name
            for stale in set(((previous or {}).get("figure_files") or {}).values()) - set(figure_files.values()):
                self.files.delete(stale)
            extra = {
                "doc_id": doc_id,
                "source": doc.metadata.get("source"),
                "filename": doc.metadata.get("filename"),
                "kind": doc.metadata.get("kind"),
                "source_version": source_version,
                "version": hashlib.sha256(doc.text.encode()).hexdigest()[:16],
                "extractor": doc.metadata.get("extractor"),
                "extracted_at": _utc().isoformat(timespec="seconds"),
                # What the reader masked before this copy was kept: counts and a risk score, never the text.
                "masking": doc.metadata.get("masking") if isinstance(doc.metadata.get("masking"), dict) else None,
                "chunking": chunking or None,
                "user_metadata": user_metadata or None,
                "figure_files": figure_files or None,
            }
            extra = {k: v for k, v in extra.items() if v is not None}
            self.files.write(rel, doc.to_markdown(extra).encode("utf-8"))
            entry = self._entry_from(extra, rel)
            index["documents"][doc_id] = entry
            self._save_index(index)
            return dict(entry)

    def _free_name(self, doc_id: str, doc: LoadedDocument, index: Dict[str, Any]) -> str:
        taken = {e.get("file") for e in index["documents"].values()}
        rel = stored_name(doc_id)
        if rel not in taken and not self.files.exists(rel):
            return rel
        import hashlib

        return rel[: -len(".md")] + "~" + hashlib.sha256(doc_id.encode()).hexdigest()[:8] + ".md"

    def touch(self, doc_id: str, *, source_version: str, source_sha: Optional[str] = None) -> None:
        """Record that the original changed and its text did not; ``source_sha`` is the hash of its bytes, so the same bytes under a new version are not read again."""
        with self._lock:
            index = self._load_index()
            entry = index["documents"].get(doc_id)
            if entry is None:
                return
            text = self.files.read(entry["file"]).decode("utf-8")
            front, body = split_front_matter(text)
            front["source_version"] = source_version
            if source_sha:
                front["source_sha"] = source_sha
            from .ingest import _front_matter_text

            self.files.write(entry["file"], (_front_matter_text(dict(front)) + body).encode("utf-8"))
            entry["source_version"] = source_version
            if source_sha:
                entry["source_sha"] = source_sha
            self._save_index(index)

    # -- reading

    def ids(self) -> List[str]:
        return sorted(self._load_index()["documents"])

    def __contains__(self, doc_id: object) -> bool:
        return isinstance(doc_id, str) and doc_id in self._load_index()["documents"]

    def __len__(self) -> int:
        return len(self._load_index()["documents"])

    def entry(self, doc_id: str) -> Optional[Dict[str, Any]]:
        found = self._load_index()["documents"].get(doc_id)
        return dict(found) if found else None

    def entries(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self._load_index()["documents"].items()}

    def markdown(self, doc_id: str) -> str:
        entry = self.entry(doc_id)
        if entry is None:
            raise DocumentNotFoundError(doc_id)
        return self.files.read(entry["file"]).decode("utf-8")

    def get(self, doc_id: str, images: bool = False) -> LoadedDocument:
        entry = self.entry(doc_id)
        if entry is None:
            raise DocumentNotFoundError(doc_id)
        doc = LoadedDocument.from_markdown(self.files.read(entry["file"]).decode("utf-8"))
        if images:
            for src, rel in (entry.get("figure_files") or {}).items():
                try:
                    doc.images[src] = self.files.read(rel)
                except Exception:
                    continue
        return doc

    def figure_bytes(self, doc_id: str, src: str) -> Optional[bytes]:
        entry = self.entry(doc_id) or {}
        rel = (entry.get("figure_files") or {}).get(src)
        if rel is None:
            return None
        try:
            return self.files.read(rel)
        except Exception:
            return None

    def figure_file(self, doc_id: str, src: str) -> Optional[str]:
        return ((self.entry(doc_id) or {}).get("figure_files") or {}).get(src)

    def holds(self, uri: str) -> bool:
        """Is this path or address inside the store? A watcher and a worker
        ask before they ingest anything, so the store's own files never
        come round again as new documents."""
        return bool(self.files.holds(uri))

    # -- deleting

    def delete(self, doc_id: str) -> bool:
        """Move a document under ``_deleted``. It is kept, not thrown away:
        what was indexed is part of the record of what was answered from.
        With ``keep_deleted=False`` it is removed instead, and nothing is kept."""
        with self._lock:
            index = self._load_index()
            entry = index["documents"].pop(doc_id, None)
            if entry is None:
                return False
            if not self.keep_deleted:
                for rel in [entry["file"], *(entry.get("figure_files") or {}).values()]:
                    self.files.delete(rel)
                self._save_index(index)
                return True
            stamp = _utc().strftime("%Y%m%dT%H%M%SZ")
            moved: List[str] = []
            for rel in [entry["file"], *(entry.get("figure_files") or {}).values()]:
                try:
                    target = f"{DELETED}/{stamp}/{rel}"
                    self.files.write(target, self.files.read(rel))
                    self.files.delete(rel)
                    moved.append(target)
                except Exception:
                    continue
            index["deleted"].append({"doc_id": doc_id, "deleted_at": _utc().isoformat(timespec="seconds"), "files": moved})
            self._save_index(index)
            return True

    def purge_deleted(self, older_than_days: Optional[int] = None) -> int:
        """Remove deleted documents older than ``older_than_days``, or than
        ``retain_days``. With neither set nothing is removed."""
        days = older_than_days if older_than_days is not None else self.retain_days
        if days is None:
            return 0
        cutoff = _utc() - timedelta(days=days)
        removed = 0
        with self._lock:
            index = self._load_index()
            kept = []
            for record in index["deleted"]:
                try:
                    when = datetime.fromisoformat(record["deleted_at"])
                except (KeyError, ValueError):
                    kept.append(record)
                    continue
                if when <= cutoff:
                    for rel in record.get("files") or []:
                        self.files.delete(rel)
                    removed += 1
                else:
                    kept.append(record)
            index["deleted"] = kept
            self._save_index(index)
        return removed


# ============================================================================
# THE CHUNKS KEPT, AND THE OPENERS
# ============================================================================
#
# INPUT   a document's id; what Vectrix(keep_source=, keep_chunks=) was given
# OUTPUT  where a document's chunks go, one JSON Lines file a document; the
#         store either was given, opened; the documents matching a where
#
# A value stays what JSON holds it as, whichever library read it.


def chunks_name(doc_id: str) -> str:
    """Where a document's chunks go: its Markdown's name, with ``.jsonl`` for ``.md``.

    ``inbox/acme/scan.pdf`` is ``inbox/acme/scan.pdf.jsonl``, so the chunks
    of an original sit at the same path as its Markdown, in a root of their
    own.
    """
    return stored_name(doc_id)[: -len(".md")] + ".jsonl"


def _plain(value: Any) -> Any:
    """``value`` as JSON holds it. A number stays a number, whichever library
    made it: a NumPy one becomes Python's own. Anything else JSON cannot hold
    becomes its string."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return _plain(tolist())
    return str(value)


class ChunkStore:
    """One JSON Lines file per document: the chunks it was cut into, a line each.

    Each line is a chunk as it was written to the index, before anything
    embedded it: its id, its text, and its metadata, which says where in the
    document it came from, the page, the heading, its place in the order and
    the section it belongs to. So what was indexed can be read, counted and
    compared without the index.

    A document cut again is written again, and a deleted document's file goes
    with it: the chunks can always be cut again from the kept Markdown, so
    nothing is lost that the document store does not keep. ``files`` is a
    :class:`LocalFiles`, :class:`S3Files` or :class:`BlobFiles`, in a root of
    its own, like the document store's.
    """

    def __init__(self, files: Any) -> None:
        for method in ("read", "write", "exists", "delete", "holds"):
            if not callable(getattr(files, method, None)):
                raise ConfigurationError(f"a chunk store needs files with a {method}() method")
        self.files = files

    def __repr__(self) -> str:
        where = self.files.describe() if callable(getattr(self.files, "describe", None)) else type(self.files).__name__
        return f"ChunkStore({where})"

    def put(self, doc_id: str, ids: Sequence[str], texts: Sequence[str], metadata: Sequence[Mapping[str, Any]]) -> str:
        """A document's chunks, replacing whatever was written for it before. Returns the file's name."""
        lines = [
            json.dumps({"id": str(i), "text": t, "metadata": _plain(dict(m or {}))}, ensure_ascii=False, sort_keys=True)
            for i, t, m in zip(ids, texts, metadata)
        ]
        rel = chunks_name(doc_id)
        self.files.write(rel, ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))
        return rel

    def get(self, doc_id: str) -> List[Dict[str, Any]]:
        """A document's chunks, in the order they were cut."""
        rel = chunks_name(doc_id)
        if not self.files.exists(rel):
            raise DocumentNotFoundError(doc_id)
        return [json.loads(line) for line in self.files.read(rel).decode("utf-8").splitlines() if line.strip()]

    def __contains__(self, doc_id: object) -> bool:
        return isinstance(doc_id, str) and bool(self.files.exists(chunks_name(doc_id)))

    def delete(self, doc_id: str) -> bool:
        rel = chunks_name(doc_id)
        if not self.files.exists(rel):
            return False
        self.files.delete(rel)
        return True

    def holds(self, uri: str) -> bool:
        """Is this path or address inside the store? Asked for the same reason the document store is."""
        return bool(self.files.holds(uri))


def open_chunks(keep_chunks: Any, default_root: Optional[Path]) -> Optional[ChunkStore]:
    """What ``Vectrix(keep_chunks=...)`` was given, as a store."""
    if keep_chunks in (None, False):
        return None
    if isinstance(keep_chunks, ChunkStore):
        return keep_chunks
    if keep_chunks is True:
        if default_root is None:
            raise ConfigurationError(
                "keep_chunks=True keeps chunks beside the collection, and this collection has no path; "
                "pass a folder, or a ChunkStore"
            )
        return ChunkStore(LocalFiles(default_root))
    if isinstance(keep_chunks, (str, Path)):
        return ChunkStore(LocalFiles(keep_chunks))
    return ChunkStore(keep_chunks)


def open_store(keep_source: Any, default_root: Optional[Path]) -> Optional[DocumentStore]:
    """What ``Vectrix(keep_source=...)`` was given, as a store."""
    if keep_source in (None, False):
        return None
    if isinstance(keep_source, DocumentStore):
        return keep_source
    if keep_source is True:
        if default_root is None:
            raise ConfigurationError(
                "keep_source=True keeps documents beside the collection, and this collection has "
                "no path; pass a folder, or a DocumentStore"
            )
        return DocumentStore(LocalFiles(default_root))
    if isinstance(keep_source, (str, Path)):
        return DocumentStore(LocalFiles(keep_source))
    return DocumentStore(keep_source)


Where = Callable[[Dict[str, Any]], bool]


def matching(store: DocumentStore, where: Optional[Where], ids: Optional[Iterable[str]] = None) -> List[str]:
    wanted = list(ids) if ids is not None else store.ids()
    if where is None:
        return wanted
    entries = store.entries()
    return [i for i in wanted if i in entries and where({"doc_id": i, **entries[i]})]
