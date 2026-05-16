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

    # Bundled model aliases for easy selection
    # These map to MODEL_CONFIG keys in embedded.py
    _DENSE_ALIASES = {
        "multilingual": "dense",  # Multilingual (download on first use)
        "multi": "dense",
        "e5-small": "dense_en",  # Bundled (33MB quantized)
        "bge-small": "bge_small_en",  # Download from GitHub (127MB)
        "e5-small-fp32": "e5_small",  # Download from GitHub (127MB)
        # Higher quality (v1.9.0)
        "bge-base": "bge_base_en",  # Download from GitHub (~110MB INT8)
        "bge-base-en-v1.5": "bge_base_en",
        "bge": "bge_base_en",  # Default BGE points to base (higher quality)
    }

    _SPARSE_ALIASES = {
        "bm25": "sparse",  # Bundled BM25 vocabulary (1MB)
        "splade": "splade_pp_en",  # Download from GitHub release (508MB)
        "splade++": "splade_pp_en",
        "neural": "splade_pp_en",  # Neural sparse (SPLADE++)
    }

    _RERANKER_ALIASES = {
        "l6": "reranker_en_l6",  # Download from GitHub (87MB)
        "L6": "reranker_en_l6",
        "l12": "reranker_en",  # Bundled (33MB)
        "L12": "reranker_en",
        "minilm-l6": "reranker_en_l6",
        "minilm-l12": "reranker_en",
        # Higher quality (v1.9.0)
        "bge-reranker": "bge_reranker_base",  # Download from GitHub (~110MB INT8)
        "bge-reranker-base": "bge_reranker_base",
    }

    _LATE_INTERACTION_ALIASES = {
        "colbert": "late_interaction_en",  # Bundled (33MB)
        "colbert-small": "late_interaction_en",
        "answerai-colbert": "late_interaction_en",
        # Higher quality (v1.9.0)
        "colbert-v2": "colbert_v2",  # Download from GitHub (~110MB INT8)
        "colbertv2": "colbert_v2",
        "colbertv2.0": "colbert_v2",
    }

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
        # New: Search mode and model selection
        mode: Literal["dense", "hybrid", "ultimate", "graph"] = None,
        dense_model: str = None,
        sparse_model: str = None,
        reranker_model: str = None,
        late_interaction_model: str = None,
        # Storage backend (Lakebase, DeltaLake, CosmosDB, etc.)
        storage_backend: Any = None,
    ):
        """
        Create or open a VectrixDB collection.

        Args:
            name: Collection name
            path: Storage path (default: ./vectrixdb_data)
            model: Embedding model identifier (deprecated, use dense_model instead)
            dimension: Vector dimension (auto-detected from model)
            embed_fn: Custom embedding function: fn(texts: List[str]) -> np.ndarray
            model_path: Path to custom ONNX model directory
            language: Language for bundled models - None/"multi" for multilingual (default),
                      "en"/"english" for English-optimized (smaller, faster)
            tier: Storage tier (deprecated, use 'mode' instead)
            mode: Default search mode - validates required models at creation:
                  - "dense": Vector similarity only (fastest)
                  - "hybrid": Dense + Sparse + Reranker (balanced)
                  - "ultimate": Dense + Sparse + Reranker + ColBERT (best quality)
                  - "graph": Ultimate + Knowledge Graph (for GraphRAG)
            dense_model: Dense embedding model (bundled alias or HuggingFace path)
                  Bundled: "multilingual", "e5-small", "bge-small", "e5-small-fp32"
                  HuggingFace: "BAAI/bge-large-en-v1.5", "intfloat/e5-large-v2", etc.
            sparse_model: Sparse embedding model (bundled alias or HuggingFace path)
                  Bundled: "bm25", "splade", "splade++"
                  HuggingFace: "naver/splade-cocondenser-ensembledistil", etc.
            reranker_model: Cross-encoder reranker model (bundled alias or HuggingFace path)
                  Bundled: "L6", "L12"
                  HuggingFace: "cross-encoder/ms-marco-MiniLM-L-12-v2", etc.
            late_interaction_model: ColBERT model (bundled alias or HuggingFace path)
                  Bundled: "colbert"
                  HuggingFace: "colbert-ir/colbertv2.0", etc.
            storage_backend: External storage backend (VectrixDB instance with Lakebase, DeltaLake, etc.)
                  When provided, uses external storage instead of local SQLite.
                  Schema adapts based on mode:
                  - dense: dense_embedding only
                  - hybrid: dense_embedding + sparse_embedding
                  - ultimate: + late_interaction_embedding
                  - graph: + graph tables

        Examples:
            # Basic usage (bundled models, offline)
            >>> db = Vectrix("docs")
            >>> results = db.search("query")

            # With mode and model selection (bundled)
            >>> db = Vectrix(
            ...     "docs",
            ...     mode="hybrid",
            ...     dense_model="bge-small",
            ...     sparse_model="splade",
            ...     reranker_model="L6",
            ... )
            >>> results = db.search("query")  # Uses hybrid by default

            # With HuggingFace models
            >>> db = Vectrix(
            ...     "docs",
            ...     mode="ultimate",
            ...     dense_model="BAAI/bge-large-en-v1.5",
            ...     sparse_model="naver/splade-cocondenser-ensembledistil",
            ...     reranker_model="cross-encoder/ms-marco-MiniLM-L-12-v2",
            ...     late_interaction_model="colbert-ir/colbertv2.0",
            ... )

            # Mixed bundled + HuggingFace
            >>> db = Vectrix(
            ...     "docs",
            ...     mode="hybrid",
            ...     dense_model="bge-small",  # Bundled
            ...     sparse_model="splade",     # Bundled
            ...     reranker_model="cross-encoder/ms-marco-TinyBERT-L-2-v2",  # HuggingFace
            ... )

            # With storage backend (Lakebase)
            >>> from vectrixdb import VectrixDB
            >>> lakebase = VectrixDB.with_lakebase(host="...", database="...", user="...", password="...")
            >>> db = Vectrix(
            ...     "products",
            ...     mode="ultimate",
            ...     dense_model="bge-small",
            ...     sparse_model="splade",
            ...     reranker_model="L6",
            ...     late_interaction_model="colbert",
            ...     storage_backend=lakebase,
            ... )
            >>> db.add(texts=["Product A", "Product B"])  # Stores all embeddings in Lakebase
            >>> results = db.search("query")  # Full ultimate search from Lakebase
        """
        self.name = name
        self.path = path
        self.embed_fn = embed_fn
        self.model_path = model_path
        self.language = language
        self.storage_backend = storage_backend

        # Handle mode/tier (mode takes precedence)
        self.default_mode = mode.lower() if mode else (tier.lower() if tier else "dense")
        self.tier = self.default_mode  # Keep for backwards compatibility

        # Store model selections
        self.dense_model_name = dense_model
        self.sparse_model_name = sparse_model
        self.reranker_model_name = reranker_model
        self.late_interaction_model_name = late_interaction_model

        # Validate mode
        valid_modes = ["dense", "hybrid", "ultimate", "graph"]
        if self.default_mode not in valid_modes:
            raise ValueError(f"Invalid mode '{self.default_mode}'. Must be one of: {valid_modes}")

        # Validate required models for the selected mode
        self._validate_mode_models()

        # Parse model identifier (for dense model)
        self._parse_model(model or dense_model, dimension)

        self._model = None
        self._db = None
        self._collection = None
        self._texts: Dict[str, str] = {}  # id -> text storage
        self._instance_reranker = None  # Instance-level reranker
        self._instance_late_interaction = None  # Instance-level late interaction
        self._instance_sparse_embedder = None  # Instance-level sparse embedder

        self._init_db()

    def _validate_mode_models(self):
        """Validate that required models are configured for the selected mode."""
        mode = self.default_mode

        if mode == "dense":
            # No additional models required
            pass
        elif mode == "hybrid":
            # Requires: dense + sparse + reranker
            if self.sparse_model_name is None:
                self.sparse_model_name = "bm25"  # Default to BM25
            if self.reranker_model_name is None:
                self.reranker_model_name = "L6"  # Default to L6
        elif mode == "ultimate":
            # Requires: dense + sparse + reranker + late_interaction
            if self.sparse_model_name is None:
                self.sparse_model_name = "bm25"
            if self.reranker_model_name is None:
                self.reranker_model_name = "L6"
            if self.late_interaction_model_name is None:
                self.late_interaction_model_name = "colbert"
        elif mode == "graph":
            # Same as ultimate + graph (graph handled separately)
            if self.sparse_model_name is None:
                self.sparse_model_name = "bm25"
            if self.reranker_model_name is None:
                self.reranker_model_name = "L6"
            if self.late_interaction_model_name is None:
                self.late_interaction_model_name = "colbert"

    def _is_huggingface_model(self, model_name: str) -> bool:
        """Check if model name is a HuggingFace model (contains /)."""
        if model_name is None:
            return False
        return "/" in model_name and not model_name.startswith("vectrixdb/") and not model_name.startswith("github:")

    def _is_github_model(self, model_name: str) -> bool:
        """Check if model name is a GitHub release (starts with github:)."""
        if model_name is None:
            return False
        return model_name.startswith("github:")

    def _download_github_model(self, model_name: str, model_type: str) -> str:
        """
        Download model from GitHub release and return path.

        Args:
            model_name: Model name in format "github:release-tag"
            model_type: Type of model ("sparse", "reranker", "late_interaction")

        Returns:
            Path to downloaded model directory
        """
        import tempfile
        import zipfile
        import urllib.request
        import shutil

        # Parse release tag
        release_tag = model_name.replace("github:", "")

        # GitHub release URL
        github_repo = "knowusuboaky/VectrixDB"

        # Map model types to expected zip file names
        zip_names = {
            "sparse": "sparse.zip",
            "reranker": "reranker.zip",
            "late_interaction": "late_interaction.zip",
            "dense": "dense.zip",
        }

        zip_name = zip_names.get(model_type, f"{model_type}.zip")
        url = f"https://github.com/{github_repo}/releases/download/{release_tag}/{zip_name}"

        # Download to cache directory
        cache_dir = Path.home() / ".cache" / "vectrixdb" / "github" / release_tag
        model_dir = cache_dir / model_type

        # Check if already downloaded
        if model_dir.exists() and (model_dir / "model.onnx").exists():
            return str(model_dir)

        # Create cache directory
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Download zip file
        print(f"Downloading {model_type} model from GitHub release '{release_tag}'...")
        zip_path = cache_dir / zip_name

        try:
            urllib.request.urlretrieve(url, zip_path)
        except Exception as e:
            raise RuntimeError(f"Failed to download model from {url}: {e}")

        # Extract zip file
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(cache_dir)

        # Clean up zip
        zip_path.unlink()

        # Find extracted model directory
        for item in cache_dir.iterdir():
            if item.is_dir() and (item / "model.onnx").exists():
                # Rename to standard name
                if item.name != model_type:
                    target = cache_dir / model_type
                    if target.exists():
                        shutil.rmtree(target)
                    item.rename(target)
                    return str(target)
                return str(item)

        # Check if files are directly in cache_dir
        if (cache_dir / "model.onnx").exists():
            model_dir.mkdir(exist_ok=True)
            for f in cache_dir.glob("*"):
                if f.is_file():
                    shutil.move(str(f), str(model_dir / f.name))
            return str(model_dir)

        raise RuntimeError(f"Model not found after extracting {zip_name}")

    def _validate_search_mode(self, mode: str):
        """Validate that the requested search mode is compatible with configured models."""
        # Mode hierarchy: dense < hybrid < ultimate < graph
        mode_levels = {"dense": 1, "sparse": 1, "hybrid": 2, "ultimate": 3, "graph": 4}
        default_level = mode_levels.get(self.default_mode, 1)
        requested_level = mode_levels.get(mode, 1)

        if requested_level > default_level:
            raise ValueError(
                f"Cannot use '{mode}' mode. Instance configured for '{self.default_mode}' mode. "
                f"You can only use modes at or below your configured level: "
                f"{[m for m, l in mode_levels.items() if l <= default_level]}"
            )

    # Bundled model folder names (in vectrixdb/models/data/)
    _BUNDLED_MODEL_FOLDERS = {"dense_en", "colbert", "reranker_en", "sparse"}

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

        # Resolve dense model aliases (e.g., "e5-small" -> "dense_en")
        resolved_model = self._DENSE_ALIASES.get(model, model)

        # Check if it's a bundled model folder name
        if resolved_model in self._BUNDLED_MODEL_FOLDERS:
            self.model_type = "embedded"
            self.model_name = resolved_model
            self.dimension = dimension or 384
            return

        # Check if it's a github: model (needs download)
        if resolved_model.startswith("github:"):
            self.model_type = "embedded"
            self.model_name = resolved_model
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
        # Use storage backend if provided, otherwise use local SQLite
        if self.storage_backend is not None:
            self._db = self.storage_backend
            self._using_storage_backend = True
        else:
            from .core.database import VectrixDB
            self._db = VectrixDB(self.path)
            self._using_storage_backend = False

        # Store schema config for internal use (what embeddings to generate)
        self._schema_config = self._get_schema_config()

        try:
            self._collection = self._db.get_collection(self.name)
        except:
            # Create collection with mode tag so storage backend knows the schema
            # Tags are used by VectrixDB to infer mode for storage backends
            mode_tags = [self.default_mode.capitalize()]  # e.g., ["Ultimate"]
            self._collection = self._db.create_collection(
                name=self.name,
                dimension=self.dimension,
                metric="cosine",
                enable_text_index=True,
                tags=mode_tags,
            )

    def _get_schema_config(self) -> Dict[str, Any]:
        """Get schema configuration based on mode for storage backends."""
        if not self._using_storage_backend if hasattr(self, '_using_storage_backend') else self.storage_backend is None:
            return {}  # Local SQLite doesn't need extra config

        # Schema adapts based on mode
        config = {
            "mode": self.default_mode,
            "store_dense": True,  # Always store dense
            "store_sparse": self.default_mode in ("hybrid", "ultimate", "graph"),
            "store_late_interaction": self.default_mode in ("ultimate", "graph"),
            "store_text": True,  # Always store text for reranker
        }
        return config

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
                    # Pass the resolved model name (e.g., "dense_en", "colbert")
                    # DenseEmbedder will resolve aliases and load from the correct folder
                    self._model = DenseEmbedder(model=self.model_name, language=self.language)
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
        """Lazy load sparse embedder with model selection."""
        if self._instance_sparse_embedder is None:
            model_name = self.sparse_model_name

            # Check if it's a GitHub release model
            if self._is_github_model(model_name):
                model_path = self._download_github_model(model_name, "sparse")
                from .models import SparseEmbedder
                self._instance_sparse_embedder = SparseEmbedder(model_path=model_path)
            # Check if it's a HuggingFace model
            elif self._is_huggingface_model(model_name):
                # Use HuggingFace SPLADE model
                try:
                    from transformers import AutoModelForMaskedLM, AutoTokenizer
                    import torch

                    class HuggingFaceSparseEmbedder:
                        def __init__(self, model_id):
                            self.tokenizer = AutoTokenizer.from_pretrained(model_id)
                            self.model = AutoModelForMaskedLM.from_pretrained(model_id)
                            self.model.eval()

                        def embed(self, texts):
                            if isinstance(texts, str):
                                texts = [texts]
                            results = []
                            with torch.no_grad():
                                for text in texts:
                                    inputs = self.tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=512)
                                    outputs = self.model(**inputs)
                                    # SPLADE: log(1 + ReLU(logits)) * attention_mask
                                    logits = outputs.logits
                                    splade_rep = torch.log(1 + torch.relu(logits)) * inputs["attention_mask"].unsqueeze(-1)
                                    splade_rep = torch.max(splade_rep, dim=1).values.squeeze()
                                    # Convert to sparse dict
                                    non_zero = torch.nonzero(splade_rep).squeeze(-1)
                                    sparse_dict = {idx.item(): splade_rep[idx].item() for idx in non_zero}
                                    results.append(sparse_dict)
                            return results

                    self._instance_sparse_embedder = HuggingFaceSparseEmbedder(model_name)
                except ImportError:
                    raise ImportError(
                        f"Install transformers for HuggingFace models: pip install transformers torch"
                    )
            else:
                # Use bundled ONNX model
                from .models import SparseEmbedder
                # Resolve alias to model name
                resolved_model = self._SPARSE_ALIASES.get(model_name, model_name) if model_name else "sparse"
                self._instance_sparse_embedder = SparseEmbedder(model=resolved_model)

        return self._instance_sparse_embedder

    @property
    def reranker(self):
        """Lazy load cross-encoder reranker with model selection."""
        if self._instance_reranker is None:
            model_name = self.reranker_model_name

            # Check if it's a GitHub release model
            if self._is_github_model(model_name):
                model_path = self._download_github_model(model_name, "reranker")
                from .models import RerankerEmbedder
                self._instance_reranker = RerankerEmbedder(model_path=model_path)
            # Check if it's a HuggingFace model
            elif self._is_huggingface_model(model_name):
                # Use HuggingFace cross-encoder
                try:
                    from sentence_transformers import CrossEncoder

                    class HuggingFaceReranker:
                        def __init__(self, model_id):
                            self.model = CrossEncoder(model_id)

                        def rerank(self, query: str, documents: list, limit: int = None):
                            pairs = [[query, doc] for doc in documents]
                            scores = self.model.predict(pairs)
                            # Return sorted indices by score (descending)
                            sorted_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
                            if limit:
                                sorted_indices = sorted_indices[:limit]
                            return [(idx, scores[idx]) for idx in sorted_indices]

                    self._instance_reranker = HuggingFaceReranker(model_name)
                except ImportError:
                    raise ImportError(
                        f"Install sentence-transformers for HuggingFace reranker: pip install sentence-transformers"
                    )
            else:
                # Use bundled ONNX model
                from .models import RerankerEmbedder
                # Resolve alias to model name
                resolved_model = self._RERANKER_ALIASES.get(model_name, model_name) if model_name else None
                self._instance_reranker = RerankerEmbedder(model=resolved_model, language=self.language)

        return self._instance_reranker

    @property
    def late_interaction(self):
        """Lazy load late interaction (ColBERT) embedder with model selection."""
        if self._instance_late_interaction is None:
            model_name = self.late_interaction_model_name

            # Check if it's a GitHub release model
            if self._is_github_model(model_name):
                model_path = self._download_github_model(model_name, "late_interaction")
                from .models import LateInteractionEmbedder
                self._instance_late_interaction = LateInteractionEmbedder(model_path=model_path)
            # Check if it's a HuggingFace model
            elif self._is_huggingface_model(model_name):
                # Use HuggingFace ColBERT
                try:
                    from colbert.infra import ColBERTConfig
                    from colbert.modeling.checkpoint import Checkpoint

                    class HuggingFaceColBERT:
                        def __init__(self, model_id):
                            config = ColBERTConfig(checkpoint=model_id)
                            self.checkpoint = Checkpoint(model_id, colbert_config=config)

                        def embed_query(self, query: str):
                            return self.checkpoint.queryFromText([query])[0]

                        def embed_documents(self, documents: list):
                            return self.checkpoint.docFromText(documents)

                        def score(self, query_emb, doc_emb):
                            # MaxSim scoring
                            import torch
                            scores = torch.einsum("qd,pd->qp", query_emb, doc_emb)
                            return scores.max(dim=-1).values.sum().item()

                    self._instance_late_interaction = HuggingFaceColBERT(model_name)
                except ImportError:
                    # Fallback to fastembed
                    try:
                        from fastembed import LateInteractionTextEmbedding
                        self._instance_late_interaction = LateInteractionTextEmbedding(model_name=model_name)
                    except ImportError:
                        raise ImportError(
                            f"Install colbert-ai or fastembed for ColBERT: pip install colbert-ai[torch] or pip install fastembed"
                        )
            else:
                # Use bundled ONNX model
                from .models import LateInteractionEmbedder
                # Check if it's a bundled ColBERT alias (English model)
                bundled_colbert_aliases = {"colbert", "colbert-small", "answerai-colbert"}
                if model_name in bundled_colbert_aliases:
                    # Use bundled English ColBERT (33MB)
                    self._instance_late_interaction = LateInteractionEmbedder(language="en")
                else:
                    # Use specified language or default (multilingual)
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

    def _rerank_with_cross_encoder(
        self,
        query: str,
        candidates: List[Dict],
        limit: int
    ) -> List[Dict]:
        """Rerank candidates using cross-encoder."""
        if not candidates or not self.reranker_model_name:
            return candidates[:limit]

        try:
            reranker = self.reranker
            if hasattr(reranker, 'rerank'):
                # HuggingFace reranker
                docs = [c["text"] for c in candidates]
                reranked_indices = reranker.rerank(query, docs, limit=limit)
                return [candidates[idx] for idx, _ in reranked_indices]
            else:
                # Bundled reranker - use cross-encoder scoring
                pairs = [(query, c["text"]) for c in candidates]
                scores_list = reranker.score_pairs(pairs)
                sorted_candidates = sorted(
                    zip(candidates, scores_list),
                    key=lambda x: x[1],
                    reverse=True
                )
                return [c for c, _ in sorted_candidates[:limit]]
        except Exception:
            # Fall back to original scores
            return candidates[:limit]

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

        With storage backend, embeddings are generated based on mode:
            - dense: dense_embedding only
            - hybrid: dense_embedding + sparse_embedding
            - ultimate: + late_interaction_embedding
            - graph: + graph relationships
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

        # Generate dense embeddings (always)
        dense_vectors = self._embed(texts)

        # Store texts for retrieval
        for id_, text in zip(ids, texts):
            self._texts[id_] = text

        # For storage backends, generate additional embeddings based on mode
        if self._using_storage_backend if hasattr(self, '_using_storage_backend') else self.storage_backend is not None:
            extra_embeddings = {}

            # Generate sparse embeddings for hybrid/ultimate/graph modes
            if self.default_mode in ("hybrid", "ultimate", "graph"):
                sparse_vectors = self._embed_sparse(texts)
                extra_embeddings["sparse_embeddings"] = sparse_vectors

            # Generate late interaction embeddings for ultimate/graph modes
            if self.default_mode in ("ultimate", "graph"):
                late_interaction_vectors = self._embed_late_interaction(texts)
                extra_embeddings["late_interaction_embeddings"] = late_interaction_vectors

            # Add to collection with all embeddings
            self._collection.add(
                ids=ids,
                vectors=dense_vectors,
                metadata=metadata,
                texts=texts,
                **extra_embeddings
            )
        else:
            # Local SQLite - just dense vectors
            self._collection.add(
                ids=ids,
                vectors=dense_vectors,
                metadata=metadata,
                texts=texts
            )

        return self  # Enable chaining

    def _embed_late_interaction(self, texts: Union[str, List[str]]) -> List[np.ndarray]:
        """Generate late interaction (ColBERT) embeddings."""
        if isinstance(texts, str):
            texts = [texts]

        colbert = self.late_interaction

        if hasattr(colbert, 'embed_documents'):
            # HuggingFace ColBERT
            return colbert.embed_documents(texts)
        elif hasattr(colbert, 'embed'):
            # FastEmbed or bundled ColBERT
            return list(colbert.embed(texts))
        elif hasattr(colbert, 'encode_documents'):
            # Bundled LateInteractionEmbedder
            return colbert.encode_documents(texts)
        else:
            raise AttributeError(f"ColBERT embedder has no encode method. Available: {dir(colbert)}")

    def search(
        self,
        query: str,
        limit: int = 10,
        mode: Literal["dense", "sparse", "hybrid", "ultimate", "graph"] = None,
        rerank: Literal[None, "mmr", "exact", "cross-encoder"] = None,
        filter: Dict[str, Any] = None,
        diversity: float = 0.7,
    ) -> Results:
        """
        Search the collection.

        Args:
            query: Search query text
            limit: Number of results (default: 10)
            mode: Search mode (defaults to mode set in constructor)
                - "dense": Semantic search only (fastest)
                - "sparse": Keyword/BM25 only
                - "hybrid": Dense + Sparse + Reranker (balanced)
                - "ultimate": Dense + Sparse + Reranker + ColBERT (best quality)
                - "graph": Ultimate + Knowledge Graph (for GraphRAG)
            rerank: Additional reranking method (only for dense/sparse modes)
                - None: No additional reranking
                - "mmr": Maximal Marginal Relevance (diversity)
                - "exact": Exact score recalculation
                - "cross-encoder": Neural cross-encoder
            filter: Metadata filter (e.g., {"category": "tech"})
            diversity: Diversity parameter for MMR (0-1, default: 0.7)

        Returns:
            Results object with search results

        Example:
            >>> # Uses default mode from constructor
            >>> results = db.search("python programming")
            >>> # Override mode (can only downgrade, not upgrade)
            >>> results = db.search("AI", mode="dense")
            >>> print(results.top.text)
        """
        import time
        start = time.time()

        # Use default mode if not specified
        if mode is None:
            mode = self.default_mode

        # Validate mode is compatible with configured models
        self._validate_search_mode(mode)

        # Embed query
        query_vector = self._embed(query)[0]

        # Determine search strategy
        # Use storage backend optimized search if available
        use_backend = self._using_storage_backend if hasattr(self, '_using_storage_backend') else self.storage_backend is not None

        if mode == "ultimate" or mode == "graph":
            results = self._ultimate_search(query, query_vector, limit, filter, diversity, use_backend=use_backend)
        elif mode == "dense":
            results = self._dense_search(query_vector, limit, filter)
        elif mode == "sparse":
            results = self._sparse_search(query, limit, filter)
        elif mode == "hybrid":
            results = self._hybrid_search(query, query_vector, limit, filter, use_backend=use_backend)
        else:
            raise ValueError(f"Unknown mode: {mode}. Use 'dense', 'sparse', 'hybrid', 'ultimate', or 'graph'")

        # Apply additional reranking if requested (only for dense/sparse modes)
        if rerank and mode in ("dense", "sparse"):
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
        filter: Dict = None,
        use_backend: bool = False
    ) -> List[Dict]:
        """
        Hybrid search: Dense + Sparse + Reranker.

        Pipeline:
        1. Dense semantic search (vector similarity)
        2. Sparse keyword search (BM25/SPLADE) - uses pre-computed if storage backend
        3. RRF fusion with intersection boost
        4. Cross-encoder reranking for final results
        """
        # Stage 1: Get candidates from dense and sparse search
        prefetch_limit = min(limit * 10, max(self._collection.count(), 1))

        # For storage backends, use optimized multi-embedding search if available
        if use_backend and hasattr(self._collection, 'hybrid_search'):
            # Generate sparse query embedding
            query_sparse = self._embed_sparse(query)[0]
            results = self._collection.hybrid_search(
                dense_query=query_vector,
                sparse_query=query_sparse,
                limit=prefetch_limit,
                filter=filter
            )
            # Rerank with cross-encoder
            candidates = [
                {"id": r.id, "score": r.score, "metadata": r.metadata, "text": r.text or self._texts.get(r.id, "")}
                for r in results.results
            ]
            return self._rerank_with_cross_encoder(query, candidates, limit)

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

        # Stage 2: RRF Fusion with intersection boost
        rrf_k = 60
        scores = {}
        vector_map = {r.id: r.vector for r in dense_results.results if r.vector is not None}
        metadata_map = {r.id: r.metadata for r in dense_results.results}

        for rank, r in enumerate(dense_results.results):
            scores[r.id] = {"rrf_dense": 1.0 / (rrf_k + rank + 1), "rrf_sparse": 0, "metadata": r.metadata}

        for rank, r in enumerate(sparse_results.results):
            if r.id not in scores:
                scores[r.id] = {"rrf_dense": 0, "rrf_sparse": 0, "metadata": r.metadata}
                metadata_map[r.id] = r.metadata
            scores[r.id]["rrf_sparse"] = 1.0 / (rrf_k + rank + 1)

        # Calculate combined scores
        for doc_id in scores:
            combined = 0.5 * scores[doc_id]["rrf_dense"] + 0.5 * scores[doc_id]["rrf_sparse"]
            if scores[doc_id]["rrf_dense"] > 0 and scores[doc_id]["rrf_sparse"] > 0:
                combined *= 1.15  # 15% boost for appearing in both
            scores[doc_id]["combined"] = combined

        # Sort and build candidates
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x]["combined"], reverse=True)
        rerank_limit = min(limit * 3, len(sorted_ids))

        candidates = []
        for doc_id in sorted_ids[:rerank_limit]:
            text = self._texts.get(doc_id, "")
            candidates.append({
                "id": doc_id,
                "score": scores[doc_id]["combined"],
                "vector": vector_map.get(doc_id),
                "metadata": metadata_map.get(doc_id, {}),
                "text": text
            })

        # Stage 3: Cross-encoder reranking
        if candidates and self.reranker_model_name:
            try:
                reranker = self.reranker
                if hasattr(reranker, 'rerank'):
                    # HuggingFace reranker
                    docs = [c["text"] for c in candidates]
                    reranked_indices = reranker.rerank(query, docs, limit=limit)
                    return [candidates[idx] for idx, _ in reranked_indices]
                else:
                    # Bundled reranker - use cross-encoder scoring
                    pairs = [(query, c["text"]) for c in candidates]
                    scores_list = reranker.score_pairs(pairs)
                    sorted_candidates = sorted(
                        zip(candidates, scores_list),
                        key=lambda x: x[1],
                        reverse=True
                    )
                    return [c for c, _ in sorted_candidates[:limit]]
            except Exception:
                # Fall back to RRF scores
                pass

        return candidates[:limit]

    def _ultimate_search(
        self,
        query: str,
        query_vector: np.ndarray,
        limit: int,
        filter: Dict = None,
        diversity: float = 0.7,
        use_backend: bool = False
    ) -> List[Dict]:
        """
        Ultimate search: Dense + Sparse + Reranker + ColBERT.

        This is the highest quality search mode, combining:
        1. Dense semantic search (vector similarity)
        2. Sparse keyword search (BM25/SPLADE) - uses pre-computed if storage backend
        3. RRF fusion with intersection boost
        4. ColBERT late interaction scoring - uses pre-computed if storage backend
        5. Cross-encoder reranking for final results
        """
        # Stage 1: Get large candidate pools from multiple sources
        prefetch_limit = min(limit * 10, max(self._collection.count(), 1))

        # For storage backends, use optimized multi-embedding search if available
        if use_backend and hasattr(self._collection, 'ultimate_search'):
            # Generate query embeddings
            query_sparse = self._embed_sparse(query)[0]
            query_late_interaction = self._embed_late_interaction([query])[0]

            results = self._collection.ultimate_search(
                dense_query=query_vector,
                sparse_query=query_sparse,
                late_interaction_query=query_late_interaction,
                limit=prefetch_limit,
                filter=filter
            )
            # Rerank with cross-encoder
            candidates = [
                {"id": r.id, "score": r.score, "metadata": r.metadata, "text": r.text or self._texts.get(r.id, "")}
                for r in results.results
            ]
            return self._rerank_with_cross_encoder(query, candidates, limit)

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

        # Stage 2: RRF Fusion with intersection boost
        rrf_k = 60
        scores = {}
        vector_map = {r.id: r.vector for r in dense_results.results if r.vector is not None}
        metadata_map = {r.id: r.metadata for r in dense_results.results}

        for rank, r in enumerate(dense_results.results):
            scores[r.id] = {"rrf_dense": 1.0 / (rrf_k + rank + 1), "rrf_sparse": 0, "metadata": r.metadata}

        for rank, r in enumerate(sparse_results.results):
            if r.id not in scores:
                scores[r.id] = {"rrf_dense": 0, "rrf_sparse": 0, "metadata": r.metadata}
                metadata_map[r.id] = r.metadata
            scores[r.id]["rrf_sparse"] = 1.0 / (rrf_k + rank + 1)

        # Calculate combined scores
        for doc_id in scores:
            combined = 0.5 * scores[doc_id]["rrf_dense"] + 0.5 * scores[doc_id]["rrf_sparse"]
            if scores[doc_id]["rrf_dense"] > 0 and scores[doc_id]["rrf_sparse"] > 0:
                combined *= 1.15  # 15% boost for appearing in both
            scores[doc_id]["combined"] = combined

        # Sort and build candidates
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x]["combined"], reverse=True)
        rerank_limit = min(limit * 5, len(sorted_ids))  # More candidates for ColBERT

        candidates = []
        for doc_id in sorted_ids[:rerank_limit]:
            text = self._texts.get(doc_id, "")
            candidates.append({
                "id": doc_id,
                "score": scores[doc_id]["combined"],
                "vector": vector_map.get(doc_id),
                "metadata": metadata_map.get(doc_id, {}),
                "text": text
            })

        # Stage 3: ColBERT late interaction scoring (if configured)
        if candidates and self.late_interaction_model_name:
            try:
                colbert = self.late_interaction
                doc_texts = [c["text"] for c in candidates]

                # Score with ColBERT
                if hasattr(colbert, 'embed_query') and hasattr(colbert, 'embed_documents'):
                    # HuggingFace ColBERT
                    query_emb = colbert.embed_query(query)
                    doc_embs = colbert.embed_documents(doc_texts)
                    colbert_scores = [colbert.score(query_emb, doc_emb) for doc_emb in doc_embs]
                elif hasattr(colbert, 'embed'):
                    # FastEmbed ColBERT
                    query_emb = list(colbert.embed([query]))[0]
                    doc_embs = list(colbert.embed(doc_texts))
                    # MaxSim scoring
                    colbert_scores = []
                    for doc_emb in doc_embs:
                        import numpy as np
                        sim = np.dot(query_emb, doc_emb.T)
                        max_sim = np.max(sim, axis=1)
                        colbert_scores.append(float(np.sum(max_sim)))
                else:
                    # Bundled ColBERT
                    colbert_scores = colbert.score(query, doc_texts)

                # Combine RRF scores with ColBERT scores
                for i, c in enumerate(candidates):
                    rrf_score = c["score"]
                    colbert_score = colbert_scores[i] if i < len(colbert_scores) else 0
                    # Weighted combination: 60% RRF, 40% ColBERT
                    c["score"] = 0.6 * rrf_score + 0.4 * (colbert_score / max(colbert_scores) if max(colbert_scores) > 0 else 0)

                # Re-sort by combined score
                candidates.sort(key=lambda x: x["score"], reverse=True)
            except Exception as e:
                # Continue without ColBERT if it fails
                pass

        # Stage 4: Cross-encoder reranking
        if candidates and self.reranker_model_name:
            try:
                reranker = self.reranker
                if hasattr(reranker, 'rerank'):
                    # HuggingFace reranker
                    docs = [c["text"] for c in candidates]
                    reranked_indices = reranker.rerank(query, docs, limit=limit)
                    return [candidates[idx] for idx, _ in reranked_indices]
                else:
                    # Bundled reranker
                    pairs = [(query, c["text"]) for c in candidates]
                    scores_list = reranker.score_pairs(pairs)
                    sorted_candidates = sorted(
                        zip(candidates, scores_list),
                        key=lambda x: x[1],
                        reverse=True
                    )
                    return [c for c, _ in sorted_candidates[:limit]]
            except Exception:
                pass

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
