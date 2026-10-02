"""
VectrixDB exception hierarchy.

Every exception raised deliberately by VectrixDB derives from :class:`VectrixError`,
so callers can insulate themselves from the library with a single ``except``::

    from vectrixdb import VectrixError

    try:
        results = db.search("query")
    except VectrixError as exc:
        log.warning("vector search unavailable: %s", exc)

Backend failures are wrapped rather than swallowed. A read that cannot reach its
storage backend raises :class:`StorageOperationError`; it never reports an empty
result, because "the collection is empty" and "the database is unreachable" must
not look the same to a caller.

Guidance for choosing an exception when adding code:

* The caller asked for something that does not exist -> ``*NotFoundError``.
* The caller passed something invalid -> :class:`ConfigurationError` or
  :class:`DimensionMismatchError`.
* A dependency, network hop, or driver failed -> :class:`StorageError` subclass,
  raised ``from`` the original exception so the traceback is preserved.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple

__all__ = [
    "VectrixError",
    "ConfigurationError",
    "DependencyError",
    "ExtractionError",
    "StorageError",
    "StorageConnectionError",
    "StorageOperationError",
    "CollectionNotFoundError",
    "CollectionAlreadyExistsError",
    "DocumentNotFoundError",
    "DimensionMismatchError",
    "SearchError",
    "GraphUnavailable",
    "QuantizationError",
    "IndexBuildError",
    "ModelError",
    "ModelNotFoundError",
    "ModelDownloadError",
    "ModelMismatchWarning",
    "CollectionLoadWarning",
    "SparseModelUnavailableWarning",
    "InvalidCollectionName",
    "PolicyError",
    "PolicyDefinitionError",
    "PrincipalRequired",
    "PrincipalIncomplete",
    "PolicyMismatch",
    "PushdownUnavailable",
    "CollectionStoreUnavailable",
    "AuditUnavailable",
    "PolicyNotEnforcedWarning",
    "MetadataContractError",
    "MetadataContractWarning",
    "ExtractionQualityError",
    "ExtractionQualityWarning",
]


# ============================================================================
# THE BASE
# ============================================================================
#
# INPUT   any failure the library raises deliberately
# OUTPUT  one class every exception derives from, so a caller can catch the
#         library and nothing else
#
# Warnings derive from UserWarning instead: they are said, and the call goes
# on.


class VectrixError(Exception):
    """Base class for every error raised by VectrixDB.

    Catching this catches anything the library raises on purpose. It does not
    catch programming errors such as ``TypeError``, which are allowed to
    propagate unchanged.
    """


# ============================================================================
# CONFIGURATION AND THE ENVIRONMENT
# ============================================================================
#
# INPUT   a setting, a dependency, a file, a translation
# OUTPUT  a value missing, malformed or mutually exclusive; an optional
#         dependency not installed, naming the extra; a file that could not be
#         turned into text, nothing written; a translation that failed
#
# Each says what to change, not only what went wrong.


class ConfigurationError(VectrixError):
    """A configuration value is missing, malformed, or mutually exclusive."""


class DependencyError(VectrixError):
    """An optional dependency is required for this code path but not installed.

    Raised instead of a bare ``ImportError`` so the message can name the extra
    that provides the missing package.
    """

    def __init__(self, package: str, extra: Optional[str] = None) -> None:
        self.package = package
        self.extra = extra
        hint = f"pip install vectrixdb[{extra}]" if extra else f"pip install {package}"
        super().__init__(f"{package!r} is required for this feature. Install it with: {hint}")


# ============================================================================
# STORAGE
# ============================================================================
#
# INPUT   a backend
# OUTPUT  it could not be reached or authenticated against; an operation on it
#         failed
#
# One base for both, so a host can catch storage as a kind.


class ExtractionError(VectrixError):
    """Turning a file into text failed, and nothing was written.

    One type for every way that goes wrong, an extractor that raised, an
    endpoint that answered 500, a reply whose page offsets run past its own
    text, so the ingestion worker and the REST route catch this and nothing
    else. ``route`` and ``status`` are set when an endpoint was involved.
    """

    def __init__(
        self, message: str, *, route: Optional[str] = None, status: Optional[int] = None
    ) -> None:
        self.route = route
        self.status = status
        super().__init__(message)


class TranslationError(VectrixError):
    """A translation, a language detection or the language list failed.

    Its own type, apart from ExtractionError, because a translation is
    something a caller asked for and is waiting on, not a step inside
    reading a file, and it is caught in different places. ``route`` is the
    operation, ``translate``, ``detect`` or ``languages``, and ``status`` the
    service's answer when there was one.
    """

    def __init__(
        self, message: str, *, route: Optional[str] = None, status: Optional[int] = None
    ) -> None:
        self.route = route
        self.status = status
        super().__init__(message)


class StorageError(VectrixError):
    """Base class for storage backend failures."""


class StorageConnectionError(StorageError):
    """The storage backend could not be reached or authenticated against."""


class StorageOperationError(StorageError):
    """A storage operation failed.

    Carries the backend name and the operation that failed so the message is
    actionable without reading a traceback.
    """

    def __init__(
        self,
        operation: str,
        backend: str,
        message: Optional[str] = None,
        *,
        collection: Optional[str] = None,
    ) -> None:
        self.operation = operation
        self.backend = backend
        self.collection = collection
        where = f" on collection {collection!r}" if collection else ""
        detail = f": {message}" if message else ""
        super().__init__(f"{backend} {operation} failed{where}{detail}")


# ============================================================================
# COLLECTIONS AND DOCUMENTS
# ============================================================================
#
# INPUT   a name, an id, a vector
# OUTPUT  a collection not found or already there; a document not found; a
#         vector whose dimension is not the collection's
#
# A name that cannot be a directory is refused before anything is made.


class CollectionNotFoundError(VectrixError):
    """The requested collection does not exist."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Collection {name!r} does not exist")


class CollectionAlreadyExistsError(VectrixError):
    """A collection with this name already exists."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Collection {name!r} already exists")


class DocumentNotFoundError(VectrixError):
    """The requested document does not exist."""

    def __init__(self, doc_id: str, collection: Optional[str] = None) -> None:
        self.doc_id = doc_id
        self.collection = collection
        where = f" in collection {collection!r}" if collection else ""
        super().__init__(f"Document {doc_id!r} not found{where}")


class DimensionMismatchError(VectrixError):
    """A vector's dimension does not match the collection's dimension."""

    def __init__(self, expected: int, actual: int, *, collection: Optional[str] = None) -> None:
        self.expected = expected
        self.actual = actual
        self.collection = collection
        where = f" for collection {collection!r}" if collection else ""
        super().__init__(f"Expected {expected}-dimensional vector{where}, got {actual}")


# ============================================================================
# SEARCH AND INDEXING
# ============================================================================
#
# INPUT   a search, a vector, an index
# OUTPUT  a search that could not be completed; a graph search with no graph
#         to run on; a vector that could not be quantized; an index that could
#         not be built or loaded
#
# Graph search unavailable is its own class, since the fix is a build, not a
# retry.


class SearchError(VectrixError):
    """A search could not be completed."""


class GraphUnavailable(SearchError):
    """Graph search could not run because there is no graph to run it on.

    Raised when the pipeline was never built, or has no searcher for the
    requested search type. ``Vectrix.search(mode="graph")`` treats this as
    ordinary: it falls back to vector results and records the reason in
    ``Results.degraded``. Any other error out of the graph is a real failure
    and is reported as one.
    """


class QuantizationError(VectrixError):
    """A vector could not be quantized or dequantized."""


class IndexBuildError(VectrixError):
    """An index could not be built or loaded.

    Named ``IndexBuildError`` rather than ``IndexError`` so it does not shadow
    the builtin.
    """


# ============================================================================
# EMBEDDED MODELS
# ============================================================================
#
# INPUT   a model's name
# OUTPUT  not bundled and not on disk; could not be downloaded; opened with a
#         different model than the collection was built with, warned; a sparse
#         model that cannot be the one that runs, warned; a registered
#         collection that would not open, warned
#
# A mismatch is a warning, since scores are still scores, only worse.


class ModelError(VectrixError):
    """Base class for embedded model failures."""


class ModelNotFoundError(ModelError, FileNotFoundError):
    """A requested model is not bundled and is not present on disk.

    Also a ``FileNotFoundError``, which is what the model loader used to
    raise. Callers catching either keep working, and the promise that one
    ``except VectrixError`` covers the library now holds on this path too.
    """

    def __init__(self, name: str, available: Optional[Any] = None) -> None:
        self.name = name
        self.available = available
        if available is None and "\n" in name:
            # The loader raises with a full, multi-line message of its own.
            super().__init__(name)
            return
        hint = f" Available: {', '.join(sorted(available))}." if available else ""
        super().__init__(f"Model {name!r} is not available.{hint}")


class ModelDownloadError(ModelError):
    """A model could not be downloaded."""


class ModelMismatchWarning(UserWarning):
    """A collection was opened with a different embedding model than it was
    built with. Queries embed with one model and documents with another, so
    scores are meaningless until ``reembed()`` runs."""


class InvalidCollectionName(VectrixError, ValueError):
    """A collection name cannot be used as a directory name.

    The name becomes a directory under the database path, so separators,
    parent references and reserved device names are refused. Also a
    ``ValueError``, which is what this used to raise, so existing handlers
    keep working.
    """


class SparseModelUnavailableWarning(UserWarning):
    """A named sparse model cannot be the one that actually runs.

    The sparse half of a hybrid search reaches a learned model only through a
    storage backend that implements its own hybrid search. On a local
    collection it is the bundled BM25 index whatever ``sparse_model`` says,
    and the results are BM25 results."""


class CollectionLoadWarning(UserWarning):
    """A registered collection would not open, so it is not in the database.

    The rest of the database opened normally and the collection's data on
    disk was left alone. ``VectrixDB.failed_collections`` names it and says
    why, and asking for it by name raises rather than reporting it missing."""


# ============================================================================
# THE ENTITLEMENT POLICY, THE AUDIT, THE CONTRACT, AND QUALITY
# ============================================================================
#
# INPUT   a policied read or write
# OUTPUT  the policy malformed; a principal missing or incomplete; a stored
#         policy not the one opened with; the records unreadable with nothing
#         to stand in; pushdown the backend cannot do; the audit unavailable
#         under a deny policy; a search without the policy, warned; a document
#         the policy could never show anybody, refused, or written without a
#         field it decides by, warned; text that reads as a failed extraction,
#         refused or warned
#
# A policied collection that cannot answer says so; it never answers as if it
# had.


class PolicyError(VectrixError):
    """A policied collection could not answer, and said so.

    Note what does not live under here: there is no ``AccessDenied``. A
    denial is an empty result, never an exception. An exception a caller can
    catch and tell apart from "nothing matched" is itself the side channel,
    and it defeats an ethical wall however careful the layer above is. Every
    subclass below is a refusal to run, not a refusal of access.
    """


class PolicyDefinitionError(PolicyError, ValueError):
    """The policy itself is malformed, so nothing was evaluated."""


class PrincipalRequired(PolicyError):
    """This collection carries a policy and the search supplied no principal.

    The whole point of binding a policy to the collection is that forgetting
    it cannot silently return everything, so it raises instead.
    """

    def __init__(self, collection: Optional[str] = None) -> None:
        self.collection = collection
        where = f" on {collection!r}" if collection else ""
        super().__init__(
            f"this collection carries an entitlement policy{where}, so a search needs a "
            f"principal. Use .as_principal({{...}}).search(...) rather than .search(...)."
        )


class PrincipalIncomplete(PolicyError):
    """The principal is missing a key the policy names.

    This is the host's entitlement resolver failing, not a user without
    permission, and the difference matters operationally: failing closed is
    correct, but it belongs on a pager rather than in the denial count where
    it would look like a busy afternoon.
    """

    def __init__(self, key: str, rule: str, reason: str = "is missing") -> None:
        self.key = key
        self.rule = rule
        super().__init__(
            f"the principal {reason} the key {key!r}, which {rule} reads. This is an "
            f"entitlement resolver fault rather than a denial; the search did not run."
        )


class PolicyMismatch(PolicyError):
    """The collection's stored policy is not the one it was opened with.

    A policy travels with the data it governs. Opening a collection under a
    different rule set is either a deployment that has drifted or an attempt
    to relax a control by reopening, and neither should be a silent success.
    """

    def __init__(self, stored: str, supplied: str) -> None:
        self.stored = stored
        self.supplied = supplied
        super().__init__(
            f"this collection was created with policy {stored}, and was opened with "
            f"{supplied}. Open it with the policy it carries, or migrate it deliberately."
        )


class CollectionStoreUnavailable(PolicyError):
    """The collection records could not be read, and nothing read earlier can stand in.

    A collection's rules live in one record every server reads. When that
    record cannot be read and no copy of it is held from a moment ago, the
    search does not run: running it without the rules is the one outcome
    that is worse than not answering.
    """

    def __init__(self, collection: str, where: str, cause: BaseException) -> None:
        self.collection = collection
        self.where = where
        super().__init__(
            f"the rules for collection {collection!r} are kept in {where}, which could not be read "
            f"({type(cause).__name__}), so nothing was searched"
        )


class PushdownUnavailable(PolicyError):
    """The policy requires engine-side filtering and this backend cannot.

    A backend that fetches candidates and filters them afterwards still
    returns the right documents, but the filter is no longer the thing the
    engine enforced, and the number of rows examined varies with how
    selective the entitlements are. Where that matters the policy says so and
    this refuses rather than quietly degrading.
    """

    def __init__(self, backend: str) -> None:
        self.backend = backend
        super().__init__(
            f"this policy requires the filter to run in the engine, and {backend} applies "
            f"it after fetching. A backend pushes a policy down only when every field the "
            f"policy names is one it can filter on inside the store (on Azure AI Search, "
            f"azure_search_filter_fields). Promote those fields, or build the policy "
            f"without require_pushdown and treat the filter as correct rather than as "
            f"enforcement."
        )


class AuditUnavailable(PolicyError):
    """A decision record could not be stored, and the policy is to deny.

    The other choice is to answer and spool, which is why neither is a
    default. Where an unaudited answer is worse than no answer, this is what
    that looks like.
    """

    def __init__(self, sink: str, reason: str) -> None:
        self.sink = sink
        self.reason = reason
        super().__init__(
            f"the audit sink {sink!r} could not store this decision ({reason}), and its "
            f"failure policy is DENY, so the read did not complete. Pass "
            f"on_failure=Spool(path) to answer and buffer instead."
        )


class PolicyNotEnforcedWarning(UserWarning):
    """A collection with an entitlement policy was searched without it.

    Policies are enforced by ``Vectrix``. Anything that reaches a
    ``Collection`` directly, which is what the REST server and everything
    else built on ``VectrixDB`` does, sees every document the query matches
    with the policy sitting unread in the collection's metadata.

    The warning exists because silence is the worst of both: the collection
    looks protected and is not.
    """


class MetadataContractError(PolicyError, ValueError):
    """A document arrived that the policy could never show anybody.

    Two spellings of one bug. The field the policy decides by is absent, or
    it is there and holds something the rule cannot reach a verdict on, like
    a classification rank written as "high". Either way the rule is false for
    every principal, the chunk is invisible to everybody, and the symptom
    reads as a permissions problem.

    Caught at ingestion rather than at query time because of where the two
    failures land. Written, somebody spends an afternoon in the entitlement
    resolver. Refused, it is a pipeline bug with a stack trace pointing at
    the pipeline.
    """

    def __init__(
        self,
        index: int,
        missing: Sequence[str],
        doc_id: object = None,
        undecidable: Optional[Sequence[Tuple[str, Any, str]]] = None,
    ) -> None:
        self.index = index
        self.missing = list(missing)
        self.undecidable = list(undecidable or ())
        self.doc_id = doc_id
        where = f"document {index}" + (f" ({doc_id})" if doc_id else "")

        faults = []
        if self.missing:
            faults.append(f"is missing {self.missing}")
        for path, value, accepts in self.undecidable:
            faults.append(f"stamps {path}={value!r}, where the rule needs {accepts}")

        super().__init__(
            f"{where} " + ", and ".join(faults) + ". This collection's entitlement "
            f"policy decides by those fields, so written as it is, no principal would "
            f"ever see this document, and the reason would look like a permissions "
            f"problem rather than the pipeline bug it is. Stamp the field, or open the "
            f'collection with on_incomplete_document="warn" while you backfill.'
        )


class MetadataContractWarning(UserWarning):
    """A document was written without a field the policy decides by.

    What ``on_incomplete_document="warn"`` produces: the write goes through so
    an existing collection can be backfilled, and the chunk stays invisible to
    every principal until it is.
    """


class ExtractionQualityError(VectrixError, ValueError):
    """A document arrived whose text reads as a failed extraction.

    The chunk that passes every schema check and destroys retrieval: a
    scanned page that OCRed into fragments and glyph noise. Refused at the
    write, where the stack trace points at the pipeline, rather than found
    later as a ranking problem nobody can explain.
    """

    def __init__(self, doc_id: object, score: float, threshold: float) -> None:
        self.doc_id = doc_id
        self.score = score
        self.threshold = threshold
        super().__init__(
            f"document {doc_id} scores {score:.2f} on extraction quality, below the "
            f"threshold of {threshold:.2f}: its text reads as a failed extraction, broken "
            f"words, glyph noise or joined lines, and would pass every schema check while "
            f"answering nothing. Check the extraction, or open the collection with "
            f'on_low_quality="warn" to write it anyway; extraction_quality(text).signals '
            f"says which signal failed."
        )


class ExtractionQualityWarning(UserWarning):
    """A document whose text reads as a failed extraction was written anyway."""
