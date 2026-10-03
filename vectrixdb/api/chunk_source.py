"""Where a collection page reads its chunks: the chunk store when the collection has one, the table beside the process otherwise.

The collection pages were drawn from the SQLite table each collection keeps
beside the process, and on a deployment that scales out that table holds only
what this instance wrote. With a chunk store (:mod:`vectrixdb.chunk_store`) the
pages read that instead, which every instance writes. Every page asks through
here, so the choice is made in one place and a collection with no store reads
exactly what it read before.

A policy is applied the same way from either source: a principal sees what the
collection's policy lets them see, and a caller who is nobody in particular is
refused by the route before any of this is asked.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Tuple

__all__ = ["changed_at", "count", "document_chunks", "page", "point", "shared", "written"]


# ============================================================================
# READING THE CHUNKS: shared, counted, changed, written
# ============================================================================
#
# INPUT   a collection
# OUTPUT  its rows in the chunk store, or None when it has none; how many
#         chunks it holds; when it last changed; each chunk's time of writing
#         with its build and quality stamps
#
# The collection pages were drawn from the SQLite table each collection keeps
# beside the process; on a deployment the chunks live in a shared store, and
# every instance reads the same rows.


def shared(collection: Any) -> Any:
    """The collection's rows in its chunk store, or None when it has none."""
    return getattr(collection, "_chunk_store", None)


def count(collection: Any) -> int:
    """How many chunks the collection holds."""
    store = shared(collection)
    return int(store.count()) if store is not None else collection.count()


def changed_at(collection: Any) -> Optional[str]:
    """When the collection last changed, ISO 8601, or None."""
    store = shared(collection)
    if store is not None:
        when = store.changed_at()
        if when:
            return str(when)
    updated = getattr(collection, "_updated_at", None)
    return updated.isoformat() if updated else None


def written(collection: Any) -> Iterable[Tuple[Any, dict]]:
    """Each chunk's time of writing with its build and quality stamps. Nothing for a collection that keeps neither."""
    store = shared(collection)
    if store is not None:
        return store.written()
    each = getattr(collection, "_iter_written_raw", None)
    return each() if callable(each) else iter(())


# ============================================================================
# ONE CHUNK, A FIND, A PAGE, AND A DOCUMENT'S CHUNKS
# ============================================================================
#
# INPUT   a collection, a principal, and an id, a find, or a page
# OUTPUT  one chunk as the principal may see it, or None; the ids holding the
#         find in the id or the source, with how many match; a page of ids and
#         the total, as the principal may see them; the ids of one document's
#         chunks
#
# A denial and a miss are the same answer, as in Collection.get. A find reads
# every id, so it costs what a policied listing costs; a plain page does not.


def point(collection: Any, point_id: str, principal: Optional[dict] = None) -> Any:
    """One chunk as ``principal`` may see it, or None. A denial and a miss are the same answer, as in ``Collection.get``."""
    store = shared(collection)
    if store is None:
        return (
            collection.get(point_id, principal=principal)
            if principal is not None
            else collection.get(point_id)
        )
    found = store.get(point_id)
    policy = collection.policy
    if policy is None or found is None:
        return found
    if principal is None:
        from ..exceptions import PrincipalRequired

        raise PrincipalRequired(collection.name)
    return found if policy.decide(principal, found.metadata or {}).allowed else None


def find(
    collection: Any, q: str, limit: int, offset: int, principal: Optional[dict] = None
) -> Tuple[List[str], int]:
    """The page of ids holding ``q`` in the id or, where the chunks are kept, in the source, and how many match in all.

    A find reads every id, so it costs what a policied listing costs; a page
    without a find goes through :func:`page` and does not.
    """
    needle = q.strip().lower()
    store = shared(collection)
    matched: List[str] = []
    if store is None:
        every = (
            collection.list_ids(limit=1_000_000, offset=0, principal=principal)
            if principal is not None
            else collection.list_ids(limit=1_000_000, offset=0)
        )
        matched = [i for i in every if needle in str(i).lower()]
    else:
        policy = collection.policy
        if policy is not None and principal is None:
            from ..exceptions import PrincipalRequired

            raise PrincipalRequired(collection.name)
        for point_id, metadata in store.each():
            if policy is not None and not policy.decide(principal, metadata).allowed:
                continue
            source = " ".join(
                str((metadata or {}).get(k) or "")
                for k in ("_vx_citation", "_vx_source", "source", "_vx_doc")
            )
            if needle in str(point_id).lower() or needle in source.lower():
                matched.append(point_id)
    return matched[offset : offset + limit], len(matched)


def page(
    collection: Any, limit: int, offset: int, principal: Optional[dict] = None
) -> Tuple[List[str], int]:
    """A page of ids and how many there are in all, as ``principal`` may see them."""
    store = shared(collection)
    if store is None:
        if principal is not None:
            return collection.list_ids(
                limit=limit, offset=offset, principal=principal
            ), collection.count(principal=principal)
        return collection.list_ids(limit=limit, offset=offset), collection.count()
    policy = collection.policy
    if policy is None:
        return store.ids(limit, offset), int(store.count())
    if principal is None:
        from ..exceptions import PrincipalRequired

        raise PrincipalRequired(collection.name)
    # What a principal may see is decided chunk by chunk, so the page and the
    # total both come from reading them all, as Collection.list_ids does.
    ids: List[str] = []
    total = 0
    for point_id, metadata in store.each():
        if not policy.decide(principal, metadata).allowed:
            continue
        if offset <= total < offset + limit:
            ids.append(point_id)
        total += 1
    return ids, total


def document_chunks(collection: Any, doc_id: str) -> List[str]:
    """The ids of one document's chunks, the ones whose ``_vx_doc`` it is."""
    store = shared(collection)
    if store is not None:
        return list(store.of_document(doc_id))
    return [
        i
        for i, _text, metadata in collection._iter_documents_raw()
        if metadata.get("_vx_doc") == doc_id
    ]
