"""Azure AI Search storage backend.

One Azure index per collection, named ``<prefix>-<collection>``, with a fixed
schema: a key, the original document id, a vector field for the dense
embedding, a searchable text field, and one JSON string holding everything
else the caller stored. Azure indexes need a declared schema, and a JSON
blob is the honest way to carry arbitrary metadata through one; filters on
metadata are applied on the client, as they are for the other cloud backends.

What Azure does natively and the backend uses:

* vector search over the HNSW index Azure builds for the vector field;
* BM25 full-text search over ``text_content``;
* hybrid queries, which the service fuses with reciprocal rank fusion when a
  query carries both text and a vector;
* the semantic ranker, Azure's own reranking tier, as an option.

Metadata named in ``azure_search_filter_fields`` is also written to a real
filterable field beside the JSON blob, and a filter or a policy over those
fields is compiled to OData and runs inside the service. On Azure an absent
field and a null one are the same thing, so a null promoted field fails
every rule, including a wall, which is the closed reading.

What it does not do: late interaction and the knowledge graph stay local, so
the backend serves dense and hybrid modes. Writes are visible to queries
after the service indexes them, normally within a second or two; a test
that reads straight after a write should allow for that, and ``flush()``
does not wait because the service offers nothing to wait on.

Document keys on Azure allow letters, digits, underscore, dash and equals
only. Ids are stored as given in ``doc_id`` and the key is a URL-safe
base64 encoding of the id, so "id with spaces" and unicode ids round-trip.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import importlib.util
import json
import re
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .._time import utcnow_iso
from ..exceptions import ConfigurationError, DependencyError, StorageConnectionError, StorageOperationError
from .storage import BaseStorage, StorageConfig

__all__ = ["AzureSearchStorage"]


# ============================================================================
# SETTINGS: the index layout, the field names, and the per-search selection
# ============================================================================
#
# The profile, algorithm and semantic configuration every index is made with,
# the fixed field names, the filter kinds the service can be handed, and a
# context variable holding one search's choice of vectors so two threads
# searching one store do not see each other's.

BACKEND = "AzureSearch"
VECTOR_PROFILE = "vx-hnsw"
VECTOR_ALGORITHM = "vx-hnsw-config"
SEMANTIC_CONFIG = "vx-semantic"
COLLECTIONS_INDEX = "collections"

# Field names in every collection index.
F_KEY = "id"
F_DOC_ID = "doc_id"
F_VECTOR = "dense_embedding"
F_VECTOR_AZURE = "dense_azure"
F_TEXT = "text_content"
F_PAYLOAD = "payload"
F_CREATED = "created_at"
F_UPDATED = "updated_at"

FILTER_KINDS = ("string", "strings", "number", "boolean")
EMBEDDINGS = ("vectrixdb", "azure", "both")
AZURE_PROFILE = "vx-azure-openai-profile"
VECTORIZER = "vx-azure-openai"

# Which vectors answer the search in progress, and the words of the
# question, which the service's vectorizer needs. Set by the caller around
# one search; a context variable so two threads searching one store do not
# see each other's choice.
_SELECTION: "contextvars.ContextVar[Optional[Tuple[Optional[str], Optional[str]]]]" = contextvars.ContextVar(
    "vectrixdb_azure_vectors", default=None
)
_SEPARATORS = (",", "|", ";", "#", "~")


# ============================================================================
# FIELDS, PATHS, KEYS, AND NOT FOUND
# ============================================================================
#
# INPUT   a metadata path; a document id; a value; an exception
# OUTPUT  the index field a promoted path lands in; the value at a dotted
#         path; a filter the service cannot be handed as written; a key safe
#         for Azure; an OData string literal; whether the exception means not
#         found
#
# Azure allows only a few characters in a field name and a key, so both are
# derived, never passed through.


def _field_name(path: str) -> str:
    """The index field a promoted metadata path lands in. Azure allows
    letters, digits and underscores; a dotted path keeps its shape as ``__``."""
    return "f_" + re.sub(r"[^A-Za-z0-9_]", "_", path.replace(".", "__"))


def _nested(data: Any, path: str) -> Any:
    """The value at a dotted path, or None."""
    value = data
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return None
    return value


class _Unsupported(Exception):
    """A filter the service cannot be handed as written."""


def _key(doc_id: str) -> str:
    return "k" + base64.urlsafe_b64encode(doc_id.encode("utf-8")).decode("ascii").rstrip("=")


def _odata_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _is_not_found(exc: BaseException) -> bool:
    try:
        from azure.core.exceptions import ResourceNotFoundError
    except ImportError:  # pragma: no cover - only reachable with the SDK missing
        return False
    return isinstance(exc, ResourceNotFoundError)


# ============================================================================
# THE BACKEND
# ============================================================================
#
# INPUT   an Azure AI Search endpoint and an index prefix
# OUTPUT  one index per collection with a fixed schema: vector search over
#         Azure's HNSW, BM25 over the text, hybrid fused by the service, and
#         one JSON string for everything else the caller stored
#
# Azure indexes need a declared schema, and a JSON payload is the honest way
# to carry arbitrary metadata through one.


class AzureSearchStorage(BaseStorage):
    """Azure AI Search backend. See the module docstring for the layout.

    ``index_client`` and ``client_factory`` exist so tests can hand in
    stand-ins; production code leaves them None and ``connect()`` builds
    the SDK clients from the config.
    """

    def __init__(
        self,
        config: StorageConfig,
        index_client: Any = None,
        client_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.config = config
        self._index_client = index_client
        self._client_factory = client_factory
        self._clients: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._dimensions: Dict[str, int] = {}
        # Per collection: the name of the index's semantic configuration, or
        # None when it has none. Learnt when the collection is opened.
        self._semantic: Dict[str, Optional[str]] = {}
        self._staged: Dict[str, Dict[str, Any]] = {}
        self._azure_embedder: Any = config.azure_search_embed_fn
        mode = config.azure_search_embeddings
        if mode not in EMBEDDINGS:
            raise ConfigurationError(f"embeddings is one of {', '.join(EMBEDDINGS)}, got {mode!r}")
        if mode != "vectrixdb":
            spec = config.azure_search_vectorizer or {}
            if not isinstance(spec.get("dimensions"), int) or spec["dimensions"] <= 0:
                raise ConfigurationError(
                    f"embeddings={mode!r} needs azure_embedding with the deployment's dimensions, "
                    "for example {'endpoint': ..., 'deployment': 'text-embedding-3-large', 'dimensions': 3072}"
                )
            if self._azure_embedder is None and not (spec.get("endpoint") and spec.get("deployment")):
                raise ConfigurationError(
                    f"embeddings={mode!r} needs azure_embedding's endpoint and deployment, or an embed_fn"
                )
            if self._azure_embedder is None:
                # The deployment is called through the openai package. Said
                # when the collection opens, not at its first write, which
                # comes after every document has been read and cut.
                try:
                    installed = importlib.util.find_spec("openai") is not None
                except ValueError:  # a stand-in put in sys.modules, which imports
                    installed = True
                if not installed:
                    raise DependencyError("openai", extra="azure")
        weights = config.azure_search_vector_weights or {}
        unknown = sorted(set(weights) - {"vectrixdb", "azure"})
        if unknown or any(not isinstance(w, (int, float)) or w <= 0 for w in weights.values()):
            raise ConfigurationError("vector_weights maps 'vectrixdb' and 'azure' to positive numbers")

    # ------------------------------------------------------------ connection

    def connect(self) -> None:
        if self._index_client is not None:
            self._ensure_collections_index()
            return
        try:
            from azure.search.documents import SearchClient
            from azure.search.documents.indexes import SearchIndexClient
        except ImportError as exc:
            raise ImportError(
                "Install the Azure AI Search dependency: pip install azure-search-documents "
                "(or pip install vectrixdb[azure])."
            ) from exc
        endpoint = self.config.azure_search_endpoint
        if not endpoint:
            raise StorageConnectionError(BACKEND, "azure_search_endpoint is not set")
        credential = self._credential()
        self._index_client = SearchIndexClient(endpoint=endpoint, credential=credential)
        self._client_factory = lambda name: SearchClient(
            endpoint=endpoint, index_name=name, credential=credential
        )
        self._ensure_collections_index()

    def _credential(self) -> Any:
        if self.config.azure_search_key:
            from azure.core.credentials import AzureKeyCredential

            return AzureKeyCredential(self.config.azure_search_key)
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise StorageConnectionError(
                BACKEND,
                "No azure_search_key given and azure-identity is not installed; "
                "pip install azure-identity for DefaultAzureCredential.",
            ) from exc
        return DefaultAzureCredential()

    def close(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - best effort
                    pass
        self._clients = {}

    # ---------------------------------------------------------------- naming

    def _index_name(self, collection: str) -> str:
        """The index a collection lives in.

        With no prefix the index is the collection, which is what you want
        when the collection names are already yours. Joining an empty prefix
        with a dash would give ``-financial``, and Azure refuses an index
        name that does not start with a letter or a digit.
        """
        safe = "".join(ch if ch.isalnum() else "-" for ch in collection.lower()).strip("-") or "default"
        prefix = str(self.config.azure_search_index_prefix or "").strip("-")
        return f"{prefix}-{safe}" if prefix else safe

    def _client(self, collection: str) -> Any:
        name = self._index_name(collection)
        with self._lock:
            client = self._clients.get(name)
            if client is None:
                if self._client_factory is None:
                    if self._index_client is not None and hasattr(
                        self._index_client, "get_search_client"
                    ):
                        client = self._index_client.get_search_client(name)
                    else:
                        raise StorageConnectionError(BACKEND, "connect() first")
                else:
                    client = self._client_factory(name)
                self._clients[name] = client
        return client

    def _collections_client(self) -> Any:
        return self._client(COLLECTIONS_INDEX)

    # --------------------------------------------------------------- schema

    def _ensure_collections_index(self) -> None:
        from azure.search.documents.indexes.models import (
            SearchField,
            SearchFieldDataType,
            SearchIndex,
        )

        name = self._index_name(COLLECTIONS_INDEX)
        fields = [
            SearchField(name=F_KEY, type=SearchFieldDataType.String, key=True),
            SearchField(name=F_DOC_ID, type=SearchFieldDataType.String, filterable=True),
            SearchField(name=F_PAYLOAD, type=SearchFieldDataType.String),
        ]
        try:
            self._index_client.get_index(name)
        except Exception as exc:
            if not _is_not_found(exc):
                raise StorageConnectionError(BACKEND, str(exc)) from exc
            self._index_client.create_or_update_index(SearchIndex(name=name, fields=fields))

    def _collection_index(self, name: str, dimension: int) -> Any:
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            VectorSearch,
            VectorSearchProfile,
        )

        fields = [
            SearchField(name=F_KEY, type=SearchFieldDataType.String, key=True),
            SearchField(name=F_DOC_ID, type=SearchFieldDataType.String, filterable=True),
            SearchField(
                name=F_VECTOR,
                type="Collection(Edm.Single)",
                searchable=True,
                vector_search_dimensions=dimension,
                vector_search_profile_name=VECTOR_PROFILE,
            ),
            SearchField(name=F_TEXT, type=SearchFieldDataType.String, searchable=True),
            SearchField(name=F_PAYLOAD, type=SearchFieldDataType.String),
            SearchField(name=F_CREATED, type=SearchFieldDataType.String, sortable=True),
            SearchField(name=F_UPDATED, type=SearchFieldDataType.String),
        ]
        kinds = {
            "string": SearchFieldDataType.String,
            "strings": "Collection(Edm.String)",
            "number": SearchFieldDataType.Double,
            "boolean": SearchFieldDataType.Boolean,
        }
        for path, kind in self._promoted().items():
            fields.append(SearchField(name=_field_name(path), type=kinds[kind], filterable=True))
        mode = self.config.azure_search_embeddings
        profiles = [VectorSearchProfile(name=VECTOR_PROFILE, algorithm_configuration_name=VECTOR_ALGORITHM)]
        vectorizers: List[Any] = []
        if mode != "vectrixdb":
            vectorizer = self._vectorizer()
            if vectorizer is not None:
                vectorizers.append(vectorizer)
            profiles.append(
                VectorSearchProfile(
                    name=AZURE_PROFILE,
                    algorithm_configuration_name=VECTOR_ALGORITHM,
                    **({"vectorizer_name": VECTORIZER} if vectorizer is not None else {}),
                )
            )
        if mode == "azure":
            # One field, holding the deployment's vectors, which is what the
            # collection's own embedder produces in this mode.
            for f in fields:
                if f.name == F_VECTOR:
                    f.vector_search_profile_name = AZURE_PROFILE
        elif mode == "both":
            fields.append(
                SearchField(
                    name=F_VECTOR_AZURE,
                    type="Collection(Edm.Single)",
                    searchable=True,
                    vector_search_dimensions=int((self.config.azure_search_vectorizer or {})["dimensions"]),
                    vector_search_profile_name=AZURE_PROFILE,
                )
            )
        vector_search = VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name=VECTOR_ALGORITHM)],
            profiles=profiles,
            **({"vectorizers": vectorizers} if vectorizers else {}),
        )
        index = SearchIndex(name=self._index_name(name), fields=fields, vector_search=vector_search)
        if self.config.azure_search_semantic:
            try:
                from azure.search.documents.indexes.models import (
                    SemanticConfiguration,
                    SemanticField,
                    SemanticPrioritizedFields,
                    SemanticSearch,
                )

                index.semantic_search = SemanticSearch(
                    configurations=[
                        SemanticConfiguration(
                            name=SEMANTIC_CONFIG,
                            prioritized_fields=SemanticPrioritizedFields(
                                content_fields=[SemanticField(field_name=F_TEXT)]
                            ),
                        )
                    ]
                )
            except ImportError:  # pragma: no cover - older SDKs
                pass
        return index

    @staticmethod
    def _semantic_config_name(index: Any) -> Optional[str]:
        """The semantic configuration a query should name, or None when the index has none."""
        semantic = getattr(index, "semantic_search", None)
        configurations = list(getattr(semantic, "configurations", None) or []) if semantic is not None else []
        names = [str(getattr(c, "name", "")) for c in configurations if getattr(c, "name", None)]
        if not names:
            return None
        if SEMANTIC_CONFIG in names:
            return SEMANTIC_CONFIG
        default = getattr(semantic, "default_configuration_name", None)
        return str(default) if default in names else names[0]

    def _existing_index(self, name: str) -> Any:
        try:
            return self._index_client.get_index(self._index_name(name))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise

    # ---------------------------------------------------------- collections

    def create_collection(self, name: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Create the collection's index, or bring an existing one up to date.

        An update replaces the whole definition, so what this handle does not
        ask for would be taken away. The semantic ranker is the one that
        matters: opening an index with ``semantic=False`` used to remove the
        semantic configuration a production search depends on, and opening
        it with ``semantic=True`` replaced one made in the portal. An index's
        semantic configuration is kept as it is now, whichever way the handle
        was opened; ``semantic=True`` adds one only to an index that has
        none. A handle opened either way can ask for the ranker per search.
        """
        config = dict(metadata or {})
        dimension = int(config.get("dimension", 384))
        try:
            index = self._collection_index(name, dimension)
            existing = self._existing_index(name)
            if existing is not None and self._semantic_config_name(existing) is not None:
                index.semantic_search = existing.semantic_search
            self._index_client.create_or_update_index(index)
            self._collections_client().upload_documents(
                [{F_KEY: _key(name), F_DOC_ID: name, F_PAYLOAD: json.dumps(config)}]
            )
        except Exception as exc:
            raise StorageOperationError("create_collection", BACKEND, str(exc)) from exc
        self._dimensions[name] = dimension
        self._semantic[name] = self._semantic_config_name(index)

    def semantic_name(self, collection: str) -> Optional[str]:
        """The name of this collection's semantic configuration, or None when its index has none."""
        if collection not in self._semantic:
            try:
                existing = self._existing_index(collection)
            except Exception:  # a question about the index is not worth failing a search over
                existing = None
            self._semantic[collection] = self._semantic_config_name(existing) if existing is not None else None
        return self._semantic[collection]

    def _use_semantic(self, collection: str, semantic: Optional[bool]) -> Optional[str]:
        """The semantic configuration one search should run with, or None for none.

        ``semantic`` is the search's own choice; None leaves it to how the
        store was opened. Asking for the ranker on an index that has no
        semantic configuration is an error, not a quiet search without it.
        """
        wanted = bool(self.config.azure_search_semantic) if semantic is None else bool(semantic)
        if not wanted:
            return None
        name = self.semantic_name(collection)
        if name is None:
            raise ConfigurationError(
                f"The index behind {collection!r} has no semantic configuration, so its semantic ranker "
                "cannot run. Open it once with VectrixDB.with_azure_search(..., semantic=True) to add one."
            )
        return name

    def delete_collection(self, name: str) -> None:
        try:
            self._index_client.delete_index(self._index_name(name))
            self._collections_client().delete_documents([{F_KEY: _key(name)}])
        except Exception as exc:
            if not _is_not_found(exc):
                raise StorageOperationError("delete_collection", BACKEND, str(exc)) from exc
        self._clients.pop(self._index_name(name), None)
        self._dimensions.pop(name, None)

    def list_collections(self) -> List[str]:
        try:
            rows = self._collections_client().search(search_text="*", select=[F_DOC_ID], top=1000)
            return [row[F_DOC_ID] for row in rows]
        except Exception as exc:
            raise StorageOperationError("list_collections", BACKEND, str(exc)) from exc

    def get_collection_config(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            row = self._collections_client().get_document(_key(name))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise StorageOperationError("get_collection_config", BACKEND, str(exc)) from exc
        return json.loads(row.get(F_PAYLOAD) or "{}")

    # ------------------------------------------------------------ documents

    def _promoted(self) -> Dict[str, str]:
        fields = dict(self.config.azure_search_filter_fields or {})
        for path, kind in fields.items():
            if kind not in FILTER_KINDS:
                raise ValueError(
                    f"azure_search_filter_fields[{path!r}] is {kind!r}; expected one of {FILTER_KINDS}"
                )
        return fields

    @staticmethod
    def _coerce(value: Any, kind: str) -> Any:
        """A metadata value as the promoted field takes it, or None to leave it null."""
        if value is None or isinstance(value, bool) and kind != "boolean":
            return None
        if kind == "strings":
            items = value if isinstance(value, (list, tuple, set)) else [value]
            return [str(x) for x in items if isinstance(x, (str, int, float)) and not isinstance(x, bool)]
        if kind == "string":
            return str(value) if isinstance(value, (str, int, float)) else None
        if kind == "number":
            return float(value) if isinstance(value, (int, float)) else None
        return value if isinstance(value, bool) else None

    def _to_row(self, doc_id: str, data: Dict[str, Any], created: Optional[str] = None) -> Dict[str, Any]:
        data = dict(data)
        named = data.pop("named_vectors", None) or {}
        row = self._base_row(doc_id, data, created)
        if self.config.azure_search_embeddings == "both" and named.get("azure") is not None:
            row[F_VECTOR_AZURE] = [float(x) for x in named["azure"]]
        for path, kind in self._promoted().items():
            coerced = self._coerce(_nested(data, path), kind)
            if coerced is not None:
                row[_field_name(path)] = coerced
        return row

    @staticmethod
    def _base_row(doc_id: str, data: Dict[str, Any], created: Optional[str] = None) -> Dict[str, Any]:
        data = dict(data)
        vector = data.pop("dense_embedding", None)
        if vector is None:
            vector = data.pop("_embedding", None)
        text = data.pop("text_content", None)
        data.pop("created_at", None)
        data.pop("updated_at", None)
        now = utcnow_iso()
        row: Dict[str, Any] = {
            F_KEY: _key(doc_id),
            F_DOC_ID: doc_id,
            F_TEXT: text or "",
            F_PAYLOAD: json.dumps(data, default=str),
            F_CREATED: created or now,
            F_UPDATED: now,
        }
        if vector is not None:
            row[F_VECTOR] = [float(x) for x in vector]
        return row

    @staticmethod
    def _from_row(row: Dict[str, Any], include_vector: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = json.loads(row.get(F_PAYLOAD) or "{}")
        if row.get(F_TEXT):
            data["text_content"] = row[F_TEXT]
        if include_vector and row.get(F_VECTOR) is not None:
            data["dense_embedding"] = list(row[F_VECTOR])
        for field in (F_CREATED, F_UPDATED):
            if row.get(field):
                data[field] = row[field]
        return data

    def insert(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        self.insert_batch(collection, [(id, data)])

    def insert_batch(self, collection: str, documents: List[Tuple[str, Dict[str, Any]]]) -> int:
        if not documents:
            return 0
        client = self._client(collection)
        documents = self._with_azure_vectors(documents)
        rows = [self._to_row(doc_id, data) for doc_id, data in documents]
        try:
            for start in range(0, len(rows), 1000):
                results = client.upload_documents(rows[start : start + 1000])
                failed = [r.key for r in results if not getattr(r, "succeeded", True)]
                if failed:
                    raise StorageOperationError(
                        "insert_batch", BACKEND, f"{len(failed)} documents were rejected"
                    )
        except StorageOperationError:
            raise
        except Exception as exc:
            raise StorageOperationError("insert_batch", BACKEND, str(exc)) from exc
        return len(rows)

    # ------------------------------------------------------ the second vector

    def vector_names(self) -> tuple:
        return {"vectrixdb": ("vectrixdb",), "azure": ("azure",), "both": ("vectrixdb", "azure")}[
            self.config.azure_search_embeddings
        ]

    def _vectorizer(self) -> Any:
        """The index's Azure OpenAI vectorizer, which is what lets the service
        embed the question. None when the SDK is too old to have one, or no
        endpoint was given; the question is then embedded here instead."""
        spec = self.config.azure_search_vectorizer or {}
        if not (spec.get("endpoint") and spec.get("deployment")):
            return None
        try:
            from azure.search.documents.indexes.models import AzureOpenAIVectorizer, AzureOpenAIVectorizerParameters
        except ImportError:  # pragma: no cover - older SDKs
            return None
        parameters = AzureOpenAIVectorizerParameters(
            resource_url=spec["endpoint"],
            deployment_name=spec["deployment"],
            model_name=spec.get("model") or spec["deployment"],
            **({"api_key": spec["api_key"]} if spec.get("api_key") else {}),
        )
        return AzureOpenAIVectorizer(vectorizer_name=VECTORIZER, parameters=parameters)

    def embed_azure(self, texts: List[str]) -> List[List[float]]:
        """The deployment's vectors for these texts. Ingest calls this; so
        does a query when the index has no vectorizer to do it."""
        if self._azure_embedder is None:
            from ..models.openai_compat import OpenAIEmbedder

            spec = self.config.azure_search_vectorizer or {}
            self._azure_embedder = OpenAIEmbedder(
                spec.get("model") or spec["deployment"],
                base_url=str(spec["endpoint"]).rstrip("/") + "/openai/v1/",
                api_key=spec.get("api_key"),
            )
        vectors = self._azure_embedder(list(texts))
        want = int((self.config.azure_search_vectorizer or {})["dimensions"])
        out = [[float(x) for x in v] for v in vectors]
        if out and len(out[0]) != want:
            raise ConfigurationError(
                f"the Azure embedding returned {len(out[0])} dimensions and the index field was made for "
                f"{want}; azure_embedding's dimensions has to be what the deployment produces"
            )
        return out

    @property
    def default_embed_fn(self) -> Any:
        """With ``embeddings=\"azure\"`` the collection's own embedder is the
        deployment, so a caller that named no embedder is handed this one."""
        if self.config.azure_search_embeddings != "azure":
            return None
        import numpy as np

        return lambda texts: np.asarray(self.embed_azure(list(texts)), dtype=np.float32)

    def named_embedders(self) -> dict:
        if self.config.azure_search_embeddings != "both":
            return {}
        spec = self.config.azure_search_vectorizer or {}
        label = "azure-openai:" + str(spec.get("model") or spec.get("deployment") or "custom")
        return {"azure": (label, self.embed_azure)}

    def stage_named_vectors(self, vectors: Dict[str, Dict[str, Any]]) -> None:
        """Vectors a caller already has, by name and document id, for the
        insert that follows. A caller with a cache embeds through it and
        stages the result; anything not staged is embedded at insert."""
        with self._lock:
            for name, by_id in vectors.items():
                self._staged.setdefault(name, {}).update(by_id)

    def _with_azure_vectors(self, documents: List[Tuple[str, Dict[str, Any]]]) -> List[Tuple[str, Dict[str, Any]]]:
        if self.config.azure_search_embeddings != "both":
            return documents
        with self._lock:
            staged = self._staged.get("azure", {})
            taken = {doc_id: staged.pop(doc_id) for doc_id, _ in documents if doc_id in staged}
        out: List[Tuple[str, Dict[str, Any]]] = []
        missing: List[int] = []
        for i, (doc_id, data) in enumerate(documents):
            data = dict(data)
            named = dict(data.get("named_vectors") or {})
            if doc_id in taken:
                named["azure"] = taken[doc_id]
            if named.get("azure") is None and (data.get("text_content") or "").strip():
                missing.append(i)
            data["named_vectors"] = named
            out.append((doc_id, data))
        if missing:
            fresh = self.embed_azure([out[i][1]["text_content"] for i in missing])
            for i, vector in zip(missing, fresh):
                out[i][1]["named_vectors"]["azure"] = vector
        return out

    @contextlib.contextmanager
    def using_vectors(self, vectors: Optional[str], query_text: Optional[str] = None) -> Any:
        """Which vectors answer the searches inside this block, and the words
        of the question for the service's vectorizer."""
        if vectors is not None:
            self._fields_for(vectors)  # refuse a name this index does not hold, now
        token = _SELECTION.set((vectors, query_text))
        try:
            yield self
        finally:
            _SELECTION.reset(token)

    def needs_the_store(self) -> bool:
        """Does the search in progress ask for a vector the collection's
        local index does not hold? True for the deployment's vector, alone
        or with the collection's own. A store holding only the collection's
        own vector never needs to be asked for it."""
        if self.config.azure_search_embeddings == "vectrixdb":
            return False
        selection = _SELECTION.get()
        return self._fields_for(selection[0] if selection else None) != ["vectrixdb"]

    @staticmethod
    def scoped() -> bool:
        """Is a search already inside ``using_vectors``?"""
        return _SELECTION.get() is not None

    def _fields_for(self, vectors: Optional[str]) -> List[str]:
        have = self.vector_names()
        if vectors is None:
            return list(have)
        if vectors == "both":
            if len(have) < 2:
                raise ConfigurationError(
                    f"vectors='both' needs an index built with embeddings='both'; this one holds {have[0]!r}"
                )
            return list(have)
        if vectors not in have:
            raise ConfigurationError(
                f"vectors={vectors!r} is not in this index, which holds {' and '.join(repr(h) for h in have)}. "
                "What an index holds is decided when it is built, with embeddings=."
            )
        return [vectors]

    def get(self, collection: str, id: str) -> Optional[Dict[str, Any]]:
        try:
            row = self._client(collection).get_document(_key(id))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise StorageOperationError("get", BACKEND, str(exc)) from exc
        return self._from_row(row)

    def get_batch(self, collection: str, ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        if not ids:
            return []
        found: Dict[str, Dict[str, Any]] = {}
        client = self._client(collection)
        try:
            for start in range(0, len(ids), 100):
                chunk = ids[start : start + 100]
                joined = ",".join(chunk)
                if any("," in i for i in chunk):
                    for i in chunk:
                        got = self.get(collection, i)
                        if got is not None:
                            found[i] = got
                    continue
                expr = f"search.in({F_DOC_ID}, {_odata_str(joined)}, ',')"
                for row in client.search(search_text="*", filter=expr, top=len(chunk)):
                    found[row[F_DOC_ID]] = self._from_row(row)
        except Exception as exc:
            raise StorageOperationError("get_batch", BACKEND, str(exc)) from exc
        return [found.get(i) for i in ids]

    def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        current = self.get(collection, id)
        if current is None:
            return False
        merged = {**current, **data}
        row = self._to_row(id, merged, created=current.get("created_at"))
        try:
            self._client(collection).upload_documents([row])
        except Exception as exc:
            raise StorageOperationError("update", BACKEND, str(exc)) from exc
        return True

    def delete(self, collection: str, id: str) -> bool:
        return self.delete_batch(collection, [id]) == 1

    def delete_batch(self, collection: str, ids: List[str]) -> int:
        if not ids:
            return 0
        # The service reports success for keys that do not exist, so the
        # count comes from a lookup first: the contract says delete() tells
        # the truth about whether anything went.
        present = [i for i, got in zip(ids, self.get_batch(collection, ids)) if got is not None]
        if not present:
            return 0
        try:
            self._client(collection).delete_documents([{F_KEY: _key(i)} for i in present])
        except Exception as exc:
            raise StorageOperationError("delete_batch", BACKEND, str(exc)) from exc
        return len(present)

    def count(self, collection: str) -> int:
        try:
            return int(self._client(collection).get_document_count())
        except Exception as exc:
            if _is_not_found(exc):
                return 0
            raise StorageOperationError("count", BACKEND, str(exc)) from exc

    def scan(
        self,
        collection: str,
        limit: int = 100,
        offset: int = 0,
        filter_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        try:
            rows = list(
                self._client(collection).search(
                    search_text="*", top=limit, skip=offset, order_by=[f"{F_CREATED} asc"]
                )
            )
        except Exception as exc:
            if _is_not_found(exc):
                return
            raise StorageOperationError("scan", BACKEND, str(exc)) from exc
        for row in rows:
            data = self._from_row(row)
            if filter_func is None or filter_func(data):
                yield row[F_DOC_ID], data

    def iterate(
        self, collection: str, batch_size: int = 1000
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        offset = 0
        while True:
            page = list(self.scan(collection, limit=batch_size, offset=offset))
            if not page:
                return
            yield from page
            offset += batch_size

    def flush(self) -> None:
        """Azure indexes writes on its own schedule; there is nothing to wait on."""
        return None

    # ---------------------------------------------------------------- search

    @property
    def reranks(self) -> bool:
        """Whether the service reranks its own text and hybrid results.

        True with the semantic option: Azure's ranker then scores the top
        candidates inside the service, and the score handed back is its
        reranker score. A caller asking for rerank on such a collection
        gets that, and not a second pass through the local cross-encoder.
        """
        return bool(self.config.azure_search_semantic)

    @staticmethod
    def _ranked_score(row: Dict[str, Any]) -> float:
        """The semantic ranker's score when the service produced one, else the fused score."""
        reranked = row.get("@search.reranker_score")
        return float(reranked) if reranked is not None else float(row["@search.score"])

    def _hit(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = self._from_row(row, include_vector=False)
        if row.get("@search.reranker_score") is not None:
            data["_semantic_score"] = float(row["@search.reranker_score"])
        return data

    # ------------------------------------------------------------ pushdown

    def filter_fields(self, collection: str) -> frozenset:
        return frozenset(self._promoted())

    def compile_filter(self, collection: str, filter_dict: Dict[str, Any]) -> Optional[str]:
        """The library's filter grammar as OData over the promoted fields.

        None when any field is not promoted or any operator has no honest
        translation, and the collection then applies that filter itself. On
        the service a null field and an absent one are the same, so every
        clause is guarded with ``ne null`` (or ``/any()`` for a list) and a
        null value fails the rule, walls included. The local engine lets a
        present null pass a wall; the closed reading is the one to keep when
        the two cannot be told apart.
        """
        try:
            expr = self._odata(filter_dict, self._promoted())
        except _Unsupported:
            return None
        return expr or None

    @classmethod
    def _odata(cls, node: Any, kinds: Dict[str, str]) -> str:
        if not isinstance(node, dict) or not node:
            raise _Unsupported
        if "$and" in node or "$or" in node:
            op = "and" if "$and" in node else "or"
            parts = [cls._odata(n, kinds) for n in node["$" + op]]
            if not parts:
                raise _Unsupported
            return "(" + f" {op} ".join(parts) + ")"
        if "field" in node and "op" in node:
            return cls._clause(node["field"], node["op"], node.get("value"), kinds)
        # The field-keyed form: {"f": v} or {"f": {"$in": [...]}}.
        parts = []
        for field, spec in node.items():
            if isinstance(spec, dict) and spec and all(k.startswith("$") for k in spec):
                for op, value in spec.items():
                    parts.append(cls._clause(field, op[1:], value, kinds))
            else:
                parts.append(cls._clause(field, "eq", spec, kinds))
        return "(" + " and ".join(parts) + ")" if len(parts) > 1 else parts[0]

    @staticmethod
    def _literal(value: Any, kind: str) -> str:
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _Unsupported
            return repr(float(value))
        if kind == "boolean":
            if not isinstance(value, bool):
                raise _Unsupported
            return "true" if value else "false"
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise _Unsupported
        return _odata_str(str(value))

    @staticmethod
    def _search_in(name: str, values: Any) -> str:
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        items = []
        for v in values:
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise _Unsupported
            items.append(str(v))
        if not items:
            raise _Unsupported
        sep = next((s for s in _SEPARATORS if not any(s in v for v in items)), None)
        if sep is None:
            raise _Unsupported
        return f"search.in({name}, {_odata_str(sep.join(items))}, '{sep}')"

    @classmethod
    def _clause(cls, field: str, op: str, value: Any, kinds: Dict[str, str]) -> str:
        kind = kinds.get(field)
        if kind is None:
            raise _Unsupported
        name = _field_name(field)
        if kind == "strings":
            if op == "exists":
                return f"{name}/any()" if value else f"not {name}/any()"
            if op == "in":
                return f"({name}/any() and {name}/any(x: {cls._search_in('x', value)}))"
            if op == "nin":
                return f"({name}/any() and not {name}/any(x: {cls._search_in('x', value)}))"
            raise _Unsupported
        guard = f"{name} ne null"
        if op == "exists":
            return guard if value else f"{name} eq null"
        if op in ("eq", "ne", "gt", "gte", "lt", "lte"):
            odata_op = {"eq": "eq", "ne": "ne", "gt": "gt", "gte": "ge", "lt": "lt", "lte": "le"}[op]
            return f"({guard} and {name} {odata_op} {cls._literal(value, kind)})"
        if op in ("in", "nin"):
            if kind == "string":
                inner = cls._search_in(name, value)
            else:
                values = value if isinstance(value, (list, tuple, set)) else [value]
                if not values:
                    raise _Unsupported
                inner = "(" + " or ".join(f"{name} eq {cls._literal(v, kind)}" for v in values) + ")"
            return f"({guard} and {inner})" if op == "in" else f"({guard} and not {inner})"
        raise _Unsupported

    def _vector_query(self, query_vector: List[float], k: int) -> Any:
        from azure.search.documents.models import VectorizedQuery

        return VectorizedQuery(
            vector=[float(x) for x in query_vector], k_nearest_neighbors=k, fields=F_VECTOR
        )

    def _vector_queries(
        self, query_vector: List[float], k: int, only: Optional[str] = None, text: Optional[str] = None
    ) -> List[Any]:
        """One query per vector that answers this search. The collection's
        own vector goes up as numbers. The deployment's goes up as the
        question's words when the index has a vectorizer, so the service
        embeds it, and as numbers embedded here when it has not."""
        from azure.search.documents.models import VectorizedQuery

        selection = _SELECTION.get()
        chosen, query_text = selection if selection else (None, None)
        query_text = text or query_text
        names = [only] if only else self._fields_for(chosen)
        weights = self.config.azure_search_vector_weights or {}
        mode = self.config.azure_search_embeddings
        queries: List[Any] = []
        for name in names:
            weight = {"weight": float(weights[name])} if name in weights and len(names) > 1 else {}
            if name == "vectrixdb":
                queries.append(self._build(VectorizedQuery, weight, vector=[float(x) for x in query_vector], k_nearest_neighbors=k, fields=F_VECTOR))
                continue
            field = F_VECTOR if mode == "azure" else F_VECTOR_AZURE
            if mode == "azure" and not query_text:
                # The collection's embedder is the deployment, so the vector
                # handed in already is the deployment's.
                queries.append(self._build(VectorizedQuery, weight, vector=[float(x) for x in query_vector], k_nearest_neighbors=k, fields=field))
                continue
            if not query_text:
                raise StorageOperationError(
                    "vector_search", BACKEND,
                    "the Azure vector is searched with the question's words, and this search gave only a vector; "
                    "search through Vectrix, or pass vectors='vectrixdb'",
                )
            text_query = self._text_query()
            if text_query is not None and self._vectorizer() is not None:
                queries.append(self._build(text_query, weight, text=query_text, k_nearest_neighbors=k, fields=field))
            else:
                vector = self.embed_azure([query_text])[0]
                queries.append(self._build(VectorizedQuery, weight, vector=vector, k_nearest_neighbors=k, fields=field))
        return queries

    @staticmethod
    def _text_query() -> Any:
        try:
            from azure.search.documents.models import VectorizableTextQuery
        except ImportError:  # pragma: no cover - older SDKs
            return None
        return VectorizableTextQuery

    @staticmethod
    def _build(kind: Any, weight: Dict[str, float], **kwargs: Any) -> Any:
        try:
            return kind(**kwargs, **weight)
        except TypeError:  # pragma: no cover - an SDK from before query weights
            return kind(**kwargs)

    def vector_ranks(
        self, collection: str, query_vector: List[float], limit: int = 50, filter: Optional[str] = None
    ) -> Dict[str, Dict[str, int]]:
        """``{doc_id: {"vectrixdb": rank, "azure": rank}}``, one request per
        vector. The service fuses and does not say who ranked what, so an
        explanation asks each vector on its own. Only explain pays for it."""
        ranks: Dict[str, Dict[str, int]] = {}
        selection = _SELECTION.get()
        for name in self._fields_for(selection[0] if selection else None):
            rows = self._client(collection).search(
                search_text=None,
                vector_queries=self._vector_queries(query_vector, limit, only=name),
                top=limit,
                select=[F_DOC_ID],
                **({"filter": filter} if filter else {}),
            )
            for rank, row in enumerate(rows, start=1):
                ranks.setdefault(row[F_DOC_ID], {})[name] = rank
        return ranks

    def _similar(self, collection: str, query_vector: List[float], limit: int, filter: Optional[str]) -> Dict[str, float]:
        """How similar the nearest documents are, from one vector on its own.

        A hybrid search and a search over two vectors come back as fused
        ranks: the service does not say how close anything was. One vector
        asked alone does, because ``@search.score`` is then
        ``1 / (1 + cosine_distance)``, which Microsoft documents along with
        the way back. The collection's own vector is the one asked, since a
        threshold is set against one model and not a blend of two.
        """
        if not getattr(self.config, "azure_search_relevance", True):
            return {}
        from . import relevance as _relevance

        names = list(self._fields_for(None))
        primary = "vectrixdb" if "vectrixdb" in names else (names[0] if names else None)
        if primary is None:
            return {}
        try:
            rows = self._client(collection).search(
                search_text=None,
                vector_queries=self._vector_queries(query_vector, limit, only=primary),
                top=limit,
                select=[F_DOC_ID],
                **({"filter": filter} if filter else {}),
            )
            found = {}
            for row in rows:
                value = _relevance.from_azure_cosine_score(float(row["@search.score"]))
                if value is not None:
                    found[row[F_DOC_ID]] = value
            return found
        except Exception:  # a number that helps is not worth failing a search that worked
            return {}

    @staticmethod
    def _judged(data: Dict[str, Any], similar: Dict[str, float], doc_id: str, *, fused: bool) -> Dict[str, Any]:
        """The hit, with how relevant it is. The semantic ranker's verdict when
        there is one, from 0 to 4 as Microsoft documents it; the similarity otherwise."""
        if data.get("_semantic_score") is not None:
            data["_vx_relevance"] = round(min(1.0, max(0.0, float(data["_semantic_score"]) / 4.0)), 6)
            data["_vx_relevance_kind"] = "reranker"
        elif doc_id in similar:
            data["_vx_relevance"] = similar[doc_id]
        if fused and similar and doc_id not in similar:
            # Not among the nearest by meaning, so the words are what found it.
            data["_vx_matched_by"] = ["keywords"]
        return data

    def vector_search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10,
        filter: Optional[str] = None,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """Nearest neighbours by the index's HNSW. The third element is a
        distance, ``1 - score``, to match the other backends: Collection
        turns it back into a similarity."""
        from . import relevance as _relevance

        try:
            queries = self._vector_queries(query_vector, limit)
            rows = list(
                self._client(collection).search(
                    search_text=None,
                    vector_queries=queries,
                    top=limit,
                    select=[F_DOC_ID, F_TEXT, F_PAYLOAD],
                    **({"filter": filter} if filter else {}),
                )
            )
            # One vector: the score is a transform of the cosine. Two: it is
            # a fused rank, and the similarity has to be asked for.
            similar = self._similar(collection, query_vector, limit, filter) if len(queries) > 1 else {}
            out = []
            for row in rows:
                data = self._from_row(row, include_vector=False)
                if len(queries) == 1:
                    value = _relevance.from_azure_cosine_score(float(row["@search.score"]))
                    if value is not None:
                        data["_vx_relevance"] = value
                elif row[F_DOC_ID] in similar:
                    data["_vx_relevance"] = similar[row[F_DOC_ID]]
                out.append((row[F_DOC_ID], data, 1.0 - float(row["@search.score"])))
            return out
        except Exception as exc:
            raise StorageOperationError("vector_search", BACKEND, str(exc)) from exc

    def text_search(
        self,
        collection: str,
        query_text: str,
        limit: int = 10,
        filter: Optional[str] = None,
        semantic: Optional[bool] = None,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """BM25 over ``text_content``, the service's own keyword search.

        ``semantic`` puts Azure's semantic ranker on top for this search;
        None leaves it to how the store was opened.
        """
        kwargs: Dict[str, Any] = {"filter": filter} if filter else {}
        config_name = self._use_semantic(collection, semantic)
        if config_name:
            # Update rather than replace: the filter is already in there, and
            # a semantic query that lost it would rank over every document.
            kwargs.update({"query_type": "semantic", "semantic_configuration_name": config_name})
        try:
            rows = self._client(collection).search(
                search_text=query_text, top=limit, select=[F_DOC_ID, F_TEXT, F_PAYLOAD], **kwargs
            )
            return [
                (row[F_DOC_ID], self._judged(self._hit(row), {}, row[F_DOC_ID], fused=False), self._ranked_score(row))
                for row in rows
            ]
        except Exception as exc:
            raise StorageOperationError("text_search", BACKEND, str(exc)) from exc

    def hybrid_search(
        self,
        collection: str,
        query_vector: List[float],
        query_text: Any,
        limit: int = 10,
        dense_weight: float = 0.7,
        sparse_weight: float = 0.3,
        rrf_k: int = 60,
        filter: Optional[str] = None,
        semantic: Optional[bool] = None,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """One request with text and vector; the service fuses them with RRF.

        Azure does not take weights for its fusion, so ``dense_weight`` and
        ``sparse_weight`` are accepted for the common signature and ignored;
        the score returned is the service's fused score. ``semantic`` puts
        Azure's semantic ranker on top for this search; None leaves it to
        how the store was opened.

        The local backends spell this ``hybrid_search(collection, dense,
        sparse_dict)``. Given a sparse dict instead of text, Azure has no
        term ids to search, so the top vector candidates are fetched and
        fused on the client with the sparse dot product over the stored
        ``sparse_embedding``; text is what makes the hybrid native.
        """
        if isinstance(query_text, dict):
            return self._hybrid_with_sparse(
                collection, query_vector, query_text, limit, dense_weight, sparse_weight, rrf_k, filter
            )
        kwargs: Dict[str, Any] = {"filter": filter} if filter else {}
        config_name = self._use_semantic(collection, semantic)
        if config_name:
            # Update rather than replace: the filter is already in there, and
            # a semantic query that lost it would rank over every document.
            kwargs.update({"query_type": "semantic", "semantic_configuration_name": config_name})
        try:
            rows = list(
                self._client(collection).search(
                    search_text=query_text,
                    vector_queries=self._vector_queries(query_vector, max(limit, 50), text=query_text),
                    top=limit,
                    select=[F_DOC_ID, F_TEXT, F_PAYLOAD],
                    **kwargs,
                )
            )
            # The semantic ranker's verdict needs no second question. Without it, one is asked.
            similar = {} if config_name else self._similar(collection, query_vector, max(limit, 50), filter)
            return [
                (row[F_DOC_ID], self._judged(self._hit(row), similar, row[F_DOC_ID], fused=True), self._ranked_score(row))
                for row in rows
            ]
        except Exception as exc:
            raise StorageOperationError("hybrid_search", BACKEND, str(exc)) from exc

    def _hybrid_with_sparse(
        self,
        collection: str,
        query_vector: List[float],
        sparse_query: Dict[Any, float],
        limit: int,
        dense_weight: float,
        sparse_weight: float,
        rrf_k: int,
        filter: Optional[str] = None,
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        prefetch = max(limit * 5, 20)
        dense = self.vector_search(collection, query_vector, prefetch, filter=filter)
        if not dense:
            return []
        wanted = {str(k): float(v) for k, v in sparse_query.items()}
        sparse_scores: Dict[str, float] = {}
        for doc_id, data, _ in dense:
            stored = data.get("sparse_embedding") or {}
            dot = sum(wanted.get(str(k), 0.0) * float(v) for k, v in stored.items())
            if dot > 0:
                sparse_scores[doc_id] = dot
        fused: Dict[str, float] = {}
        payload = {doc_id: data for doc_id, data, _ in dense}
        for rank, (doc_id, _, _) in enumerate(dense):
            fused[doc_id] = fused.get(doc_id, 0.0) + dense_weight / (rrf_k + rank + 1)
        by_sparse = sorted(sparse_scores, key=lambda i: sparse_scores[i], reverse=True)
        for rank, doc_id in enumerate(by_sparse):
            fused[doc_id] = fused.get(doc_id, 0.0) + sparse_weight / (rrf_k + rank + 1)
        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [(doc_id, payload[doc_id], score) for doc_id, score in ordered]
