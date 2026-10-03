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

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

import copy
import logging
import json
import os
import warnings
import sqlite3
import threading
import time
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from .._time import parse_iso, utcnow
from ..exceptions import ConfigurationError, PolicyNotEnforcedWarning, StorageOperationError
from math import log
from pathlib import Path
from typing import Dict, Any, Callable, Generator, Iterator, Optional, Sequence, Union, List

import numpy as np

from .types import (
    FilterPushdown,
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


# ============================================================================
# THE INSERT THREADS
# ============================================================================
#
# INPUT   the machine's cores
# OUTPUT  how many threads usearch inserts with, as keyword arguments
#
# Read once, so every collection inserts the same way.


def _build_threads() -> dict:
    """How many threads insert into the usearch graph, as keyword arguments.

    usearch inserts on every core at once, so two builds of the same vectors
    are two slightly different graphs, and the tail of an approximate search
    moves with them: five builds of SciFact scored between 0.704 and 0.708
    nDCG@10. On one thread the inserts happen in one order, every build is
    the same graph, and on that set it matched exact search. It is slower to
    build, which is why it is a setting and not the default:
    ``VECTRIXDB_BUILD_THREADS=1``. Anything else, or nothing, is usearch's own
    choice of every core.
    """
    given = os.environ.get("VECTRIXDB_BUILD_THREADS", "").strip()
    if not given:
        return {}
    try:
        threads = int(given)
    except ValueError:
        raise ConfigurationError(
            f"VECTRIXDB_BUILD_THREADS is a whole number of threads, and 1 makes every build the same. It is {given!r}"
        ) from None
    if threads < 0:
        raise ConfigurationError(
            f"VECTRIXDB_BUILD_THREADS is a whole number of threads, 0 or more. It is {given!r}"
        )
    return {"threads": threads} if threads else {}


#: An index holding at most this many vectors is searched exactly rather than
#: through its graph. Measured with usearch at 384 dimensions: exact search of
#: 1,000 vectors took 0.17 ms against 0.25 ms for HNSW, and the two met near
#: 4,000. Exact is also exact, which HNSW on a small graph is not.
EXACT_SEARCH_BELOW = 4096

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


# ============================================================================
# SETTINGS: the logger, and what the module exports
# ============================================================================
#
# One logger for the collection's lines, and the four names other modules
# import.

logger = logging.getLogger(__name__)


__all__ = [
    "PorterStemmer",
    "STOPWORDS",
    "TextIndex",
    "Collection",
]


# ============================================================================
# THE STEMMER, AND THE STOPWORDS
# ============================================================================
#
# INPUT   a word
# OUTPUT  its stem, by Porter's rules; the English words BM25 ignores
#
# A small Porter stemmer of its own, so keyword search matches running to run
# without NLTK.


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
        vowels = "aeiou"
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
        return any(c in "aeiou" for c in word)

    def _ends_double_consonant(self, word: str) -> bool:
        """Check if word ends with double consonant."""
        if len(word) >= 2:
            return word[-1] == word[-2] and word[-1] not in "aeiou"
        return False

    def _ends_cvc(self, word: str) -> bool:
        """Check if word ends consonant-vowel-consonant."""
        if len(word) >= 3:
            c1, v, c2 = word[-3], word[-2], word[-1]
            return c1 not in "aeiou" and v in "aeiou" and c2 not in "aeiouwxy"
        return False

    def _step1a(self, word: str) -> str:
        if word.endswith("sses"):
            return word[:-2]
        if word.endswith("ies"):
            return word[:-2]
        if word.endswith("ss"):
            return word
        if word.endswith("s"):
            return word[:-1]
        return word

    def _step1b(self, word: str) -> str:
        if word.endswith("eed"):
            stem = word[:-3]
            if self._measure(stem) > 0:
                return word[:-1]
            return word
        if word.endswith("ed"):
            stem = word[:-2]
            if self._has_vowel(stem):
                return self._step1b_helper(stem)
            return word
        if word.endswith("ing"):
            stem = word[:-3]
            if self._has_vowel(stem):
                return self._step1b_helper(stem)
            return word
        return word

    def _step1b_helper(self, word: str) -> str:
        if word.endswith(("at", "bl", "iz")):
            return word + "e"
        if self._ends_double_consonant(word) and not word.endswith(("l", "s", "z")):
            return word[:-1]
        if self._measure(word) == 1 and self._ends_cvc(word):
            return word + "e"
        return word

    def _step1c(self, word: str) -> str:
        if word.endswith("y") and self._has_vowel(word[:-1]):
            return word[:-1] + "i"
        return word

    def _step2(self, word: str) -> str:
        suffixes = {
            "ational": "ate",
            "tional": "tion",
            "enci": "ence",
            "anci": "ance",
            "izer": "ize",
            "abli": "able",
            "alli": "al",
            "entli": "ent",
            "eli": "e",
            "ousli": "ous",
            "ization": "ize",
            "ation": "ate",
            "ator": "ate",
            "alism": "al",
            "iveness": "ive",
            "fulness": "ful",
            "ousness": "ous",
            "aliti": "al",
            "iviti": "ive",
            "biliti": "ble",
        }
        for suffix, replacement in suffixes.items():
            if word.endswith(suffix):
                stem = word[: -len(suffix)]
                if self._measure(stem) > 0:
                    return stem + replacement
        return word

    def _step3(self, word: str) -> str:
        suffixes = {
            "icate": "ic",
            "ative": "",
            "alize": "al",
            "iciti": "ic",
            "ical": "ic",
            "ful": "",
            "ness": "",
        }
        for suffix, replacement in suffixes.items():
            if word.endswith(suffix):
                stem = word[: -len(suffix)]
                if self._measure(stem) > 0:
                    return stem + replacement
        return word

    def _step4(self, word: str) -> str:
        suffixes = [
            "al",
            "ance",
            "ence",
            "er",
            "ic",
            "able",
            "ible",
            "ant",
            "ement",
            "ment",
            "ent",
            "ion",
            "ou",
            "ism",
            "ate",
            "iti",
            "ous",
            "ive",
            "ize",
        ]
        for suffix in suffixes:
            if word.endswith(suffix):
                stem = word[: -len(suffix)]
                if self._measure(stem) > 1:
                    if suffix == "ion" and stem and stem[-1] in "st":
                        return stem
                    elif suffix != "ion":
                        return stem
        return word

    def _step5(self, word: str) -> str:
        if word.endswith("e"):
            stem = word[:-1]
            if self._measure(stem) > 1:
                return stem
            if self._measure(stem) == 1 and not self._ends_cvc(stem):
                return stem
        if word.endswith("ll") and self._measure(word[:-1]) > 1:
            return word[:-1]
        return word


# English stopwords for filtering
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "he",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
        "this",
        "but",
        "they",
        "have",
        "had",
        "what",
        "when",
        "where",
        "who",
        "which",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "can",
        "just",
        "should",
        "now",
        "do",
        "does",
        "did",
        "doing",
        "would",
        "could",
        "might",
        "must",
        "shall",
        "may",
        "am",
        "been",
        "being",
        "if",
        "or",
        "because",
        "until",
        "while",
        "about",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "any",
        "also",
    }
)


# ============================================================================
# THE TEXT INDEX: BM25
# ============================================================================
#
# INPUT   texts, and a query
# OUTPUT  keyword scores, for full-text search and for the keyword half of
#         hybrid search
#
# The inverted index behind every sparse and hybrid search a collection
# answers.


class TextIndex:
    """
    Enhanced BM25-based text index for keyword search and hybrid search.

    Features:
    - Porter stemming for better term matching
    - Stopword removal to focus on meaningful terms
    - Optimized BM25 parameters (k1=1.2, b=0.75)
    - Query expansion with original + stemmed terms
    """

    def __init__(
        self,
        k1: float = 1.2,
        b: float = 0.75,
        use_stemming: bool = True,
        field_boosts: Optional[Dict[str, float]] = None,
        language: str = "en",
    ):
        """
        Initialize TextIndex with tuned BM25 parameters.

        Args:
            k1: Term frequency saturation (1.2 is optimal for most datasets)
            b: Length normalization (0.75 is standard)
            use_stemming: Enable Porter stemming
            language: what the text is. "en" stems ASCII words with Porter
                and drops English stopwords. Anything else turns both off,
                because Porter on German turns "garantie" into "garanti" and
                the English stopword list drops nothing useful in French:
                unstemmed BM25 in the document's own language beats both.
            field_boosts: Weight per metadata field, e.g. ``{"title": 2.0}``.
                A term in a boosted field counts that many times toward its
                frequency, so a title match outranks the same match in the
                body. Fields not listed count once.
        """
        self.k1 = k1
        self.b = b
        self.use_stemming = use_stemming
        self.field_boosts: Dict[str, float] = dict(field_boosts or {})
        self.language = language
        self._english = language == "en"
        self._stemmer = PorterStemmer() if use_stemming and self._english else None
        self._docs: dict[str, dict] = {}  # id -> {text, tokens, length, term_freq}
        self._inverted_index: dict[str, set] = {}  # term -> set of doc ids
        self._doc_count = 0
        self._avg_doc_length: float = 0
        self._total_length = 0

    def add(self, doc_id: str, text: str, fields: Optional[dict] = None) -> None:
        """Add a document to the text index."""
        tokens = self._tokenize(text)
        # Boosted fields add fractional weight, so counts are floats.
        term_freq: Dict[str, float] = defaultdict(float, Counter(tokens))

        # Indexed fields add to the same frequencies, weighted by their boost.
        if fields:
            for field_name, field_value in fields.items():
                if isinstance(field_value, str):
                    boost = self.field_boosts.get(field_name, 1.0)
                    field_tokens = self._tokenize(field_value)
                    tokens.extend(field_tokens)
                    for token in field_tokens:
                        term_freq[token] += boost

        self._docs[doc_id] = {
            "text": text,
            "tokens": tokens,
            "length": len(tokens),
            "term_freq": term_freq,
        }

        for token in set(tokens):
            if token not in self._inverted_index:
                self._inverted_index[token] = set()
            self._inverted_index[token].add(doc_id)

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

    def search(
        self,
        query: str,
        limit: int = 10,
        doc_ids: Optional[set] = None,
        statistics_over_subset: bool = False,
    ) -> List[tuple[str, float]]:
        """
        Search for documents matching the query using BM25.

        Uses query expansion: searches both original and stemmed terms.

        Args:
            query: Search query
            limit: Maximum results
            doc_ids: Optional set of doc IDs to search within
            statistics_over_subset: compute document frequency, the document
                count and the average length over ``doc_ids`` rather than the
                whole index. Off by default, because it changes the scores of
                an ordinary filtered search. An entitlement policy turns it
                on, and has to: BM25 weights a term by how many documents
                contain it, so a document the principal may not see otherwise
                changes the score of one they may, and can reorder two of
                them. Hiding the score does not help, because the ranking
                carries it too, and rrf fuses that ranking into hybrid.

        Returns:
            List of (doc_id, score) tuples
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        subset: Optional[set] = None
        if statistics_over_subset:
            if doc_ids is None:
                raise ValueError("statistics_over_subset needs a doc_ids set to compute over")
            subset = set(doc_ids) & self._docs.keys()
            if not subset:
                return []
            doc_count = len(subset)
            total_length = sum(self._docs[d]["length"] for d in subset)
            avg_doc_length = total_length / doc_count
        else:
            doc_count = self._doc_count
            avg_doc_length = self._avg_doc_length

        # An index of empty documents would divide by zero, and so would one
        # visible document with no tokens in it.
        avg_doc_length = avg_doc_length or 1.0

        scores: dict[str, float] = {}

        # Also try original tokens (before stemming) for exact matches
        original_tokens = self._tokenize_raw(query)
        all_query_tokens = list(set(query_tokens) | set(original_tokens))

        for token in all_query_tokens:
            if token not in self._inverted_index:
                continue

            postings = self._inverted_index[token]
            if subset is not None:
                postings = postings & subset
                if not postings:
                    continue

            # BM25 IDF in the Lucene / rank_bm25 form. The previous
            # log((N + 1) / (df + 0.5)) is not BM25's IDF: it never reaches zero
            # for a term in every document and overweights rare terms in small
            # collections. This one is what the reference implementations use
            # and what the tests compare against.
            df = len(postings)
            idf = log(1.0 + (doc_count - df + 0.5) / (df + 0.5))

            for doc_id in postings:
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
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_length / avg_doc_length)
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
        sentences = re.split(r"[.!?]+", text)

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

    # Scripts written without spaces between words, cut into overlapping
    # character bigrams: CJK, plus Thai, Lao, Khmer and Myanmar. Bigrams are
    # the standard model-free segmentation, no dictionary, and a query bigram
    # matches wherever the two characters are adjacent.
    _SPACELESS = re.compile(
        "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af"
        "\u0e00-\u0e7f\u0e80-\u0eff\u1780-\u17ff\u1000-\u109f]+"
    )

    # Everything else is a run of letters, digits and combining marks in
    # any script. The old pattern was [a-z0-9]+, which kept "fung" out of
    # "Prüfung", indexed "prêt" as "pr" and "t", and dropped Thai entirely,
    # so every accented language had a sparse index full of fragments. The
    # marks matter too: Python's \w does not match them, so a Thai vowel or
    # the dot that casefold puts on a Turkish capital I would otherwise end
    # its word.
    @staticmethod
    def _is_word_char(c: str) -> bool:
        return c.isalnum() or unicodedata.category(c) in ("Mn", "Mc")

    @classmethod
    def _bigrams(cls, run: str) -> List[str]:
        if len(run) == 1:
            return [run]
        return [run[i : i + 2] for i in range(len(run) - 1)]

    @classmethod
    def _split(cls, text: str) -> List[str]:
        """Words in any script as words; spaceless scripts as bigrams.

        ``casefold`` rather than ``lower``: it folds ß to ss and handles the
        dotted and dotless i, which ``lower`` gets wrong for German and
        Turkish. A run that mixes scripts, a CJK word glued to Latin letters
        as in "東京office", is split at the script boundary.
        """
        out: List[str] = []
        folded = unicodedata.normalize("NFC", text.casefold())
        for token in cls._runs(folded):
            last = 0
            for inner in cls._SPACELESS.finditer(token):
                if inner.start() > last:
                    out.append(token[last : inner.start()])
                out.extend(cls._bigrams(inner.group(0)))
                last = inner.end()
            if last < len(token):
                out.append(token[last:])
        return out

    @classmethod
    def _runs(cls, text: str) -> List[str]:
        """Maximal runs of word characters, marks included."""
        runs: List[str] = []
        current: List[str] = []
        for c in text:
            if cls._is_word_char(c) and c != "_":
                current.append(c)
            elif current:
                runs.append("".join(current))
                current = []
        if current:
            runs.append("".join(current))
        return runs

    def _keep(self, token: str) -> bool:
        if self._english and token.isascii():
            return len(token) > 2 and token not in STOPWORDS
        return True

    def _tokenize_raw(self, text: str) -> List[str]:
        """Tokenize without stemming (for exact match fallback)."""
        return [t for t in self._split(text) if self._keep(t)]

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text with stemming and stopword removal."""
        tokens = [t for t in self._split(text) if self._keep(t)]
        if self._stemmer:
            tokens = [self._stemmer.stem(t) if t.isascii() else t for t in tokens]
        return tokens


# ============================================================================
# THE COLLECTION
# ============================================================================
#
# INPUT   vectors with metadata
# OUTPUT  insert, search, filter, and persist: HNSW similarity search, hybrid
#         search, twenty filter operators, batch operations, full-text search
#         and geo filtering
#
# One class, and everything a caller does to vectors goes through it.


#: The columns a storage backend keeps beside the metadata, which are not metadata.
_STORE_COLUMNS = frozenset(
    {
        "text_content",
        "_embedding",
        "dense_embedding",
        "sparse_embedding",
        "late_interaction_embedding",
    }
)


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
        storage_backend: Optional[Any] = None,  # Storage backend (Lakebase, Delta Lake, etc.)
        text_boosts: Optional[Dict[str, float]] = None,
        readonly: bool = False,
        shard_size: Optional[int] = None,
        text_language: str = "en",
        chunk_store: Optional[Any] = None,
        collection_store: Optional[Any] = None,
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
            storage_backend: External storage backend (Lakebase, Delta Lake, CosmosDB)
                            When provided, vectors are persisted to the backend.
            shard_size: Vectors per shard. None keeps the single in-memory
                        index, which is the default and what every existing
                        collection uses. Set it and the index seals a shard
                        to disk whenever it fills, memory-maps it, and starts
                        a new one, so only one shard is ever held in memory
                        and a collection can be written past RAM. Needs a
                        path and the usearch backend; ignored without them.
            chunk_store: This collection's rows in a chunk store, which every
                        write reaches and the collection pages read when more
                        than one process writes. See vectrixdb.chunk_store.
            collection_store: Where every collection's rules are kept, shared
                        by every server. When it holds a record for this
                        collection, that record's policy is the one enforced,
                        whoever opened it and however. See
                        vectrixdb.collection_records.
        """
        self.name = name
        self.dimension = dimension
        self.metric = metric
        self.path = Path(path) if path else None
        self.description = description
        # Built from the arguments the index actually uses. Storing a bare
        # IndexConfig() here made info() report m=16 and ef_construction=200
        # whatever the caller asked for.
        self.index_config = index_config or IndexConfig(
            hnsw_m=m, hnsw_ef_construction=ef_construction
        )
        self.tags = tags or []

        self._ef_construction = ef_construction
        self._m = m
        self.shard_size = int(shard_size) if shard_size else None
        self._lock = threading.RLock()

        # True once the ANN index differs from what is on disk. save() is a
        # no-op otherwise, so a process that only searched never rewrites the
        # index file under a writer's feet on close.
        self._dirty = False

        # Track metadata
        self._created_at = utcnow()
        self._updated_at: Optional[datetime] = None
        self._count = 0

        # ID mapping (internal int ID <-> external string ID)
        self._id_to_idx: dict[str, int] = {}
        self._idx_to_id: dict[int, str] = {}
        self._next_idx = 0

        # Text index for hybrid search (BM25)
        self.text_boosts: Dict[str, float] = dict(text_boosts or {})
        self.text_language = text_language
        self.readonly = readonly
        self._text_index: Optional[TextIndex] = (
            TextIndex(field_boosts=self.text_boosts, language=text_language)
            if enable_text_index
            else None
        )
        self._indexed_fields: List[str] = []

        # Sparse vector index for dense+sparse hybrid search
        sparse_path = self.path / "sparse" if self.path else None
        self._sparse_index: SparseIndex = SparseIndex(path=sparse_path)
        # Read back from what was saved: this started False on every open, so
        # a reopened collection's sparse_search answered nothing.
        self._has_sparse_vectors: bool = self._sparse_index.count() > 0
        self._sparse_dirty = False
        # Set while rebuild_index builds its new index; see there.
        self._rebuild_touched: Optional[set] = None
        self._rebuild_deleted: Optional[set] = None
        self._rebuild_lock = threading.Lock()

        # Cache (injected by VectrixDB)
        self._cache: Any = None

        # External storage backend for vector persistence (Lakebase, Delta Lake, etc.)
        self._storage_backend: Any = storage_backend
        self._use_backend_for_vectors = storage_backend is not None and hasattr(
            storage_backend, "vector_search"
        )

        # The copy of every chunk the collection pages read when more than one
        # process writes: none unless one was given. Never searched.
        self._chunk_store: Any = chunk_store

        # Every collection's rules, kept where every server reads them: none
        # unless one was given, and then the policy is read from there first.
        self._collection_store: Any = collection_store

        # ANN index: a usearch Index or an hnswlib Index, whichever is installed.
        self._index: Any = None

        # Initialize storage
        self._init_storage()
        try:
            self._init_index()
        except BaseException:
            # A collection that failed to load is still deleted from disk by
            # its database, and on Windows an open SQLite file cannot be.
            self._db.close()
            raise

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

    @staticmethod
    def _native_path(path) -> str:
        r"""A path usearch's C++ can open: extended-length on Windows when long.

        Python handles paths past 260 characters on Windows by itself; the
        native library does not, so a deep collection directory failed with
        "No such file or directory" from inside usearch while SQLite beside
        it worked. The \\?\ prefix lifts the limit.
        """
        text = str(path)
        if os.name == "nt" and len(text) > 240 and not text.startswith("\\\\?\\"):
            return "\\\\?\\" + os.path.abspath(text)
        return text

    def _init_usearch(self) -> None:
        """Initialize usearch index."""
        if self.shard_size and self.path:
            # Sealed shards: the head is the same file a single index uses,
            # so this reads an existing collection without converting it.
            from .sharded_index import ShardedIndex

            self._index = ShardedIndex(
                dimension=self.dimension,
                metric=self.metric.usearch_metric,
                connectivity=self._m,
                expansion_add=self._ef_construction,
                expansion_search=self.index_config.hnsw_ef_search,
                shard_size=self.shard_size,
                directory=self.path,
                stem=self.name,
                readonly=self.readonly,
                native_path=self._native_path,
            )
            self._backend = "usearch"
            return

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
            # The header first. usearch reads a file that is not one of its
            # indexes natively and takes the whole process down with it (exit
            # 127 on Windows, no traceback); its metadata reader refuses the
            # same file with a ValueError, which the database records as a
            # collection that failed to load.
            try:
                try:
                    UsearchIndex.metadata(self._native_path(index_path))
                except (RuntimeError, ValueError):
                    # The native reader may not open the path at all (spaces,
                    # non-ASCII, a long path on Windows) and says so with
                    # either; the bytes are the file's own answer.
                    UsearchIndex.metadata(Path(index_path).read_bytes())
            except ValueError as exc:
                raise ValueError(f"{index_path.name} is not a usearch index: {exc}") from exc
            if self.readonly:
                # Memory-mapped, read-only. The file is the index; nothing is
                # copied into RAM, so a collection larger than memory can be
                # searched, and opening is instant.
                try:
                    self._index.view(self._native_path(index_path))
                except RuntimeError:
                    # The native library could not open the path (non-ASCII
                    # characters, or a long path on Windows). Load through
                    # Python instead; still read-only at the API, just not
                    # memory-mapped.
                    self._index.load(Path(index_path).read_bytes())
            else:
                try:
                    self._index.load(self._native_path(index_path))
                except RuntimeError:
                    self._index.load(Path(index_path).read_bytes())
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

        # When it was last written to is on the rows, and a reopened collection
        # said "never" until its next write.
        if self._updated_at is None and self._count:
            row = self._db.execute(
                "SELECT MAX(COALESCE(updated_at, created_at)) AS at FROM points"
            ).fetchone()
            if row is not None and row["at"]:
                try:
                    self._updated_at = parse_iso(row["at"])
                except ValueError:  # pragma: no cover - a timestamp written by something else
                    pass

        # And when it was made: that was the moment this process opened it, so
        # a collection could be "created" after its last write. Kept with the
        # collection, and for one made before this, the oldest row stands in.
        kept = self._db.execute(
            "SELECT value FROM collection_meta WHERE key = 'created_at'"
        ).fetchone()
        if kept is None and self._count:
            oldest = self._db.execute("SELECT MIN(created_at) AS at FROM points").fetchone()
            if oldest is not None and oldest["at"]:
                kept = {"value": oldest["at"]}
        try:
            if kept is not None:
                self._created_at = parse_iso(kept["value"]) or self._created_at
        except ValueError:  # pragma: no cover
            pass
        if self.readonly:
            return  # a read-only open writes nothing
        self._db.execute(
            "INSERT OR IGNORE INTO collection_meta (key, value) VALUES ('created_at', ?)",
            (self._created_at.isoformat(),),
        )
        self._db.commit()

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
        sparse_embeddings: Optional[list] = None,  # Sparse embeddings for storage backend
        late_interaction_embeddings: Optional[
            list
        ] = None,  # ColBERT embeddings for storage backend
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
            sparse_embeddings: Optional sparse embeddings for storage backend (ultimate mode)
            late_interaction_embeddings: Optional late interaction embeddings for storage backend
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

        self._writable()
        if sparse_embeddings and len(sparse_embeddings) != len(ids):
            raise ValueError(
                f"sparse_embeddings ({len(sparse_embeddings)}) must match ids ({len(ids)})"
            )

        if late_interaction_embeddings and len(late_interaction_embeddings) != len(ids):
            raise ValueError(
                f"late_interaction_embeddings ({len(late_interaction_embeddings)}) must match ids ({len(ids)})"
            )

        # Nothing to add is not an error: shape[1] of an empty array was an
        # IndexError.
        if not len(ids):
            return 0

        vectors = np.array(vectors, dtype=np.float32)
        self._check_vector_shape(vectors, len(ids))

        metadata = metadata or [{} for _ in ids]
        text_list: list[Optional[str]] = list(texts) if texts else [None for _ in ids]
        sparse_list: list[Optional[Union[SparseVector, dict]]] = (
            list(sparse_vectors) if sparse_vectors else [None for _ in ids]
        )
        sparse_embeddings = sparse_embeddings or [None for _ in ids]
        late_interaction_embeddings = late_interaction_embeddings or [None for _ in ids]

        total_added = 0
        total = len(ids)

        # Process in batches for large-scale operations
        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)

            batch_ids = ids[batch_start:batch_end]
            batch_vectors = vectors[batch_start:batch_end]
            batch_metadata = metadata[batch_start:batch_end]
            batch_texts = text_list[batch_start:batch_end]
            batch_sparse = sparse_list[batch_start:batch_end]
            batch_sparse_emb = sparse_embeddings[batch_start:batch_end]
            batch_late_interaction = late_interaction_embeddings[batch_start:batch_end]

            added = self._add_batch(
                batch_ids,
                batch_vectors,
                batch_metadata,
                batch_texts,
                batch_sparse,
                batch_sparse_emb,
                batch_late_interaction,
            )
            total_added += added

            if on_progress:
                on_progress(batch_end, total)

        return total_added

    def _check_vector_shape(self, vectors: np.ndarray, count: int) -> None:
        """Refuse anything but ``count`` vectors of this collection's dimension."""
        if vectors.ndim != 2 or vectors.shape[1] != self.dimension:
            got = vectors.shape[1] if vectors.ndim == 2 else f"shape {vectors.shape}"
            raise ValueError(f"Vector dimension {got} != collection dimension {self.dimension}")
        if vectors.shape[0] != count:
            raise ValueError(
                f"ids ({count}) and vectors ({vectors.shape[0]}) must have same length"
            )

    def _add_batch(
        self,
        ids: list[str],
        vectors: np.ndarray,
        metadata: list[dict],
        texts: list[Optional[str]],
        sparse_vectors: Optional[list[Optional[Union[SparseVector, dict]]]] = None,
        sparse_embeddings: Optional[list] = None,
        late_interaction_embeddings: Optional[list] = None,
    ) -> int:
        """Add a batch of vectors with optional sparse vectors and storage backend embeddings."""
        # Before anything is written: a wrong dimension used to commit the
        # rows and fail only at the index, leaving points with no vector.
        vectors = np.asarray(vectors, dtype=np.float32)
        self._check_vector_shape(vectors, len(ids))
        sparse_vectors = sparse_vectors or [None] * len(ids)
        sparse_embeddings = sparse_embeddings or [None] * len(ids)
        late_interaction_embeddings = late_interaction_embeddings or [None] * len(ids)

        with self._lock:
            # Invalidate cache since data is changing
            self._invalidate_cache()

            added = 0
            indices: List[int] = []
            now = utcnow().isoformat()

            # Prepare batch data for storage backend
            backend_documents = []

            for i, (id_, vector, meta, text, sparse, sparse_emb, late_emb) in enumerate(
                zip(
                    ids,
                    vectors,
                    metadata,
                    texts,
                    sparse_vectors,
                    sparse_embeddings,
                    late_interaction_embeddings,
                )
            ):
                # Handle sparse vector
                if sparse is not None:
                    if isinstance(sparse, dict):
                        sparse = SparseVector.from_dict(sparse)
                    self._sparse_index.add(id_, sparse)
                    self._has_sparse_vectors = True
                    self._sparse_dirty = True

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

                # Prepare data for storage backend (with all embeddings)
                if self._use_backend_for_vectors:
                    doc_data = {
                        **meta,
                        "_embedding": vector.tolist()
                        if hasattr(vector, "tolist")
                        else list(vector),
                        "text_content": text,
                    }
                    # Add sparse embedding for storage backend (hybrid/ultimate mode)
                    if sparse_emb is not None:
                        # Convert to list if numpy array
                        if hasattr(sparse_emb, "tolist"):
                            doc_data["sparse_embedding"] = sparse_emb.tolist()
                        elif isinstance(sparse_emb, dict):
                            doc_data["sparse_embedding"] = sparse_emb
                        else:
                            doc_data["sparse_embedding"] = list(sparse_emb) if sparse_emb else None
                    # Add late interaction embedding for storage backend (ultimate mode)
                    if late_emb is not None:
                        if hasattr(late_emb, "tolist"):
                            doc_data["late_interaction_embedding"] = late_emb.tolist()
                        else:
                            doc_data["late_interaction_embedding"] = (
                                list(late_emb) if late_emb else None
                            )
                    backend_documents.append((id_, doc_data))

            self._db.commit()

            # Persist vectors to storage backend (Lakebase, Delta Lake, etc.)
            if self._use_backend_for_vectors and backend_documents:
                try:
                    self._storage_backend.insert_batch(self.name, backend_documents)
                except Exception as e:
                    # Re-raise to surface the actual error instead of silently failing
                    raise RuntimeError(f"Failed to persist vectors to storage backend: {e}") from e

            # Add to local vector index (for fast in-memory search)
            keys = np.array(indices, dtype=np.uint64)

            if self._backend == "usearch":
                # Ids are derived from content, so re-adding the same document
                # yields the same key. usearch refuses duplicate keys with a raw
                # RuntimeError, which meant indexing a corpus containing any
                # repeated text crashed outright. Drop the previous copy first so
                # a repeat add is an upsert, which is what content-addressed ids
                # imply. Deduplicate within the batch too, keeping the last.
                seen: dict[int, int] = {}
                for position, key in enumerate(keys.tolist()):
                    seen[key] = position
                if len(seen) != len(keys):
                    keep = sorted(seen.values())
                    keys = keys[keep]
                    vectors = vectors[keep]

                for key in keys.tolist():
                    try:
                        if self._index.contains(key):
                            self._index.remove(key)
                    except Exception:  # pragma: no cover - backend specific
                        # An index that cannot answer `contains` is empty for
                        # this key, so there is nothing to replace.
                        pass

                self._index.add(keys, vectors, **_build_threads())
                if self._rebuild_touched is not None:
                    self._rebuild_touched.update(int(k) for k in keys.tolist())
            else:  # hnswlib
                # Resize if needed
                current_count = self._index.get_current_count()
                max_elements = self._index.get_max_elements()
                if current_count + len(vectors) > max_elements:
                    self._index.resize_index(max(max_elements * 2, current_count + len(vectors)))
                self._index.add_items(vectors, keys)

            self._dirty = True
            self._count = len(self._id_to_idx)
            self._updated_at = utcnow()

            # Last, so a store that refuses leaves this collection and the
            # index whole. Writing the same chunks again puts them there.
            self._share("add", lambda store: store.put(ids, texts, metadata, now))

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

    def _mark_relevance(self, results: List[SearchResult]) -> None:
        """Relevance for results this collection's own index scored.

        For a cosine collection the score already is the cosine similarity.
        For any other metric it is ``1 / (1 + distance)``, which is bounded
        and ordered and not comparable with a similarity, and says so.
        """
        from . import relevance as rel

        cosine = self.metric == DistanceMetric.COSINE
        for r in results:
            if r.relevance is None:
                r.relevance = (
                    rel.from_cosine(r.score)
                    if cosine
                    else round(min(1.0, max(0.0, float(r.score))), 6)
                )
                r.relevance_kind = rel.SIMILARITY if cosine else rel.DISTANCE
            if not r.matched_by and (r.relevance is None or r.relevance > 0.0):
                # A chunk with no likeness at all is on the list because the
                # list had room, not because anything found it.
                r.matched_by = [rel.MATCHED_MEANING]

    def _similarity_of(self, query: Any, doc_id: str) -> Optional[float]:
        """The exact similarity of one stored chunk to a query, for a chunk a
        keyword search found and the vector search did not. None when this
        collection's own index does not hold it."""
        if self.metric != DistanceMetric.COSINE or self._index is None:
            return None
        idx = self._id_to_idx.get(doc_id)
        if idx is None:
            return None
        try:
            found = self._score_exactly(np.asarray(query, dtype=np.float32), [idx], None, False)
        except Exception:  # pragma: no cover - an index that cannot hand a vector back
            return None
        from . import relevance as rel

        return rel.from_cosine(found[0].score) if found else None

    def _distances_to_scores(self, distances: List[float]) -> List[float]:
        """Convert backend distances into similarity scores, higher being better.

        Cosine distance is in [0, 2], so ``1 - d`` maps it back to the familiar
        cosine similarity in [-1, 1]. Other metrics are unbounded, so they are
        squashed into (0, 1] instead. Both index backends go through here, which
        is what keeps ``SearchResult.score`` meaning the same thing regardless of
        which one is in use.
        """
        if self.metric == DistanceMetric.COSINE:
            return [1.0 - d for d in distances]
        return [1.0 / (1.0 + d) for d in distances]

    #: Set by Vectrix once it has loaded this collection's policy, so the
    #: warning below fires only for callers that bypass it.
    _policy_enforced = False
    _policy_warned = False
    #: Set by close(). A search after that used to be answered from the
    #: result cache, which is stale data from a handle the caller has said
    #: they are finished with, and the worse of the two possible failures.
    _closed = False
    _policy_loaded = False
    _policy_obj = None

    @property
    def policy(self):
        """The entitlement policy this collection carries, if any.

        Read from the collection's own metadata, so it applies to every
        caller rather than only to the ones that went through ``Vectrix``.
        That is the whole point: a policy that a layer above enforces is a
        policy the layer below can be reached around. The collection's
        record, where a server keeps one, says who may search the collection
        and nothing about its documents: that is this policy's alone.
        """
        if self._policy_loaded:
            return self._policy_obj
        self._policy_loaded = True
        try:
            stored = self.get_meta("entitlement_policy")
        except Exception:  # pragma: no cover - backends without the meta table
            return None
        if stored:
            from ..policy import Policy

            self._policy_obj = Policy.from_dict(json.loads(stored))
        return self._policy_obj

    @property
    def pushdown_mode(self) -> "FilterPushdown":
        """Where a filter runs for this collection, as it is actually driven.

        The local index is POST by construction: it hands back candidates and
        the loop decides. A storage backend answers for itself, and every one
        of them is POST too, for now.
        """
        return self._pushdown_source()[1]

    def _pushdown_source(self):
        """What decides a filter here, and where it runs.

        The name matters in the refusal message: saying "SQLiteStorage" when
        the vectors never went near it sends whoever reads it to the wrong
        place.
        """
        backend = getattr(self, "_storage_backend", None)
        # The same condition search() uses. `_use_backend_for_vectors` only
        # says the backend has the method; the backend actually serves a
        # search when the local index has nothing in it. Naming the backend
        # when the local index did the work sends whoever reads the refusal
        # to the wrong place.
        pushable = self._policy_pushable(backend)
        serves = (
            backend is not None
            and getattr(self, "_use_backend_for_vectors", False)
            and (self._count == 0 or pushable)
        )
        if serves:
            mode = getattr(type(backend), "FILTER_PUSHDOWN", FilterPushdown.POST)
            if pushable:
                mode = FilterPushdown.ENGINE
            return f"the {type(backend).__name__} backend", mode
        return "the local index", FilterPushdown.POST

    def _policy_pushable(self, backend: Any) -> bool:
        """Whether this backend can run every field the policy names.

        A store that can run every field is the engine for that policy and
        serves the search even when the local index holds a copy of the
        vectors, because a filter the local index applies afterwards is a
        filter the store did not enforce. A store that can run some of the
        fields is not the engine: one rule decided here over the rows that
        came back is one rule the store never saw.
        """
        policy = self.policy
        fields_of = getattr(backend, "filter_fields", None)
        if backend is None or policy is None or not callable(fields_of):
            return False
        promoted = fields_of(self.name)
        return bool(promoted) and all(f in promoted for f in policy.required_document_fields)

    def _engine_filter(
        self, filter: Optional[dict], policy: Any, principal: Optional[dict]
    ) -> Optional[Any]:
        """The filter the store runs itself, in its own language, or None.

        The policy's scope rules when the store is the engine for the policy,
        plus the caller's filter when the store can take it. Anything the
        store cannot take is applied here afterwards, as before.
        """
        compile_filter = getattr(self._storage_backend, "compile_filter", None)
        if not callable(compile_filter):
            return None
        pushable = []
        if policy is not None and self._pushdown_source()[1] is FilterPushdown.ENGINE:
            scope = policy.compile(principal, scope_only=True)
            if scope and scope.get("$and") and compile_filter(self.name, scope) is not None:
                pushable.append(scope)
        if filter and compile_filter(self.name, filter) is not None:
            pushable.append(filter)
        if not pushable:
            return None
        return compile_filter(self.name, {"$and": pushable} if len(pushable) > 1 else pushable[0])

    def _refuse_if_closed(self, op: str) -> None:
        """A closed collection answers nothing, cache or no cache."""
        if not self._closed:
            return
        from ..exceptions import VectrixError

        raise VectrixError(
            f"{op} on collection {self.name!r}, which is closed. Reopen it rather than "
            f"reusing a handle you have finished with; answering from the result cache "
            f"would be stale."
        )

    def _refuse_if_policied(self, op: str, why: str) -> None:
        """Refuse a search surface the policy does not reach.

        These combine or rank by paths that never see a per-candidate
        decision, so running them on a policied collection would answer with
        documents nothing had authorised. Refusing is the only honest option
        until each one is gated in its own right.
        """
        if self.policy is None:
            return
        from ..exceptions import PolicyError

        raise PolicyError(
            f"{op} is not available on a collection carrying an entitlement policy: {why} "
            f"Use search() or keyword_search() with a principal."
        )

    def _warn_if_policy_unenforced(self, op: str) -> None:
        """Say so when a policied collection is read by a path that is not
        gated yet.

        ``search`` refuses outright. These others are reached from inside the
        library often enough that refusing would break the collection's own
        bookkeeping, so for now they say so instead. Silence would be the
        worst of both: the collection looks protected and is not.
        """
        if self._policy_enforced or self._policy_warned:
            return
        if self.policy is None:
            return
        self._policy_warned = True
        warnings.warn(
            f"Collection {self.name!r} carries an entitlement policy and {op} does not "
            f"apply it, so this returns documents without regard to who is asking. "
            f"search() is gated; this path is not yet. Reach the collection through "
            f"Vectrix(...).as_principal({{...}}).",
            PolicyNotEnforcedWarning,
            stacklevel=3,
        )

    def search(
        self,
        query: Union[list[float], np.ndarray],
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        include_vectors: bool = False,
        ef: Optional[int] = None,
        score_threshold: Optional[float] = None,
        use_cache: bool = True,
        use_backend: Optional[
            bool
        ] = None,  # None = auto, True = force backend, False = force local
        principal: Optional[dict] = None,
        strict_backend: bool = False,  # with use_backend=True: a store that fails raises, no local answer in its place
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
            use_backend: Whether to use storage backend for search.
                        None (default) = auto-detect (use backend if available and local index is empty)
                        True = force use backend (Lakebase/pgvector)
                        False = force use local HNSW index

        Returns:
            SearchResults with matches
        """
        self._refuse_if_closed("search()")
        policy = self.policy
        if policy is not None:
            if principal is None:
                from ..exceptions import PrincipalRequired

                raise PrincipalRequired(self.name)
            compiled = policy.compile(principal)
            if compiled is None:
                # The principal matches nothing at all. No search to run, and
                # skipping it leaks nothing about documents: whether you hold
                # any groups is a fact about you.
                return SearchResults(
                    results=[],
                    query_time_ms=0.0,
                    total_searched=self._count,
                    search_mode=SearchMode.VECTOR,
                    policy_candidates=0,
                    policy_withheld_disclosable=0,
                    policy_withheld_undisclosable=0,
                )
            # The search cache is keyed on query, filter and limit and not on
            # the principal, so leaving it on would serve one principal's
            # results to another.
            use_cache = False
        start_time = time.perf_counter()

        query = np.array(query, dtype=np.float32)
        if query.ndim == 1:
            query = query.reshape(1, -1)

        if query.shape[1] != self.dimension:
            raise ValueError(
                f"Query dimension {query.shape[1]} != collection dimension {self.dimension}"
            )

        # The search cache is keyed on query, filter and limit, and not on
        # which dense vector answered. A store that holds more than one can
        # answer the same query three ways, so the cache would hand one
        # choice the results of another.
        names = getattr(self._storage_backend, "vector_names", None)
        if callable(names) and len(names()) > 1:
            use_cache = False

        # Check cache first
        use_cache_result = use_cache and self._cache and not include_vectors
        # Everything else that changes the answer is part of the key too.
        cache_options = {"score_threshold": score_threshold, "ef": ef}
        if use_cache_result:
            cached = self._cache.get_search_results(
                collection=self.name,
                query=query.flatten().tolist(),
                filter=filter,
                limit=limit,
                options=cache_options,
            )
            if cached:
                # A copy, so a caller that edits its results cannot change
                # what the next caller is served.
                cached = copy.deepcopy(cached)
                cached.query_time_ms = (time.perf_counter() - start_time) * 1000
                return cached

        # Determine whether to use storage backend for search
        # Auto-detect: use backend if available and local index is empty, or if explicitly requested
        # The store serves when the local index has nothing, and also when it
        # can enforce the policy itself: the same condition pushdown_mode
        # reports, so what it says and what happens cannot part company.
        # And when the search asks for a dense vector only the store holds: the
        # local index has the collection's own vectors and no others, so
        # answering from it would answer a different question.
        needs_store = getattr(self._storage_backend, "needs_the_store", None)
        store_only = bool(self._use_backend_for_vectors and callable(needs_store) and needs_store())
        should_use_backend = (
            use_backend
            if use_backend is not None
            else (
                self._use_backend_for_vectors
                and (self._count == 0 or store_only or self._policy_pushable(self._storage_backend))
            )
        )

        # Try backend search if configured and requested
        if should_use_backend and self._use_backend_for_vectors:
            try:
                # The role, when the policy names one, so the database
                # decides underneath the filter rather than after it.
                role = policy.db_role(principal) if policy is not None else None
                with self._storage_backend.session_role(role):
                    return self._search_backend(
                        query=query,
                        limit=limit,
                        filter=filter,
                        include_vectors=include_vectors,
                        score_threshold=score_threshold,
                        start_time=start_time,
                        policy=policy,
                        principal=principal,
                    )
            except Exception as e:
                if store_only or strict_backend:
                    # The local index does not hold the vector that was asked
                    # for, or the caller asked for the store by name. Falling
                    # back would return a confident answer to a different
                    # question, or from a different place, and say neither.
                    raise
                if policy is not None and self._policy_pushable(self._storage_backend):
                    # The store was the engine for this policy. Answering from
                    # the local copy instead would move enforcement without
                    # telling anyone, which is the quiet degradation the
                    # pushdown option exists to refuse.
                    raise
                logger.warning("backend search failed, falling back to the local index: %s", e)
                # Fall through to local search

        with self._lock:
            # Set search parameter
            if ef and self._backend == "hnswlib":
                self._index.set_ef(ef)

            # How wide a window to ask the index for. A filter is applied to
            # whatever the index has already chosen, so the window has to be
            # wider than the limit or a selective filter finds nothing in it.
            # This was a flat limit * 10, which at a one percent pass rate
            # returned an empty result while plenty of documents matched, and
            # an empty result is indistinguishable from "nothing matched".
            # The window now grows until the limit is filled or the index is
            # exhausted.
            filter_obj = Filter.from_dict(filter) if filter else None
            held = max(self._count, self._index_size())
            search_limit = limit * 10 if (filter_obj or policy) else limit
            examined = 0
            disclosable = 0
            undisclosable = 0
            results: list = []

            while True:
                # A deleted vector stays in the graph as a tombstone, so the
                # index holds more keys than the collection has live
                # documents. Asking for exactly the live count could come back
                # holding nothing but tombstones: with two documents, one
                # deleted, a query whose nearest neighbour was the deleted one
                # returned no results at all while the survivor sat in the
                # index. Ask for the tombstones on top of what was wanted, so
                # even if every one of them ranks ahead there are still enough
                # live neighbours behind them; the loop below drops them by id
                # lookup as it already did.
                reachable = max(1, min(held, search_limit + (held - self._count)))

                if self._backend == "usearch":
                    if held <= EXACT_SEARCH_BELOW and isinstance(self._index, UsearchIndex):
                        # A small index is searched exactly: below a few
                        # thousand vectors brute force costs what the graph
                        # walk costs, and HNSW, built on several threads,
                        # misses a true neighbour now and then even at forty
                        # vectors. A sharded index already asks each small
                        # shard for everything it holds.
                        matches = self._index.search(query, reachable, exact=True)
                    else:
                        matches = self._index.search(query, reachable)
                    if hasattr(matches, "keys"):
                        indices = matches.keys.flatten().tolist()
                        # usearch reports a distance; SearchResult.score is a
                        # similarity where higher is better. Converted with the
                        # same formula the hnswlib branch below uses, so the two
                        # backends agree. Returning the raw distance here meant
                        # score_threshold filtered out the closest matches and
                        # callers ranking by score got the order backwards.
                        distances = matches.distances.flatten().tolist()
                        scores = self._distances_to_scores(distances)
                    else:
                        indices, scores = [], []
                else:  # hnswlib
                    if self._count == 0:
                        indices, scores = [], []
                    else:
                        indices, distances = self._index.knn_query(query, k=reachable)
                        indices = indices[0].tolist()
                        scores = self._distances_to_scores(distances[0].tolist())

                # Build results with metadata. The counters start again with
                # the results: a wider window looks at the candidates of the
                # narrower one too, and counting them twice put withheld
                # counts on the decision record that were too high.
                results = []
                examined = 0
                disclosable = 0
                undisclosable = 0

                # One round trip for every candidate's payload instead of one
                # per result. Profiled at 2,000 vectors: the index answered in
                # about a millisecond and ten single-row SELECTs took six.
                wanted = [
                    idx
                    for idx, score in zip(indices, scores)
                    if idx in self._idx_to_id
                    and (score_threshold is None or score >= score_threshold)
                ]
                rows = self._fetch_payloads(wanted)

                for idx, score in zip(indices, scores):
                    if idx not in self._idx_to_id:
                        continue

                    # Apply score threshold
                    if score_threshold is not None and score < score_threshold:
                        continue

                    id_ = self._idx_to_id[idx]
                    row = rows.get(idx)

                    if not row:
                        continue

                    metadata = json.loads(row["metadata"]) if row["metadata"] else {}

                    # Apply filter
                    if filter_obj and not filter_obj.matches(metadata):
                        continue

                    if policy is not None:
                        # Every candidate the policy sees is counted, and the
                        # two kinds of withholding are kept apart: a caller
                        # inside the scope may be told something was held
                        # back, one outside it may not.
                        examined += 1
                        decision = policy.decide(principal, metadata)
                        if not decision.allowed:
                            if decision.in_scope:
                                disclosable += 1
                            else:
                                undisclosable += 1
                            continue

                    result = SearchResult(
                        id=id_,
                        score=float(score),
                        metadata=metadata,
                        text=row["text_content"],
                    )

                    if include_vectors:
                        # Get vector from index
                        if self._backend == "usearch":
                            result.vector = self._index.get(idx).tolist()

                    results.append(result)

                    if len(results) >= limit:
                        break

                # Only a filtered search can come up short for want of a wider
                # window; an unfiltered one already has the nearest neighbours.
                if (
                    (filter_obj is None and policy is None)
                    or len(results) >= limit
                    or reachable >= held
                ):
                    break
                search_limit *= 4

            # The index is approximate, and asked for every node it returns
            # most of them, not all: a few are unreachable in the graph. With
            # the window already the whole index and the result still short,
            # the points it did not return are scored exactly, so a filter
            # that matches ten documents in a thousand finds all ten.
            if (
                (filter_obj is not None or policy is not None)
                and len(results) < limit
                and reachable >= held
                and self._count > 0
            ):
                returned = set(indices)
                unseen = [idx for idx in self._idx_to_id if idx not in returned]
                for result in self._score_exactly(query, unseen, score_threshold, include_vectors):
                    if filter_obj and not filter_obj.matches(result.metadata):
                        continue
                    if policy is not None:
                        examined += 1
                        decision = policy.decide(principal, result.metadata)
                        if not decision.allowed:
                            if decision.in_scope:
                                disclosable += 1
                            else:
                                undisclosable += 1
                            continue
                    results.append(result)
                results.sort(key=lambda r: r.score, reverse=True)
                results = results[:limit]

            query_time = (time.perf_counter() - start_time) * 1000
            self._mark_relevance(results)

            search_results = SearchResults(
                results=results,
                query_time_ms=query_time,
                total_searched=self._count,
                search_mode=SearchMode.VECTOR,
                policy_candidates=examined if policy is not None else None,
                policy_withheld_disclosable=disclosable if policy is not None else None,
                policy_withheld_undisclosable=undisclosable if policy is not None else None,
            )

            # Cache results
            if use_cache_result and self._cache:
                self._cache.set_search_results(
                    collection=self.name,
                    query=query.flatten().tolist(),
                    results=copy.deepcopy(search_results),
                    filter=filter,
                    limit=limit,
                    options=cache_options,
                )

            return search_results

    def get_meta(self, key: str) -> Optional[str]:
        """A value from the collection's own key/value table, or None."""
        row = self._db.execute("SELECT value FROM collection_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: Optional[str]) -> None:
        """Set or clear a value in the collection's key/value table."""
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM collection_meta WHERE key = ?", (key,))
            else:
                self._db.execute(
                    "INSERT OR REPLACE INTO collection_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
            self._db.commit()

    def iter_documents(self, batch_size: int = 1000, principal: Optional[dict] = None):
        """Yield (id, text, metadata) for every document this principal may
        see, oldest idx first."""
        policy = self.policy
        if policy is not None and principal is None:
            from ..exceptions import PrincipalRequired

            raise PrincipalRequired(self.name)
        for id_, text, metadata in self._iter_documents_raw(batch_size):
            if policy is not None and not policy.decide(principal, metadata).allowed:
                continue
            yield id_, text, metadata

    def _iter_written_raw(self, batch_size: int = 1000):
        """Every chunk's time of writing and metadata, in the order written, with no policy applied.

        For the library's own bookkeeping: growth by day, and which build
        wrote what and when. The time is when the row was first made; a chunk
        written again under the same id keeps it.
        """
        offset = 0
        while True:
            rows = self._db.execute(
                "SELECT created_at, metadata FROM points ORDER BY idx LIMIT ? OFFSET ?",
                (batch_size, offset),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield row["created_at"], (json.loads(row["metadata"]) if row["metadata"] else {})
            offset += batch_size

    def _iter_documents_raw(self, batch_size: int = 1000):
        """Every document with no policy applied. See :meth:`_get_raw`."""
        offset = 0
        while True:
            rows = self._db.execute(
                "SELECT id, text_content, metadata FROM points ORDER BY idx LIMIT ? OFFSET ?",
                (batch_size, offset),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                yield row["id"], row["text_content"] or "", metadata
            offset += batch_size

    @property
    def tombstone_ratio(self) -> float:
        """How much of the ANN index is deleted vectors, from 0.0 to 1.0.

        A deleted vector stays in the HNSW graph and is filtered out after
        the search, so the index keeps doing work for documents that are
        gone. At a fifth deleted this was measured taking a query from 1.0 ms
        to 18.0 ms on 20,000 vectors. ``rebuild_index()`` clears it.
        """
        held = self._index_size()
        if held <= 0:
            return 0.0
        return max(0.0, (held - self._count) / held)

    def _index_size(self) -> int:
        """How many keys the ANN index holds, tombstones included.

        Both backends count deleted entries here; that is the point. The
        live count is ``self._count``.
        """
        try:
            if self._backend == "usearch":
                return len(self._index)
            counter = getattr(self._index, "get_current_count", None)
            return int(counter()) if callable(counter) else self._count
        except Exception:  # pragma: no cover - backend without a size
            return self._count

    def _score_exactly(
        self, query: np.ndarray, idxs: list, score_threshold: Optional[float], include_vectors: bool
    ) -> list:
        """Results for these index keys by exact distance, best first.

        For the few points an approximate search did not return. The vectors
        come back out of the index, and the distance is the collection's own
        metric, turned into a score the way every other result's is.
        """
        if not idxs:
            return []
        try:
            if self._backend == "usearch":
                stored = self._index.get(np.asarray(idxs, dtype=np.uint64))
            else:
                stored = self._index.get_items(idxs)
        except Exception:  # pragma: no cover - an index that cannot hand vectors back
            return []
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        kept, vectors = [], []
        for idx, vector in zip(idxs, stored):
            if vector is None:
                continue
            kept.append(idx)
            vectors.append(np.asarray(vector, dtype=np.float32).reshape(-1))
        if not kept:
            return []
        matrix = np.vstack(vectors)
        distances: Any
        if self.metric == DistanceMetric.COSINE:
            norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(q) or 1.0)
            distances = 1.0 - (matrix @ q) / np.where(norms == 0, 1.0, norms)
        elif self.metric == DistanceMetric.EUCLIDEAN:
            distances = np.sum((matrix - q) ** 2, axis=1)
        elif self.metric == DistanceMetric.MANHATTAN:
            distances = np.sum(np.abs(matrix - q), axis=1)
        else:
            distances = 1.0 - (matrix @ q)
        scores = self._distances_to_scores([float(d) for d in distances])
        rows = self._fetch_payloads(kept)
        out = []
        for idx, score, vector in zip(kept, scores, vectors):
            row = rows.get(idx)
            if not row or (score_threshold is not None and score < score_threshold):
                continue
            result = SearchResult(
                id=self._idx_to_id[idx],
                score=float(score),
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                text=row["text_content"],
            )
            if include_vectors:
                result.vector = vector.tolist()
            out.append(result)
        return sorted(out, key=lambda r: r.score, reverse=True)

    def _fetch_payloads(self, idxs: list) -> dict:
        """metadata and text for each idx, by idx, in as few statements as
        SQLite's parameter limit allows."""
        rows: dict = {}
        step = 500
        for start in range(0, len(idxs), step):
            chunk = idxs[start : start + step]
            marks = ",".join("?" * len(chunk))
            for row in self._db.execute(
                f"SELECT idx, metadata, text_content FROM points WHERE idx IN ({marks})",
                chunk,
            ):
                rows[row["idx"]] = row
        return rows

    def _search_backend(
        self,
        query: np.ndarray,
        limit: int,
        filter: Optional[dict[str, Any]],
        include_vectors: bool,
        score_threshold: Optional[float],
        start_time: float,
        policy: Any = None,
        principal: Optional[dict] = None,
    ) -> SearchResults:
        """Search using the storage backend (Lakebase/pgvector, Delta Lake, etc.).

        The backend returns candidates and the filter is applied here, so the
        policy is applied here too and its counts are as exact as the local
        path's. A backend that filtered in the query would enforce but could
        not count, which is what the pushdown work on ROADMAP is about.
        """
        query_list = query.flatten().tolist()

        # What the store can decide for itself. A backend that declares
        # filterable fields is handed the policy's scope rules and the
        # caller's filter in its own language. Every redaction rule, and
        # anything the store could not take, is decided here over what came
        # back, so the withheld-disclosable count stays exact. A document
        # outside the scope never leaves the store, and that count is never
        # disclosed anyway.
        engine_filter = self._engine_filter(filter, policy, principal)
        extra = {"filter": engine_filter} if engine_filter is not None else {}
        filter_obj = Filter.from_dict(filter) if filter else None
        narrowed = filter_obj is not None or policy is not None
        # The window the store is asked for. A filter is applied to what the
        # store already chose, so the window has to be wider than the limit
        # or a selective filter finds nothing in it. This was a flat limit *
        # 2, which at a one percent pass rate came back short while plenty of
        # documents matched. The window now grows, as the local path's does,
        # until the limit is filled or the store has nothing more to give.
        window = limit * 2 if narrowed else limit
        results: list = []
        examined = 0
        disclosable = 0
        undisclosable = 0

        while True:
            backend_results = self._storage_backend.vector_search(
                collection=self.name,
                query_vector=query_list,
                limit=window,
                **extra,
            )

            # Build results. The counters start again with the results: a
            # wider window looks at the candidates of the narrower one too.
            results = []
            examined = 0
            disclosable = 0
            undisclosable = 0

            for id_, data, distance in backend_results:
                # A distance to a score the way the local index's distances
                # are converted, so a euclidean or dot collection scores the
                # same from the store as from its own index. A negative
                # cosine distance is not a distance and is passed through as
                # it was.
                if distance < 0 and self.metric == DistanceMetric.COSINE:
                    score = distance
                else:
                    score = self._distances_to_scores([distance])[0]

                # Apply score threshold
                if score_threshold is not None and score < score_threshold:
                    continue

                # The metadata as it was given, without the store's own
                # columns: the text, the embeddings, and what its engine
                # reported about the match. A caller's own key starting
                # with an underscore is theirs and stays, as it does on the
                # local path.
                metadata = {
                    k: v
                    for k, v in data.items()
                    if k not in _STORE_COLUMNS
                    and not k.startswith("_vx_relevance")
                    and k != "_vx_matched_by"
                }

                # Apply filter
                if filter_obj and not filter_obj.matches(metadata):
                    continue

                if policy is not None:
                    examined += 1
                    decision = policy.decide(principal, metadata)
                    if not decision.allowed:
                        if decision.in_scope:
                            disclosable += 1
                        else:
                            undisclosable += 1
                        continue

                result = SearchResult(
                    id=id_,
                    score=float(score),
                    metadata=metadata,
                )
                # What the store worked out from its own engine's scoring. A
                # store that does not know says nothing, and neither does this.
                if data.get("_vx_relevance") is not None:
                    result.relevance = float(data["_vx_relevance"])
                    result.relevance_kind = str(data.get("_vx_relevance_kind") or "similarity")
                if data.get("_vx_relevances"):
                    result.relevances = dict(data["_vx_relevances"])
                result.matched_by = list(data.get("_vx_matched_by") or ["meaning"])
                if data.get("text_content") is not None:
                    result.text = data["text_content"]

                results.append(result)

                if len(results) >= limit:
                    break

            # Only a narrowed search can come up short for want of a wider
            # window; fewer rows than asked for means the store is exhausted.
            if not narrowed or len(results) >= limit or len(backend_results) < window:
                break
            window *= 4

        query_time = (time.perf_counter() - start_time) * 1000

        return SearchResults(
            results=self._with_texts(results),
            query_time_ms=query_time,
            total_searched=-1,  # Unknown when using backend
            search_mode=SearchMode.VECTOR,
            policy_candidates=examined if policy is not None else None,
            policy_withheld_disclosable=disclosable if policy is not None else None,
            policy_withheld_undisclosable=undisclosable if policy is not None else None,
        )

    def document_count(self) -> int:
        """How many documents the chunks came from: the distinct ``_vx_doc`` values.

        A chunk written with no document, a point sent to the REST API say,
        belongs to none. Counted again only when the collection has changed,
        since only a write changes it.
        """
        key = (self._count_raw(), self.get_meta("index_build_id"))
        seen = getattr(self, "_documents_seen", None)
        if seen is not None and seen[0] == key:
            return int(seen[1])
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT COUNT(DISTINCT json_extract(metadata, '$._vx_doc')) FROM points"
                ).fetchone()
        except (
            sqlite3.Error
        ):  # an SQLite built without JSON support counts nothing rather than failing a page
            return 0
        count = int(row[0] or 0) if row else 0
        self._documents_seen = (key, count)
        return count

    def _with_texts(self, results: list[SearchResult]) -> list[SearchResult]:
        """Each result's chunk text, where the search that found it did not read it.

        The vector search always carried the text. The keyword, hybrid,
        sparse and reranked searches did not, so a collection filled through
        the library, which keeps the text beside the vector and not in the
        metadata, came back from them with nothing to show but its metadata.
        One read for the lot, and only for the results that lack it.
        """
        wanted: dict[int, SearchResult] = {}
        for result in results:
            if result.text is None:
                idx = self._id_to_idx.get(result.id)
                if idx is not None:
                    wanted[idx] = result
        if not wanted:
            return results
        marks = ",".join("?" * len(wanted))
        with self._lock:
            rows = self._db.execute(
                f"SELECT idx, text_content FROM points WHERE idx IN ({marks})", tuple(wanted)
            ).fetchall()
        for row in rows:
            wanted[row["idx"]].text = row["text_content"]
        return results

    def _store_holds_the_text(self, method: str) -> bool:
        """Is the store the place this collection's text is searched?

        The rule vector search already follows: a collection with a store and
        nothing on this disk is read from the store. A query instance opening
        a collection another instance filled has an empty local text index,
        and its keyword and hybrid searches answered nothing at all, with a
        200, until 2026-10-02 (found on the live Azure query app).

        Only a store that searches words itself, one whose method takes
        ``query_text`` (Azure AI Search, OpenSearch). The local stores'
        ``hybrid_search`` takes a dense and a sparse vector instead, and their
        collections keep their own text index.
        """
        backend = self._storage_backend
        if backend is None or not self._use_backend_for_vectors or self._count != 0:
            return False
        found = getattr(backend, method, None)
        if not callable(found):
            return False
        import inspect

        try:
            return "query_text" in inspect.signature(found).parameters
        except (TypeError, ValueError):  # pragma: no cover - a builtin without a signature
            return False

    def _rows_from_store(
        self,
        rows: list,
        limit: int,
        filter: Optional[dict[str, Any]],
        principal: Optional[dict],
        matched_by: list[str],
    ) -> tuple[list[SearchResult], int, int, int]:
        """The store's ``(id, data, score)`` rows as results, filtered and policed here.

        The same rules :meth:`_search_backend` applies: the store's own columns
        are not metadata, the caller's filter and the policy decide over what
        came back, and the counts of what was withheld stay exact.
        """
        policy = self.policy
        filter_obj = Filter.from_dict(filter) if filter else None
        results: list[SearchResult] = []
        examined = disclosable = undisclosable = 0
        for id_, data, score in rows:
            metadata = {
                k: v
                for k, v in data.items()
                if k not in _STORE_COLUMNS
                and not k.startswith("_vx_relevance")
                and k != "_vx_matched_by"
            }
            if filter_obj and not filter_obj.matches(metadata):
                continue
            if policy is not None:
                examined += 1
                decision = policy.decide(principal, metadata)
                if not decision.allowed:
                    if decision.in_scope:
                        disclosable += 1
                    else:
                        undisclosable += 1
                    continue
            result = SearchResult(id=id_, score=float(score), metadata=metadata)
            if data.get("_vx_relevance") is not None:
                result.relevance = float(data["_vx_relevance"])
                result.relevance_kind = str(data.get("_vx_relevance_kind") or "similarity")
            if data.get("_vx_relevances"):
                result.relevances = dict(data["_vx_relevances"])
            result.matched_by = list(data.get("_vx_matched_by") or matched_by)
            if data.get("text_content") is not None:
                result.text = data["text_content"]
            results.append(result)
            if len(results) >= limit:
                break
        return results, examined, disclosable, undisclosable

    def _keyword_from_store(
        self,
        query_text: str,
        limit: int,
        filter: Optional[dict[str, Any]],
        principal: Optional[dict],
        start_time: float,
    ) -> SearchResults:
        """Keyword search answered by the store's own engine (BM25 on Azure AI Search)."""
        from . import relevance as rel

        policy = self.policy
        if policy is not None and principal is None:
            from ..exceptions import PrincipalRequired

            raise PrincipalRequired(self.name)
        engine_filter = self._engine_filter(filter, policy, principal)
        extra = {"filter": engine_filter} if engine_filter is not None else {}
        narrowed = filter is not None or policy is not None
        window = limit * 2 if narrowed else limit
        while True:
            rows = self._storage_backend.text_search(
                collection=self.name, query_text=query_text, limit=window, **extra
            )
            results, examined, disclosable, undisclosable = self._rows_from_store(
                rows, limit, filter, principal, [rel.MATCHED_KEYWORDS]
            )
            if not narrowed or len(results) >= limit or len(rows) < window:
                break
            window *= 4
        for result in results:
            result.text_score = result.score
        # Whatever the store said about relevance stands; a bare BM25 weight
        # has no ceiling, so it is shown as a share of the best hit.
        if not any(r.relevance is not None for r in results):
            for result, share in zip(results, rel.relative(r.score for r in results)):
                result.relevance, result.relevance_kind = share, rel.RELATIVE
        return SearchResults(
            results=results,
            query_time_ms=(time.perf_counter() - start_time) * 1000,
            total_searched=-1,
            search_mode=SearchMode.KEYWORD,
            policy_candidates=examined if policy is not None else None,
            policy_withheld_disclosable=disclosable if policy is not None else None,
            policy_withheld_undisclosable=undisclosable if policy is not None else None,
        )

    def _hybrid_from_store(
        self,
        query: Union[list[float], np.ndarray],
        query_text: str,
        limit: int,
        filter: Optional[dict[str, Any]],
        start_time: float,
    ) -> SearchResults:
        """Hybrid search the store fuses itself: one request with the words and the vector."""
        engine_filter = self._engine_filter(filter, None, None)
        extra = {"filter": engine_filter} if engine_filter is not None else {}
        vector = np.asarray(query, dtype=np.float32).flatten().tolist()
        window = limit * 2 if filter else limit
        rows = self._storage_backend.hybrid_search(
            collection=self.name, query_vector=vector, query_text=query_text, limit=window, **extra
        )
        results, _, _, _ = self._rows_from_store(rows, limit, filter, None, ["meaning", "keywords"])
        return SearchResults(
            results=results,
            query_time_ms=(time.perf_counter() - start_time) * 1000,
            total_searched=-1,
            search_mode=SearchMode.HYBRID,
        )

    def keyword_search(
        self,
        query_text: str,
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
        include_highlights: bool = True,
        principal: Optional[dict] = None,
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
        self._refuse_if_closed("keyword_search()")
        start_time = time.perf_counter()

        if self._store_holds_the_text("text_search"):
            return self._keyword_from_store(query_text, limit, filter, principal, start_time)

        if not self._text_index:
            raise RuntimeError(
                "Text index not enabled. Create collection with enable_text_index=True"
            )

        # This is the sparse half of a hybrid search, so leaving it
        # unpoliced would put documents the principal may not see into
        # the fusion and out through the answer.
        policy = self.policy
        if policy is not None and principal is None:
            from ..exceptions import PrincipalRequired

            raise PrincipalRequired(self.name)

        with self._lock:
            # Which documents may be scored at all. The policy decides here
            # rather than over the results, because BM25 weights a term by how
            # many documents hold it: deciding afterwards leaves the withheld
            # documents in the statistics, where they change the score of a
            # visible document and can reorder two of them.
            filtered_ids = None
            filter_obj = Filter.from_dict(filter) if filter else None
            if filter_obj is not None or policy is not None:
                live = self._id_to_idx
                filtered_ids = set()
                for row in self._db.execute("SELECT id, metadata FROM points"):
                    if row["id"] not in live:
                        continue
                    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                    if filter_obj is not None and not filter_obj.matches(metadata):
                        continue
                    if policy is not None and not policy.decide(principal, metadata).allowed:
                        continue
                    filtered_ids.add(row["id"])

            # Search text index
            text_results = self._text_index.search(
                query_text,
                limit * 2,
                filtered_ids,
                statistics_over_subset=policy is not None,
            )

            results = []
            for id_, text_score in text_results:
                idx = self._id_to_idx.get(id_)
                if idx is None:
                    continue

                row = self._db.execute(
                    "SELECT metadata, text_content FROM points WHERE idx = ?", (idx,)
                ).fetchone()
                metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}

                # filtered_ids already carries this decision, so this never
                # fires today. It stays as the belt: the day somebody stops
                # computing that set, this is what still refuses.
                if policy is not None and not policy.decide(principal, metadata).allowed:
                    continue

                result = SearchResult(
                    id=id_,
                    score=text_score,
                    metadata=metadata,
                    text_score=text_score,
                )

                if include_highlights:
                    result.highlights = self._text_index.get_highlights(id_, query_text)

                results.append(result)
                if len(results) >= limit:
                    break

            # A BM25 weight has no ceiling, so the honest number is a share of
            # the best hit, and it is labelled as that.
            from . import relevance as rel

            for result, share in zip(results, rel.relative(r.score for r in results)):
                result.relevance, result.relevance_kind = share, rel.RELATIVE
                result.matched_by = [rel.MATCHED_KEYWORDS]

            return SearchResults(
                results=self._with_texts(results),
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
        self._refuse_if_policied(
            "hybrid_search()",
            "its sparse half reads the text index directly, which no policy sees.",
        )
        start_time = time.perf_counter()

        if self._store_holds_the_text("hybrid_search"):
            return self._hybrid_from_store(query, query_text, limit, filter, start_time)

        if not self._text_index:
            # Fall back to pure vector search
            return self.search(query, limit, filter, include_vectors)

        # Normalize weights
        total_weight = vector_weight + text_weight
        vector_weight = vector_weight / total_weight
        text_weight = text_weight / total_weight

        # Calculate prefetch size - get more candidates for better fusion
        prefetch_limit = max(limit, min(limit * prefetch_multiplier, self._count))

        with self._lock:
            # Get vector search results (get more for fusion)
            vector_results = self.search(query, prefetch_limit, filter, include_vectors=False)
            similar = {
                r.id: (r.relevance, r.relevance_kind)
                for r in vector_results.results
                if r.relevance is not None
            }
            # "Found by meaning" is being among the nearest, with some
            # likeness at all. The prefetch is wide, and on a small collection
            # it is everything, which is not the same as having been found.
            near = {
                r.id
                for r in vector_results.results[:limit]
                if (r.relevance or 0.0) > 0.0 or r.relevance is None
            }

            # Get text search results (search all, not just vector results)
            # This allows BM25 to find documents that dense search might miss
            text_results = self._text_index.search(query_text, prefetch_limit, None)

            # Apply filter to text results if needed
            if filter:
                filter_obj = Filter.from_dict(filter)
                filtered_text_results = []
                for id_, score in text_results:
                    idx = self._id_to_idx.get(id_)
                    if idx is not None:
                        row = self._db.execute(
                            "SELECT metadata, text_content FROM points WHERE idx = ?", (idx,)
                        ).fetchone()
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
                        "vector_score": 0,
                        "text_score": 0,
                        "rrf_vector": 0,
                        "rrf_text": 0,
                        "metadata": result.metadata,
                    }
                scores[result.id]["vector_score"] = result.score
                scores[result.id]["rrf_vector"] = 1.0 / (rrf_k + rank + 1)

            # Add text scores with RRF
            for rank, (id_, text_score) in enumerate(text_results):
                if id_ not in scores:
                    idx = self._id_to_idx.get(id_)
                    if idx is not None:
                        row = self._db.execute(
                            "SELECT metadata, text_content FROM points WHERE idx = ?", (idx,)
                        ).fetchone()
                        metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}
                        scores[id_] = {
                            "vector_score": 0,
                            "text_score": 0,
                            "rrf_vector": 0,
                            "rrf_text": 0,
                            "metadata": metadata,
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
                scores[id_]["combined_score"] = vector_weight * rrf_vector + text_weight * rrf_text

                # Bonus for appearing in both lists (intersection boost)
                if rrf_vector > 0 and rrf_text > 0:
                    # Small boost for documents found by both methods
                    scores[id_]["combined_score"] *= 1.1

            # Sort by combined score
            sorted_ids = sorted(
                scores.keys(), key=lambda x: scores[x]["combined_score"], reverse=True
            )

            results = []
            for id_ in sorted_ids[:limit]:
                score_data = scores[id_]

                result = SearchResult(
                    id=id_,
                    score=score_data["combined_score"],
                    metadata=score_data["metadata"],
                    text_score=score_data["text_score"],
                )
                # The order came from ranks. How good the match is comes from
                # the meaning: the similarity the vector search measured, or,
                # for a chunk only the keywords found, the exact one.
                known = similar.get(id_)
                result.relevance = known[0] if known else self._similarity_of(query, id_)
                result.relevance_kind = (
                    (known[1] if known else "similarity") if result.relevance is not None else None
                )
                result.matched_by = [
                    name
                    for name, found in (
                        ("meaning", id_ in near),
                        ("keywords", score_data["rrf_text"] > 0),
                    )
                    if found
                ]

                if include_vectors:
                    idx = self._id_to_idx.get(id_)
                    if idx is not None and self._backend == "usearch":
                        result.vector = self._index.get(idx).tolist()

                if include_highlights and query_text:
                    result.highlights = self._text_index.get_highlights(id_, query_text)

                results.append(result)

            return SearchResults(
                results=self._with_texts(results),
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
        principal: Optional[dict] = None,
    ) -> SearchResults:
        """
        Search using sparse vectors only.

        Use this for SPLADE, BM25, or other sparse embedding models.

        Args:
            query: Sparse query vector (SparseVector or {index: value} dict)
            limit: Maximum results
            filter: Metadata filter
            score_threshold: Minimum score threshold
            principal: required when the collection carries a policy

        Returns:
            SearchResults with sparse similarity scores
        """
        self._require_principal("sparse_search()", principal)
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

        policy = self.policy
        with self._lock:
            # Which documents may be scored at all. The policy decides here,
            # as in text_search: it was never applied, so a principal saw
            # withheld documents, and deciding after the search would let
            # them use up the limit.
            filtered_ids = None
            filter_obj = Filter.from_dict(filter) if filter else None
            if filter_obj is not None or policy is not None:
                live = self._id_to_idx
                filtered_ids = set()
                for row in self._db.execute("SELECT id, metadata FROM points"):
                    if row["id"] not in live:
                        continue
                    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                    if filter_obj is not None and not filter_obj.matches(metadata):
                        continue
                    if policy is not None and not policy.decide(principal or {}, metadata).allowed:
                        continue
                    filtered_ids.add(row["id"])

            # Search sparse index
            sparse_results = self._sparse_index.search(
                query, limit=limit, doc_ids=filtered_ids, score_threshold=score_threshold
            )

            results = []
            for sparse_result in sparse_results:
                idx = self._id_to_idx.get(sparse_result.id)
                if idx is None:
                    continue

                row = self._db.execute(
                    "SELECT metadata, text_content FROM points WHERE idx = ?", (idx,)
                ).fetchone()
                metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}

                result = SearchResult(
                    id=sparse_result.id,
                    score=sparse_result.score,
                    metadata=metadata,
                    sparse_score=sparse_result.score,
                )
                results.append(result)

            return SearchResults(
                results=self._with_texts(results),
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
                        "dense_score": 0,
                        "sparse_score": 0,
                        "metadata": result.metadata,
                    }
                scores[result.id]["dense_score"] = result.score
                scores[result.id]["rrf_dense"] = 1 / (k + rank + 1)

            # Add sparse scores
            for rank, sparse_result in enumerate(sparse_results):
                if sparse_result.id not in scores:
                    idx = self._id_to_idx.get(sparse_result.id)
                    if idx is not None:
                        row = self._db.execute(
                            "SELECT metadata, text_content FROM points WHERE idx = ?", (idx,)
                        ).fetchone()
                        metadata = json.loads(row["metadata"]) if row and row["metadata"] else {}
                        scores[sparse_result.id] = {
                            "dense_score": 0,
                            "sparse_score": 0,
                            "metadata": metadata,
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
                scores.keys(), key=lambda x: scores[x]["combined_score"], reverse=True
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
                    if idx is not None and self._backend == "usearch":
                        result.vector = self._index.get(idx).tolist()

                results.append(result)

            return SearchResults(
                results=self._with_texts(results),
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
        filter_dict: Optional[Dict[str, Any]] = None
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

        # Convert to dict format for reranker. The text is the chunk's own:
        # the library keeps it beside the vector and not in the metadata, and
        # a cross-encoder handed an empty string gives every candidate the
        # same score, so the order it hands back is the order it was given.
        candidates = [
            {
                "id": r.id,
                "score": r.score,
                "vector": r.vector,
                "metadata": r.metadata,
                "text": r.text or (r.metadata or {}).get("text", ""),
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
                text=r.get("text") or None,
                dense_score=r.get("original_score"),
            )
            for r in reranked
        ]
        from . import relevance as rel

        first = {r.id: r for r in candidates_results.results}
        if rerank_method == "cross_encoder" and query_text:
            # The cross-encoder read the pair, which beats a similarity.
            for result, judged in zip(results, rel.from_reranker(r.score for r in results)):
                result.relevance, result.relevance_kind = judged, rel.RERANKER
        else:
            for result in results:
                before = first.get(result.id)
                if before is not None:
                    result.relevance, result.relevance_kind = (
                        before.relevance,
                        before.relevance_kind,
                    )
        for result in results:
            before = first.get(result.id)
            result.matched_by = (
                list(before.matched_by)
                if before is not None and before.matched_by
                else [rel.MATCHED_MEANING]
            )

        total_time = (time.perf_counter() - start_time) * 1000

        return SearchResults(
            results=self._with_texts(results),
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
        user_principals: Sequence[Union[str, ACLPrincipal]],
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
        self._refuse_if_policied(
            "search_with_acl()",
            "ACL principals and an entitlement policy are two different controls, and running only one of them silently is the hazard.",
        )
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
            {"id": r.id, "score": r.score, "metadata": r.metadata} for r in search_results.results
        ]

        filtered = acl_filter.filter(
            documents=documents,
            user_principals=list(user_principals),
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
            results=self._with_texts(results),
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
        user_principals: Optional[Sequence[Union[str, ACLPrincipal]]] = None,
        facets: Optional[List[str]] = None,
        rerank: bool = False,
        rerank_method: str = "mmr",
        rerank_limit: int = 100,
        default_allow: bool = False,
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
        self._refuse_if_policied(
            "enterprise_search()",
            "ACL principals and an entitlement policy are two different controls, and running only one of them silently is the hazard.",
        )
        start_time = time.perf_counter()

        # Initial retrieval
        candidate_limit = rerank_limit if rerank else limit * 5
        search_results = self.search(
            query=query,
            limit=candidate_limit,
            filter=filter,
            include_vectors=rerank,
        )

        candidates: List[Dict[str, Any]] = [
            {
                "id": r.id,
                "score": r.score,
                "vector": r.vector,
                "metadata": r.metadata,
            }
            for r in search_results.results
        ]

        # ACL filtering. ``is not None``, not truthiness: the docstring says
        # only None skips ACL, and a user who belongs to no group must match
        # nothing rather than everything. Read for truthiness, an empty
        # principal list turned the filter off and returned every document,
        # which is strictly worse than passing a wrong principal.
        if user_principals is not None:
            acl_filter = ACLFilter()
            candidates = acl_filter.filter(
                documents=candidates,
                user_principals=list(user_principals),
                default_allow=default_allow,
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

    def get(self, id: str, principal: Optional[dict] = None) -> Optional[Point]:
        """Get a point by ID.

        A denial and a miss are the same answer, deliberately: telling a
        caller that an id exists but is not theirs confirms the document,
        which is what an id lookup could otherwise leak.
        """
        policy = self.policy
        if policy is not None:
            if principal is None:
                from ..exceptions import PrincipalRequired

                raise PrincipalRequired(self.name)
            point = self._get_raw(id)
            if point is None or not policy.decide(principal, point.metadata or {}).allowed:
                return None
            return point
        return self._get_raw(id)

    def _get_raw(self, id: str) -> Optional[Point]:
        """A point by id with no policy applied.

        For the collection's own bookkeeping, which has to see what is there
        rather than what some principal may have. Every call site is a write
        path or an internal index, and naming it this way is what keeps that
        reviewable.
        """
        with self._lock:
            if id not in self._id_to_idx:
                return None

            idx = self._id_to_idx[id]
            row = self._db.execute("SELECT * FROM points WHERE idx = ?", (idx,)).fetchone()

            if not row:
                return None

            # Get vector
            vector = None
            if self._backend == "usearch":
                # An index that was never saved comes back without this
                # key; the point is still a point, with no vector to show.
                # Asked first whether it holds the key: usearch before 2.26
                # answered get() for a removed key with the vector it had.
                held = self._index.get(idx) if self._index.contains(idx) else None
                vector = held.tolist() if held is not None else None

            return Point(
                id=row["id"],
                vector=vector or [],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                text=row["text_content"],
                created_at=(parse_iso(row["created_at"]) if row["created_at"] else None)
                or utcnow(),
                updated_at=parse_iso(row["updated_at"]) if row["updated_at"] else None,
            )

    def get_batch(self, ids: List[str], principal: Optional[dict] = None) -> List[Optional[Point]]:
        """Get multiple points by ID, gated the same way as :meth:`get`."""
        return [self.get(id_, principal=principal) for id_ in ids]

    def _get_batch_raw(self, ids: List[str]) -> List[Optional[Point]]:
        """Points by id with no policy applied. See :meth:`_get_raw`."""
        return [self._get_raw(id_) for id_ in ids]

    def delete(self, ids: list[str]) -> int:
        """
        Delete points by ID.

        Args:
            ids: List of IDs to delete

        Returns:
            Number of points deleted
        """
        self._writable()
        # The store first. It holds a row for every point, a search can be
        # served from it, and under a policy the decision is made on what it
        # hands back, so a point that is deleted here and not there is not
        # deleted: it stays in somebody's index, and it can come back in a
        # result. If the store refuses, nothing has been removed yet and the
        # caller hears about it.
        from_store = 0
        if self._use_backend_for_vectors and ids:
            from_store = int(self._storage_backend.delete_batch(self.name, list(ids)) or 0)
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
                # And from the sparse index, where it stayed searchable.
                if self._sparse_index.remove(id_):
                    self._sparse_dirty = True

                # Remove from mappings
                del self._id_to_idx[id_]
                del self._idx_to_id[idx]
                # A rebuild under way may have copied this key into its new
                # index already; told here, it takes the key out at the swap.
                if self._rebuild_deleted is not None:
                    self._rebuild_deleted.add(int(idx))

                # Note: Most HNSW implementations don't support deletion
                # The vector remains in index but won't be returned
                deleted += 1

            self._db.commit()
            self._count = len(self._id_to_idx)
            self._updated_at = utcnow()

        self._invalidate_cache()
        if ids:
            self._share("delete", lambda store: store.delete(list(ids)))
        # A point another process wrote is in the store and not here.
        return max(deleted, from_store)

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
        # What was asked, before the merge below replaces it: the chunk store
        # merges into its own copy, which another process may have changed.
        given = dict(metadata)
        with self._lock:
            if id not in self._id_to_idx:
                # Written by another process: the store has it and this
                # collection's local copy does not.
                if self._use_backend_for_vectors:
                    updated = bool(self._storage_backend.update(self.name, id, dict(metadata)))
                    if updated:
                        self._invalidate_cache()
                    self._share("update", lambda store: store.update(id, given, merge=merge))
                    return updated
                return bool(
                    self._share("update", lambda store: store.update(id, given, merge=merge))
                )

            idx = self._id_to_idx[id]
            removed: List[str] = []

            if merge:
                row = self._db.execute(
                    "SELECT metadata, text_content FROM points WHERE idx = ?", (idx,)
                ).fetchone()
                existing = json.loads(row["metadata"]) if row and row["metadata"] else {}
                existing.update(metadata)
                metadata = existing
            else:
                row = self._db.execute(
                    "SELECT metadata FROM points WHERE idx = ?", (idx,)
                ).fetchone()
                before = json.loads(row["metadata"]) if row and row["metadata"] else {}
                removed = [k for k in before if k not in metadata]

            # The store before the local copy, for the reason delete() gives:
            # under a policy the decision is made on the store's metadata, so
            # a revocation that stopped here would not revoke anything a
            # store-served search returns. A key that was replaced away goes
            # as null, which every rule reads as a refusal.
            if self._use_backend_for_vectors:
                self._storage_backend.update(
                    self.name, id, {**metadata, **{k: None for k in removed}}
                )

            now = utcnow().isoformat()
            self._db.execute(
                "UPDATE points SET metadata = ?, updated_at = ? WHERE idx = ?",
                (json.dumps(metadata), now, idx),
            )
            self._db.commit()
            self._updated_at = utcnow()

        self._invalidate_cache()
        self._share("update", lambda store: store.update(id, given, merge=merge))
        return True

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def _require_principal(self, op: str, principal: Optional[dict]) -> None:
        """A public read under a policy needs a principal or it refuses.

        The internal callers use the _raw paths below, so this never fires on
        the library's own bookkeeping. It fires on a caller, which is the
        point: the REST server holds a Collection directly.
        """
        if self.policy is not None and principal is None:
            from ..exceptions import PrincipalRequired

            raise PrincipalRequired(self.name)

    def _count_raw(self) -> int:
        """Every document, whatever the policy says. For internals."""
        return self._count

    def count(self, principal: Optional[dict] = None) -> int:
        """Number of vectors in the collection, as this principal sees it.

        A total is a fact about documents a principal may not know exist. A
        walled analyst watching the number move learns that a client they
        were refused is being written to, so under a policy this counts what
        the principal may see and refuses without one.
        """
        policy = self.policy
        if policy is None:
            return self._count
        self._require_principal("count()", principal)

        seen = 0
        live = self._id_to_idx
        for row in self._db.execute("SELECT id, metadata FROM points"):
            if row["id"] not in live:
                continue
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            if policy.decide(principal, metadata).allowed:
                seen += 1
        return seen

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

    def list_ids(
        self, limit: int = 100, offset: int = 0, principal: Optional[dict] = None
    ) -> list[str]:
        """List point IDs with pagination, as this principal sees them.

        An id is a hash of the text rather than the text, but a list of them
        is still a list of documents that exist, and how many.
        """
        policy = self.policy
        if policy is None:
            cursor = self._db.execute(
                "SELECT id FROM points ORDER BY idx LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return [row["id"] for row in cursor]

        self._require_principal("list_ids()", principal)
        live = self._id_to_idx
        found: list[str] = []
        skipped = 0
        for row in self._db.execute("SELECT id, metadata FROM points ORDER BY idx"):
            if row["id"] not in live:
                continue
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            if not policy.decide(principal, metadata).allowed:
                continue
            if skipped < offset:
                skipped += 1
                continue
            found.append(row["id"])
            if len(found) >= limit:
                break
        return found

    def scroll(
        self,
        limit: int = 100,
        offset: int = 0,
        filter: Optional[dict[str, Any]] = None,
        include_vectors: bool = False,
        principal: Optional[dict] = None,
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
        self._require_principal("scroll()", principal)
        return self._scroll(limit, offset, filter, include_vectors, principal)

    def _scroll_raw(
        self,
        limit: int = 100,
        offset: int = 0,
        filter: Optional[dict[str, Any]] = None,
        include_vectors: bool = False,
    ) -> tuple[List[Point], Optional[int]]:
        """Paging for the library's own bookkeeping, past the policy."""
        return self._scroll(limit, offset, filter, include_vectors, None)

    def _scroll(
        self,
        limit: int,
        offset: int,
        filter: Optional[dict[str, Any]],
        include_vectors: bool,
        principal: Optional[dict],
    ) -> tuple[List[Point], Optional[int]]:
        policy = self.policy

        def visible(meta: dict) -> bool:
            if filter_obj is not None and not filter_obj.matches(meta):
                return False
            if policy is not None and principal is not None:
                return policy.decide(principal, meta).allowed
            return True

        with self._lock:
            filter_obj = Filter.from_dict(filter) if filter else None

            # Without a filter one page of rows is one page of results. With
            # one, rows have to be read until the page is full, or a caller
            # asking for a hundred got however many of the first hundred rows
            # happened to match. Paging to exhaustion always collected
            # everything; it just took a surprising number of pages.
            rows = []
            scanned = 0
            narrowing = filter_obj is not None or policy is not None
            batch = limit + 1 if not narrowing else max(limit * 4, limit + 1)
            while True:
                page = self._db.execute(
                    "SELECT * FROM points ORDER BY idx LIMIT ? OFFSET ?",
                    (batch, offset + scanned),
                ).fetchall()
                if not page:
                    break
                scanned += len(page)
                for candidate in page:
                    meta = json.loads(candidate["metadata"]) if candidate["metadata"] else {}
                    if visible(meta):
                        rows.append(candidate)
                        if len(rows) > limit:
                            break
                if len(rows) > limit or len(page) < batch:
                    break

            # Where the next page starts: past everything read, unless this
            # page stopped early with rows still behind it.
            if len(rows) > limit:
                consumed = 0
                kept = 0
                for candidate in self._db.execute(
                    "SELECT * FROM points ORDER BY idx LIMIT ? OFFSET ?",
                    (scanned, offset),
                ).fetchall():
                    consumed += 1
                    meta = json.loads(candidate["metadata"]) if candidate["metadata"] else {}
                    if visible(meta):
                        kept += 1
                        if kept == limit:
                            break
                scanned = consumed

            points = []
            for row in rows[:limit]:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}

                vector = None
                if include_vectors and self._backend == "usearch":
                    vector = self._index.get(row["idx"]).tolist()

                points.append(
                    Point(
                        id=row["id"],
                        vector=vector or [],
                        metadata=metadata,
                        text=row["text_content"],
                        created_at=(parse_iso(row["created_at"]) if row["created_at"] else None)
                        or utcnow(),
                        updated_at=parse_iso(row["updated_at"]) if row["updated_at"] else None,
                    )
                )

            next_offset = offset + scanned if len(rows) > limit else None

            return points, next_offset

    def _writable(self) -> None:
        if self.readonly:
            from ..exceptions import ConfigurationError

            raise ConfigurationError(
                f"Collection {self.name!r} was opened read-only (memory-mapped); "
                "open it without readonly=True to write."
            )

    def _share(self, operation: str, action: Callable[[Any], Any]) -> Any:
        """``action`` on this collection's chunk store, when it has one, and what it returns.

        A store that refuses is an error rather than a page gone quietly out
        of date: the pages read it, and a write it missed is a chunk they
        never show.
        """
        store = self._chunk_store
        if store is None:
            return None
        try:
            return action(store)
        except Exception as exc:
            raise StorageOperationError(
                operation, "chunk store", f"{type(exc).__name__}: {exc}", collection=self.name
            ) from exc

    def _vector_from_storage(self, doc_id: str) -> Optional[np.ndarray]:
        """The stored copy of a document's vector, if the store has one."""
        backend = self._storage_backend
        if backend is None:
            return None
        try:
            data = backend.get(self.name, doc_id)
        except Exception:  # pragma: no cover - backend specific
            return None
        if not data:
            return None
        raw = data.get("_embedding") or data.get("dense_embedding")
        if not raw:
            return None
        vector = np.asarray(raw, dtype=np.float32).reshape(-1)
        return vector if vector.size == self.dimension else None

    def rebuild_index(self) -> int:
        """Rebuild the ANN index from the live vectors, without blocking readers.

        Deletions leave tombstones in an HNSW graph and recall drifts as the
        graph is edited in place. The new index is built outside the lock, so
        searches keep running against the old one; only the swap is locked.
        Vectors written meanwhile go to the old index and are carried over at
        the swap. Returns the number of vectors in the rebuilt index.
        """
        self._writable()
        if self._backend != "usearch":
            raise NotImplementedError("rebuild_index is implemented for the usearch backend")
        with self._rebuild_lock:
            return self._rebuild_index()

    def _rebuild_index(self) -> int:
        with self._lock:
            keys = np.array(sorted(self._idx_to_id.keys()), dtype=np.uint64)
            old = self._index
            # Keys written while the new index is built. They go to the old
            # index, and the swap used to drop them with it. Keys deleted
            # meanwhile: one already copied into the new index came back.
            self._rebuild_touched = set()
            self._rebuild_deleted = set()

        # Vectors come from the index where it has them, and from the document
        # store for any it does not. Collections written by 2.1.x never saved
        # the index at all, so their vectors survive only as the copy the
        # store keeps; this is how such a collection becomes searchable again.
        rows: List[Optional[np.ndarray]] = []
        for key in keys.tolist():
            vector = None
            try:
                if old.contains(key):
                    vector = np.asarray(old.get(key), dtype=np.float32).reshape(-1)
            except Exception:  # pragma: no cover - backend specific
                vector = None
            if vector is None or vector.size != self.dimension:
                vector = self._vector_from_storage(self._idx_to_id[key])
            rows.append(vector)
        keep = [i for i, v in enumerate(rows) if v is not None]
        keys = keys[keep]
        vectors = (
            np.vstack([row for row in rows if row is not None]).astype(np.float32)
            if keep
            else np.zeros((0, self.dimension), dtype=np.float32)
        )

        if hasattr(old, "rebuilt"):
            # A sharded index rebuilds into fresh shards, which is also its
            # compaction: shadowed copies and removed keys are not carried
            # over, and the shards are packed full again.
            fresh = old.rebuilt(keys.tolist(), list(vectors))
        else:
            fresh = UsearchIndex(
                ndim=self.dimension,
                metric=self.metric.usearch_metric,
                dtype="f32",
                connectivity=self._m,
                expansion_add=self._ef_construction,
                expansion_search=self.index_config.hnsw_ef_search,
            )
            if len(keys):
                fresh.add(keys, vectors, **_build_threads())

        with self._lock:
            touched, self._rebuild_touched = self._rebuild_touched, None
            deleted, self._rebuild_deleted = self._rebuild_deleted, None
            held = set(keys.tolist())
            for key in sorted(deleted or ()):
                if key in self._idx_to_id:
                    continue  # written again since: carried over below
                held.discard(key)
                try:
                    if fresh.contains(key):
                        fresh.remove(key)
                except Exception:  # pragma: no cover - backend specific
                    pass
            for key in sorted(touched or ()):
                if key not in self._idx_to_id:
                    continue
                vector = old.get(key)
                if vector is None:
                    continue
                if fresh.contains(key):
                    fresh.remove(key)
                fresh.add(
                    np.array([key], dtype=np.uint64),
                    np.asarray(vector, dtype=np.float32).reshape(1, -1),
                )
                held.add(key)
            self._index = fresh
            self._dirty = True
            self.save()
            return len(held)

    def save(self) -> None:
        """Save index to disk."""
        if not self.path:
            return

        if self.readonly:
            return
        with self._lock:
            self._db.commit()
            # The sparse index lives beside the dense one and was never
            # written, so sparse vectors were gone after a reopen.
            if self._sparse_dirty:
                self._sparse_index.save()
                self._sparse_dirty = False
            if not self._dirty:
                return
            # Write beside the target and rename over it. A reader that opens
            # the index while it is being written then sees the old file or
            # the new one, never a truncated one; and a process killed
            # mid-save leaves the last good index in place.
            if self._backend == "usearch":
                target = self.path / f"{self.name}.usearch"
                tmp = self.path / f"{self.name}.usearch.tmp"
                try:
                    self._index.save(self._native_path(tmp))
                except RuntimeError:
                    # Same path limits as on load: serialise in memory and
                    # let Python write the file.
                    blob = self._index.save()
                    assert blob is not None
                    Path(tmp).write_bytes(bytes(blob))
            else:
                target = self.path / f"{self.name}.hnsw"
                tmp = self.path / f"{self.name}.hnsw.tmp"
                self._index.save_index(str(tmp))
            self._replace(tmp, target)
            self._dirty = False

    @staticmethod
    def _replace(tmp, target) -> None:
        """os.replace, retried briefly on Windows.

        Windows refuses to rename over a file another process has open
        without delete sharing, and a reader loading the index holds it
        open for a few milliseconds. Wait it out rather than fail a save.
        """
        delay = 0.02
        for attempt in range(8):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)

    def close(self) -> None:
        """Close the collection and save."""
        self.save()
        self._closed = True
        self._db.close()

    def __len__(self) -> int:
        return self._count

    def __repr__(self) -> str:
        return f"Collection(name='{self.name}', dimension={self.dimension}, count={self._count})"
