"""LlamaIndex vector store over a ``Vectrix`` collection.

    from llama_index.core import VectorStoreIndex, StorageContext
    from vectrixdb.integrations.llamaindex import VectrixLlamaStore

    store = VectrixLlamaStore("docs", path="./data")
    index = VectorStoreIndex.from_documents(
        documents, storage_context=StorageContext.from_defaults(vector_store=store)
    )

LlamaIndex embeds nodes with its own ``embed_model`` and hands the vectors
over, so the collection is created at that model's dimension on the first
``add()``. Queries arrive as vectors too. Set ``embed_model`` to whatever you
like; the store does not embed. Needs ``pip install llama-index-core``.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

import numpy as np

try:
    from llama_index.core.schema import BaseNode, TextNode
    from llama_index.core.vector_stores.types import (
        BasePydanticVectorStore,
        VectorStoreQuery,
        VectorStoreQueryResult,
    )
    from llama_index.core.vector_stores.utils import node_to_metadata_dict
except ImportError as exc:  # pragma: no cover - exercised by the import test
    raise ImportError(
        "The LlamaIndex adapter needs llama-index-core: pip install llama-index-core"
    ) from exc

from ..easy import Vectrix

__all__ = ["VectrixLlamaStore"]


# ============================================================================
# THE LLAMAINDEX VECTOR STORE
# ============================================================================
#
# INPUT   nodes with vectors LlamaIndex embedded
# OUTPUT  a VectorStore over a Vectrix collection, created at that model's
#         dimension on the first add; queries arrive as vectors too
#
# The store does not embed; LlamaIndex's embed_model does.


class VectrixLlamaStore(BasePydanticVectorStore):
    """A LlamaIndex ``VectorStore`` backed by VectrixDB, vectors supplied by LlamaIndex."""

    stores_text: bool = True
    flat_metadata: bool = False

    _name: str
    _path: str
    _kwargs: dict
    _db: Optional[Vectrix]

    def __init__(
        self,
        name: str = "llamaindex",
        path: str = "./vectrixdb_data",
        **vectrix_kwargs: Any,
    ) -> None:
        super().__init__(stores_text=True)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_kwargs", dict(vectrix_kwargs))
        object.__setattr__(self, "_db", None)

    @classmethod
    def class_name(cls) -> str:
        return "VectrixLlamaStore"

    def _open(self, dimension: Optional[int]) -> Vectrix:
        if self._db is None:
            kwargs = dict(self._kwargs)
            if dimension:
                kwargs.setdefault("dimension", dimension)
            object.__setattr__(self, "_db", Vectrix(self._name, path=self._path, **kwargs))
        return self._db  # type: ignore[return-value]

    @property
    def client(self) -> Any:
        return self._open(None)

    def add(self, nodes: Sequence[BaseNode], **add_kwargs: Any) -> List[str]:
        if not nodes:
            return []
        vectors = [n.get_embedding() for n in nodes]
        db = self._open(len(vectors[0]))
        texts = [n.get_content() for n in nodes]
        metas = []
        for n in nodes:
            meta = node_to_metadata_dict(n, remove_text=True, flat_metadata=self.flat_metadata)
            meta["ref_doc_id"] = n.ref_doc_id
            metas.append(meta)
        ids = [n.node_id for n in nodes]
        db.add(texts, metadata=metas, ids=ids, vectors=np.asarray(vectors, dtype=np.float32))
        return ids

    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        db = self._open(None)
        coll: Any = db._collection
        ids = [i for i, _, m in coll._iter_documents_raw() if m.get("ref_doc_id") == ref_doc_id]
        if ids:
            db.delete(ids)

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        db = self._open(None)
        if query.query_embedding is None:
            raise ValueError("VectrixLlamaStore needs query_embedding; set an embed_model")
        coll: Any = db._collection
        hits = coll.search(query=query.query_embedding, limit=query.similarity_top_k).results
        nodes: List[TextNode] = []
        ids: List[str] = []
        scores: List[float] = []
        for h in hits:
            meta = dict(h.metadata or {})
            ref = meta.pop("ref_doc_id", None)
            node = TextNode(id_=h.id, text=db._texts.get(h.id, h.text or ""), metadata=meta)
            if ref:
                node.relationships = node.relationships or {}
            nodes.append(node)
            ids.append(h.id)
            scores.append(float(h.score))
        return VectorStoreQueryResult(nodes=nodes, similarities=scores, ids=ids)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            object.__setattr__(self, "_db", None)
