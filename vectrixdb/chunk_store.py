"""A copy of every chunk, shared by every process that writes a collection, for the collection pages.

A collection keeps each chunk's text and metadata in a SQLite table beside the
process, and the collection pages are drawn from it: the count, the builds,
growth by day, extraction quality, the points, a chunk's provenance and a
document's chunks. A process that does its own writing sees everything that
way. A deployment that scales out does not. The instance that ingested a
document is not the one drawing the page, and the table beside that one holds
only what it wrote itself, which on a fresh instance is nothing. Every page is
then empty, and nothing says why.

A chunk store is the copy they share. Every write to the collection reaches
it: the chunks added, with their text, their metadata and when each was first
written; the chunks deleted; the metadata changed. The pages read it instead
of the table. What a search reads does not change. The index answers
questions, and nothing here is searched.

Where it lives is the choice of whoever opens the collection, and nothing in
the library assumes one cloud:

    chunk_store="cosmos://<account>.documents.azure.com/<database>/<container>"
    chunk_store=your_store     anything whose collection(name) answers CollectionChunks

Cosmos DB is the one built in: one container for every collection,
partitioned by collection, so a page reads one partition. A store of your own
is any object with ``collection(name)`` returning something with the methods
of :class:`CollectionChunks`. It has to answer a count, a page of ids, one
chunk by id and one document's chunks without reading everything, so it is a
database of some kind: DynamoDB and a PostgreSQL table both can. An object
store, S3 or Blob, holds files rather than rows, and could answer only by
reading every file on every page.

Not ``keep_chunks``, which keeps a file a document so what was cut can be read
back. This keeps a row a chunk, for the pages.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, Iterator, List, Mapping, Optional, Protocol, Sequence, Tuple, cast
from urllib.parse import urlsplit

from ._time import parse_iso, utcnow, utcnow_iso
from .exceptions import ConfigurationError, DependencyError

if TYPE_CHECKING:  # pragma: no cover
    from .core.types import Point

__all__ = ["CollectionChunks", "CosmosChunks", "INDEXING", "describe", "open_chunk_store"]


# ============================================================================
# WHAT A STORE KEEPS FOR ONE COLLECTION
# ============================================================================
#
# INPUT   a collection's name
# OUTPUT  the rows the pages read, and the writes that keep them
#
# A collection keeps each chunk's text and metadata in a SQLite table beside
# the process; a shared store keeps a copy every process that writes the
# collection can read.


class CollectionChunks(Protocol):
    """What a chunk store keeps for one collection: the rows the pages read, and the writes that keep them.

    A store's ``collection(name)`` returns one of these. Ids are the
    collection's own point ids. Times are ISO 8601 in UTC with ``+00:00`` on
    the end, as the collection writes them.
    """

    def put(
        self, ids: Sequence[str], texts: Sequence[Optional[str]], metadata: Sequence[Mapping[str, Any]], written: str
    ) -> None:
        """These chunks, replacing any under the same ids. One already here keeps the time it was first written; a new one takes ``written``."""

    def delete(self, ids: Sequence[str]) -> int:
        """Remove these chunks. Returns how many were here."""

    def update(self, id: str, metadata: Mapping[str, Any], merge: bool = True) -> bool:
        """One chunk's metadata merged with ``metadata``, or replaced by it when ``merge`` is False. False when it is not here."""

    def clear(self) -> int:
        """Remove every chunk of the collection. Returns how many there were."""

    def count(self) -> int:
        """How many chunks the collection has."""

    def changed_at(self) -> Optional[str]:
        """When a chunk was last written or changed, or None when there are none."""

    def written(self) -> Iterable[Tuple[str, Dict[str, Any]]]:
        """Every chunk's time of first writing, with its ``_vx_build`` and ``_vx_quality`` where it has them."""

    def scores(self) -> Iterable[Tuple[str, Optional[float]]]:
        """Every chunk's id and extraction quality, None where it has none."""

    def get(self, id: str) -> Optional["Point"]:
        """One chunk with its text and metadata and no vector, or None when it is not here."""

    def ids(self, limit: int, offset: int) -> List[str]:
        """A page of ids, in an order that holds from one page to the next."""

    def each(self) -> Iterable[Tuple[str, Dict[str, Any]]]:
        """Every id with its metadata, in the order :meth:`ids` pages them."""

    def of_document(self, doc_id: str) -> List[str]:
        """The ids of one document's chunks: the ones whose ``_vx_doc`` it is."""


# ============================================================================
# COSMOS: the queries, the batches, and the store
# ============================================================================
#
# INPUT   a Cosmos DB for NoSQL container, partitioned by collection
# OUTPUT  the queries the pages need, each on its partition and no other;
#         operations cut into batches Cosmos takes, a hundred at most and
#         under a megabyte; the store, and one collection's chunks in it
#
# A point id can be any string; an item id may not hold slashes, question
# marks or hashes and holds 255 characters at most, so the key is derived from
# the id.

#: A transactional batch takes at most this many operations, and this many
#: bytes. The SDK documents 1.2 MB, and this stays under it.
_BATCH_OPERATIONS = 100
_BATCH_BYTES = 1_000_000
#: Ids looked up in one query.
_LOOKUP = 100
#: Rows asked for in one page of a query that reads every chunk.
_PAGE = 1000

#: How the container is indexed: every path a query filters or sorts by, and
#: not the text or the metadata. No query reads those by value, and they are
#: most of what indexing a write costs.
INDEXING: Dict[str, Any] = {
    "indexingMode": "consistent",
    "automatic": True,
    "includedPaths": [{"path": "/*"}],
    "excludedPaths": [{"path": "/text/?"}, {"path": "/metadata/*"}, {"path": '/"_etag"/?'}],
}

# Every query runs inside one collection's partition, and none groups:
# Python's Cosmos SDK does not run GROUP BY, so a total over builds or days is
# added up by the page from a small projection of each chunk.
_COUNT = "SELECT VALUE COUNT(1) FROM c"
_CHANGED = "SELECT VALUE MAX(c._ts) FROM c"
_WRITTEN = "SELECT c.written, c.build, c.quality FROM c"
_SCORES = "SELECT c.point, c.quality FROM c"
_PAGE_OF_IDS = "SELECT VALUE c.point FROM c ORDER BY c.id OFFSET @offset LIMIT @limit"
_EACH = "SELECT c.point, c.metadata FROM c ORDER BY c.id"
_OF_DOCUMENT = "SELECT VALUE c.point FROM c WHERE c.doc = @doc"
_HELD = "SELECT c.id, c.written FROM c WHERE ARRAY_CONTAINS(@ids, c.id)"
_EVERY_ID = "SELECT VALUE c.id FROM c"


def _status(exc: BaseException) -> Optional[int]:
    return getattr(exc, "status_code", None)


def _key(point_id: str) -> str:
    """The item id for a point id. An item id may not hold / \\ ? or #, and holds 255 characters at most; a point id can be any string."""
    return hashlib.sha256(str(point_id).encode("utf-8")).hexdigest()


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _if_unchanged() -> Any:
    try:
        from azure.core import MatchConditions
    except ImportError:  # only where the container is not the SDK's
        return "IfNotModified"
    return MatchConditions.IfNotModified


def _weight(operation: Tuple[str, Tuple[Any, ...]]) -> int:
    """About how many bytes an operation adds to a batch."""
    kind, args = operation
    return len(json.dumps(args[0], default=str)) if kind == "upsert" else 100


def _batches(operations: Sequence[Tuple[str, Tuple[Any, ...]]]) -> Iterator[List[Tuple[str, Tuple[Any, ...]]]]:
    """Operations cut into batches Cosmos takes: a hundred at most, and under a megabyte."""
    group: List[Tuple[str, Tuple[Any, ...]]] = []
    size = 0
    for operation in operations:
        weight = _weight(operation)
        if group and (len(group) >= _BATCH_OPERATIONS or size + weight > _BATCH_BYTES):
            yield group
            group, size = [], 0
        group.append(operation)
        size += weight
    if group:
        yield group


class CosmosChunks:
    """A chunk store in one Cosmos DB for NoSQL container, partitioned by collection.

    An item a chunk: an id that is a hash of the point id, so Cosmos can hold
    it whatever the point id is; the collection; the point id; the document
    it came from; its build; its quality; when it was first written; its text;
    and its metadata. No vector. The index has those, and nothing here is
    searched.
    """

    def __init__(self, container: Any, *, where: str = "Cosmos DB") -> None:
        self._container = container
        self._where = where

    @classmethod
    def open(cls, url: str, *, key: Optional[str] = None) -> "CosmosChunks":
        """The store at ``cosmos://<account>.documents.azure.com/<database>/<container>``.

        ``key`` is the account's key. Left out, the default Azure credential is
        used, which inside Azure is the managed identity, and that identity
        needs a Cosmos DB data role on the account. A container that is not
        there is made, partitioned by ``/collection``, when the credential may
        make one.
        """
        try:
            from azure.cosmos import CosmosClient, PartitionKey
        except ImportError as exc:
            raise DependencyError("azure-cosmos", "azure") from exc
        parts = urlsplit(url)
        names = [part for part in parts.path.split("/") if part]
        if not parts.hostname or len(names) != 2:
            raise ConfigurationError(
                "A chunk store in Cosmos DB reads cosmos://<account>.documents.azure.com/<database>/<container>"
            )
        credential: Any = key
        if not credential:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:
                raise DependencyError("azure-identity", "azure") from exc
            credential = DefaultAzureCredential()
        client = CosmosClient(f"https://{parts.hostname}:{parts.port or 443}/", credential=credential)
        database_name, container_name = names
        container = client.get_database_client(database_name).get_container_client(container_name)
        try:
            properties = container.read()
        except Exception as exc:
            if _status(exc) != 404:
                raise
            try:
                database = client.create_database_if_not_exists(database_name)
                container = database.create_container_if_not_exists(
                    id=container_name, partition_key=PartitionKey(path="/collection"), indexing_policy=INDEXING
                )
            except Exception as refused:
                raise ConfigurationError(
                    f"The Cosmos DB container {database_name}/{container_name} is not there, and this may not "
                    "make it. Make it with the partition key /collection, then start again"
                ) from refused
            properties = cast(Any, {"partitionKey": {"paths": ["/collection"]}})
        paths = list((cast(Any, properties or {}).get("partitionKey") or {}).get("paths") or [])
        if paths and paths != ["/collection"]:
            raise ConfigurationError(
                f"The Cosmos DB container {database_name}/{container_name} is partitioned by {', '.join(paths)}. "
                "A chunk store needs /collection, because every page asks one collection's partition"
            )
        return cls(container, where=f"Cosmos DB {parts.hostname}/{database_name}/{container_name}")

    def collection(self, name: str) -> "_CosmosCollection":
        """One collection's chunks."""
        return _CosmosCollection(self._container, name)

    def describe(self) -> str:
        """Where the chunks are, for a log line on a cold start."""
        return self._where

    def __repr__(self) -> str:
        return f"CosmosChunks({self._where})"


class _CosmosCollection:
    """One collection's chunks in the container. Every call reads or writes its partition and no other."""

    def __init__(self, container: Any, name: str) -> None:
        self._container = container
        self.name = name

    def __repr__(self) -> str:
        return f"CosmosChunks.collection({self.name!r})"

    # -------------------------------------------------------------- writes

    def put(
        self, ids: Sequence[str], texts: Sequence[Optional[str]], metadata: Sequence[Mapping[str, Any]], written: str
    ) -> None:
        rows: Dict[str, Tuple[str, Optional[str], Mapping[str, Any]]] = {}
        for point_id, text, meta in zip(ids, texts, metadata):
            # The last of two under one id wins, as it does in the collection.
            rows[_key(point_id)] = (str(point_id), text, meta or {})
        if not rows:
            return
        first = self._held(list(rows))
        self._write(
            [("upsert", (self._item(key, point, text, meta, first.get(key) or written),)) for key, (point, text, meta) in rows.items()]
        )

    def delete(self, ids: Sequence[str]) -> int:
        here = list(self._held(list(dict.fromkeys(_key(point_id) for point_id in ids))))
        self._write([("delete", (key,)) for key in here])
        return len(here)

    def update(self, id: str, metadata: Mapping[str, Any], merge: bool = True) -> bool:
        key = _key(id)
        for _ in range(3):
            try:
                item = self._container.read_item(item=key, partition_key=self.name)
            except Exception as exc:
                if _status(exc) == 404:
                    return False
                raise
            changed = {**dict(item.get("metadata") or {}), **dict(metadata)} if merge else dict(metadata)
            body = self._item(key, str(item.get("point") or id), item.get("text"), changed, str(item.get("written") or utcnow_iso()))
            try:
                self._container.replace_item(item=key, body=body, etag=item.get("_etag"), match_condition=_if_unchanged())
            except Exception as exc:
                if _status(exc) == 404:
                    return False
                # Changed by another process between the read and this write: read it again.
                if _status(exc) == 412:
                    continue
                raise
            return True
        raise RuntimeError(f"chunk {id!r} changed three times while this wrote it")

    def clear(self) -> int:
        keys = [str(key) for key in self._query(_EVERY_ID)]
        self._write([("delete", (key,)) for key in keys])
        return len(keys)

    def _item(self, key: str, point_id: str, text: Optional[str], meta: Mapping[str, Any], written: str) -> Dict[str, Any]:
        from .documents import _plain

        metadata = _plain(dict(meta or {}))
        doc, build = metadata.get("_vx_doc"), metadata.get("_vx_build")
        return {
            "id": key,
            "collection": self.name,
            "point": point_id,
            "doc": doc if isinstance(doc, str) else None,
            "build": build if isinstance(build, str) else None,
            "quality": _number(metadata.get("_vx_quality")),
            "written": written,
            "text": text or "",
            "metadata": metadata,
        }

    def _held(self, keys: Sequence[str]) -> Dict[str, str]:
        """Which of these item ids are here already, with when each was first written."""
        held: Dict[str, str] = {}
        for start in range(0, len(keys), _LOOKUP):
            for row in self._query(_HELD, ids=list(keys[start : start + _LOOKUP])):
                held[str(row["id"])] = str(row.get("written") or "")
        return held

    def _write(self, operations: List[Tuple[str, Tuple[Any, ...]]]) -> None:
        """Operations in transactional batches, or one at a time on an SDK too old to batch.

        An item too big for a batch goes on its own.
        """
        batch = getattr(self._container, "execute_item_batch", None)
        if not callable(batch):
            for operation in operations:
                self._one(*operation)
            return
        small = []
        for operation in operations:
            if _weight(operation) > _BATCH_BYTES:
                self._one(*operation)
            else:
                small.append(operation)
        for group in _batches(small):
            try:
                batch(batch_operations=group, partition_key=self.name)
            except Exception as exc:
                # A delete of what another process deleted a moment ago fails
                # the whole batch. The rest still go, one at a time.
                if _status(exc) == 404 and all(kind == "delete" for kind, _ in group):
                    for operation in group:
                        self._one(*operation)
                    continue
                raise

    def _one(self, kind: str, args: Tuple[Any, ...]) -> None:
        if kind == "upsert":
            self._container.upsert_item(body=args[0])
            return
        try:
            self._container.delete_item(item=args[0], partition_key=self.name)
        except Exception as exc:
            if _status(exc) != 404:
                raise

    # --------------------------------------------------------------- reads

    def count(self) -> int:
        # An aggregate can come back as one partial answer a page. Together they are the answer.
        return int(sum(value for value in self._query(_COUNT) if _number(value) is not None))

    def changed_at(self) -> Optional[str]:
        stamps = [value for value in self._query(_CHANGED) if _number(value) is not None]
        return datetime.fromtimestamp(max(stamps), timezone.utc).isoformat() if stamps else None

    def written(self) -> Iterator[Tuple[str, Dict[str, Any]]]:
        for row in self._query(_WRITTEN):
            stamps: Dict[str, Any] = {}
            if row.get("build"):
                stamps["_vx_build"] = row["build"]
            if _number(row.get("quality")) is not None:
                stamps["_vx_quality"] = row["quality"]
            yield row.get("written"), stamps

    def scores(self) -> Iterator[Tuple[str, Optional[float]]]:
        for row in self._query(_SCORES):
            yield str(row.get("point")), _number(row.get("quality"))

    def get(self, id: str) -> Optional["Point"]:
        from .core.types import Point

        try:
            item = self._container.read_item(item=_key(id), partition_key=self.name)
        except Exception as exc:
            if _status(exc) == 404:
                return None
            raise
        changed = _number(item.get("_ts"))
        return Point(
            id=str(item.get("point") or id),
            vector=[],
            metadata=dict(item.get("metadata") or {}),
            text=item.get("text"),
            created_at=parse_iso(item.get("written")) or utcnow(),
            updated_at=datetime.fromtimestamp(changed, timezone.utc) if changed is not None else None,
        )

    def ids(self, limit: int, offset: int) -> List[str]:
        return [str(value) for value in self._query(_PAGE_OF_IDS, offset=int(offset), limit=int(limit))]

    def each(self) -> Iterator[Tuple[str, Dict[str, Any]]]:
        for row in self._query(_EACH):
            yield str(row.get("point")), dict(row.get("metadata") or {})

    def of_document(self, doc_id: str) -> List[str]:
        return [str(value) for value in self._query(_OF_DOCUMENT, doc=str(doc_id))]

    def _query(self, text: str, **parameters: Any) -> Iterable[Any]:
        return self._container.query_items(
            query=text,
            parameters=[{"name": f"@{name}", "value": value} for name, value in parameters.items()],
            partition_key=self.name,
            max_item_count=_PAGE,
        )


# ============================================================================
# OPENING AND DESCRIBING
# ============================================================================
#
# INPUT   an address
# OUTPUT  the chunk store there, or None when there is none; where it keeps
#         its rows, for a log line, nothing opened
#
# None is an answer: a server without a shared store reads the table beside
# the process.


def open_chunk_store(where: Any, *, key: Optional[str] = None) -> Any:
    """The chunk store at ``where``, or None when there is none.

    ``where`` is an address, ``cosmos://<account>.documents.azure.com/<database>/<container>``,
    or a store already made: anything with ``collection(name)``. ``key`` is the
    Cosmos account's key. Left out, the default Azure credential is used,
    which inside Azure is the managed identity.
    """
    if where is None or (isinstance(where, str) and not where.strip()):
        return None
    if not isinstance(where, (str, os.PathLike)):
        if callable(getattr(where, "collection", None)):
            return where
        raise TypeError(
            f"A chunk store is an address or an object with collection(name), not {type(where).__name__}. "
            "keep_chunks is where a file a document goes; a chunk store keeps a row a chunk"
        )
    text = str(where).strip()
    scheme = urlsplit(text).scheme.lower() if "://" in text else ""
    if scheme == "cosmos":
        return CosmosChunks.open(text, key=key)
    raise ConfigurationError(
        "A chunk store is cosmos://<account>.documents.azure.com/<database>/<container>, or an object with "
        f"collection(name) passed as chunk_store=, not {scheme + '://' if scheme else 'a path'}"
    )


def describe(store: Any) -> str:
    """Where a chunk store keeps its rows, for a log line. Nothing is opened."""
    if store is None:
        return "nowhere: the pages read the table beside the process"
    told = getattr(store, "describe", None)
    return str(told()) if callable(told) else type(store).__name__
