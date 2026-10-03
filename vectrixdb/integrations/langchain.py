"""LangChain VectorStore over a ``Vectrix`` collection.

    from vectrixdb.integrations.langchain import VectrixVectorStore

    store = VectrixVectorStore("docs", path="./data")          # bundled model, offline
    store.add_texts(["Basalt forms when lava cools."], metadatas=[{"topic": "rocks"}])
    store.similarity_search("volcanic rock", k=3)
    retriever = store.as_retriever(search_kwargs={"k": 5})

The embedding model is VectrixDB's own unless you pass ``embedding=``, in
which case LangChain's ``Embeddings`` object is used for both documents and
queries. Hybrid mode and the reranker are one keyword away
(``search_kwargs={"mode": "hybrid"}``); ``filter=`` takes VectrixDB's filter
grammar. Needs ``pip install langchain-core``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    from langchain_core.documents import Document
    from langchain_core.embeddings import Embeddings
    from langchain_core.vectorstores import VectorStore
except ImportError as exc:  # pragma: no cover - exercised by the import test
    raise ImportError(
        "The LangChain adapter needs langchain-core: pip install langchain-core"
    ) from exc

from ..easy import Vectrix

__all__ = ["VectrixVectorStore"]


# ============================================================================
# THE LANGCHAIN VECTOR STORE
# ============================================================================
#
# INPUT   texts and metadata, from LangChain
# OUTPUT  a VectorStore over a Vectrix collection: the bundled model unless an
#         Embeddings object is passed, hybrid mode and the reranker one
#         keyword away
#
# VectrixDB's filter grammar goes through as filter=.


class VectrixVectorStore(VectorStore):
    """A LangChain ``VectorStore`` backed by VectrixDB."""

    def __init__(
        self,
        name: str = "langchain",
        path: str = "./vectrixdb_data",
        embedding: Optional[Embeddings] = None,
        db: Optional[Vectrix] = None,
        **vectrix_kwargs: Any,
    ) -> None:
        self._embedding = embedding
        if db is not None:
            self._db = db
        elif embedding is not None:
            probe = embedding.embed_query("dimension probe")
            self._db = Vectrix(
                name,
                path=path,
                embed_fn=lambda texts: embedding.embed_documents(list(texts)),
                dimension=len(probe),
                **vectrix_kwargs,
            )
        else:
            self._db = Vectrix(name, path=path, **vectrix_kwargs)

    # ------------------------------------------------------------- required

    @property
    def embeddings(self) -> Optional[Embeddings]:
        return self._embedding

    @property
    def db(self) -> Vectrix:
        """The underlying ``Vectrix`` for anything the adapter does not expose."""
        return self._db

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> List[str]:
        texts = list(texts)
        if not texts:
            return []
        self._db.add(texts, metadata=metadatas, ids=ids)
        report = self._db.last_add_report
        return (
            list(report.added) if report is not None else [self._db._generate_id(t) for t in texts]
        )

    def delete(self, ids: Optional[List[str]] = None, **kwargs: Any) -> Optional[bool]:
        if not ids:
            return False
        self._db.delete(ids)
        return True

    def similarity_search(
        self, query: str, k: int = 4, filter: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> List[Document]:
        return [
            doc for doc, _ in self.similarity_search_with_score(query, k=k, filter=filter, **kwargs)
        ]

    def similarity_search_with_score(
        self, query: str, k: int = 4, filter: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        results = self._db.search(query, limit=k, filter=filter, **kwargs)
        return [
            (
                Document(page_content=r.text, metadata={**r.metadata, "id": r.id}, id=r.id),
                float(r.score),
            )
            for r in results
        ]

    def similarity_search_by_vector(
        self, embedding: List[float], k: int = 4, **kwargs: Any
    ) -> List[Document]:
        coll: Any = self._db._collection
        hits = coll.search(query=embedding, limit=k).results
        return [
            Document(
                page_content=self._db._texts.get(h.id, h.text or ""),
                metadata={**(h.metadata or {}), "id": h.id},
                id=h.id,
            )
            for h in hits
        ]

    def _select_relevance_score_fn(self) -> Callable[[float], float]:
        # Scores from search() are already similarities in [0, 1] for dense
        # mode; the fused modes return fused scores, also non-negative.
        return lambda score: max(0.0, min(1.0, float(score)))

    @classmethod
    def from_texts(
        cls,
        texts: List[str],
        embedding: Optional[Embeddings] = None,
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> "VectrixVectorStore":
        store = cls(embedding=embedding, **kwargs)
        store.add_texts(texts, metadatas=metadatas, ids=ids)
        return store

    def close(self) -> None:
        self._db.close()
