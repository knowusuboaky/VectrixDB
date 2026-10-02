"""Lines appended where nothing rewrites them: the audit trail and the access log, in one shape.

Both logs are the same thing underneath: one JSON object a line, appended,
never changed. This is where those lines go, whichever log it is, and the
choice is the host's:

    a path                                                         a file on this machine
    https://<account>.blob.core.windows.net/<container>/<prefix>   Azure Blob, one append blob a day

A file is honest for one server with a disk that lasts. It is not a system of
record on its own: it rotates away, dies with a scale-in, and is rewritable by
whatever can write to it.

A Blob log is one append blob a UTC day, ``<prefix>2026/09/23.jsonl``, each
line one block appended to it. Several servers append to the same blob
safely, because Azure appends a block whole or not at all. The container must
carry an immutability policy: a time-based retention policy with protected
append writes allowed, or version-level immutability. Then a line once
written cannot be changed or deleted until the retention runs out, by the
server that wrote it or anybody else, and the log opens only when that is so,
because a log anybody with the key can rewrite is not the record these are.
An append blob takes 50,000 blocks; a day past that goes on in
``2026/09/23.p001.jsonl``, and so on.

S3 has no append. An audit trail there is one object a record under Object
Lock, :class:`vectrixdb.objectlock.ObjectLockSink`; an access log is better
sent to the server's output and collected, ``VECTRIXDB_ACCESS_LOG=stdout``.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable
from urllib.parse import urlparse

from .exceptions import ConfigurationError

__all__ = ["AppendLog", "BlobDayLog", "FileLog", "is_blob_address", "open_append_log"]


# ============================================================================
# SETTINGS: the block limit, and the name
# ============================================================================
#
# How many blocks an append blob may hold before a new one is started, and the
# name a log goes by.

#: The most blocks Azure lets one append blob take.
BLOCK_LIMIT = 50_000
_NAME = re.compile(r"^(\d{4})/(\d{2})/(\d{2})(?:\.p(\d{3}))?\.jsonl$")


# ============================================================================
# THE SHAPE: somewhere lines are appended and read back
# ============================================================================
#
# INPUT   a line
# OUTPUT  appended, never changed, and read back oldest first
#
# Both logs are the same thing underneath: one JSON object a line.


@runtime_checkable
class AppendLog(Protocol):
    """Somewhere lines are appended and read back, oldest first."""

    def append(self, line: str) -> None:
        """Add one line. It is there, whole, when this returns; otherwise this raises."""

    def lines(self, since: Optional[float] = None) -> List[str]:
        """Every line, oldest first. ``since``, seconds since the epoch, may skip days wholly before it."""

    def describe(self) -> str:
        """Where the lines are, in words fit for a log or a page: never a key."""


# ============================================================================
# A FILE, AND A BLOB A DAY
# ============================================================================
#
# INPUT   a path, or an Azure Blob address
# OUTPUT  lines in one file, each written through to the disk before the call
#         returns; one append blob a UTC day under a prefix, in a container
#         whose policy keeps what is written
#
# What in a container's properties keeps a blob from being changed is read
# first, so a locked container is said, not fought.


def is_blob_address(where: Any) -> bool:
    """Whether ``where`` is an Azure Blob address, ``https://<account>.blob.core.windows.net/...``."""
    parsed = urlparse(str(where))
    return parsed.scheme in ("https", "http") and ".blob." in parsed.netloc


class FileLog:
    """Lines in one file, each written through to the disk before the call returns."""

    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
            handle.flush()
            # A line only in the page cache is not a line in the log.
            os.fsync(handle.fileno())

    def lines(self, since: Optional[float] = None) -> List[str]:
        if not self.path.exists():
            return []
        with self._lock:
            return [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def describe(self) -> str:
        return str(self.path)


def _status(exc: BaseException) -> Optional[int]:
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "error_code", "") or "")


def _locked(properties: Any) -> Dict[str, bool]:
    """What in a container's properties keeps a blob from being changed: empty when nothing does."""

    def read(name: str) -> bool:
        value = properties.get(name) if isinstance(properties, dict) else getattr(properties, name, None)
        return bool(value)

    found = {
        name: read(name)
        for name in ("has_immutability_policy", "immutable_storage_with_versioning_enabled", "has_legal_hold")
    }
    return {name: on for name, on in found.items() if on}


class BlobDayLog:
    """One append blob a UTC day under ``prefix``, in a container whose policy keeps what is written.

    ``container`` is an ``azure.storage.blob.ContainerClient``, or anything
    with its calls. ``require_lock=False`` opens a container with no
    immutability policy, for a test or a host whose control is elsewhere; the
    default refuses one.
    """

    def __init__(
        self,
        container: Any,
        prefix: str = "",
        *,
        where: str = "",
        require_lock: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._container = container
        self._prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self._where = where or f"Blob {getattr(container, 'container_name', 'container')}/{self._prefix}"
        self._clock = clock
        self._parts: Dict[str, int] = {}
        self._lock = threading.Lock()
        self.lock_configuration = _locked(container.get_container_properties())
        if require_lock and not self.lock_configuration:
            raise ConfigurationError(
                f"{self._where} carries no immutability policy, so whoever holds its key could change or "
                "delete what is written there. That is a file, not a record. Give the container a time-based "
                "retention policy that allows protected append writes, or version-level immutability"
            )

    # ------------------------------------------------------------ writing ---

    def _name(self, day: str, part: int) -> str:
        return f"{self._prefix}{day}.jsonl" if part == 0 else f"{self._prefix}{day}.p{part:03d}.jsonl"

    def _create(self, blob: Any) -> None:
        """The day's blob, made only if nobody made it first: making it over one would empty it."""
        try:
            from azure.core import MatchConditions

            condition: Dict[str, Any] = {"match_condition": MatchConditions.IfMissing}
        except ImportError:  # a stand-in client with no azure-core beside it
            condition = {"if_none_match": "*"}
        try:
            blob.create_append_blob(**condition)
        except Exception as exc:
            # Somebody made it a moment ago, which is as good as making it.
            if _status(exc) not in (409, 412):
                raise

    def append(self, line: str) -> None:
        data = (line.rstrip("\n") + "\n").encode("utf-8")
        day = datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y/%m/%d")
        with self._lock:
            part = self._parts.get(day, 0)
        made = False
        for _ in range(1000):
            blob = self._container.get_blob_client(self._name(day, part))
            try:
                blob.append_block(data)
                break
            except Exception as exc:
                if _status(exc) == 404 and not made:
                    # The day's first line, here or anywhere: make the blob, then append as any other.
                    self._create(blob)
                    made = True
                    continue
                if _code(exc) == "BlockCountExceedsLimit":
                    part, made = part + 1, False
                    continue
                if "Immutable" in _code(exc):
                    raise ConfigurationError(
                        f"{self._where}: the container's policy keeps a blob from being appended to. Allow "
                        "protected append writes on its time-based retention policy, and lines can be added "
                        "while nothing already written can be changed"
                    ) from exc
                raise
        else:  # pragma: no cover - a thousand full blobs in one day
            raise ConfigurationError(f"{self._where}: {day} has filled a thousand append blobs")
        with self._lock:
            self._parts[day] = max(self._parts.get(day, 0), part)

    # ------------------------------------------------------------ reading ---

    def _names(self, since: Optional[float]) -> List[str]:
        first: Optional[date] = datetime.fromtimestamp(since, tz=timezone.utc).date() if since is not None else None
        found = []
        for item in self._container.list_blobs(name_starts_with=self._prefix):
            name = str(item["name"] if isinstance(item, dict) else item.name)
            matched = _NAME.match(name[len(self._prefix):])
            if matched is None:
                continue
            day = date(int(matched.group(1)), int(matched.group(2)), int(matched.group(3)))
            if first is not None and day < first:
                continue
            found.append((day, int(matched.group(4) or 0), name))
        return [name for _, _, name in sorted(found)]

    def lines(self, since: Optional[float] = None) -> List[str]:
        out: List[str] = []
        for name in self._names(since):
            body = self._container.get_blob_client(name).download_blob().readall()
            out.extend(line for line in body.decode("utf-8").splitlines() if line.strip())
        return out

    def describe(self) -> str:
        return self._where


# ============================================================================
# OPENING
# ============================================================================
#
# INPUT   a log already made, a Blob address, or a path
# OUTPUT  the log, used as it is or opened
#
# One opener for both kinds.


def open_append_log(where: Any, *, setting: str, require_lock: bool = True) -> AppendLog:
    """``where`` as a log: one already made is used as it is, a Blob address or a path opened.

    ``setting`` names the setting ``where`` came from, for what an address
    that cannot be used is told.
    """
    if isinstance(where, AppendLog) and not isinstance(where, (str, Path)):
        return where
    text = str(where).strip()
    if is_blob_address(text):
        from .evaluation import _blob_client

        parsed = urlparse(text)
        container, _, prefix = parsed.path.lstrip("/").partition("/")
        if not container:
            raise ConfigurationError(f"{setting} is a Blob address with no container in it: {text}")
        account = f"{parsed.scheme}://{parsed.netloc}"
        client = _blob_client(account).get_container_client(container)
        return BlobDayLog(client, prefix, where=f"Blob {parsed.netloc}/{container}/{prefix.strip('/')}".rstrip("/"), require_lock=require_lock)
    if "://" in text:
        raise ConfigurationError(
            f"{setting} is a path or https://<account>.blob.core.windows.net/<container>/<prefix>, not {text.split('://', 1)[0]}://"
        )
    return FileLog(Path(text))
