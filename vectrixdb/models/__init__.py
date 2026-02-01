"""
VectrixDB Embedded Models

Zero network calls after initial setup. Models bundled with the package.

Models (all support language="en" for English-optimized variants):
- DenseEmbedder: multilingual-e5-small / e5-small-v2 (384 dim)
- SparseEmbedder: BM25 (vocabulary-based, language agnostic)
- RerankerEmbedder: mmarco-mMiniLMv2 / ms-marco-MiniLM
- LateInteractionEmbedder: BGE-M3 (1024 dim) / ColBERT (128 dim)
- GraphExtractor: mREBEL triplet extraction (18 languages)

Usage:
    from vectrixdb.models import (
        DenseEmbedder,
        SparseEmbedder,
        RerankerEmbedder,
        LateInteractionEmbedder,
        GraphExtractor,
    )

    # Multilingual (default)
    dense = DenseEmbedder()
    reranker = RerankerEmbedder()
    late = LateInteractionEmbedder()

    # English-optimized (smaller, faster)
    dense = DenseEmbedder(language="en")
    reranker = RerankerEmbedder(language="en")
    late = LateInteractionEmbedder(language="en")

    # Graph extraction
    graph = GraphExtractor()
    triplets = graph.extract("Einstein was born in Germany.")
"""

from .embedded import (
    DenseEmbedder,
    SparseEmbedder,
    RerankerEmbedder,
    LateInteractionEmbedder,
    GraphExtractor,
    Triplet,
    get_models_dir,
    is_models_installed,
    download_models,
    MODEL_CONFIG,
)

__all__ = [
    "DenseEmbedder",
    "SparseEmbedder",
    "RerankerEmbedder",
    "LateInteractionEmbedder",
    "GraphExtractor",
    "Triplet",
    "get_models_dir",
    "is_models_installed",
    "download_models",
    "MODEL_CONFIG",
]
