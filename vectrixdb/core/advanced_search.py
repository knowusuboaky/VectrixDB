"""
VectrixDB Advanced Search Features.

Enterprise-grade search capabilities:
- Re-ranking / Two-stage retrieval
- Faceted search with aggregations
- ACL/Security filtering
- Text analyzers (stemming, synonyms, stopwords)

Author: Daddy Nyame Owusu - Boakye
"""

import re
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np


# =============================================================================
# Re-Ranking / Two-Stage Retrieval
# =============================================================================


class RerankMethod(str, Enum):
    """Re-ranking methods for two-stage retrieval."""

    EXACT = "exact"  # Exact distance recalculation
    CROSS_ENCODER = "cross_encoder"  # Neural cross-encoder (if available)
    MMR = "mmr"  # Maximal Marginal Relevance (diversity)
    RECIPROCAL_RANK = "rrf"  # Reciprocal Rank Fusion
    WEIGHTED = "weighted"  # Weighted combination of scores


@dataclass
class RerankConfig:
    """Configuration for re-ranking."""

    method: RerankMethod = RerankMethod.EXACT
    candidate_multiplier: int = 10  # Fetch limit * multiplier candidates
    diversity_lambda: float = 0.5  # For MMR: 0=diversity, 1=relevance
    score_weights: Dict[str, float] = field(default_factory=lambda: {
        "vector": 0.7,
        "text": 0.2,
        "recency": 0.1,
    })

    # Cross-encoder settings (if using neural reranking)
    cross_encoder_model: Optional[str] = None
    cross_encoder_batch_size: int = 32


class Reranker:
    """
    Two-stage retrieval with re-ranking.

    First stage: Fast approximate nearest neighbor (ANN) search
    Second stage: Precise re-ranking of top candidates

    Example:
        >>> reranker = Reranker(config=RerankConfig(method=RerankMethod.MMR))
        >>> results = reranker.rerank(
        ...     query_vector=[0.1, 0.2, ...],
        ...     candidates=initial_results,
        ...     limit=10
        ... )
    """

    def __init__(self, config: Optional[RerankConfig] = None):
        self.config = config or RerankConfig()
        self._cross_encoder = None

    def rerank(
        self,
        query_vector: np.ndarray,
        candidates: List[Dict[str, Any]],
        limit: int = 10,
        query_text: Optional[str] = None,
        get_vector_fn: Optional[Callable[[str], np.ndarray]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Re-rank candidates using configured method.

        Args:
            query_vector: Query vector for similarity
            candidates: List of candidate results with 'id', 'score', 'vector'
            limit: Final number of results
            query_text: Optional query text for cross-encoder
            get_vector_fn: Function to get vector by ID if not in candidates

        Returns:
            Re-ranked results
        """
        if not candidates:
            return []

        method = self.config.method

        if method == RerankMethod.EXACT:
            return self._rerank_exact(query_vector, candidates, limit, get_vector_fn)
        elif method == RerankMethod.MMR:
            return self._rerank_mmr(query_vector, candidates, limit, get_vector_fn)
        elif method == RerankMethod.CROSS_ENCODER:
            return self._rerank_cross_encoder(query_text, candidates, limit)
        elif method == RerankMethod.WEIGHTED:
            return self._rerank_weighted(candidates, limit)
        else:
            # Default: return top by existing score
            return sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:limit]

    def _rerank_exact(
        self,
        query_vector: np.ndarray,
        candidates: List[Dict],
        limit: int,
        get_vector_fn: Optional[Callable],
    ) -> List[Dict]:
        """Re-rank using exact distance calculation."""
        query_norm = np.linalg.norm(query_vector)

        scored = []
        for c in candidates:
            vec = c.get("vector")
            if vec is None and get_vector_fn:
                vec = get_vector_fn(c["id"])
            if vec is None:
                scored.append((c, c.get("score", 0)))
                continue

            vec = np.array(vec)
            # Cosine similarity
            dot = np.dot(query_vector, vec)
            vec_norm = np.linalg.norm(vec)
            if query_norm > 0 and vec_norm > 0:
                exact_score = dot / (query_norm * vec_norm)
            else:
                exact_score = 0

            c_copy = c.copy()
            c_copy["score"] = float(exact_score)
            c_copy["original_score"] = c.get("score", 0)
            scored.append((c_copy, exact_score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in scored[:limit]]

    def _rerank_mmr(
        self,
        query_vector: np.ndarray,
        candidates: List[Dict],
        limit: int,
        get_vector_fn: Optional[Callable],
    ) -> List[Dict]:
        """
        Maximal Marginal Relevance re-ranking for diversity.

        Balances relevance to query with diversity among results.
        """
        if not candidates:
            return []

        lambda_param = self.config.diversity_lambda

        # Get vectors for all candidates
        vectors = []
        for c in candidates:
            vec = c.get("vector")
            if vec is None and get_vector_fn:
                vec = get_vector_fn(c["id"])
            vectors.append(np.array(vec) if vec is not None else None)

        # Calculate query similarities
        query_norm = np.linalg.norm(query_vector)
        query_sims = []
        for vec in vectors:
            if vec is not None and query_norm > 0:
                sim = np.dot(query_vector, vec) / (query_norm * np.linalg.norm(vec) + 1e-10)
            else:
                sim = 0
            query_sims.append(sim)

        # MMR selection
        selected_indices = []
        remaining = set(range(len(candidates)))

        while len(selected_indices) < limit and remaining:
            best_idx = None
            best_mmr = float("-inf")

            for idx in remaining:
                if vectors[idx] is None:
                    continue

                # Relevance to query
                relevance = query_sims[idx]

                # Max similarity to already selected
                max_sim_to_selected = 0
                for sel_idx in selected_indices:
                    if vectors[sel_idx] is not None:
                        sim = np.dot(vectors[idx], vectors[sel_idx]) / (
                            np.linalg.norm(vectors[idx]) * np.linalg.norm(vectors[sel_idx]) + 1e-10
                        )
                        max_sim_to_selected = max(max_sim_to_selected, sim)

                # MMR score
                mmr = lambda_param * relevance - (1 - lambda_param) * max_sim_to_selected

                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = idx

            if best_idx is not None:
                selected_indices.append(best_idx)
                remaining.remove(best_idx)
            else:
                break

        # Build result list
        results = []
        for i, idx in enumerate(selected_indices):
            c_copy = candidates[idx].copy()
            c_copy["score"] = query_sims[idx]
            c_copy["mmr_rank"] = i + 1
            results.append(c_copy)

        return results

    def _rerank_cross_encoder(
        self,
        query_text: Optional[str],
        candidates: List[Dict],
        limit: int,
    ) -> List[Dict]:
        """Re-rank using cross-encoder model (requires sentence-transformers)."""
        if not query_text:
            return candidates[:limit]

        try:
            from sentence_transformers import CrossEncoder

            if self._cross_encoder is None:
                model_name = self.config.cross_encoder_model or "cross-encoder/ms-marco-MiniLM-L-6-v2"
                self._cross_encoder = CrossEncoder(model_name)

            # Prepare pairs
            texts = [c.get("text", c.get("metadata", {}).get("text", "")) for c in candidates]
            pairs = [[query_text, text] for text in texts]

            # Score in batches
            scores = self._cross_encoder.predict(
                pairs,
                batch_size=self.config.cross_encoder_batch_size,
            )

            # Combine with original scores
            results = []
            for c, score in zip(candidates, scores):
                c_copy = c.copy()
                c_copy["cross_encoder_score"] = float(score)
                c_copy["original_score"] = c.get("score", 0)
                c_copy["score"] = float(score)
                results.append(c_copy)

            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:limit]

        except ImportError:
            # Fall back to original scores
            return sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:limit]

    def _rerank_weighted(
        self,
        candidates: List[Dict],
        limit: int,
    ) -> List[Dict]:
        """Re-rank using weighted combination of multiple scores."""
        weights = self.config.score_weights

        results = []
        for c in candidates:
            combined = 0
            for key, weight in weights.items():
                score = c.get(f"{key}_score", c.get("score", 0) if key == "vector" else 0)
                combined += weight * score

            c_copy = c.copy()
            c_copy["combined_score"] = combined
            c_copy["score"] = combined
            results.append(c_copy)

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]


# =============================================================================
# Faceted Search
# =============================================================================


@dataclass
class FacetConfig:
    """Configuration for a facet."""

    field: str
    limit: int = 10  # Max values to return
    min_count: int = 1  # Minimum count to include
    sort_by: str = "count"  # "count" or "value"
    include_zero: bool = False


@dataclass
class FacetValue:
    """A single facet value with count."""

    value: Any
    count: int


@dataclass
class FacetResult:
    """Result of facet aggregation."""

    field: str
    values: List[FacetValue]
    total_count: int
    other_count: int = 0  # Count of values not in top-N


class FacetAggregator:
    """
    Faceted search aggregator.

    Computes aggregations/counts for categorical fields.

    Example:
        >>> aggregator = FacetAggregator()
        >>> facets = aggregator.aggregate(
        ...     documents=[{"category": "tech"}, {"category": "science"}, ...],
        ...     facet_fields=["category", "author"]
        ... )
        >>> # facets["category"] = FacetResult(values=[FacetValue("tech", 45), ...])
    """

    def aggregate(
        self,
        documents: List[Dict[str, Any]],
        facet_configs: Union[List[str], List[FacetConfig]],
    ) -> Dict[str, FacetResult]:
        """
        Aggregate facet values from documents.

        Args:
            documents: List of documents with metadata
            facet_configs: List of field names or FacetConfig objects

        Returns:
            Dict mapping field names to FacetResult
        """
        # Normalize configs
        configs = []
        for fc in facet_configs:
            if isinstance(fc, str):
                configs.append(FacetConfig(field=fc))
            else:
                configs.append(fc)

        results = {}
        for config in configs:
            results[config.field] = self._aggregate_field(documents, config)

        return results

    def _aggregate_field(
        self,
        documents: List[Dict],
        config: FacetConfig,
    ) -> FacetResult:
        """Aggregate a single field."""
        counter: Counter = Counter()

        for doc in documents:
            value = self._get_nested_value(doc, config.field)
            if value is None:
                continue

            # Handle arrays
            if isinstance(value, list):
                for v in value:
                    counter[v] += 1
            else:
                counter[value] += 1

        # Filter by min_count
        if not config.include_zero:
            counter = Counter({k: v for k, v in counter.items() if v >= config.min_count})

        # Sort
        if config.sort_by == "count":
            sorted_items = counter.most_common()
        else:
            sorted_items = sorted(counter.items(), key=lambda x: str(x[0]))

        # Limit
        top_items = sorted_items[:config.limit]
        other_count = sum(count for _, count in sorted_items[config.limit:])

        facet_values = [FacetValue(value=val, count=count) for val, count in top_items]

        return FacetResult(
            field=config.field,
            values=facet_values,
            total_count=sum(counter.values()),
            other_count=other_count,
        )

    def _get_nested_value(self, doc: Dict, field: str) -> Any:
        """Get value from nested dict using dot notation."""
        keys = field.split(".")
        value = doc

        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return None

            if value is None:
                return None

        return value

    def to_dict(self, facet_results: Dict[str, FacetResult]) -> Dict[str, Any]:
        """Convert facet results to dictionary format."""
        return {
            field: {
                "values": {fv.value: fv.count for fv in result.values},
                "total_count": result.total_count,
                "other_count": result.other_count,
            }
            for field, result in facet_results.items()
        }


# =============================================================================
# ACL / Security Filtering
# =============================================================================


class ACLOperator(str, Enum):
    """ACL matching operators."""

    USER = "user"  # Specific user
    GROUP = "group"  # Group membership
    ROLE = "role"  # Role-based
    EVERYONE = "everyone"  # Public access
    DENY = "deny"  # Explicit deny


@dataclass
class ACLPrincipal:
    """An ACL principal (user, group, or role)."""

    type: ACLOperator
    value: str

    @classmethod
    def parse(cls, acl_string: str) -> "ACLPrincipal":
        """Parse ACL string like 'user:alice' or 'group:engineering'."""
        if ":" in acl_string:
            type_str, value = acl_string.split(":", 1)
            try:
                acl_type = ACLOperator(type_str.lower())
            except ValueError:
                acl_type = ACLOperator.USER
        else:
            acl_type = ACLOperator.USER
            value = acl_string

        return cls(type=acl_type, value=value)

    def matches(self, other: "ACLPrincipal") -> bool:
        """Check if this principal matches another (supports wildcards)."""
        if self.type != other.type:
            return False

        if self.value == "*" or other.value == "*":
            return True

        return self.value == other.value

    def __str__(self) -> str:
        return f"{self.type.value}:{self.value}"


@dataclass
class ACLConfig:
    """ACL configuration for a document or collection."""

    # Who can read
    read_principals: List[ACLPrincipal] = field(default_factory=list)

    # Who cannot read (takes precedence)
    deny_principals: List[ACLPrincipal] = field(default_factory=list)

    # Is public?
    is_public: bool = False

    @classmethod
    def from_list(cls, acl_list: List[str]) -> "ACLConfig":
        """Create from list of ACL strings."""
        read = []
        deny = []
        is_public = False

        for acl_str in acl_list:
            if acl_str.lower() in ("everyone", "public", "*"):
                is_public = True
            elif acl_str.startswith("deny:"):
                deny.append(ACLPrincipal.parse(acl_str[5:]))
            else:
                read.append(ACLPrincipal.parse(acl_str))

        return cls(read_principals=read, deny_principals=deny, is_public=is_public)


class ACLFilter:
    """
    Access Control List filter for security-aware search.

    Filters search results based on user permissions.

    Example:
        >>> acl_filter = ACLFilter()
        >>> user_principals = [
        ...     ACLPrincipal(ACLOperator.USER, "alice"),
        ...     ACLPrincipal(ACLOperator.GROUP, "engineering"),
        ... ]
        >>> filtered = acl_filter.filter(results, user_principals)
    """

    ACL_FIELD = "_acl"  # Default metadata field for ACLs

    def __init__(self, acl_field: str = "_acl"):
        self.acl_field = acl_field

    def filter(
        self,
        documents: List[Dict[str, Any]],
        user_principals: List[Union[str, ACLPrincipal]],
        default_allow: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Filter documents based on user's ACL principals.

        Args:
            documents: List of documents with metadata containing ACLs
            user_principals: User's principals (e.g., ["user:alice", "group:engineering"])
            default_allow: Allow access if no ACL defined

        Returns:
            Documents the user can access
        """
        # Parse user principals
        principals = []
        for p in user_principals:
            if isinstance(p, str):
                principals.append(ACLPrincipal.parse(p))
            else:
                principals.append(p)

        filtered = []
        for doc in documents:
            if self._can_access(doc, principals, default_allow):
                filtered.append(doc)

        return filtered

    def _can_access(
        self,
        doc: Dict[str, Any],
        user_principals: List[ACLPrincipal],
        default_allow: bool,
    ) -> bool:
        """Check if user can access document."""
        metadata = doc.get("metadata", doc)
        acl_data = metadata.get(self.acl_field)

        if acl_data is None:
            return default_allow

        # Parse ACL config
        if isinstance(acl_data, list):
            acl_config = ACLConfig.from_list(acl_data)
        elif isinstance(acl_data, dict):
            acl_config = ACLConfig(
                read_principals=[ACLPrincipal.parse(p) for p in acl_data.get("read", [])],
                deny_principals=[ACLPrincipal.parse(p) for p in acl_data.get("deny", [])],
                is_public=acl_data.get("public", False),
            )
        else:
            return default_allow

        # Public access
        if acl_config.is_public:
            return True

        # Check deny list first
        for deny in acl_config.deny_principals:
            for user_p in user_principals:
                if deny.matches(user_p):
                    return False

        # Check allow list
        for allow in acl_config.read_principals:
            for user_p in user_principals:
                if allow.matches(user_p):
                    return True

        return False

    def add_acl_to_metadata(
        self,
        metadata: Dict[str, Any],
        principals: List[str],
    ) -> Dict[str, Any]:
        """Add ACL to document metadata."""
        metadata = metadata.copy()
        metadata[self.acl_field] = principals
        return metadata

    def create_acl_filter_condition(
        self,
        user_principals: List[str],
    ) -> Dict[str, Any]:
        """
        Create a filter condition for ACL matching.

        Returns a filter dict that can be used with the standard filter system.
        """
        return {
            "$or": [
                # Public documents
                {self.acl_field: {"$contains": "everyone"}},
                {self.acl_field: {"$contains": "public"}},
                # User's specific principals
                *[{self.acl_field: {"$contains": p}} for p in user_principals],
            ]
        }


# =============================================================================
# Text Analyzers
# =============================================================================


class AnalyzerType(str, Enum):
    """Built-in analyzer types."""

    STANDARD = "standard"  # Lowercase + basic tokenization
    SIMPLE = "simple"  # Lowercase + letter tokenization
    WHITESPACE = "whitespace"  # Split on whitespace only
    KEYWORD = "keyword"  # No tokenization
    ENGLISH = "english"  # English with stemming + stopwords
    CUSTOM = "custom"  # Custom configuration


class TextAnalyzer:
    """
    Text analyzer for search indexing.

    Provides text processing pipelines:
    - Tokenization
    - Lowercasing
    - Stopword removal
    - Stemming
    - Synonym expansion

    Example:
        >>> analyzer = TextAnalyzer.english()
        >>> tokens = analyzer.analyze("The quick brown foxes are jumping")
        >>> # ["quick", "brown", "fox", "jump"]
    """

    # Common English stopwords
    ENGLISH_STOPWORDS = frozenset([
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
        "if", "in", "into", "is", "it", "no", "not", "of", "on", "or",
        "such", "that", "the", "their", "then", "there", "these", "they",
        "this", "to", "was", "will", "with", "the", "and", "but", "or",
        "because", "as", "what", "which", "who", "when", "where", "how",
    ])

    def __init__(
        self,
        lowercase: bool = True,
        remove_stopwords: bool = False,
        stopwords: Optional[Set[str]] = None,
        stemmer: Optional[str] = None,  # "porter", "snowball", or None
        synonyms: Optional[Dict[str, List[str]]] = None,
        min_token_length: int = 1,
        max_token_length: int = 100,
        token_pattern: str = r"\b[a-zA-Z0-9]+\b",
    ):
        self.lowercase = lowercase
        self.remove_stopwords = remove_stopwords
        self.stopwords = stopwords or self.ENGLISH_STOPWORDS
        self.stemmer_type = stemmer
        self.synonyms = synonyms or {}
        self.min_token_length = min_token_length
        self.max_token_length = max_token_length
        self.token_pattern = re.compile(token_pattern)

        # Initialize stemmer if requested
        self._stemmer = None
        if stemmer:
            self._init_stemmer(stemmer)

    def _init_stemmer(self, stemmer_type: str) -> None:
        """Initialize the stemmer."""
        try:
            if stemmer_type == "porter":
                from nltk.stem import PorterStemmer
                self._stemmer = PorterStemmer()
            elif stemmer_type == "snowball":
                from nltk.stem import SnowballStemmer
                self._stemmer = SnowballStemmer("english")
            elif stemmer_type == "lancaster":
                from nltk.stem import LancasterStemmer
                self._stemmer = LancasterStemmer()
        except ImportError:
            # Fall back to simple suffix stripping
            self._stemmer = SimpleStemmer()

    def analyze(self, text: str) -> List[str]:
        """
        Analyze text and return tokens.

        Args:
            text: Input text

        Returns:
            List of processed tokens
        """
        if not text:
            return []

        # Lowercase
        if self.lowercase:
            text = text.lower()

        # Tokenize
        tokens = self.token_pattern.findall(text)

        # Filter by length
        tokens = [
            t for t in tokens
            if self.min_token_length <= len(t) <= self.max_token_length
        ]

        # Remove stopwords
        if self.remove_stopwords:
            tokens = [t for t in tokens if t.lower() not in self.stopwords]

        # Stem
        if self._stemmer:
            tokens = [self._stemmer.stem(t) for t in tokens]

        # Expand synonyms
        if self.synonyms:
            expanded = []
            for t in tokens:
                expanded.append(t)
                if t in self.synonyms:
                    expanded.extend(self.synonyms[t])
            tokens = expanded

        return tokens

    def analyze_query(self, query: str) -> List[str]:
        """
        Analyze a query string.

        Same as analyze but may handle query-specific logic.
        """
        return self.analyze(query)

    @classmethod
    def standard(cls) -> "TextAnalyzer":
        """Create standard analyzer (lowercase + tokenization)."""
        return cls(lowercase=True, remove_stopwords=False)

    @classmethod
    def simple(cls) -> "TextAnalyzer":
        """Create simple analyzer (lowercase + letter tokenization)."""
        return cls(lowercase=True, token_pattern=r"[a-zA-Z]+")

    @classmethod
    def english(cls) -> "TextAnalyzer":
        """Create English analyzer with stemming and stopwords."""
        return cls(
            lowercase=True,
            remove_stopwords=True,
            stemmer="porter",
            min_token_length=2,
        )

    @classmethod
    def keyword(cls) -> "TextAnalyzer":
        """Create keyword analyzer (no tokenization)."""
        return KeywordAnalyzer()

    @classmethod
    def with_synonyms(cls, synonyms: Dict[str, List[str]]) -> "TextAnalyzer":
        """Create analyzer with synonym expansion."""
        return cls(
            lowercase=True,
            remove_stopwords=True,
            synonyms=synonyms,
        )


class KeywordAnalyzer(TextAnalyzer):
    """Analyzer that treats entire input as single token."""

    def __init__(self):
        super().__init__(lowercase=True)

    def analyze(self, text: str) -> List[str]:
        if not text:
            return []
        return [text.lower().strip()]


class SimpleStemmer:
    """Simple suffix-stripping stemmer (no NLTK dependency)."""

    SUFFIXES = [
        "ization", "ational", "fulness", "ousness", "iveness",
        "ation", "eness", "ment", "ness", "ible", "able", "ity",
        "ing", "ies", "ive", "ion", "ous", "ful", "ism", "ist",
        "ly", "ed", "er", "es", "al", "s",
    ]

    def stem(self, word: str) -> str:
        """Stem a word by removing common suffixes."""
        word = word.lower()

        for suffix in self.SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                return word[:-len(suffix)]

        return word


class AnalyzerChain:
    """
    Chain multiple analyzers together.

    Useful for custom processing pipelines.
    """

    def __init__(self, analyzers: List[TextAnalyzer]):
        self.analyzers = analyzers

    def analyze(self, text: str) -> List[str]:
        """Run text through all analyzers in sequence."""
        tokens = [text]

        for analyzer in self.analyzers:
            new_tokens = []
            for token in tokens:
                new_tokens.extend(analyzer.analyze(token))
            tokens = new_tokens

        return tokens


# =============================================================================
# Combined Search Result with All Features
# =============================================================================


@dataclass
class EnhancedSearchResults:
    """
    Search results with enterprise features.

    Includes:
    - Re-ranked results
    - Facet aggregations
    - ACL filtering info
    """

    results: List[Dict[str, Any]]
    facets: Dict[str, FacetResult]
    total_count: int
    filtered_count: int  # After ACL filtering
    query_time_ms: float
    rerank_time_ms: float = 0.0
    facet_time_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "results": self.results,
            "facets": {
                field: {
                    "values": {fv.value: fv.count for fv in result.values},
                    "total": result.total_count,
                    "other": result.other_count,
                }
                for field, result in self.facets.items()
            },
            "total_count": self.total_count,
            "filtered_count": self.filtered_count,
            "query_time_ms": self.query_time_ms,
            "rerank_time_ms": self.rerank_time_ms,
            "facet_time_ms": self.facet_time_ms,
        }
