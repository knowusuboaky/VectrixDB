"""
VectrixDB Collection - Advanced Vector Storage and Search.

Features that match/exceed Qdrant:
- Fast HNSW-based similarity search
- Hybrid search (vector + keyword)
- Rich metadata filtering (20+ operators)
- Batch operations with streaming
- Full-text search with BM25
- Geo-spatial filtering
- Large-scale optimizations

Author: Daddy Nyame Owusu - Boakye
"""

import json
import os
import sqlite3
import threading
import time
import re
from collections import Counter
from datetime import datetime
from math import log
from pathlib import Path
from typing import Any, Callable, Generator, Iterator, Optional, Union, List

import numpy as np

from .types import (
    BatchResult,
    CollectionInfo,
    DistanceMetric,
    Filter,
    IndexConfig,
    IndexType,
    Point,
    SearchMode,
    SearchQuery,
    SearchResult,
    SearchResults,
    SparseVector,
)
from .sparse_index import SparseIndex
from .advanced_search import (
    Reranker,
    RerankConfig,
    RerankMethod,
    FacetAggregator,
    FacetConfig,
    FacetResult,
    ACLFilter,
    ACLPrincipal,
    TextAnalyzer,
    EnhancedSearchResults,
)

# Try to import usearch, fall back to hnswlib
try:
    from usearch.index import Index as UsearchIndex
    USEARCH_AVAILABLE = True
except ImportError:
    USEARCH_AVAILABLE = False

try:
    import hnswlib
    HNSWLIB_AVAILABLE = True
except ImportError:
    HNSWLIB_AVAILABLE = False


class PorterStemmer:
    """
    Simple Porter Stemmer implementation for better BM25 matching.
    Reduces words to their root form (e.g., "running" -> "run").
    """

    def __init__(self):
        self._cache: dict[str, str] = {}

    def stem(self, word: str) -> str:
        """Stem a word using Porter algorithm."""
        if word in self._cache:
            return self._cache[word]

        if len(word) <= 2:
            return word

        original = word
        word = self._step1a(word)
        word = self._step1b(word)
        word = self._step1c(word)
        word = self._step2(word)
        word = self._step3(word)
        word = self._step4(word)
        word = self._step5(word)

        self._cache[original] = word
        return word

    def _measure(self, word: str) -> int:
        """Calculate the measure of a word."""
        vowels = 'aeiou'
        count = 0
        prev_vowel = False
        for char in word:
            is_vowel = char in vowels
            if prev_vowel and not is_vowel:
                count += 1
            prev_vowel = is_vowel
        return count

    def _has_vowel(self, word: str) -> bool:
        """Check if word contains a vowel."""
        return any(c in 'aeiou' for c in word)

    def _ends_double_consonant(self, word: str) -> bool:
        """Check if word ends with double consonant."""
        if len(word) >= 2:
            return word[-1] == word[-2] and word[-1] not in 'aeiou'
        return False

    def _ends_cvc(self, word: str) -> bool:
        """Check if word ends consonant-vowel-consonant."""
        if len(word) >= 3:
            c1, v, c2 = word[-3], word[-2], word[-1]
            return (c1 not in 'aeiou' and v in 'aeiou' and
                    c2 not in 'aeiouwxy')
        return False

    def _step1a(self, word: str) -> str:
        if word.endswith('sses'):
            return word[:-2]
        if word.endswith('ies'):
            return word[:-2]
        if word.endswith('ss'):
            return word
        if word.endswith('s'):
            return word[:-1]
        return word

    def _step1b(self, word: str) -> str:
        if word.endswith('eed'):
            stem = word[:-3]
            if self._measure(stem) > 0:
                return word[:-1]
            return word
        if word.endswith('ed'):
            stem = word[:-2]
            if self._has_vowel(stem):
                return self._step1b_helper(stem)
            return word
        if word.endswith('ing'):
            stem = word[:-3]
            if self._has_vowel(stem):
                return self._step1b_helper(stem)
            return word
        return word

    def _step1b_helper(self, word: str) -> str:
        if word.endswith(('at', 'bl', 'iz')):
            return word + 'e'
        if self._ends_double_consonant(word) and not word.endswith(('l', 's', 'z')):
            return word[:-1]
        if self._measure(word) == 1 and self._ends_cvc(word):
            return word + 'e'
        return word

    def _step1c(self, word: str) -> str:
        if word.endswith('y') and self._has_vowel(word[:-1]):
            return word[:-1] + 'i'
        return word

    def _step2(self, word: str) -> str:
        suffixes = {
            'ational': 'ate', 'tional': 'tion', 'enci': 'ence', 'anci': 'ance',
            'izer': 'ize', 'abli': 'able', 'alli': 'al', 'entli': 'ent',
            'eli': 'e', 'ousli': 'ous', 'ization': 'ize', 'ation': 'ate',
            'ator': 'ate', 'alism': 'al', 'iveness': 'ive', 'fulness': 'ful',
            'ousness': 'ous', 'aliti': 'al', 'iviti': 'ive', 'biliti': 'ble',
        }
        for suffix, replacement in suffixes.items():
            if word.endswith(suffix):
                stem = word[:-len(suffix)]
                if self._measure(stem) > 0:
                    return stem + replacement
        return word

    def _step3(self, word: str) -> str:
        suffixes = {
            'icate': 'ic', 'ative': '', 'alize': 'al', 'iciti': 'ic',
            'ical': 'ic', 'ful': '', 'ness': '',
        }
        for suffix, replacement in suffixes.items():
            if word.endswith(suffix):
                stem = word[:-len(suffix)]
                if self._measure(stem) > 0:
                    return stem + replacement
        return word

    def _step4(self, word: str) -> str:
        suffixes = [
            'al', 'ance', 'ence', 'er', 'ic', 'able', 'ible', 'ant', 'ement',
            'ment', 'ent', 'ion', 'ou', 'ism', 'ate', 'iti', 'ous', 'ive', 'ize',
        ]
        for suffix in suffixes:
            if word.endswith(suffix):
                stem = word[:-len(suffix)]
                if self._measure(stem) > 1:
                    if suffix == 'ion' and stem and stem[-1] in 'st':
                        return stem
                    elif suffix != 'ion':
                        return stem
        return word

    def _step5(self, word: str) -> str:
        if word.endswith('e'):
            stem = word[:-1]
            if self._measure(stem) > 1:
                return stem
            if self._measure(stem) == 1 and not self._ends_cvc(stem):
                return stem
        if word.endswith('ll') and self._measure(word[:-1]) > 1:
            return word[:-1]
        return word


# English stopwords for filtering
STOPWORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'has', 'he',
    'in', 'is', 'it', 'its', 'of', 'on', 'that', 'the', 'to', 'was', 'were',
    'will', 'with', 'the', 'this', 'but', 'they', 'have', 'had', 'what', 'when',
    'where', 'who', 'which', 'why', 'how', 'all', 'each', 'every', 'both', 'few',
    'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own',
    'same', 'so', 'than', 'too', 'very', 'can', 'just', 'should', 'now', 'do',
    'does', 'did', 'doing', 'would', 'could', 'might', 'must', 'shall', 'may',
    'am', 'been', 'being', 'if', 'or', 'because', 'until', 'while', 'about',
    'into', 'through', 'during', 'before', 'after', 'above', 'below', 'between',
    'under', 'again', 'further', 'then', 'once', 'here', 'there', 'any', 'also',
})


class TextIndex:
    """
    Enhanced BM25-based text index for keyword search and hybrid search.

    Features:
    - Porter stemming for better term matching
    - Stopword removal to focus on meaningful terms
    - Optimized BM25 parameters (k1=1.2, b=0.75)
    - Query expansion with original + stemmed terms
    """

    def __init__(self, k1: float = 1.2, b: float = 0.75, use_stemming: bool = True):
        """
        Initialize TextIndex with tuned BM25 parameters.

        Args:
            k1: Term frequency saturation (1.2 is optimal for most datasets)
            b: Length normalization (0.75 is standard)
            use_stemming: Enable Porter stemming
        """
        self.k1 = k1
        self.b = b
        self.use_stemming = use_stemming
        self._stemmer = PorterStemmer() if use_stemming else None
        self._docs: dict[str, dict] = {}  # id -> {text, tokens, length}
        self._inverted_index: dict[str, set] = {}  # term -> set of doc ids
        self._doc_count = 0
        self._avg_doc_length = 0
        self._total_length = 0

    def add(self, doc_id: str, text: str, fields: Optional[dict] = None) -> None:
        """Add a document to the text index."""
        # Tokenize with stemming
        tokens = self._tokenize(text)

        # Also index specified fields
        if fields:
            for field_name, field_value in fields.items():
                if isinstance(field_value, str):
                    tokens.extend(self._tokenize(field_value))

        # Store document
        self._docs[doc_id] = {
            "text": text,
            "tokens": tokens,
            "length": len(tokens),
            "term_freq": Counter(tokens),
        }

        # Update inverted index
        for token in set(tokens):
            if token not in self._inverted_index:
                self._inverted_index[token] = set()
            self._inverted_index[token].add(doc_id)

        # Update stats
        self._doc_count += 1
        self._total_length += len(tokens)
        self._avg_doc_length = self._total_length / self._doc_count if self._doc_count > 0 else 0

    def remove(self, doc_id: str) -> None:
        """Remove a document from the text index."""
        if doc_id not in self._docs:
            return

        doc = self._docs[doc_id]

        # Remove from inverted index
        for token in set(doc["tokens"]):
            if token in self._inverted_index:
                self._inverted_index[token].discard(doc_id)
                if not self._inverted_index[token]:
                    del self._inverted_index[token]

        # Update stats
        self._total_length -= doc["length"]
        self._doc_count -= 1
        self._avg_doc_length = self._total_length / self._doc_count if self._doc_count > 0 else 0

        del self._docs[doc_id]

    def search(self, query: str, limit: int = 10, doc_ids: Optional[set] = None) -> List[tuple[str, float]]:
        """
        Search for documents matching the query using BM25.

        Uses query expansion: searches both original and stemmed terms.

        Args:
            query: Search query
            limit: Maximum results
            doc_ids: Optional set of doc IDs to search within

        Returns:
            List of (doc_id, score) tuples
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scores: dict[str, float] = {}

        # Also try original tokens (before stemming) for exact matches
        original_tokens = self._tokenize_raw(query)
        all_query_tokens = list(set(query_tokens) | set(original_tokens))

        for token in all_query_tokens:
            if token not in self._inverted_index:
                continue

            # IDF with smoothing
            df = len(self._inverted_index[token])
            # Robertson-Sparck Jones IDF formula (better for small collections)
            idf = log((self._doc_count + 1) / (df + 0.5))

            for doc_id in self._inverted_index[token]:
                # Skip if filtering and doc not in allowed set
                if doc_ids is not None and doc_id not in doc_ids:
                    continue

                doc = self._docs[doc_id]
                tf = doc["term_freq"].get(token, 0)
                if tf == 0:
                    continue
                doc_length = doc["length"]

                # BM25 score
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_length / self._avg_doc_length)
                score = idf * numerator / denominator

                scores[doc_id] = scores.get(doc_id, 0) + score

        # Sort by score
        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_results[:limit]

    def get_highlights(self, doc_id: str, query: str, max_length: int = 150) -> List[str]:
        """Get text snippets containing query terms."""
        if doc_id not in self._docs:
            return []

        text = self._docs[doc_id]["text"]
        query_tokens = set(self._tokenize(query)) | set(self._tokenize_raw(query))
        highlights = []

        # Split into sentences
        sentences = re.split(r'[.!?]+', text)

        for sentence in sentences:
            sentence_tokens = set(self._tokenize(sentence)) | set(self._tokenize_raw(sentence))
            if sentence_tokens & query_tokens:
                snippet = sentence.strip()[:max_length]
                if len(sentence) > max_length:
                    snippet += "..."
                highlights.append(snippet)

                if len(highlights) >= 3:
                    break

        return highlights

    def _tokenize_raw(self, text: str) -> List[str]:
        """Tokenize without stemming (for exact match fallback)."""
        text = text.lower()
        tokens = re.findall(r'\b[a-z0-9]+\b', text)
        # Remove stopwords and very short tokens
        return [t for t in tokens if len(t) > 2 and t not in STOPWORDS]

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text with stemming and stopword removal."""
        text = text.lower()
        tokens = re.findall(r'\b[a-z0-9]+\b', text)

        # Remove stopwords and very short tokens
        tokens = [t for t in tokens if len(t) > 2 and t not in STOPWORDS]

        # Apply stemming
        if self._stemmer:
            tokens = [self._stemmer.stem(t) for t in tokens]

        return tokens


class Collection:
    """
    A collection of vectors with metadata.

    Features:
    - Fast HNSW-based similarity search (better than Qdrant)
    - Hybrid search: vector + keyword combined
    - Rich metadata filtering (20+ operators)
    - Batch operations with progress streaming
    - Full-text search with BM25
    - Geo-spatial filtering
    - Thread-safe operations

    Example:
        >>> collection = Collection("documents", dimension=384, path="./data")
        >>> collection.add(
        ...     ids=["doc1", "doc2"],
        ...     vectors=[[0.1, 0.2, ...], [0.3, 0.4, ...]],
        ...     metadata=[{"title": "Doc 1"}, {"title": "Doc 2"}],
        ...     texts=["Full text of doc 1", "Full text of doc 2"]  # For hybrid search
        ... )
        >>> # Pure vector search
        >>> results = collection.search(query=[0.1, 0.2, ...], limit=10)
        >>> # Hybrid search (vector + keyword)
        >>> results = collection.hybrid_search(
        ...     query=[0.1, 0.2, ...],
        ...     query_text="machine learning",
        ...     limit=10
        ... )
    """

    def __init__(
        self,
        name: str,
        dimension: int,
        path: Optional[Union[str, Path]] = None,
        metric: DistanceMetric = DistanceMetric.COSINE,
        index_config: Optional[IndexConfig] = None,
        ef_construction: int = 200,
        m: int = 16,
        description: Optional[str] = None,
        enable_text_index: bool = True,
        tags: Optional[List[str]] = None,
    ):
        """
        Initialize a collection.

        Args:
            name: Collection name
            dimension: Vector dimension
            path: Storage path (None for in-memory)
            metric: Distance metric
            index_config: Advanced index configuration
            ef_construction: HNSW construction parameter (higher = better quality, slower build)
            m: HNSW M parameter (higher = better quality, more memory)
            description: Optional description
            enable_text_index: Enable full-text search for hybrid search
        """
        self.name = name
        self.dimension = dimension
        self.metric = metric
        self.path = Path(path) if path else None
        self.description = description
        self.index_config = index_config or IndexConfig()
        self.tags = tags or []

        self._ef_construction = ef_construction
        self._m = m
        self._lock = threading.RLock()

        # Track metadata
        self._created_at = datetime.utcnow()
        self._updated_at: Optional[datetime] = None
        self._count = 0

        # ID mapping (internal int ID <-> external string ID)
        self._id_to_idx: dict[str, int] = {}
        self._idx_to_id: dict[int, str] = {}
        self._next_idx = 0

        # Text index for hybrid search (BM25)
        self._text_index: Optional[TextIndex] = TextIndex() if enable_text_index else None
        self._indexed_fields: List[str] = []

        # Sparse vector index for dense+sparse hybrid search
        sparse_path = self.path / "sparse" if self.path else None
        self._sparse_index: SparseIndex = SparseIndex(path=sparse_path)
        self._has_sparse_vectors: bool = False

        # Cache (injected by VectrixDB)
        self._cache: Optional[Any] = None

        # Initialize storage
        self._init_storage()
        self._init_index()

    def _init_storage(self) -> None:
        """Initialize SQLite storage for metadata."""
        if self.path:
            os.makedirs(self.path, exist_ok=True)
            db_path = self.path / f"{self.name}.db"
            self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        else:
            self._db = sqlite3.connect(":memory:", check_same_thread=False)

        self._db.row_factory = sqlite3.Row

        # Create tables with additional columns for text search
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS points (
                idx INTEGER PRIMARY KEY,
                id TEXT UNIQUE NOT NULL,
                metadata TEXT,
                text_content TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS collection_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_points_id ON points(id);

            -- Create FTS5 virtual table for fast text search (SQLite full-text)
            CREATE VIRTUAL TABLE IF NOT EXISTS points_fts USING fts5(
                id, text_content, metadata_text,
                content='points',
                content_rowid='idx'
            );
        """)
        self._db.commit()

        # Load existing data
        self._load_id_mappings()

    def _init_index(self) -> None:
        """Initialize the vector index."""
        if USEARCH_AVAILABLE:
            self._init_usearch()
        elif HNSWLIB_AVAILABLE:
            self._init_hnswlib()
        else:
            raise ImportError(
                "No vector index backend available. "
                "Install usearch: pip install usearch "
                "Or hnswlib: pip install hnswlib"
            )

    def _init_usearch(self) -> None:
        """Initialize usearch index."""
        index_path = self.path / f"{self.name}.usearch" if self.path else None

        self._index = UsearchIndex(
            ndim=self.dimension,
            metric=self.metric.usearch_metric,
            dtype="f32",
            connectivity=self._m,
            expansion_add=self._ef_construction,
            expansion_search=self.index_config.hnsw_ef_search,
        )

        if index_path and index_path.exists():
            self._index.load(str(index_path))
        # Note: usearch auto-expands, no reserve needed

        self._backend = "usearch"

    def _init_hnswlib(self) -> None:
        """Initialize hnswlib index."""
        space_map = {
            DistanceMetric.COSINE: "cosine",
            DistanceMetric.EUCLIDEAN: "l2",
            DistanceMetric.DOT: "ip",
        }

        index_path = self.path / f"{self.name}.hnsw" if self.path else None

        self._index = hnswlib.Index(space=space_map.get(self.metric, "cosine"), dim=self.dimension)

        if index_path and index_path.exists():
            self._index.load_index(str(index_path))
        else:
            self._index.init_index(
                max_elements=10000,
                ef_construction=self._ef_construction,
                M=self._m,
            )

        self._index.set_ef(self.index_config.hnsw_ef_search)
        self._backend = "hnswlib"

    def _load_id_mappings(self) -> None:
        """Load ID mappings from storage."""
        cursor = self._db.execute("SELECT idx, id, text_content, metadata FROM points")
        for row in cursor:
            self._id_to_idx[row["id"]] = row["idx"]
            self._idx_to_id[row["idx"]] = row["id"]
            self._next_idx = max(self._next_idx, row["idx"] + 1)

            # Rebuild text index
            if self._text_index and row["text_content"]:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                self._text_index.add(row["id"], row["text_content"], metadata)

        self._count = len(self._id_to_idx)

    # =========================================================================
    # Core Operations
    # =========================================================================

    def add(
        self,
        ids: list[str],
        vectors: Union[list[list[float]], np.ndarray],
        metadata: Optional[list[dict[str, Any]]] = None,
        texts: Optional[list[str]] = None,  # For hybrid search (BM25)
        sparse_vectors: Optional[list[Union[SparseVector, dict]]] = None,  # For dense+sparse hybrid
        batch_size: int = 1000,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """
        Add vectors to the collection.

        Args:
            ids: Unique identifiers for each vector
            vectors: Dense vector data (list of lists or numpy array)
            metadata: Optional metadata for each vector
            texts: Optional text content for BM25 hybrid search
            sparse_vectors: Optional sparse vectors for dense+sparse hybrid search
            batch_size: Process in batches of this size
            on_progress: Optional callback(current, total) for progress

        Returns:
            Number of vectors added

        Example:
            # Dense only
            collection.add(ids=["doc1"], vectors=[[0.1, 0.2, ...]])

            # With sparse vectors for hybrid search
            from vectrixdb import SparseVector
            collection.add(
                ids=["doc1"],
                vectors=[[0.1, 0.2, ...]],  # Dense
                sparse_vectors=[SparseVector.from_dict({0: 0.5, 42: 1.2})]  # Sparse
            )
        """
        if len(ids) != len(vectors):
            raise ValueError(f"ids ({len(ids)}) and vectors ({len(vectors)}) must have same length")

        if metadata and len(metadata) != len(ids):
            raise ValueError(f"metadata ({len(metadata)}) must match ids ({len(ids)})")

        if texts and len(texts) != len(ids):
            raise ValueError(f"texts ({len(texts)}) must match ids ({len(ids)})")

        if sparse_vectors and len(sparse_vectors) != len(ids):
            raise ValueError(f"sparse_vectors ({len(sparse_vectors)}) must match ids ({len(ids)})")

        vectors = np.array(vectors, dtype=np.float32)

        if vectors.shape[1] != self.dimension:
            raise ValueError(f"Vector dimension {vectors.shape[1]} != collection dimension {self.dimension}")

        metadata = metadata or [{} for _ in ids]
        texts = texts or [None for _ in ids]
        sparse_vectors = sparse_vectors or [None for _ in ids]

        total_added = 0
        total = len(ids)

        # Process in batches for large-scale operations
        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)

            batch_ids = ids[batch_start:batch_end]
            batch_vectors = vectors[batch_start:batch_end]
            batch_metadata = metadata[batch_start:batch_end]
            batch_texts = texts[batch_start:batch_end]
            batch_sparse = sparse_vectors[batch_start:batch_end]

            added = self._add_batch(batch_ids, batch_vectors, batch_metadata, batch_texts, batch_sparse)
            total_added += added

            if on_progress:
                on_progress(batch_end, total)

        return total_added

    def _add_batch(
        self,
        ids: list[str],
        vectors: np.ndarray,
        metadata: list[dict],
        texts: list[Optional[str]],
        sparse_vectors: Optional[list[Optional[Union[SparseVector, dict]]]] = None,
    ) -> int:
        """Add a batch of vectors with optional sparse vectors."""
        sparse_vectors = sparse_vectors or [None] * len(ids)

        with self._lock:
            # Invalidate cache since data is changing
            self._invalidate_cache()

            added = 0
            indices = []
            now = datetime.utcnow().isoformat()

            for i, (id_, vector, meta, text, sparse) in enumerate(
                zip(ids, vectors, metadata, texts, sparse_vectors)
            ):
                # Handle sparse vector
                if sparse is not None:
                    if isinstance(sparse, dict):
                        sparse = SparseVector.from_dict(sparse)
                    self._sparse_index.add(id_, sparse)
                    self._has_sparse_vectors = True

                if id_ in self._id_to_idx:
                    # Update existing
                    idx = self._id_to_idx[id_]
                    self._db.execute(
                        "UPDATE points SET metadata = ?, text_content = ?, updated_at = ? WHERE idx = ?",
                        (json.dumps(meta), text, now, idx),
                    )
                    # Update text index
                    if self._text_index:
                        self._text_index.remove(id_)
                        if text:
                            self._text_index.add(id_, text, meta)
                else:
                    # Add new
                    idx = self._next_idx
                    self._next_idx += 1
                    self._id_to_idx[id_] = idx
                    self._idx_to_id[idx] = id_

                    self._db.execute(
                        "INSERT INTO points (idx, id, metadata, text_content, created_at) VALUES (?, ?, ?, ?, ?)",
                        (idx, id_, json.dumps(meta), text, now),
                    )

                    # Add to text index
                    if self._text_index and text:
                        self._text_index.add(id_, text, meta)

                    added += 1

                indices.append(idx)

            self._db.commit()

            # Add to vector index
            indices = np.array(indices, dtype=np.uint64)

            if self._backend == "usearch":
                # usearch auto-expands, just add directly
                self._index.add(indices, vectors)
            else:  # hnswlib
                # Resize if needed
                current_count = self._index.get_current_count()
                max_elements = self._index.get_max_elements()
                if current_count + len(vectors) > max_elements:
                    self._index.resize_index(max(max_elements * 2, current_count + len(vectors)))
                self._index.add_items(vectors, indices)

            self._count = len(self._id_to_idx)
            self._updated_at = datetime.utcnow()

            return added

    def add_batch(
        self,
        points: List[Point],
        batch_size: int = 1000,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> BatchResult:
        """
        Add points using batch API with detailed results.

        Args:
            points: List of Point objects to add
            batch_size: Batch size for processing
            on_progress: Progress callback

        Returns:
            BatchResult with success/error counts
        """
        start_time = time.perf_counter()
        errors = []
        success_count = 0

        for batch_start in range(0, len(points), batch_size):
            batch_end = min(batch_start + batch_size, len(points))
            batch = points[batch_start:batch_end]

            try:
                ids = [p.id for p in batch]
                vectors = [p.vector for p in batch]
                metadata = [p.metadata for p in batch]
                texts = [p.text for p in batch]

                added = self._add_batch(ids, np.array(vectors, dtype=np.float32), metadata, texts)
                success_count += added

            except Exception as e:
                for p in batch:
                    errors.append({"id": p.id, "error": str(e)})

            if on_progress:
                on_progress(batch_end, len(points))

        return BatchResult(
            success_count=success_count,
            error_count=len(errors),
            errors=errors,
            operation_time_ms=(time.perf_counter() - start_time) * 1000,
        )

    def add_stream(
        self,
        point_stream: Iterator[Point],
        batch_size: int = 1000,
    ) -> Generator[BatchResult, None, None]:
        """
        Add points from a stream, yielding batch results.

        Args:
            point_stream: Iterator of Point objects
            batch_size: Batch size

        Yields:
            BatchResult for each batch
        """
        batch = []

        for point in point_stream:
            batch.append(point)

            if len(batch) >= batch_size:
                yield self.add_batch(batch)
                batch = []

        if batch:
            yield self.add_batch(batch)

    # =========================================================================
    # Search Operations
    # =========================================================================

    def _generate_cache_key(
        self,
        query: np.ndarray,
        limit: int,
        filter: Optional[dict] = None,
        query_text: Optional[str] = None,
    ) -> str:
        """Generate a cache key for search results."""
        import hashlib
        # Create hash from query vector
        query_hash = hashlib.md5(query.tobytes()).hexdigest()[:16]
        filter_hash = hashlib.md5(json.dumps(filter or {}, sort_keys=True).encode()).hexdigest()[:8]
        text_hash = hashlib.md5((query_text or "").encode()).hexdigest()[:8] if query_text else ""
        return f"{self.name}:{query_hash}:{limit}:{filter_hash}:{text_hash}"

    def _invalidate_cache(self) -> None:
        """Invalidate all cached search results for this collection."""
        if self._cache:
            try:
                self._cache.invalidate_collection(self.name)
            except Exception:
                pass  # Cache invalidation is best-effort

    def search(
        self,
        query: Union[list[float], np.ndarray],
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        include_vectors: bool = False,
        ef: Optional[int] = None,
        score_threshold: Optional[float] = None,
        use_cache: bool = True,
    ) -> SearchResults:
        """
        Search for similar vectors.

        Args:
            query: Query vector
            limit: Maximum results to return
            filter: Metadata filter (e.g., {"category": "tech"})
            include_vectors: Include vectors in results
            ef: Search accuracy parameter (higher = more accurate, slower)
            score_threshold: Minimum score threshold
            use_cache: Whether to use cached results if available

        Returns:
            SearchResults with matches
        """
        start_time = time.perf_counter()

        query = np.array(query, dtype=np.float32)
        if query.ndim == 1:
            query = query.reshape(1, -1)

        if query.shape[1] != self.dimension:
            raise ValueError(f"Query dimension {query.shape[1]} != collection dimension {self.dimension}")

        # Check cache first
        use_cache_result = use_cache and self._cache and not include_vectors
        if use_cache_result:
            cached = self._cache.get_search_results(
                collection=self.name,
                query=query.flatten().tolist(),
                filter=filter,
                limit=limit
            )
            if cached:
                cached.query_time_ms = (time.perf_counter() - start_time) * 1000
                return cached

        with self._lock:
            # Set search parameter
            if ef and self._backend == "hnswlib":
                self._index.set_ef(ef)

            # Get more results if filtering
            search_limit = limit * 10 if filter else limit

            if self._backend == "usearch":
                matches = self._index.search(query, min(search_limit, self._count))
                if hasattr(matches, 'keys'):
                    indices = matches.keys.flatten().tolist()
                    scores = matches.distances.flatten().tolist()
                else:
                    indices, scores = [], []
            else:  # hnswlib
                if self._count == 0:
                    indices, scores = [], []
                else:
                    indices, distances = self._index.knn_query(query, k=min(search_limit, self._count))
                    indices = indices[0].tolist()
                    # Convert distance to similarity score
                    if self.metric == DistanceMetric.COSINE:
                        scores = [1 - d for d in distances[0].tolist()]
                    else:
                        scores = [1 / (1 + d) for d in distances[0].tolist()]

            # Build results with metadata
            results = []
            filter_obj = Filter.from_dict(filter) if filter else None

            for idx, score in zip(indices, scores):
                if idx not in self._idx_to_id:
                    continue

                # Apply score threshold
                if score_threshold is not None and score < score_threshold:
                    continue

                id_ = self._idx_to_id[idx]
                row = self._db.execute(
                    "SELECT metadata FROM points WHERE idx = ?", (idx,)
                ).fetchone()

                if not row:
                    continue

                metadata = json.loads(row["metadata"]) if row["metadata"] else {}

                # Apply filter
                if filter_obj and not filter_obj.matches(metadata):
                    continue

                result = SearchResult(
                    id=id_,
                    score=float(score),
                    metadata=metadata,
                )

                if include_vectors:
                    # Get vector from index
                    if self._backend == "usearch":
                        result.vector = self._index.get(idx).tolist()

                results.append(result)

                if len(results) >= limit:
                    break

            query_time = (time.perf_counter() - start_time) * 1000

            search_results = SearchResults(
                results=results,
                query_time_ms=query_time,
                total_searched=self._count,
                search_mode=SearchMode.VECTOR,
            )

            # Cache results
            if use_cache_result and self._cache:
                self._cache.set_search_results(
                    collection=self.name,
                    query=query.flatten().tolist(),
                    results=search_results,
                    filter=filter,
                    limit=limit
                )

            return search_results

    def keyword_search(
        self,
        query_text: str,
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        include_highlights: bool = True,
    ) -> SearchResults:
        """
        Search using keywords (full-text search).

        Args:
            query_text: Text query
            limit: Maximum results
            filter: Metadata filter
            include_highlights: Include text snippets

        Returns:
            SearchResults with matches
        """
        start_time = time.perf_counter()

        if not self._text_index:
            raise RuntimeError("Text index not enabled. Create collection with enable_text_index=True")

        with self._lock:
            # Get IDs matching filter
            filtered_ids = None
            if filter:
                filter_obj = Filter.from_dict(filter)
                filtered_ids = set()
                for id_ in self._id_to_idx:
                    idx = self._id_to_idx[id_]
                    row = self._db.execute("SELECT metadata FROM points WHERE idx = ?", (idx,)).fetchone()
                    if row:
                        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                        if filter_obj.matches(metadata):
                            filtered_ids.add(id_)

            # Search text index
            text_results = self._text_index.search(query_text, limit * 2, filtered_ids)

            results = []
            for id_, text_score in text_results[:limit]:
                idx = self._id_to_idx.get(id_)
                if idx is None:
                    continue

                row = self._db.execute("SELECT metadata FROM points WHERE idx = ?", (idx,)).fetchone()
                metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}

                result = SearchResult(
                    id=id_,
                    score=text_score,
                    metadata=metadata,
                    text_score=text_score,
                )

                if include_highlights:
                    result.highlights = self._text_index.get_highlights(id_, query_text)

                results.append(result)

            return SearchResults(
                results=results,
                query_time_ms=(time.perf_counter() - start_time) * 1000,
                total_searched=self._count,
                search_mode=SearchMode.KEYWORD,
            )

    def hybrid_search(
        self,
        query: Union[list[float], np.ndarray],
        query_text: str,
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        vector_weight: float = 0.5,
        text_weight: float = 0.5,
        include_vectors: bool = False,
        include_highlights: bool = True,
        rrf_k: int = 60,
        prefetch_multiplier: int = 10,
    ) -> SearchResults:
        """
        Enhanced hybrid search combining vector similarity and keyword matching.

        Uses optimized Reciprocal Rank Fusion (RRF) with:
        - Larger prefetch pool for better candidate coverage
        - Balanced weights (research shows 0.5/0.5 often optimal)
        - Tuned RRF k parameter

        Args:
            query: Query vector
            query_text: Text query for keyword matching
            limit: Maximum results
            filter: Metadata filter
            vector_weight: Weight for vector RRF score (default: 0.5)
            text_weight: Weight for text RRF score (default: 0.5)
            include_vectors: Include vectors in results
            include_highlights: Include text snippets
            rrf_k: RRF constant (default: 60, lower = more weight to top ranks)
            prefetch_multiplier: Candidates = limit * multiplier (default: 10)

        Returns:
            SearchResults with combined scores
        """
        start_time = time.perf_counter()

        if not self._text_index:
            # Fall back to pure vector search
            return self.search(query, limit, filter, include_vectors)

        # Normalize weights
        total_weight = vector_weight + text_weight
        vector_weight = vector_weight / total_weight
        text_weight = text_weight / total_weight

        # Calculate prefetch size - get more candidates for better fusion
        prefetch_limit = min(limit * prefetch_multiplier, self._count)

        with self._lock:
            # Get vector search results (get more for fusion)
            vector_results = self.search(query, prefetch_limit, filter, include_vectors=False)

            # Get text search results (search all, not just vector results)
            # This allows BM25 to find documents that dense search might miss
            text_results = self._text_index.search(query_text, prefetch_limit, None)

            # Apply filter to text results if needed
            if filter:
                filter_obj = Filter.from_dict(filter)
                filtered_text_results = []
                for id_, score in text_results:
                    idx = self._id_to_idx.get(id_)
                    if idx:
                        row = self._db.execute("SELECT metadata FROM points WHERE idx = ?", (idx,)).fetchone()
                        metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}
                        if filter_obj.matches(metadata):
                            filtered_text_results.append((id_, score))
                text_results = filtered_text_results

            # Reciprocal Rank Fusion (RRF)
            # Pure RRF: score = sum(1 / (k + rank)) across all sources
            scores: dict[str, dict] = {}

            # Add vector scores with RRF
            for rank, result in enumerate(vector_results.results):
                if result.id not in scores:
                    scores[result.id] = {
                        "vector_score": 0, "text_score": 0,
                        "rrf_vector": 0, "rrf_text": 0,
                        "metadata": result.metadata
                    }
                scores[result.id]["vector_score"] = result.score
                scores[result.id]["rrf_vector"] = 1.0 / (rrf_k + rank + 1)

            # Add text scores with RRF
            for rank, (id_, text_score) in enumerate(text_results):
                if id_ not in scores:
                    idx = self._id_to_idx.get(id_)
                    if idx:
                        row = self._db.execute("SELECT metadata FROM points WHERE idx = ?", (idx,)).fetchone()
                        metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}
                        scores[id_] = {
                            "vector_score": 0, "text_score": 0,
                            "rrf_vector": 0, "rrf_text": 0,
                            "metadata": metadata
                        }
                    else:
                        continue

                scores[id_]["text_score"] = text_score
                scores[id_]["rrf_text"] = 1.0 / (rrf_k + rank + 1)

            # Calculate combined RRF scores
            # Documents that appear in both lists get boosted
            for id_ in scores:
                rrf_vector = scores[id_]["rrf_vector"]
                rrf_text = scores[id_]["rrf_text"]

                # Weighted RRF combination
                scores[id_]["combined_score"] = (
                    vector_weight * rrf_vector + text_weight * rrf_text
                )

                # Bonus for appearing in both lists (intersection boost)
                if rrf_vector > 0 and rrf_text > 0:
                    # Small boost for documents found by both methods
                    scores[id_]["combined_score"] *= 1.1

            # Sort by combined score
            sorted_ids = sorted(scores.keys(), key=lambda x: scores[x]["combined_score"], reverse=True)

            results = []
            for id_ in sorted_ids[:limit]:
                score_data = scores[id_]

                result = SearchResult(
                    id=id_,
                    score=score_data["combined_score"],
                    metadata=score_data["metadata"],
                    text_score=score_data["text_score"],
                )

                if include_vectors:
                    idx = self._id_to_idx.get(id_)
                    if idx and self._backend == "usearch":
                        result.vector = self._index.get(idx).tolist()

                if include_highlights and query_text:
                    result.highlights = self._text_index.get_highlights(id_, query_text)

                results.append(result)

            return SearchResults(
                results=results,
                query_time_ms=(time.perf_counter() - start_time) * 1000,
                total_searched=self._count,
                search_mode=SearchMode.HYBRID,
            )

    def sparse_search(
        self,
        query: Union[SparseVector, dict[int, float]],
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        score_threshold: Optional[float] = None,
    ) -> SearchResults:
        """
        Search using sparse vectors only.

        Use this for SPLADE, BM25, or other sparse embedding models.

        Args:
            query: Sparse query vector (SparseVector or {index: value} dict)
            limit: Maximum results
            filter: Metadata filter
            score_threshold: Minimum score threshold

        Returns:
            SearchResults with sparse similarity scores
        """
        start_time = time.perf_counter()

        if isinstance(query, dict):
            query = SparseVector.from_dict(query)

        if not self._has_sparse_vectors:
            return SearchResults(
                results=[],
                query_time_ms=(time.perf_counter() - start_time) * 1000,
                total_searched=0,
                search_mode=SearchMode.SPARSE,
            )

        with self._lock:
            # Get IDs matching filter
            filtered_ids = None
            if filter:
                filter_obj = Filter.from_dict(filter)
                filtered_ids = set()
                for id_ in self._id_to_idx:
                    idx = self._id_to_idx[id_]
                    row = self._db.execute("SELECT metadata FROM points WHERE idx = ?", (idx,)).fetchone()
                    if row:
                        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                        if filter_obj.matches(metadata):
                            filtered_ids.add(id_)

            # Search sparse index
            sparse_results = self._sparse_index.search(
                query, limit=limit, doc_ids=filtered_ids, score_threshold=score_threshold
            )

            results = []
            for sparse_result in sparse_results:
                idx = self._id_to_idx.get(sparse_result.id)
                if idx is None:
                    continue

                row = self._db.execute("SELECT metadata FROM points WHERE idx = ?", (idx,)).fetchone()
                metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}

                result = SearchResult(
                    id=sparse_result.id,
                    score=sparse_result.score,
                    metadata=metadata,
                    sparse_score=sparse_result.score,
                )
                results.append(result)

            return SearchResults(
                results=results,
                query_time_ms=(time.perf_counter() - start_time) * 1000,
                total_searched=self._sparse_index.count(),
                search_mode=SearchMode.SPARSE,
            )

    def dense_sparse_search(
        self,
        dense_query: Union[list[float], np.ndarray],
        sparse_query: Union[SparseVector, dict[int, float]],
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        dense_weight: float = 0.5,
        sparse_weight: float = 0.5,
        include_vectors: bool = False,
    ) -> SearchResults:
        """
        Hybrid search combining dense and sparse vectors (Qdrant-style).

        This is the most powerful search mode, combining:
        - Dense vectors (e.g., sentence-transformers embeddings)
        - Sparse vectors (e.g., SPLADE, BM25)

        Uses Reciprocal Rank Fusion (RRF) to combine results.

        Args:
            dense_query: Dense query vector
            sparse_query: Sparse query vector
            limit: Maximum results
            filter: Metadata filter
            dense_weight: Weight for dense similarity (0-1)
            sparse_weight: Weight for sparse similarity (0-1)
            include_vectors: Include vectors in results

        Returns:
            SearchResults with combined scores

        Example:
            >>> results = collection.dense_sparse_search(
            ...     dense_query=[0.1, 0.2, ...],  # From sentence-transformers
            ...     sparse_query=SparseVector.from_dict({0: 0.5, 42: 1.2}),  # From SPLADE
            ...     dense_weight=0.6,
            ...     sparse_weight=0.4,
            ... )
        """
        start_time = time.perf_counter()

        if isinstance(sparse_query, dict):
            sparse_query = SparseVector.from_dict(sparse_query)

        # Normalize weights
        total_weight = dense_weight + sparse_weight
        dense_weight = dense_weight / total_weight
        sparse_weight = sparse_weight / total_weight

        with self._lock:
            # Get dense search results
            dense_results = self.search(
                dense_query, limit=limit * 3, filter=filter, include_vectors=False
            )

            # Get sparse search results
            filtered_ids = None
            if filter:
                filtered_ids = {r.id for r in dense_results.results}

            sparse_results = []
            if self._has_sparse_vectors:
                sparse_results = self._sparse_index.search(
                    sparse_query, limit=limit * 3, doc_ids=filtered_ids
                )

            # Reciprocal Rank Fusion
            k = 60  # RRF constant
            scores: dict[str, dict] = {}

            # Add dense scores
            for rank, result in enumerate(dense_results.results):
                if result.id not in scores:
                    scores[result.id] = {
                        "dense_score": 0, "sparse_score": 0, "metadata": result.metadata
                    }
                scores[result.id]["dense_score"] = result.score
                scores[result.id]["rrf_dense"] = 1 / (k + rank + 1)

            # Add sparse scores
            for rank, sparse_result in enumerate(sparse_results):
                if sparse_result.id not in scores:
                    idx = self._id_to_idx.get(sparse_result.id)
                    if idx:
                        row = self._db.execute(
                            "SELECT metadata FROM points WHERE idx = ?", (idx,)
                        ).fetchone()
                        metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}
                        scores[sparse_result.id] = {
                            "dense_score": 0, "sparse_score": 0, "metadata": metadata
                        }
                    else:
                        continue

                scores[sparse_result.id]["sparse_score"] = sparse_result.score
                scores[sparse_result.id]["rrf_sparse"] = 1 / (k + rank + 1)

            # Calculate combined RRF scores
            for id_ in scores:
                rrf_dense = scores[id_].get("rrf_dense", 0)
                rrf_sparse = scores[id_].get("rrf_sparse", 0)
                scores[id_]["combined_score"] = (
                    dense_weight * rrf_dense + sparse_weight * rrf_sparse
                )

            # Sort by combined score
            sorted_ids = sorted(
                scores.keys(),
                key=lambda x: scores[x]["combined_score"],
                reverse=True
            )

            results = []
            for id_ in sorted_ids[:limit]:
                score_data = scores[id_]

                result = SearchResult(
                    id=id_,
                    score=score_data["combined_score"],
                    metadata=score_data["metadata"],
                    dense_score=score_data["dense_score"],
                    sparse_score=score_data["sparse_score"],
                )

                if include_vectors:
                    idx = self._id_to_idx.get(id_)
                    if idx and self._backend == "usearch":
                        result.vector = self._index.get(idx).tolist()

                results.append(result)

            return SearchResults(
                results=results,
                query_time_ms=(time.perf_counter() - start_time) * 1000,
                total_searched=self._count,
                search_mode=SearchMode.HYBRID,
            )

    def query(self, search_query: SearchQuery) -> SearchResults:
        """
        Execute a search query using the SearchQuery object.

        This is the most flexible search method, supporting all search modes:
        - VECTOR: Dense vector similarity search
        - KEYWORD: Full-text BM25 search
        - HYBRID: Dense vector + keyword (RRF fusion)
        - SPARSE: Sparse vector similarity search

        Args:
            search_query: SearchQuery configuration

        Returns:
            SearchResults
        """
        filter_dict = None
        if search_query.filter:
            # Serialize filter back to dict if needed
            filter_dict = {}  # The filter is already a Filter object

        if search_query.mode == SearchMode.KEYWORD:
            if not search_query.query_text:
                raise ValueError("query_text required for keyword search")
            return self.keyword_search(
                search_query.query_text,
                search_query.limit,
                filter_dict,
            )

        elif search_query.mode == SearchMode.SPARSE:
            if search_query.sparse_vector is None:
                raise ValueError("sparse_vector required for sparse search")
            return self.sparse_search(
                search_query.sparse_vector,
                search_query.limit,
                filter_dict,
                search_query.score_threshold,
            )

        elif search_query.mode == SearchMode.HYBRID:
            # Check if it's dense+text or dense+sparse hybrid
            if search_query.vector is not None and search_query.sparse_vector is not None:
                # Dense + Sparse hybrid (Qdrant-style)
                return self.dense_sparse_search(
                    search_query.vector,
                    search_query.sparse_vector,
                    search_query.limit,
                    filter_dict,
                    search_query.vector_weight,
                    1 - search_query.vector_weight,  # sparse weight
                    search_query.include_vectors,
                )
            elif search_query.vector is not None and search_query.query_text:
                # Dense + Keyword hybrid
                return self.hybrid_search(
                    search_query.vector,
                    search_query.query_text,
                    search_query.limit,
                    filter_dict,
                    search_query.vector_weight,
                    search_query.text_weight,
                    search_query.include_vectors,
                )
            else:
                raise ValueError(
                    "Hybrid search requires either (vector + query_text) or (vector + sparse_vector)"
                )

        else:  # VECTOR search (default)
            if search_query.vector is None:
                raise ValueError("vector required for vector search")
            return self.search(
                search_query.vector,
                search_query.limit,
                filter_dict,
                search_query.include_vectors,
                search_query.ef_search,
                search_query.score_threshold,
            )

    # =========================================================================
    # Advanced Search (Enterprise Features)
    # =========================================================================

    def search_with_rerank(
        self,
        query: Union[list[float], np.ndarray],
        limit: int = 10,
        rerank_limit: int = 100,
        filter: Optional[dict[str, Any]] = None,
        rerank_method: str = "exact",
        diversity_lambda: float = 0.5,
        query_text: Optional[str] = None,
    ) -> SearchResults:
        """
        Two-stage retrieval: fast ANN search followed by precise re-ranking.

        First retrieves `rerank_limit` candidates using fast ANN,
        then re-ranks to get the final `limit` results with higher precision.

        Args:
            query: Query vector
            limit: Final number of results
            rerank_limit: Number of candidates to retrieve for re-ranking
            filter: Metadata filter
            rerank_method: "exact", "mmr", "cross_encoder", or "weighted"
            diversity_lambda: For MMR (0=diversity, 1=relevance)
            query_text: Query text (needed for cross_encoder)

        Returns:
            SearchResults with re-ranked results

        Example:
            >>> # Get more accurate results with re-ranking
            >>> results = collection.search_with_rerank(
            ...     query=[0.1, 0.2, ...],
            ...     limit=10,
            ...     rerank_limit=100,
            ...     rerank_method="mmr",  # Diverse results
            ...     diversity_lambda=0.7
            ... )
        """
        start_time = time.perf_counter()

        # First stage: fast ANN retrieval
        candidates_results = self.search(
            query=query,
            limit=rerank_limit,
            filter=filter,
            include_vectors=True,  # Need vectors for re-ranking
        )

        # Convert to dict format for reranker
        candidates = [
            {
                "id": r.id,
                "score": r.score,
                "vector": r.vector,
                "metadata": r.metadata,
                "text": r.metadata.get("text", ""),
            }
            for r in candidates_results.results
        ]

        # Second stage: re-ranking
        query_vec = np.array(query, dtype=np.float32)

        rerank_config = RerankConfig(
            method=RerankMethod(rerank_method),
            diversity_lambda=diversity_lambda,
        )
        reranker = Reranker(config=rerank_config)

        rerank_start = time.perf_counter()
        reranked = reranker.rerank(
            query_vector=query_vec,
            candidates=candidates,
            limit=limit,
            query_text=query_text,
        )
        rerank_time = (time.perf_counter() - rerank_start) * 1000

        # Convert back to SearchResults
        results = [
            SearchResult(
                id=r["id"],
                score=r["score"],
                metadata=r["metadata"],
                dense_score=r.get("original_score"),
            )
            for r in reranked
        ]

        total_time = (time.perf_counter() - start_time) * 1000

        return SearchResults(
            results=results,
            query_time_ms=total_time,
            total_searched=candidates_results.total_searched,
            search_mode=SearchMode.VECTOR,
        )

    def search_with_facets(
        self,
        query: Union[list[float], np.ndarray],
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        facets: Optional[List[Union[str, FacetConfig]]] = None,
        facet_limit: int = 10,
    ) -> EnhancedSearchResults:
        """
        Search with faceted aggregations.

        Returns search results plus aggregations/counts for specified fields.

        Args:
            query: Query vector
            limit: Number of results
            filter: Metadata filter
            facets: List of fields to aggregate (or FacetConfig objects)
            facet_limit: Max values per facet

        Returns:
            EnhancedSearchResults with results and facets

        Example:
            >>> results = collection.search_with_facets(
            ...     query=[0.1, 0.2, ...],
            ...     limit=10,
            ...     facets=["category", "author", "year"]
            ... )
            >>> print(results.facets["category"])
            >>> # {"tech": 45, "science": 23, "business": 12}
        """
        start_time = time.perf_counter()

        # Get search results (retrieve more for facet accuracy)
        search_results = self.search(
            query=query,
            limit=max(limit, 100),  # Get more for better facet counts
            filter=filter,
            include_vectors=False,
        )

        search_time = (time.perf_counter() - start_time) * 1000

        # Compute facets
        facet_start = time.perf_counter()
        facet_results = {}

        if facets:
            # Normalize facet configs
            facet_configs = []
            for f in facets:
                if isinstance(f, str):
                    facet_configs.append(FacetConfig(field=f, limit=facet_limit))
                else:
                    facet_configs.append(f)

            # Get all documents for faceting
            documents = [{"metadata": r.metadata} for r in search_results.results]

            aggregator = FacetAggregator()
            facet_results = aggregator.aggregate(
                documents=[d["metadata"] for d in documents],
                facet_configs=facet_configs,
            )

        facet_time = (time.perf_counter() - facet_start) * 1000

        # Return top limit results
        top_results = [r.to_dict() for r in search_results.results[:limit]]

        return EnhancedSearchResults(
            results=top_results,
            facets=facet_results,
            total_count=search_results.total_searched,
            filtered_count=len(search_results.results),
            query_time_ms=search_time,
            facet_time_ms=facet_time,
        )

    def search_with_acl(
        self,
        query: Union[list[float], np.ndarray],
        user_principals: List[str],
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        acl_field: str = "_acl",
        default_allow: bool = False,
    ) -> SearchResults:
        """
        Search with ACL-based security filtering.

        Only returns results the user is authorized to see.

        Args:
            query: Query vector
            user_principals: User's principals (e.g., ["user:alice", "group:engineering"])
            limit: Number of results
            filter: Additional metadata filter
            acl_field: Metadata field containing ACL info
            default_allow: Allow access if no ACL defined

        Returns:
            SearchResults filtered by user's permissions

        Example:
            >>> # Add document with ACL
            >>> collection.add(
            ...     ids=["secret-doc"],
            ...     vectors=[[0.1, 0.2, ...]],
            ...     metadata=[{"_acl": ["user:alice", "group:admins"], "title": "Secret"}]
            ... )
            >>>
            >>> # Search as alice (can see)
            >>> results = collection.search_with_acl(
            ...     query=[0.1, 0.2, ...],
            ...     user_principals=["user:alice", "group:engineering"]
            ... )
            >>>
            >>> # Search as bob (can't see)
            >>> results = collection.search_with_acl(
            ...     query=[0.1, 0.2, ...],
            ...     user_principals=["user:bob", "group:marketing"]
            ... )
        """
        start_time = time.perf_counter()

        # Get more results to account for ACL filtering
        search_results = self.search(
            query=query,
            limit=limit * 5,  # Retrieve more, filter down
            filter=filter,
            include_vectors=False,
        )

        # Apply ACL filtering
        acl_filter = ACLFilter(acl_field=acl_field)
        documents = [
            {"id": r.id, "score": r.score, "metadata": r.metadata}
            for r in search_results.results
        ]

        filtered = acl_filter.filter(
            documents=documents,
            user_principals=user_principals,
            default_allow=default_allow,
        )

        # Convert back to SearchResults
        results = [
            SearchResult(
                id=doc["id"],
                score=doc["score"],
                metadata=doc["metadata"],
            )
            for doc in filtered[:limit]
        ]

        return SearchResults(
            results=results,
            query_time_ms=(time.perf_counter() - start_time) * 1000,
            total_searched=search_results.total_searched,
            search_mode=SearchMode.VECTOR,
        )

    def enterprise_search(
        self,
        query: Union[list[float], np.ndarray],
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        query_text: Optional[str] = None,
        user_principals: Optional[List[str]] = None,
        facets: Optional[List[str]] = None,
        rerank: bool = False,
        rerank_method: str = "mmr",
        rerank_limit: int = 100,
    ) -> EnhancedSearchResults:
        """
        Full enterprise search with all advanced features.

        Combines:
        - Vector search
        - ACL filtering
        - Faceted aggregations
        - Re-ranking

        This is the most feature-complete search method.

        Args:
            query: Query vector
            limit: Number of results
            filter: Metadata filter
            query_text: Optional text for hybrid/re-ranking
            user_principals: User's ACL principals (if None, skip ACL)
            facets: Fields to aggregate
            rerank: Whether to apply re-ranking
            rerank_method: Re-ranking method
            rerank_limit: Candidates for re-ranking

        Returns:
            EnhancedSearchResults with all features

        Example:
            >>> results = collection.enterprise_search(
            ...     query=[0.1, 0.2, ...],
            ...     limit=10,
            ...     user_principals=["user:alice", "group:engineering"],
            ...     facets=["category", "author"],
            ...     rerank=True,
            ...     rerank_method="mmr"
            ... )
        """
        start_time = time.perf_counter()

        # Initial retrieval
        candidate_limit = rerank_limit if rerank else limit * 5
        search_results = self.search(
            query=query,
            limit=candidate_limit,
            filter=filter,
            include_vectors=rerank,
        )

        candidates = [
            {
                "id": r.id,
                "score": r.score,
                "vector": r.vector,
                "metadata": r.metadata,
            }
            for r in search_results.results
        ]

        # ACL filtering
        if user_principals:
            acl_filter = ACLFilter()
            candidates = acl_filter.filter(
                documents=candidates,
                user_principals=user_principals,
                default_allow=True,
            )

        filtered_count = len(candidates)

        # Re-ranking
        rerank_time = 0.0
        if rerank and candidates:
            query_vec = np.array(query, dtype=np.float32)
            rerank_config = RerankConfig(method=RerankMethod(rerank_method))
            reranker = Reranker(config=rerank_config)

            rerank_start = time.perf_counter()
            candidates = reranker.rerank(
                query_vector=query_vec,
                candidates=candidates,
                limit=len(candidates),
                query_text=query_text,
            )
            rerank_time = (time.perf_counter() - rerank_start) * 1000

        # Faceting
        facet_time = 0.0
        facet_results = {}
        if facets:
            facet_start = time.perf_counter()
            aggregator = FacetAggregator()
            facet_results = aggregator.aggregate(
                documents=[c["metadata"] for c in candidates],
                facet_configs=[FacetConfig(field=f) for f in facets],
            )
            facet_time = (time.perf_counter() - facet_start) * 1000

        # Final results
        final_results = candidates[:limit]

        total_time = (time.perf_counter() - start_time) * 1000

        return EnhancedSearchResults(
            results=final_results,
            facets=facet_results,
            total_count=search_results.total_searched,
            filtered_count=filtered_count,
            query_time_ms=total_time,
            rerank_time_ms=rerank_time,
            facet_time_ms=facet_time,
        )

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    def get(self, id: str) -> Optional[Point]:
        """Get a point by ID."""
        with self._lock:
            if id not in self._id_to_idx:
                return None

            idx = self._id_to_idx[id]
            row = self._db.execute(
                "SELECT * FROM points WHERE idx = ?", (idx,)
            ).fetchone()

            if not row:
                return None

            # Get vector
            vector = None
            if self._backend == "usearch":
                vector = self._index.get(idx).tolist()

            return Point(
                id=row["id"],
                vector=vector or [],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                text=row["text_content"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.utcnow(),
                updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            )

    def get_batch(self, ids: List[str]) -> List[Optional[Point]]:
        """Get multiple points by ID."""
        return [self.get(id_) for id_ in ids]

    def delete(self, ids: list[str]) -> int:
        """
        Delete points by ID.

        Args:
            ids: List of IDs to delete

        Returns:
            Number of points deleted
        """
        with self._lock:
            deleted = 0
            for id_ in ids:
                if id_ not in self._id_to_idx:
                    continue

                idx = self._id_to_idx[id_]

                # Delete from database
                self._db.execute("DELETE FROM points WHERE idx = ?", (idx,))

                # Remove from text index
                if self._text_index:
                    self._text_index.remove(id_)

                # Remove from mappings
                del self._id_to_idx[id_]
                del self._idx_to_id[idx]

                # Note: Most HNSW implementations don't support deletion
                # The vector remains in index but won't be returned
                deleted += 1

            self._db.commit()
            self._count = len(self._id_to_idx)
            self._updated_at = datetime.utcnow()

            return deleted

    def update_metadata(self, id: str, metadata: dict[str, Any], merge: bool = True) -> bool:
        """
        Update metadata for a point.

        Args:
            id: Point ID
            metadata: New metadata
            merge: If True, merge with existing metadata. If False, replace.

        Returns:
            True if updated, False if not found
        """
        with self._lock:
            if id not in self._id_to_idx:
                return False

            idx = self._id_to_idx[id]

            if merge:
                row = self._db.execute("SELECT metadata FROM points WHERE idx = ?", (idx,)).fetchone()
                existing = json.loads(row["metadata"]) if row and row["metadata"] else {}
                existing.update(metadata)
                metadata = existing

            now = datetime.utcnow().isoformat()
            self._db.execute(
                "UPDATE points SET metadata = ?, updated_at = ? WHERE idx = ?",
                (json.dumps(metadata), now, idx),
            )
            self._db.commit()
            self._updated_at = datetime.utcnow()

            return True

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def count(self) -> int:
        """Get number of vectors in collection."""
        return self._count

    def info(self) -> CollectionInfo:
        """Get collection information."""
        size_bytes = 0
        if self.path:
            for f in self.path.glob(f"{self.name}.*"):
                size_bytes += f.stat().st_size

        return CollectionInfo(
            name=self.name,
            dimension=self.dimension,
            metric=self.metric,
            count=self._count,
            size_bytes=size_bytes,
            created_at=self._created_at,
            updated_at=self._updated_at,
            description=self.description,
            index_config=self.index_config,
            has_text_index=self._text_index is not None,
            indexed_fields=self._indexed_fields,
            tags=self.tags,
        )

    def list_ids(self, limit: int = 100, offset: int = 0) -> list[str]:
        """List point IDs with pagination."""
        cursor = self._db.execute(
            "SELECT id FROM points ORDER BY idx LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [row["id"] for row in cursor]

    def scroll(
        self,
        limit: int = 100,
        offset: int = 0,
        filter: Optional[dict[str, Any]] = None,
        include_vectors: bool = False,
    ) -> tuple[List[Point], Optional[int]]:
        """
        Scroll through points with pagination.

        Args:
            limit: Maximum points per page
            offset: Starting offset
            filter: Optional metadata filter
            include_vectors: Include vectors

        Returns:
            Tuple of (points, next_offset). next_offset is None if no more results.
        """
        with self._lock:
            cursor = self._db.execute(
                "SELECT * FROM points ORDER BY idx LIMIT ? OFFSET ?",
                (limit + 1, offset),  # Fetch one extra to check for more
            )

            rows = cursor.fetchall()
            filter_obj = Filter.from_dict(filter) if filter else None

            points = []
            for row in rows[:limit]:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}

                if filter_obj and not filter_obj.matches(metadata):
                    continue

                vector = None
                if include_vectors and self._backend == "usearch":
                    vector = self._index.get(row["idx"]).tolist()

                points.append(Point(
                    id=row["id"],
                    vector=vector or [],
                    metadata=metadata,
                    text=row["text_content"],
                    created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.utcnow(),
                    updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
                ))

            next_offset = offset + limit if len(rows) > limit else None

            return points, next_offset

    def save(self) -> None:
        """Save index to disk."""
        if not self.path:
            return

        with self._lock:
            if self._backend == "usearch":
                self._index.save(str(self.path / f"{self.name}.usearch"))
            else:
                self._index.save_index(str(self.path / f"{self.name}.hnsw"))

            self._db.commit()

    def close(self) -> None:
        """Close the collection and save."""
        self.save()
        self._db.close()

    def __len__(self) -> int:
        return self._count

    def __repr__(self) -> str:
        return f"Collection(name='{self.name}', dimension={self.dimension}, count={self._count})"
