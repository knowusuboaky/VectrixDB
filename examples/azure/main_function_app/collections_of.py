"""Which collection a blob belongs to.

Kept apart from ``function_app.py`` so it can be read and tested without the
Functions runtime, and because this is the part that decides where a document
lands: getting it wrong puts a report in a collection other people can search.

The blob's own path is the answer::

    ingestion/raw/financial/td/ar2025.pdf           ->  financial, as td/ar2025.pdf
    ingestion/raw/media/toddler.mp4                 ->  media
    ingestion/raw/loose.pdf                         ->  nothing: no collection is named
    ingestion/markdown/financial/td/ar2025.pdf.md   ->  nothing: the function wrote it
    ingestion/chunks/financial/td/ar2025.pdf.jsonl  ->  nothing: the function wrote it

The last two matter most. The Markdown and the chunks the function writes
live in the same container, so anything outside raw/ is refused here even if
an event for it arrives: reading it would write it again, and round it would
go.

A path that names no collection is ignored rather than guessed at. A guess
here is a document in the wrong index, which nobody notices until somebody
searches for it in the right one and does not find it.

Who may retrieve from a collection is its record's to say: one record a
collection in Cosmos DB ``access/collection_records``, written by steps 04 and
07 from ``.local/cosmosdb/data_db/collection_records`` or by the dashboard, and
read by the library itself before anything is searched. Nothing here builds a
rule, and nothing a chunk carries decides who sees it: the folder says which
collection, and the collection's record says who.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse


# ============================================================================
# SETTINGS: what is exported, the container, and the records
# ============================================================================
#
# What the module exports, the ingestion container, and the collection records
# that may name collections beyond INGEST_COLLECTIONS.

__all__ = ["collection_of", "doc_id_of", "named", "metadata_for", "use_records", "written_to"]

CONTAINER = "ingestion"
#: What you upload, and the two things the function writes. Only RAW is ever read.
RAW, MARKDOWN, CHUNKS = "raw", "markdown", "chunks"

#: The collection records, once the app names them: a collection made on the
#: dashboard has a record and no line in INGEST_COLLECTIONS, and is one of
#: the collections all the same.
_records: Any = None


# ============================================================================
# THE RECORDS AND THE NAMES
# ============================================================================
#
# INPUT   the collection records; INGEST_COLLECTIONS
# OUTPUT  the collections this deployment holds: the setting first, then any
#         the records name beyond it
#
# With no records the setting alone decides.


def use_records(store: Any) -> None:
    """Read the collections from these records as well as from INGEST_COLLECTIONS. None: the setting alone."""
    global _records
    _records = store


def named(env: Optional[Dict[str, str]] = None) -> List[str]:
    """The collections this deployment holds: INGEST_COLLECTIONS, then any the records name beyond it.

    In the order they were written, the setting's first, so the walkthrough's
    three come first and one made on the dashboard after them.
    """
    env = dict(os.environ if env is None else env)
    found = [n.strip() for n in env.get("INGEST_COLLECTIONS", "").split(",") if n.strip()]
    if _records is not None:
        try:
            more = _records.names()
        except Exception:  # the records could not be listed: the setting's collections still are
            more = []
        found += [n for n in more if n not in found]
    return found


# ============================================================================
# WHICH COLLECTION, WHICH DOCUMENT, WHERE IT IS WRITTEN
# ============================================================================
#
# INPUT   a blob's address; a collection's name
# OUTPUT  the collection from the folder the blob sits in, or None; a
#         document's id, its path inside its collection; where the collection
#         writes what it makes of a file, as Vectrix options
#
# The blob's own path is the answer. Anything outside raw/ is refused even if
# an event arrives for it: the Markdown and the chunks the function writes
# live in the same container, and reading its own output again would hand the
# function its own work.


def collection_of(uri: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The collection a blob belongs to, from the folder it sits in, or None.

    Nothing is guessed: a blob outside ingestion/raw/, including everything
    the function wrote to ingestion/markdown/ and ingestion/chunks/, or one
    whose folder names no collection, is None and the worker leaves it alone.
    Folders under the collection's are part of the document's id and nothing
    more.
    """
    parts = _parts(uri)
    if len(parts) < 4 or parts[0] != CONTAINER or parts[1] != RAW:
        return None
    collection = parts[2]
    return collection if collection in named(env) else None


def doc_id_of(uri: str) -> str:
    """A document's id: its path inside its collection.

    ``ingestion/raw/financial/td/ar2025.pdf`` is ``td/ar2025.pdf`` in the
    financial collection. So its Markdown is
    ``ingestion/markdown/financial/td/ar2025.pdf.md`` and its chunks are
    ``ingestion/chunks/financial/td/ar2025.pdf.jsonl``, the same path on the
    other side. Left to itself the worker used the whole blob address, which
    put the storage account's host name into every id and every kept path:
    recreate the account under another name and every document had a new
    id, and a golden file written against the old ones matched nothing.
    """
    parts = _parts(uri)
    if len(parts) >= 4 and parts[0] == CONTAINER and parts[1] == RAW:
        return "/".join(parts[3:])
    return uri


def written_to(blobs: Any, name: str) -> Dict[str, Any]:
    """Where one collection writes what it makes of a file, as ``Vectrix`` options.

    Each step's output is written down before the next step begins:

    1. The Markdown, in ``ingestion/markdown/<name>/``, before anything is cut.
    2. The chunks, cut from the Markdown read back from there, in
       ``ingestion/chunks/<name>/<file>.jsonl``, a JSON line each, before
       anything embeds them.
    3. The index, last.

    Both folders mirror raw/, one prefix a collection, because each
    collection's Markdown keeps its own ``_index.json``. A step that fails
    leaves what came before it, and the next try starts from the Markdown
    rather than reading the file again, which is the part that costs money.
    A file deleted from raw/ takes its Markdown and its chunks with it:
    ``keep_deleted=False`` removes the Markdown rather than moving it to
    ``_deleted/``, so no copy is kept.
    """
    from vectrixdb.documents import BlobFiles, DocumentStore

    return {
        "keep_source": DocumentStore(BlobFiles(blobs, CONTAINER, prefix=f"{MARKDOWN}/{name}"), keep_deleted=False),
        "keep_chunks": BlobFiles(blobs, CONTAINER, prefix=f"{CHUNKS}/{name}"),
        "markdown_first": True,
    }


# ============================================================================
# PARTS AND METADATA
# ============================================================================
#
# INPUT   a blob address; a document
# OUTPUT  its folders, container first; what every chunk carries beyond the
#         library's own stamps, the collection it is in
#
# The collection on every chunk is what a search filters on.


def _parts(uri: str) -> List[str]:
    """The folders of a blob address, container first."""
    return [p for p in unquote(urlparse(uri).path).strip("/").split("/") if p]


def metadata_for(uri: str, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """What every chunk of this document carries beyond the library's own stamps: the collection it is in.

    Nothing else: who may retrieve from a collection is decided by its record,
    on the server, before anything is searched, so a chunk carries no field a
    rule reads. A blob that names no collection gets nothing, because it is
    not read at all.
    """
    collection = collection_of(uri, env)
    return {"collection": collection} if collection else {}
