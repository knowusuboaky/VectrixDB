"""
VectrixDB Easy API - The Simplest Vector Database in the World

Zero config. Text in, results out. One line for everything.

Example:
    >>> from vectrixdb import Vectrix
    >>>
    >>> # Create and add - ONE LINE
    >>> db = Vectrix("my_docs").add(["Python is great", "Machine learning is fun"])
    >>>
    >>> # Search - ONE LINE
    >>> results = db.search("programming")
    >>>
    >>> # Full power - STILL ONE LINE
    >>> results = db.search("AI", mode="ultimate")  # dense + sparse + rerank

Comparison with competitors:

    # Chroma (4 lines)
    client = chromadb.Client()
    collection = client.create_collection("docs")
    collection.add(documents=["text"], ids=["1"])
    results = collection.query(query_texts=["query"])

    # Pinecone (5+ lines + API key + manual embedding)
    pinecone.init(api_key="...")
    index = pinecone.Index("docs")
    embedding = model.encode("text")  # manual!
    index.upsert(vectors=[...])
    results = index.query(vector=embedding)

    # VectrixDB (1 line each)
    db = Vectrix("docs").add(["text"])
    results = db.search("query")

Author: VectrixDB Team
"""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Literal
from dataclasses import dataclass, field
import numpy as np


# =============================================================================
# Result Types
# =============================================================================

@dataclass
class Result:
    """Single search result."""
    id: str
    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self):
        preview = self.text[:50] + "..." if len(self.text) > 50 else self.text
        return f"Result(score={self.score:.4f}, text='{preview}')"


@dataclass
class Results:
    """Search results with convenient access."""
    items: List[Result]
    query: str
    mode: str
    time_ms: float

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]

    @property
    def texts(self) -> List[str]:
        """Get all result texts."""
        return [r.text for r in self.items]

    @property
    def ids(self) -> List[str]:
        """Get all result IDs."""
        return [r.id for r in self.items]

    @property
    def scores(self) -> List[float]:
        """Get all scores."""
        return [r.score for r in self.items]

    @property
    def top(self) -> Optional[Result]:
        """Get top result."""
        return self.items[0] if self.items else None

    def __repr__(self):
        return f"Results({len(self.items)} results for '{self.query[:30]}...' in {self.time_ms:.1f}ms)"


# =============================================================================
# Main API
# =============================================================================

class Vectrix:
    """
    Vectrix - The simplest vector database.

    Example:
        >>> db = Vectrix("my_collection")
        >>> db.add(["doc 1", "doc 2", "doc 3"])
        >>> results = db.search("query")
        >>> print(results.top.text)

    With metadata:
        >>> db.add(
        ...     texts=["Python guide", "ML tutorial"],
        ...     metadata=[{"category": "programming"}, {"category": "ai"}]
        ... )
        >>> results = db.search("code", filter={"category": "programming"})

    Full power:
        >>> results = db.search(
        ...     "machine learning",
        ...     mode="ultimate",    # dense + sparse + late interaction
        ...     rerank="mmr",       # diversity reranking
        ...     limit=10
        ... )
    """

    # Default embedding model (bundled, no network calls)
    _default_model = "vectrixdb/all-MiniLM-L6-v2"  # Bundled ONNX model
    _default_dimension = 384

    # Shared model cache
    _model_cache: Dict[str, Any] = {}

    # Sparse embedder (BM25) - shared instance
    _sparse_embedder = None

    # Reranker - shared instance
    _reranker = None

    # Supported model prefixes and their handlers
    _MODEL_REGISTRY = {
        # Bundled models (no network calls after setup)
        "vectrixdb/all-MiniLM-L6-v2": {"type": "embedded", "dimension": 384},
        "vectrixdb/bm25": {"type": "sparse"},
        "vectrixdb/ms-marco-MiniLM-L-6-v2": {"type": "reranker"},

        # Qdrant FastEmbed models (cached after first download)
        "qdrant/all-MiniLM-L6-v2": {"type": "fastembed", "dimension": 384, "model_id": "sentence-transformers/all-MiniLM-L6-v2"},
        "qdrant/bge-small-en-v1.5": {"type": "fastembed", "dimension": 384, "model_id": "BAAI/bge-small-en-v1.5"},
        "qdrant/bge-base-en-v1.5": {"type": "fastembed", "dimension": 768, "model_id": "BAAI/bge-base-en-v1.5"},
        "qdrant/bge-large-en-v1.5": {"type": "fastembed", "dimension": 1024, "model_id": "BAAI/bge-large-en-v1.5"},
        "qdrant/bm25": {"type": "fastembed-sparse", "model_id": "Qdrant/bm25"},
        "qdrant/colbert-v2": {"type": "fastembed-colbert", "dimension": 128, "model_id": "colbert-ir/colbertv2.0"},
        "qdrant/clip-ViT-B-32": {"type": "fastembed", "dimension": 512, "model_id": "Qdrant/clip-ViT-B-32-text"},

        # Sentence-transformers models (HuggingFace)
        "sentence-transformers/all-MiniLM-L6-v2": {"type": "sentence-transformers", "dimension": 384},
        "sentence-transformers/all-mpnet-base-v2": {"type": "sentence-transformers", "dimension": 768},
        "sentence-transformers/all-MiniLM-L12-v2": {"type": "sentence-transformers", "dimension": 384},
        "sentence-transformers/paraphrase-MiniLM-L6-v2": {"type": "sentence-transformers", "dimension": 384},
        "sentence-transformers/multi-qa-MiniLM-L6-cos-v1": {"type": "sentence-transformers", "dimension": 384},

        # BAAI models (via sentence-transformers or fastembed)
        "BAAI/bge-small-en-v1.5": {"type": "sentence-transformers", "dimension": 384},
        "BAAI/bge-base-en-v1.5": {"type": "sentence-transformers", "dimension": 768},
        "BAAI/bge-large-en-v1.5": {"type": "sentence-transformers", "dimension": 1024},
        "BAAI/bge-m3": {"type": "sentence-transformers", "dimension": 1024},

        # OpenAI (requires embed_fn)
        "openai/text-embedding-3-small": {"type": "openai", "dimension": 1536},
        "openai/text-embedding-3-large": {"type": "openai", "dimension": 3072},
        "openai/text-embedding-ada-002": {"type": "openai", "dimension": 1536},

        # Cohere (requires embed_fn)
        "cohere/embed-english-v3.0": {"type": "cohere", "dimension": 1024},
        "cohere/embed-multilingual-v3.0": {"type": "cohere", "dimension": 1024},

        # Voyage AI (requires embed_fn)
        "voyage/voyage-3": {"type": "voyage", "dimension": 1024},
        "voyage/voyage-3-lite": {"type": "voyage", "dimension": 512},

        # Jina AI
        "jina/jina-embeddings-v2-base-en": {"type": "sentence-transformers", "dimension": 768},
        "jina/jina-embeddings-v2-small-en": {"type": "sentence-transformers", "dimension": 512},
    }

    def __init__(
        self,
        name: str = "default",
        path: str = "./vectrixdb_data",
        model: str = None,
        dimension: int = None,
        embed_fn: Any = None,
        model_path: str = None,
        language: str = None,
        tier: str = "dense",
    ):
        """
        Create or open a VectrixDB collection.

        Args:
            name: Collection name
            path: Storage path (default: ./vectrixdb_data)
            model: Embedding model identifier. Examples:
                   - None: Uses bundled "vectrixdb/all-MiniLM-L6-v2" (no network)
                   - "vectrixdb/all-MiniLM-L6-v2": Bundled model (no network)
                   - "sentence-transformers/all-MiniLM-L6-v2": HuggingFace model
                   - "sentence-transformers/all-mpnet-base-v2": HuggingFace model
                   - "BAAI/bge-small-en-v1.5": BGE model via sentence-transformers
                   - "openai/text-embedding-3-small": OpenAI (requires embed_fn)
            dimension: Vector dimension (auto-detected from model)
            embed_fn: Custom embedding function: fn(texts: List[str]) -> np.ndarray
            model_path: Path to custom ONNX model directory
            language: Language for bundled models - None/"multi" for multilingual (default),
                      "en"/"english" for English-optimized (smaller, faster)
            tier: Storage tier - determines search capabilities:
                  - "dense": Vector embeddings only (fastest, smallest)
                  - "hybrid": Vector + BM25 sparse (balanced, default for search modes)
                  - "ultimate": Vector + BM25 + ColBERT late interaction (best quality)
                  - "graph": Ultimate + knowledge graph (for GraphRAG)

        Examples:
            # Bundled model - no network calls (default)
            >>> db = Vectrix("docs")
            >>> db = Vectrix("docs", model="vectrixdb/all-MiniLM-L6-v2")

            # English-optimized (smaller models)
            >>> db = Vectrix("docs", language="en")

            # With tier for hybrid search
            >>> db = Vectrix("docs", tier="hybrid")
            >>> results = db.search("query", mode="hybrid")

            # Sentence-transformers (requires HuggingFace)
            >>> db = Vectrix("docs", model="sentence-transformers/all-mpnet-base-v2")
            >>> db = Vectrix("docs", model="BAAI/bge-small-en-v1.5")

            # Custom embedding function
            >>> db = Vectrix("docs", model="openai/text-embedding-3-small", embed_fn=openai_embed)

            # Custom ONNX model
            >>> db = Vectrix("docs", model_path="/path/to/my/model", dimension=768)
        """
        self.name = name
        self.path = path
        self.embed_fn = embed_fn
        self.model_path = model_path
        self.language = language
        self.tier = tier.lower() if tier else "dense"

        # Validate tier
        valid_tiers = ["dense", "hybrid", "ultimate", "graph"]
        if self.tier not in valid_tiers:
            raise ValueError(f"Invalid tier '{tier}'. Must be one of: {valid_tiers}")

        # Parse model identifier
        self._parse_model(model, dimension)

        self._model = None
        self._db = None
        self._collection = None
        self._texts: Dict[str, str] = {}  # id -> text storage
        self._instance_reranker = None  # Instance-level reranker (respects language)
        self._instance_late_interaction = None  # Instance-level late interaction

        self._init_db()

    def _parse_model(self, model: str, dimension: int):
        """Parse model identifier and set up embedding configuration."""
        # Custom embedding function provided
        if self.embed_fn is not None:
            self.model_type = "custom"
            self.model_name = model or "custom"
            self.dimension = dimension or self._get_dimension_from_registry(model) or 384
            return

        # Custom ONNX model path provided
        if self.model_path is not None:
            self.model_type = "custom-onnx"
            self.model_name = "custom-onnx"
            self.dimension = dimension or 384
            return

        # No model specified - use bundled default
        if model is None:
            self.model_type = "embedded"
            self.model_name = "vectrixdb/all-MiniLM-L6-v2"
            self.dimension = dimension or 384
            return

        # Check registry for known models
        if model in self._MODEL_REGISTRY:
            config = self._MODEL_REGISTRY[model]
            self.model_type = config["type"]
            self.model_name = model
            self.dimension = dimension or config.get("dimension", 384)
            return

        # Handle prefix patterns
        if model.startswith("vectrixdb/"):
            self.model_type = "embedded"
            self.model_name = model
            self.dimension = dimension or 384
        elif model.startswith("qdrant/"):
            self.model_type = "fastembed"
            self.model_name = model
            self.dimension = dimension or 384
        elif model.startswith("sentence-transformers/") or model.startswith("BAAI/") or model.startswith("jina/"):
            self.model_type = "sentence-transformers"
            self.model_name = model
            self.dimension = dimension or self._get_model_dimension(model)
        elif model.startswith("openai/"):
            if self.embed_fn is None:
                raise ValueError(
                    f"Model '{model}' requires embed_fn parameter.\n"
                    f"Example: Vectrix('docs', model='{model}', embed_fn=your_openai_function)"
                )
            self.model_type = "openai"
            self.model_name = model
            self.dimension = dimension or 1536
        elif model.startswith("cohere/"):
            if self.embed_fn is None:
                raise ValueError(
                    f"Model '{model}' requires embed_fn parameter.\n"
                    f"Example: Vectrix('docs', model='{model}', embed_fn=your_cohere_function)"
                )
            self.model_type = "cohere"
            self.model_name = model
            self.dimension = dimension or 1024
        elif model.startswith("voyage/"):
            if self.embed_fn is None:
                raise ValueError(
                    f"Model '{model}' requires embed_fn parameter.\n"
                    f"Example: Vectrix('docs', model='{model}', embed_fn=your_voyage_function)"
                )
            self.model_type = "voyage"
            self.model_name = model
            self.dimension = dimension or 1024
        else:
            # Assume sentence-transformers for unknown models
            self.model_type = "sentence-transformers"
            self.model_name = model
            self.dimension = dimension or self._get_model_dimension(model)

    def _get_dimension_from_registry(self, model: str) -> Optional[int]:
        """Get dimension from model registry."""
        if model and model in self._MODEL_REGISTRY:
            return self._MODEL_REGISTRY[model].get("dimension")
        return None

    def _get_model_dimension(self, model_name: str) -> int:
        """Get dimension for known models."""
        dimensions = {
            "all-MiniLM-L6-v2": 384,
            "all-mpnet-base-v2": 768,
            "all-MiniLM-L12-v2": 384,
            "paraphrase-MiniLM-L6-v2": 384,
            "multi-qa-MiniLM-L6-cos-v1": 384,
            "msmarco-MiniLM-L6-cos-v5": 384,
        }
        return dimensions.get(model_name, 384)

    def _init_db(self):
        """Initialize the database and collection."""
        from .core.database import VectrixDB

        self._db = VectrixDB(self.path)

        try:
            self._collection = self._db.get_collection(self.name)
        except:
            self._collection = self._db.create_collection(
                name=self.name,
                dimension=self.dimension,
                metric="cosine",
                enable_text_index=True
            )

    @property
    def model(self):
        """Lazy load embedding model based on model_type."""
        if self._model is None:
            # Custom embedding function - no model needed
            if self.model_type == "custom":
                return None

            cache_key = self.model_path or self.model_name

            if cache_key in self._model_cache:
                self._model = self._model_cache[cache_key]
            elif self.model_type == "custom-onnx":
                # Custom ONNX model from user-provided path
                try:
                    from .models import DenseEmbedder
                    from pathlib import Path
                    self._model = DenseEmbedder(
                        model_dir=Path(self.model_path),
                        dimension=self.dimension
                    )
                    self._model_cache[cache_key] = self._model
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to load custom ONNX model from {self.model_path}: {e}"
                    )
            elif self.model_type == "embedded":
                # Use bundled ONNX model - NO NETWORK CALLS
                try:
                    from .models import DenseEmbedder
                    self._model = DenseEmbedder(language=self.language)
                    self._model_cache[cache_key] = self._model
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to load embedded model: {e}\n"
                        "Run: vectrixdb download-models"
                    )
            elif self.model_type == "fastembed":
                # Qdrant FastEmbed (cached after first download)
                try:
                    from fastembed import TextEmbedding
                    # Get model_id from registry or use model name
                    model_id = self._MODEL_REGISTRY.get(self.model_name, {}).get("model_id", self.model_name.replace("qdrant/", ""))
                    self._model = TextEmbedding(model_name=model_id)
                    self._model_cache[cache_key] = self._model
                except ImportError:
                    raise ImportError(
                        "Install fastembed: pip install fastembed\n"
                        "Or use bundled model: Vectrix('docs', model='vectrixdb/all-MiniLM-L6-v2')"
                    )
            elif self.model_type == "fastembed-sparse":
                # Qdrant FastEmbed Sparse (BM25)
                try:
                    from fastembed import SparseTextEmbedding
                    model_id = self._MODEL_REGISTRY.get(self.model_name, {}).get("model_id", "Qdrant/bm25")
                    self._model = SparseTextEmbedding(model_name=model_id)
                    self._model_cache[cache_key] = self._model
                except ImportError:
                    raise ImportError(
                        "Install fastembed: pip install fastembed\n"
                        "Or use bundled model: Vectrix('docs', model='vectrixdb/bm25')"
                    )
            elif self.model_type == "fastembed-colbert":
                # Qdrant FastEmbed ColBERT (Late Interaction)
                try:
                    from fastembed import LateInteractionTextEmbedding
                    model_id = self._MODEL_REGISTRY.get(self.model_name, {}).get("model_id", "colbert-ir/colbertv2.0")
                    self._model = LateInteractionTextEmbedding(model_name=model_id)
                    self._model_cache[cache_key] = self._model
                except ImportError:
                    raise ImportError(
                        "Install fastembed: pip install fastembed"
                    )
            elif self.model_type == "sentence-transformers":
                # Sentence-transformers (requires network for first download)
                try:
                    from sentence_transformers import SentenceTransformer
                    # Remove prefix if present
                    model_id = self.model_name
                    for prefix in ["sentence-transformers/", "BAAI/", "jina/"]:
                        if model_id.startswith(prefix):
                            model_id = self.model_name  # Keep full name for BAAI/jina
                            break
                    if model_id.startswith("sentence-transformers/"):
                        model_id = model_id.replace("sentence-transformers/", "")
                    self._model = SentenceTransformer(model_id)
                    self._model_cache[cache_key] = self._model
                except ImportError:
                    raise ImportError(
                        "Install sentence-transformers: pip install sentence-transformers\n"
                        "Or use bundled model: Vectrix('docs', model='vectrixdb/all-MiniLM-L6-v2')"
                    )
            else:
                # OpenAI, Cohere, Voyage, etc. - require custom function
                raise ValueError(
                    f"Model '{self.model_name}' requires embed_fn parameter."
                )
        return self._model

    @property
    def sparse_embedder(self):
        """Lazy load sparse (BM25) embedder."""
        if self._sparse_embedder is None:
            from .models import SparseEmbedder
            Vectrix._sparse_embedder = SparseEmbedder()
        return self._sparse_embedder

    @property
    def reranker(self):
        """Lazy load cross-encoder reranker (respects language setting)."""
        if self._instance_reranker is None:
            from .models import RerankerEmbedder
            self._instance_reranker = RerankerEmbedder(language=self.language)
        return self._instance_reranker

    @property
    def late_interaction(self):
        """Lazy load late interaction embedder (respects language setting)."""
        if self._instance_late_interaction is None:
            from .models import LateInteractionEmbedder
            self._instance_late_interaction = LateInteractionEmbedder(language=self.language)
        return self._instance_late_interaction

    def _generate_id(self, text: str) -> str:
        """Generate deterministic ID from text."""
        return hashlib.md5(text.encode()).hexdigest()[:12]

    def _embed(self, texts: Union[str, List[str]]) -> np.ndarray:
        """Embed text(s) to vectors."""
        if isinstance(texts, str):
            texts = [texts]

        if self.model_type == "custom":
            # Use custom embedding function
            result = self.embed_fn(texts)
            if not isinstance(result, np.ndarray):
                result = np.array(result, dtype=np.float32)
            return result
        elif self.model_type in ("embedded", "custom-onnx"):
            # Use bundled or custom ONNX model
            return self.model.embed(texts)
        elif self.model_type == "fastembed":
            # Use Qdrant FastEmbed (returns generator)
            embeddings = list(self.model.embed(texts))
            return np.array(embeddings, dtype=np.float32)
        elif self.model_type == "fastembed-sparse":
            # Use Qdrant FastEmbed Sparse - returns sparse vectors
            # This returns a different format, handled separately
            return list(self.model.embed(texts))
        elif self.model_type == "fastembed-colbert":
            # Use Qdrant FastEmbed ColBERT (Late Interaction)
            embeddings = list(self.model.embed(texts))
            return embeddings  # Returns list of token embeddings
        elif self.model_type == "sentence-transformers":
            # Use sentence-transformers
            return self.model.encode(texts, show_progress_bar=len(texts) > 100)
        else:
            # OpenAI, Cohere, Voyage, etc. - must use embed_fn
            if self.embed_fn:
                result = self.embed_fn(texts)
                if not isinstance(result, np.ndarray):
                    result = np.array(result, dtype=np.float32)
                return result
            raise ValueError(f"Model type '{self.model_type}' requires embed_fn")

    def _embed_sparse(self, texts: Union[str, List[str]]) -> List[Dict[int, float]]:
        """Generate sparse BM25 embeddings (no network calls)."""
        if isinstance(texts, str):
            texts = [texts]
        return self.sparse_embedder.embed(texts)

    # =========================================================================
    # Core Operations
    # =========================================================================

    def add(
        self,
        texts: Union[str, List[str]],
        metadata: Union[Dict, List[Dict]] = None,
        ids: List[str] = None,
    ) -> Vectrix:
        """
        Add texts to the collection.

        Args:
            texts: Single text or list of texts
            metadata: Optional metadata for each text
            ids: Optional custom IDs (auto-generated if not provided)

        Returns:
            Self for chaining

        Example:
            >>> db = Vectrix("docs").add(["text 1", "text 2"])
            >>> db.add("another text", metadata={"source": "web"})
        """
        # Normalize inputs
        if isinstance(texts, str):
            texts = [texts]

        if metadata is None:
            metadata = [{} for _ in texts]
        elif isinstance(metadata, dict):
            metadata = [metadata]

        if ids is None:
            ids = [self._generate_id(t) for t in texts]

        # Generate embeddings
        vectors = self._embed(texts)

        # Store texts for retrieval
        for id_, text in zip(ids, texts):
            self._texts[id_] = text

        # Add to collection
        self._collection.add(
            ids=ids,
            vectors=vectors,
            metadata=metadata,
            texts=texts
        )

        return self  # Enable chaining

    def search(
        self,
        query: str,
        limit: int = 10,
        mode: Literal["dense", "sparse", "hybrid", "ultimate", "neural"] = "hybrid",
        rerank: Literal[None, "mmr", "exact", "cross-encoder"] = None,
        filter: Dict[str, Any] = None,
        diversity: float = 0.7,
    ) -> Results:
        """
        Search the collection.

        Args:
            query: Search query text
            limit: Number of results (default: 10)
            mode: Search mode
                - "dense": Semantic search only
                - "sparse": Keyword/BM25 only
                - "hybrid": Dense + sparse combined (default)
                - "ultimate": Full pipeline with cross-encoder reranking
                - "neural": Advanced neural hybrid (ColBERT + cross-encoder)
            rerank: Reranking method
                - None: No reranking
                - "mmr": Maximal Marginal Relevance (diversity)
                - "exact": Exact score recalculation
                - "cross-encoder": Neural cross-encoder
            filter: Metadata filter (e.g., {"category": "tech"})
            diversity: Diversity parameter for MMR (0-1, default: 0.7)

        Returns:
            Results object with search results

        Example:
            >>> results = db.search("python programming")
            >>> results = db.search("AI", mode="ultimate", rerank="mmr")
            >>> results = db.search("AI", mode="neural")  # Best quality
            >>> print(results.top.text)
        """
        import time
        start = time.time()

        # Embed query
        query_vector = self._embed(query)[0]

        # Determine search strategy
        if mode == "ultimate":
            results = self._ultimate_search(query, query_vector, limit, filter, diversity)
        elif mode == "neural":
            results = self._neural_search(query, query_vector, limit, filter)
        elif mode == "dense":
            results = self._dense_search(query_vector, limit, filter)
        elif mode == "sparse":
            results = self._sparse_search(query, limit, filter)
        elif mode == "hybrid":
            results = self._hybrid_search(query, query_vector, limit, filter)
        else:
            raise ValueError(f"Unknown mode: {mode}. Use 'dense', 'sparse', 'hybrid', 'ultimate', or 'neural'")

        # Apply reranking if requested
        if rerank and mode != "ultimate":  # ultimate already includes reranking
            results = self._rerank(query, query_vector, results, rerank, limit, diversity)

        elapsed = (time.time() - start) * 1000

        # Convert to Results
        return Results(
            items=[
                Result(
                    id=r["id"],
                    text=self._texts.get(r["id"], r.get("text", "")),
                    score=r["score"],
                    metadata=r.get("metadata", {})
                )
                for r in results
            ],
            query=query,
            mode=mode,
            time_ms=elapsed
        )

    def _dense_search(
        self,
        query_vector: np.ndarray,
        limit: int,
        filter: Dict = None
    ) -> List[Dict]:
        """Pure dense/semantic search."""
        results = self._collection.search(
            query=query_vector,
            limit=limit,
            filter=filter
        )
        return [
            {"id": r.id, "score": r.score, "metadata": r.metadata, "vector": r.vector}
            for r in results.results
        ]

    def _sparse_search(
        self,
        query: str,
        limit: int,
        filter: Dict = None
    ) -> List[Dict]:
        """Pure sparse/keyword search."""
        results = self._collection.keyword_search(
            query_text=query,
            limit=limit,
            filter=filter
        )
        return [
            {"id": r.id, "score": r.score, "metadata": r.metadata}
            for r in results.results
        ]

    def _hybrid_search(
        self,
        query: str,
        query_vector: np.ndarray,
        limit: int,
        filter: Dict = None
    ) -> List[Dict]:
        """
        Enhanced hybrid dense + sparse search.

        Uses optimized RRF fusion with:
        - Balanced weights (0.5/0.5) for better combination
        - Larger prefetch pool (10x limit)
        - Intersection boost for documents found by both methods
        """
        results = self._collection.hybrid_search(
            query=query_vector,
            query_text=query,
            limit=limit,
            vector_weight=0.5,  # Balanced weights work better
            text_weight=0.5,
            filter=filter,
            include_vectors=True,
            rrf_k=60,
            prefetch_multiplier=10,  # Get more candidates
        )
        return [
            {"id": r.id, "score": r.score, "metadata": r.metadata, "vector": r.vector}
            for r in results.results
        ]

    def _ultimate_search(
        self,
        query: str,
        query_vector: np.ndarray,
        limit: int,
        filter: Dict = None,
        diversity: float = 0.7
    ) -> List[Dict]:
        """
        Ultimate search: dense + sparse + optimized RRF fusion + cross-encoder reranking.

        Enhanced pipeline with:
        - Larger prefetch pools (10x limit)
        - Optimized RRF with intersection boost
        - Optional cross-encoder reranking for best accuracy
        """
        from .core.search import RRFFusion
        from .core.advanced_search import Reranker, RerankConfig, RerankMethod

        # Stage 1: Get large candidate pools from multiple sources
        prefetch_limit = min(limit * 10, self._collection.count())

        dense_results = self._collection.search(
            query=query_vector,
            limit=prefetch_limit,
            filter=filter,
            include_vectors=True
        )

        sparse_results = self._collection.keyword_search(
            query_text=query,
            limit=prefetch_limit,
            filter=filter
        )

        # Stage 2: Enhanced RRF Fusion with intersection boost
        rrf_k = 60
        scores: Dict[str, Dict] = {}

        # Build maps for metadata and vectors
        vector_map = {r.id: r.vector for r in dense_results.results if r.vector is not None}
        metadata_map = {r.id: r.metadata for r in dense_results.results}

        # Add dense results with RRF scores
        for rank, r in enumerate(dense_results.results):
            scores[r.id] = {
                "rrf_dense": 1.0 / (rrf_k + rank + 1),
                "rrf_sparse": 0,
                "dense_score": r.score,
                "sparse_score": 0,
                "metadata": r.metadata
            }

        # Add sparse results with RRF scores
        for rank, r in enumerate(sparse_results.results):
            if r.id not in scores:
                scores[r.id] = {
                    "rrf_dense": 0,
                    "rrf_sparse": 0,
                    "dense_score": 0,
                    "sparse_score": 0,
                    "metadata": r.metadata
                }
                metadata_map[r.id] = r.metadata
            scores[r.id]["rrf_sparse"] = 1.0 / (rrf_k + rank + 1)
            scores[r.id]["sparse_score"] = r.score

        # Calculate combined scores with intersection boost
        for doc_id in scores:
            rrf_dense = scores[doc_id]["rrf_dense"]
            rrf_sparse = scores[doc_id]["rrf_sparse"]

            # Equal weight combination (research shows this works best)
            combined = 0.5 * rrf_dense + 0.5 * rrf_sparse

            # Intersection boost: documents found by both methods get boosted
            if rrf_dense > 0 and rrf_sparse > 0:
                combined *= 1.15  # 15% boost for appearing in both

            scores[doc_id]["combined"] = combined

        # Sort by combined score
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x]["combined"], reverse=True)

        # Build candidate list (top candidates for reranking)
        rerank_limit = min(limit * 3, len(sorted_ids))
        candidates = []
        for doc_id in sorted_ids[:rerank_limit]:
            if doc_id in vector_map:
                candidates.append({
                    "id": doc_id,
                    "score": scores[doc_id]["combined"],
                    "vector": vector_map[doc_id],
                    "metadata": metadata_map.get(doc_id, {})
                })

        # Stage 3: Cross-encoder reranking (if available) or exact score reranking
        if candidates:
            # Try cross-encoder first for best results
            try:
                reranker = Reranker(RerankConfig(
                    method=RerankMethod.CROSS_ENCODER,
                ))
                reranked = reranker.rerank(
                    query_vector=query_vector,
                    candidates=candidates,
                    limit=limit,
                    query_text=query
                )
                return reranked
            except Exception:
                # Fall back to exact score reranking
                reranker = Reranker(RerankConfig(
                    method=RerankMethod.EXACT,
                ))
                reranked = reranker.rerank(
                    query_vector=query_vector,
                    candidates=candidates,
                    limit=limit
                )
                return reranked

        return candidates[:limit]

    def _neural_search(
        self,
        query: str,
        query_vector: np.ndarray,
        limit: int,
        filter: Dict = None
    ) -> List[Dict]:
        """
        Neural hybrid search using ColBERT + cross-encoder.

        This is the most advanced search mode, combining:
        - Dense semantic search
        - BM25 keyword search
        - ColBERT late interaction scoring
        - Cross-encoder reranking

        This should match or exceed Qdrant's best hybrid search.
        """
        from .core.neural_search import NeuralHybridSearcher

        # Get large candidate pools
        prefetch_limit = min(limit * 10, self._collection.count())

        # Dense search
        dense_results = self._collection.search(
            query=query_vector,
            limit=prefetch_limit,
            filter=filter,
            include_vectors=True
        )

        # Sparse search
        sparse_results = self._collection.keyword_search(
            query_text=query,
            limit=prefetch_limit,
            filter=filter
        )

        # Build document texts map for ColBERT and cross-encoder
        document_texts = {}
        for r in dense_results.results:
            doc_id = r.id
            if doc_id in self._texts:
                document_texts[doc_id] = self._texts[doc_id]

        for r in sparse_results.results:
            doc_id = r.id
            if doc_id in self._texts and doc_id not in document_texts:
                document_texts[doc_id] = self._texts[doc_id]

        # Use neural hybrid searcher
        searcher = NeuralHybridSearcher(
            use_colbert=True,
            use_splade=False,  # SPLADE requires specific models
            use_cross_encoder=True,
            colbert_weight=0.3,
            dense_weight=0.35,
            sparse_weight=0.35,
        )

        # Convert results to expected format
        dense_list = [
            {"id": r.id, "score": r.score, "metadata": r.metadata, "vector": r.vector}
            for r in dense_results.results
        ]
        sparse_list = [(r.id, r.score) for r in sparse_results.results]

        results = searcher.search(
            query=query,
            query_vector=query_vector,
            dense_results=dense_list,
            sparse_results=sparse_list,
            document_texts=document_texts,
            limit=limit,
            prefetch_limit=prefetch_limit,
        )

        # Add text to results
        for r in results:
            if r["id"] in self._texts:
                r["text"] = self._texts[r["id"]]

        return results

    def _rerank(
        self,
        query: str,
        query_vector: np.ndarray,
        results: List[Dict],
        method: str,
        limit: int,
        diversity: float
    ) -> List[Dict]:
        """Apply reranking to results."""
        from .core.advanced_search import Reranker, RerankConfig, RerankMethod

        method_map = {
            "mmr": RerankMethod.MMR,
            "exact": RerankMethod.EXACT,
            "cross-encoder": RerankMethod.CROSS_ENCODER,
        }

        reranker = Reranker(RerankConfig(
            method=method_map[method],
            diversity_lambda=diversity
        ))

        return reranker.rerank(
            query_vector=query_vector,
            candidates=results,
            query_text=query if method == "cross-encoder" else None,
            limit=limit
        )

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def delete(self, ids: Union[str, List[str]]) -> Vectrix:
        """
        Delete documents by ID.

        Example:
            >>> db.delete("doc_id")
            >>> db.delete(["id1", "id2"])
        """
        if isinstance(ids, str):
            ids = [ids]

        self._collection.delete(ids=ids)

        for id_ in ids:
            self._texts.pop(id_, None)

        return self

    def clear(self) -> Vectrix:
        """
        Clear all documents from collection.

        Example:
            >>> db.clear()
        """
        self._db.delete_collection(self.name)
        self._collection = self._db.create_collection(
            name=self.name,
            dimension=self.dimension,
            metric="cosine",
            enable_text_index=True
        )
        self._texts.clear()
        return self

    def count(self) -> int:
        """
        Get number of documents.

        Example:
            >>> print(db.count())
        """
        return self._collection.count()

    def get(self, ids: Union[str, List[str]]) -> List[Result]:
        """
        Get documents by ID.

        Example:
            >>> docs = db.get(["id1", "id2"])
        """
        if isinstance(ids, str):
            ids = [ids]

        results = self._collection.get(ids=ids)

        return [
            Result(
                id=r.id,
                text=self._texts.get(r.id, ""),
                score=1.0,
                metadata=r.metadata
            )
            for r in results
        ]

    def similar(self, id: str, limit: int = 10) -> Results:
        """
        Find similar documents to a given document.

        Example:
            >>> similar = db.similar("doc_id", limit=5)
        """
        # Get the document's vector
        doc = self._collection.get(ids=[id])
        if not doc:
            return Results(items=[], query=f"similar to {id}", mode="dense", time_ms=0)

        vector = doc[0].vector
        if vector is None:
            raise ValueError(f"Document {id} has no vector")

        results = self._dense_search(vector, limit + 1, None)

        # Remove the query document itself
        results = [r for r in results if r["id"] != id][:limit]

        return Results(
            items=[
                Result(
                    id=r["id"],
                    text=self._texts.get(r["id"], ""),
                    score=r["score"],
                    metadata=r.get("metadata", {})
                )
                for r in results
            ],
            query=f"similar to {id}",
            mode="dense",
            time_ms=0
        )

    def close(self):
        """Close the database connection."""
        if self._db:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __len__(self):
        return self.count()

    def __repr__(self):
        return f"Vectrix('{self.name}', {self.count()} docs, model='{self.model_name}')"


# =============================================================================
# Convenience Functions
# =============================================================================

def create(name: str = "default", **kwargs) -> Vectrix:
    """
    Create a new Vectrix collection.

    Example:
        >>> db = create("my_docs")
        >>> db.add(["text 1", "text 2"])
    """
    return Vectrix(name, **kwargs)


def open(name: str = "default", path: str = "./vectrixdb_data") -> Vectrix:
    """
    Open an existing Vectrix collection.

    Example:
        >>> db = open("my_docs")
        >>> results = db.search("query")
    """
    return Vectrix(name, path=path)


# =============================================================================
# Quick One-Liners
# =============================================================================

def quick_search(texts: List[str], query: str, limit: int = 5) -> Results:
    """
    One-liner: Index texts and search immediately.

    Example:
        >>> results = quick_search(
        ...     texts=["Python is great", "Java is verbose", "Rust is fast"],
        ...     query="programming language"
        ... )
        >>> print(results.top.text)
    """
    db = Vectrix("_quick_search")
    db.clear()
    db.add(texts)
    return db.search(query, limit=limit)
