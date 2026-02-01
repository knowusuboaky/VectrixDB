"""
Embedding Manager

Manages text-to-vector conversion with multiple embedding providers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union
import numpy as np


@dataclass
class EmbeddingConfig:
    """Configuration for embedding generation."""
    model_name: str = "default"
    dimension: int = 384
    normalize: bool = True
    batch_size: int = 32
    cache_enabled: bool = True
    max_cache_size: int = 10000

    # Provider-specific settings
    provider_config: Dict[str, Any] = field(default_factory=dict)


class BaseEmbeddingProvider(ABC):
    """
    Abstract base class for embedding providers.

    Implement this to add new embedding backends (OpenAI, Sentence Transformers, etc.)
    """

    @abstractmethod
    def embed(self, texts: List[str]) -> np.ndarray:
        """
        Generate embeddings for texts.

        Args:
            texts: List of texts to embed

        Returns:
            Embeddings array, shape (n_texts, dimension)
        """
        pass

    @abstractmethod
    def get_dimension(self) -> int:
        """Get embedding dimension."""
        pass

    @property
    def name(self) -> str:
        """Provider name."""
        return self.__class__.__name__


class RandomEmbeddingProvider(BaseEmbeddingProvider):
    """
    Random embedding provider for testing.

    Generates deterministic embeddings based on text hash.
    """

    def __init__(self, dimension: int = 384, normalize: bool = True):
        """
        Initialize random provider.

        Args:
            dimension: Embedding dimension
            normalize: Normalize embeddings
        """
        self.dimension = dimension
        self.normalize = normalize

    def embed(self, texts: List[str]) -> np.ndarray:
        """Generate random (but deterministic) embeddings."""
        embeddings = []

        for text in texts:
            # Use hash for reproducibility
            seed = hash(text) % (2**32)
            rng = np.random.default_rng(seed)
            emb = rng.standard_normal(self.dimension).astype(np.float32)

            if self.normalize:
                emb = emb / (np.linalg.norm(emb) + 1e-8)

            embeddings.append(emb)

        return np.array(embeddings, dtype=np.float32)

    def get_dimension(self) -> int:
        return self.dimension


class CallableEmbeddingProvider(BaseEmbeddingProvider):
    """
    Embedding provider from a callable function.

    Allows users to provide their own embedding function.

    Example:
        >>> provider = CallableEmbeddingProvider(
        ...     embed_fn=my_embed_function,
        ...     dimension=768
        ... )
    """

    def __init__(
        self,
        embed_fn: Callable[[List[str]], np.ndarray],
        dimension: int,
    ):
        """
        Initialize with callable.

        Args:
            embed_fn: Function that takes texts and returns embeddings
            dimension: Embedding dimension
        """
        self.embed_fn = embed_fn
        self._dimension = dimension

    def embed(self, texts: List[str]) -> np.ndarray:
        return self.embed_fn(texts)

    def get_dimension(self) -> int:
        return self._dimension


class CachedEmbeddingProvider(BaseEmbeddingProvider):
    """
    Wrapper that adds caching to any embedding provider.

    Example:
        >>> cached = CachedEmbeddingProvider(base_provider, max_size=10000)
        >>> embeddings = cached.embed(texts)  # First call computes
        >>> embeddings = cached.embed(texts)  # Second call uses cache
    """

    def __init__(
        self,
        provider: BaseEmbeddingProvider,
        max_size: int = 10000,
    ):
        """
        Initialize cached provider.

        Args:
            provider: Base embedding provider
            max_size: Maximum cache size
        """
        self.provider = provider
        self.max_size = max_size
        self._cache: Dict[str, np.ndarray] = {}

    def embed(self, texts: List[str]) -> np.ndarray:
        """Embed with caching."""
        results = []
        texts_to_embed = []
        indices_to_fill = []

        for i, text in enumerate(texts):
            if text in self._cache:
                results.append(self._cache[text])
            else:
                results.append(None)
                texts_to_embed.append(text)
                indices_to_fill.append(i)

        # Compute missing embeddings
        if texts_to_embed:
            new_embeddings = self.provider.embed(texts_to_embed)

            for idx, text, emb in zip(indices_to_fill, texts_to_embed, new_embeddings):
                results[idx] = emb

                # Add to cache (with eviction if needed)
                if len(self._cache) >= self.max_size:
                    # Remove oldest entry
                    oldest_key = next(iter(self._cache))
                    del self._cache[oldest_key]

                self._cache[text] = emb

        return np.array(results, dtype=np.float32)

    def get_dimension(self) -> int:
        return self.provider.get_dimension()

    def clear_cache(self) -> None:
        """Clear the cache."""
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        """Current cache size."""
        return len(self._cache)


class EmbeddingManager:
    """
    High-level embedding manager with multiple providers.

    Manages embedding generation, caching, and batching.

    Example:
        >>> manager = EmbeddingManager()
        >>> manager.register_provider("openai", openai_provider)
        >>> embeddings = manager.embed(["hello", "world"], provider="openai")
    """

    def __init__(
        self,
        default_provider: str = "random",
        config: Optional[EmbeddingConfig] = None,
    ):
        """
        Initialize embedding manager.

        Args:
            default_provider: Name of default provider
            config: Embedding configuration
        """
        self.config = config or EmbeddingConfig()
        self.default_provider = default_provider

        # Registered providers
        self._providers: Dict[str, BaseEmbeddingProvider] = {}

        # Register default random provider
        self.register_provider(
            "random",
            RandomEmbeddingProvider(
                dimension=self.config.dimension,
                normalize=self.config.normalize,
            )
        )

    def register_provider(
        self,
        name: str,
        provider: BaseEmbeddingProvider,
        use_cache: bool = True,
    ) -> None:
        """
        Register an embedding provider.

        Args:
            name: Provider name
            provider: Provider instance
            use_cache: Wrap with caching
        """
        if use_cache and self.config.cache_enabled:
            provider = CachedEmbeddingProvider(
                provider,
                max_size=self.config.max_cache_size,
            )

        self._providers[name] = provider

    def register_callable(
        self,
        name: str,
        embed_fn: Callable[[List[str]], np.ndarray],
        dimension: int,
        use_cache: bool = True,
    ) -> None:
        """
        Register a callable as an embedding provider.

        Args:
            name: Provider name
            embed_fn: Embedding function
            dimension: Embedding dimension
            use_cache: Wrap with caching
        """
        provider = CallableEmbeddingProvider(embed_fn, dimension)
        self.register_provider(name, provider, use_cache)

    def get_provider(self, name: Optional[str] = None) -> BaseEmbeddingProvider:
        """
        Get an embedding provider.

        Args:
            name: Provider name (uses default if None)

        Returns:
            Embedding provider
        """
        name = name or self.default_provider

        if name not in self._providers:
            raise ValueError(f"Unknown provider: {name}. Available: {list(self._providers.keys())}")

        return self._providers[name]

    def embed(
        self,
        texts: Union[str, List[str]],
        provider: Optional[str] = None,
        batch_size: Optional[int] = None,
    ) -> np.ndarray:
        """
        Generate embeddings for texts.

        Args:
            texts: Single text or list of texts
            provider: Provider name (uses default if None)
            batch_size: Batch size for large inputs

        Returns:
            Embeddings array
        """
        if isinstance(texts, str):
            texts = [texts]

        provider_instance = self.get_provider(provider)
        batch_size = batch_size or self.config.batch_size

        if len(texts) <= batch_size:
            return provider_instance.embed(texts)

        # Batch processing
        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            embeddings = provider_instance.embed(batch)
            all_embeddings.append(embeddings)

        return np.vstack(all_embeddings)

    def embed_single(
        self,
        text: str,
        provider: Optional[str] = None,
    ) -> np.ndarray:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed
            provider: Provider name

        Returns:
            Embedding vector
        """
        embeddings = self.embed([text], provider)
        return embeddings[0]

    def get_dimension(self, provider: Optional[str] = None) -> int:
        """Get embedding dimension for a provider."""
        return self.get_provider(provider).get_dimension()

    def list_providers(self) -> List[str]:
        """List registered providers."""
        return list(self._providers.keys())

    def clear_caches(self) -> None:
        """Clear all provider caches."""
        for provider in self._providers.values():
            if isinstance(provider, CachedEmbeddingProvider):
                provider.clear_cache()


class SemanticProcessor:
    """
    Semantic text processing utilities.

    Includes chunking, preprocessing, and semantic similarity.
    """

    def __init__(
        self,
        embedding_manager: EmbeddingManager,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
    ):
        """
        Initialize semantic processor.

        Args:
            embedding_manager: Embedding manager
            chunk_size: Characters per chunk
            chunk_overlap: Overlap between chunks
        """
        self.manager = embedding_manager
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk_text(
        self,
        text: str,
        chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> List[str]:
        """
        Split text into overlapping chunks.

        Args:
            text: Text to chunk
            chunk_size: Characters per chunk
            overlap: Overlap between chunks

        Returns:
            List of chunks
        """
        chunk_size = chunk_size or self.chunk_size
        overlap = overlap or self.chunk_overlap

        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start = 0

        while start < len(text):
            end = start + chunk_size

            # Try to break at sentence boundary
            if end < len(text):
                # Look for period, question mark, or exclamation
                for sep in ['. ', '? ', '! ', '\n']:
                    break_point = text.rfind(sep, start + chunk_size // 2, end)
                    if break_point != -1:
                        end = break_point + 1
                        break

            chunks.append(text[start:end].strip())
            start = end - overlap

        return [c for c in chunks if c]  # Filter empty chunks

    def semantic_similarity(
        self,
        text1: str,
        text2: str,
        provider: Optional[str] = None,
    ) -> float:
        """
        Compute semantic similarity between two texts.

        Args:
            text1: First text
            text2: Second text
            provider: Embedding provider

        Returns:
            Similarity score (0-1 for cosine)
        """
        embeddings = self.manager.embed([text1, text2], provider)

        emb1 = embeddings[0]
        emb2 = embeddings[1]

        # Cosine similarity
        similarity = np.dot(emb1, emb2) / (
            np.linalg.norm(emb1) * np.linalg.norm(emb2) + 1e-8
        )

        return float(similarity)

    def find_most_similar(
        self,
        query: str,
        candidates: List[str],
        k: int = 5,
        provider: Optional[str] = None,
    ) -> List[tuple]:
        """
        Find most similar texts from candidates.

        Args:
            query: Query text
            candidates: List of candidate texts
            k: Number of results
            provider: Embedding provider

        Returns:
            List of (text, similarity) tuples
        """
        if not candidates:
            return []

        all_texts = [query] + candidates
        embeddings = self.manager.embed(all_texts, provider)

        query_emb = embeddings[0]
        candidate_embs = embeddings[1:]

        # Compute similarities
        norms = np.linalg.norm(candidate_embs, axis=1)
        query_norm = np.linalg.norm(query_emb)

        similarities = np.dot(candidate_embs, query_emb) / (norms * query_norm + 1e-8)

        # Get top k
        top_indices = np.argsort(-similarities)[:k]

        return [(candidates[i], float(similarities[i])) for i in top_indices]

    def deduplicate(
        self,
        texts: List[str],
        threshold: float = 0.95,
        provider: Optional[str] = None,
    ) -> List[str]:
        """
        Remove semantically duplicate texts.

        Args:
            texts: List of texts
            threshold: Similarity threshold for duplicates
            provider: Embedding provider

        Returns:
            Deduplicated texts
        """
        if len(texts) <= 1:
            return texts

        embeddings = self.manager.embed(texts, provider)

        # Normalize
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        normalized = embeddings / (norms + 1e-8)

        # Compute pairwise similarities
        similarities = np.dot(normalized, normalized.T)

        # Keep track of which texts to keep
        keep = [True] * len(texts)

        for i in range(len(texts)):
            if not keep[i]:
                continue

            for j in range(i + 1, len(texts)):
                if keep[j] and similarities[i, j] >= threshold:
                    keep[j] = False

        return [text for text, k in zip(texts, keep) if k]


# =============================================================================
# Embedded Model Providers (No Network Calls)
# =============================================================================

class EmbeddedDenseProvider(BaseEmbeddingProvider):
    """
    Embedded dense embedding provider using bundled ONNX model.

    No network calls - uses models bundled with the package.

    Model: sentence-transformers/all-MiniLM-L6-v2
    Dimension: 384
    """

    def __init__(self, model_dir=None, device: str = "cpu"):
        """
        Initialize embedded provider.

        Args:
            model_dir: Path to model directory (default: bundled)
            device: "cpu" or "cuda"
        """
        from ...models import DenseEmbedder
        self._embedder = DenseEmbedder(model_dir=model_dir, device=device)

    def embed(self, texts: List[str]) -> np.ndarray:
        """Generate embeddings using bundled ONNX model."""
        return self._embedder.embed(texts, normalize=True)

    def get_dimension(self) -> int:
        return self._embedder.dimension


class EmbeddedSparseProvider:
    """
    Embedded sparse (BM25) embedding provider.

    No network calls - uses bundled vocabulary.

    Output: Sparse vectors as dict[term_id, weight]
    """

    def __init__(self, model_dir=None, k1: float = 1.5, b: float = 0.75):
        """
        Initialize sparse provider.

        Args:
            model_dir: Path to model directory (default: bundled)
            k1: BM25 k1 parameter
            b: BM25 b parameter
        """
        from ...models import SparseEmbedder
        self._embedder = SparseEmbedder(model_dir=model_dir, k1=k1, b=b)

    def embed(self, texts: List[str]) -> List[Dict[int, float]]:
        """Generate sparse BM25 embeddings."""
        return self._embedder.embed(texts)

    def embed_dense(self, texts: List[str], vocab_size: int = 30522) -> np.ndarray:
        """Generate dense representation of sparse vectors."""
        return self._embedder.embed_dense(texts, vocab_size=vocab_size)


class EmbeddedRerankerProvider:
    """
    Embedded cross-encoder reranker using bundled ONNX model.

    No network calls - uses models bundled with the package.

    Model: cross-encoder/ms-marco-MiniLM-L-6-v2
    """

    def __init__(self, model_dir=None, device: str = "cpu"):
        """
        Initialize reranker.

        Args:
            model_dir: Path to model directory (default: bundled)
            device: "cpu" or "cuda"
        """
        from ...models import CrossEncoderReranker
        self._reranker = CrossEncoderReranker(model_dir=model_dir, device=device)

    def score(self, query: str, documents: List[str]) -> np.ndarray:
        """Score query-document pairs."""
        return self._reranker.score(query, documents)

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: int = None
    ) -> List[tuple]:
        """Rerank documents by relevance to query."""
        return self._reranker.rerank(query, documents, top_k=top_k)


def get_embedded_provider(
    provider_type: str = "dense",
    device: str = "cpu",
) -> Union[EmbeddedDenseProvider, EmbeddedSparseProvider, EmbeddedRerankerProvider]:
    """
    Get an embedded model provider.

    No network calls - uses bundled ONNX models.

    Args:
        provider_type: "dense", "sparse", or "reranker"
        device: "cpu" or "cuda"

    Returns:
        Provider instance

    Example:
        >>> dense = get_embedded_provider("dense")
        >>> vectors = dense.embed(["hello world"])
    """
    if provider_type == "dense":
        return EmbeddedDenseProvider(device=device)
    elif provider_type == "sparse":
        return EmbeddedSparseProvider()
    elif provider_type == "reranker":
        return EmbeddedRerankerProvider(device=device)
    else:
        raise ValueError(f"Unknown provider type: {provider_type}")
