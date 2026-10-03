# ============================================================================
# THE GRAPH STORE
# ============================================================================
#
# INPUT   where graphs are kept: a folder, s3://bucket/prefix, a Blob address,
#         or a files object of your own; a collection's name; a finished graph
# OUTPUT  one JSON object a collection, <collection>/graph.json, read back by
#         every server that serves the collection, with the build it was read
#         from so a page can say when the index has moved on
#
# A knowledge graph is read from a collection's chunks once and shown many
# times, like an evaluation run is read from the golden questions once. So it
# is kept the way runs are: an object in a store every instance reaches, not a
# SQLite file beside one process. The pipeline keeps its SQLite as the working
# file while it extracts; what it finishes goes here.
#
# Author: Kwadwo Daddy Nyame Owusu - Boakye

"""Where each collection's knowledge graph is kept, for every server that serves it.

``VECTRIXDB_GRAPH_STORE`` takes the three addresses ``VECTRIXDB_EVALUATIONS``
takes: a folder, ``s3://bucket/prefix`` or a Blob address; unset, the server
keeps graphs under ``<path>/graph``. One object a collection,
``<collection>/graph.json``: its entities and relationships, the level-0
community each entity sits in, when it was read, which index build the chunks
carried then, and which model read them. Deleting a collection deletes its
graph.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

__all__ = [
    "GRAPH_STORE_ENV",
    "GraphStore",
    "graph_store",
    "graph_json",
    "nodes_and_edges",
    "is_stale",
    "written_since",
]

#: The setting. A folder, ``s3://bucket/prefix`` or a Blob address; unset, ``<path>/graph`` beside the server.
GRAPH_STORE_ENV = "VECTRIXDB_GRAPH_STORE"

_ENTITY_FIELDS = ("id", "name", "type", "description", "importance")
_RELATIONSHIP_FIELDS = (
    "id",
    "source_id",
    "target_id",
    "type",
    "description",
    "strength",
    "confidence",
    "bidirectional",
    "valid_from",
    "valid_to",
    "superseded_by",
)


# ============================================================================
# THE STORE
# ============================================================================
#
# INPUT   a files object with read, write, exists and delete, and the address
#         it was opened from, for the page to name
# OUTPUT  get, put and delete of one collection's graph


class GraphStore:
    """One JSON object a collection behind any files object with ``read``, ``write``, ``exists`` and ``delete``."""

    def __init__(self, files: Any, where: str = "") -> None:
        self.files = files
        self.where = where

    @staticmethod
    def _rel(collection: str) -> str:
        return f"{collection}/graph.json"

    def get(self, collection: str) -> Optional[Dict[str, Any]]:
        """The kept graph, or None when the collection has none."""
        rel = self._rel(collection)
        try:
            if not self.files.exists(rel):
                return None
            return json.loads(self.files.read(rel).decode("utf-8"))
        except FileNotFoundError:
            return None

    def put(self, collection: str, graph: Dict[str, Any]) -> None:
        """Keep a finished graph, replacing the last one."""
        self.files.write(
            self._rel(collection), json.dumps(graph, ensure_ascii=False).encode("utf-8")
        )

    def delete(self, collection: str) -> bool:
        """Drop a collection's graph; False when there was none."""
        rel = self._rel(collection)
        try:
            if not self.files.exists(rel):
                return False
            self.files.delete(rel)
            return True
        except FileNotFoundError:
            return False

    def describe(self) -> str:
        return self.where


def graph_store(where: Any) -> GraphStore:
    """Open the store at a folder, ``s3://bucket/prefix``, a Blob address, or around a files object of your own."""
    if isinstance(where, GraphStore):
        return where
    if not isinstance(where, (str, os.PathLike)):
        return GraphStore(where, where=type(where).__name__)
    text = str(where)
    from .documents import BlobFiles, LocalFiles, S3Files
    from .evaluation import _blob_account, _blob_client, _s3_client

    if text.startswith("s3://"):
        parsed = urlparse(text)
        return GraphStore(S3Files(_s3_client(), parsed.netloc, parsed.path.lstrip("/")), where=text)
    account = _blob_account(text)
    if account:
        parts = urlparse(text).path.lstrip("/").split("/", 1)
        if not parts[0]:
            raise ValueError(f"{text}: a Blob address names a container")
        return GraphStore(
            BlobFiles(_blob_client(account), parts[0], parts[1] if len(parts) > 1 else ""),
            where=text,
        )
    return GraphStore(LocalFiles(text), where=text)


# ============================================================================
# THE OBJECT
# ============================================================================
#
# INPUT   the pipeline's storage after an extraction; a kept graph and the
#         collection's current build
# OUTPUT  the JSON object; the Cytoscape nodes and edges a page draws;
#         whether the index has moved on since the graph was read


def _pick(obj: Any, fields: Tuple[str, ...]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in fields:
        value = getattr(obj, name, None)
        if value is None and name in ("id", "name", "type", "source_id", "target_id"):
            value = ""
        if value is not None and hasattr(value, "value"):  # an enum, kept as its word
            value = value.value
        if value is not None:
            out[name] = value
    return out


def graph_json(
    collection: str, storage: Any, *, build: Optional[str], model: str
) -> Dict[str, Any]:
    """The object to keep, read off the pipeline's storage after an extraction."""
    entities = [_pick(e, _ENTITY_FIELDS) for e in storage.load_all_entities()]
    relationships = [_pick(r, _RELATIONSHIP_FIELDS) for r in storage.load_all_relationships()]
    communities: Dict[str, int] = {}
    try:
        hierarchy = storage.load_hierarchy()
        if hierarchy is not None:
            for entity_id, levels in hierarchy.entity_to_community.items():
                if 0 in levels:
                    communities[str(entity_id)] = levels[0]
    except Exception:  # noqa: BLE001 - colouring is optional
        communities = {}
    return {
        "collection": collection,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "build": build,
        "model": model,
        "entities": entities,
        "relationships": relationships,
        "communities": communities,
    }


def nodes_and_edges(
    graph: Dict[str, Any], limit: int = 500
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """A kept graph in the shape the page draws: Cytoscape nodes and edges, the first ``limit`` entities."""
    communities = graph.get("communities") or {}
    nodes = [
        {
            "data": {
                "id": e.get("id", ""),
                "label": e.get("name", ""),
                "type": str(e.get("type") or "concept").lower(),
                "description": e.get("description", ""),
                "importance": e.get("importance", 0.5),
                "community": communities.get(str(e.get("id", ""))),
            }
        }
        for e in (graph.get("entities") or [])[:limit]
    ]
    edges = [
        {
            "data": {
                "id": r.get("id", ""),
                "source": r.get("source_id", ""),
                "target": r.get("target_id", ""),
                "label": r.get("type") or "RELATED_TO",
                "description": r.get("description", ""),
                "strength": r.get("strength", 0.5),
                "superseded": bool(r.get("superseded_by")),
                "valid_from": r.get("valid_from"),
                "valid_to": r.get("valid_to"),
            }
        }
        for r in (graph.get("relationships") or [])[: limit * 2]
    ]
    return nodes, edges


def is_stale(graph: Dict[str, Any], current_build: Optional[str]) -> bool:
    """Whether the index has moved on: the collection has a build now, and it is not the one the graph was read from."""
    return bool(current_build) and graph.get("build") != current_build


def _moment(value: Any) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def written_since(graph: Dict[str, Any], rows: Iterator[Any]) -> Dict[str, int]:
    """How many chunks, and how many builds, were written after the graph was read: ``rows`` is each chunk's time and metadata."""
    since = _moment(graph.get("extracted_at"))
    builds, chunks = set(), 0
    if since is None:
        return {"builds": 0, "chunks": 0}
    for written_at, metadata in rows:
        when = _moment(written_at)
        if when is not None and when > since:
            chunks += 1
            build = (metadata or {}).get("_vx_build")
            if build:
                builds.add(str(build))
    return {"builds": len(builds), "chunks": chunks}
