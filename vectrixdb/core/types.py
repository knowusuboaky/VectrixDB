"""
VectrixDB Types - Data structures and enums.

Advanced filtering, indexing options, and search configurations
that match and exceed Qdrant's capabilities.

Author: Daddy Nyame Owusu - Boakye
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Union, List, Dict
import numpy as np
import re


class DistanceMetric(str, Enum):
    """Distance metrics for vector similarity."""

    COSINE = "cosine"
    EUCLIDEAN = "euclidean"  # L2
    DOT = "dot"  # Inner product
    MANHATTAN = "manhattan"  # L1

    @property
    def usearch_metric(self) -> str:
        """Convert to usearch metric name."""
        mapping = {
            "cosine": "cos",
            "euclidean": "l2sq",
            "dot": "ip",
            "manhattan": "l1",
        }
        return mapping[self.value]


class IndexType(str, Enum):
    """Vector index types for different use cases."""

    HNSW = "hnsw"  # Best for high recall, moderate memory
    IVF = "ivf"  # Best for large datasets, lower memory
    FLAT = "flat"  # Exact search, small datasets
    PQ = "pq"  # Product quantization, very large datasets
    HNSW_PQ = "hnsw_pq"  # Hybrid for scale + speed


class SearchMode(str, Enum):
    """Search modes for different query types."""

    VECTOR = "vector"  # Pure vector similarity
    KEYWORD = "keyword"  # Full-text keyword search
    HYBRID = "hybrid"  # Combined vector + keyword
    SPARSE = "sparse"  # Sparse vector search (BM25-like)


class QuantizationType(str, Enum):
    """Quantization types for memory optimization."""

    NONE = "none"
    SCALAR = "scalar"  # 8-bit quantization
    BINARY = "binary"  # 1-bit per dimension
    PRODUCT = "product"  # Product quantization


# =============================================================================
# Sparse Vector Support (Qdrant-style)
# =============================================================================


@dataclass
class SparseVector:
    """
    Sparse vector representation for efficient storage and retrieval.

    Used for:
    - BM25/TF-IDF text representations
    - SPLADE neural sparse embeddings
    - Learned sparse representations

    Example:
        # From term frequencies
        sparse = SparseVector.from_dict({0: 0.5, 42: 1.2, 100: 0.8})

        # From text using TF-IDF
        sparse = SparseVector.from_text("machine learning is great", tokenizer, idf_weights)

        # Dot product similarity
        score = sparse1.dot(sparse2)
    """

    indices: list[int]  # Non-zero dimension indices
    values: list[float]  # Corresponding values

    def __post_init__(self):
        if len(self.indices) != len(self.values):
            raise ValueError(f"indices ({len(self.indices)}) and values ({len(self.values)}) must have same length")
        # Sort by index for efficient operations
        if self.indices and not all(self.indices[i] <= self.indices[i+1] for i in range(len(self.indices)-1)):
            sorted_pairs = sorted(zip(self.indices, self.values))
            self.indices = [p[0] for p in sorted_pairs]
            self.values = [p[1] for p in sorted_pairs]

    @classmethod
    def from_dict(cls, sparse_dict: Dict[int, float]) -> "SparseVector":
        """Create from {index: value} dictionary."""
        if not sparse_dict:
            return cls(indices=[], values=[])
        indices = list(sparse_dict.keys())
        values = list(sparse_dict.values())
        return cls(indices=indices, values=values)

    @classmethod
    def from_dense(cls, dense: Union[list[float], np.ndarray], threshold: float = 1e-6) -> "SparseVector":
        """Convert dense vector to sparse, keeping only non-zero values."""
        if isinstance(dense, list):
            dense = np.array(dense)
        nonzero_mask = np.abs(dense) > threshold
        indices = np.where(nonzero_mask)[0].tolist()
        values = dense[nonzero_mask].tolist()
        return cls(indices=indices, values=values)

    @classmethod
    def from_text(
        cls,
        text: str,
        vocab: Dict[str, int],
        idf_weights: Optional[Dict[str, float]] = None,
        normalize: bool = True,
    ) -> "SparseVector":
        """
        Create sparse vector from text using TF-IDF weighting.

        Args:
            text: Input text
            vocab: Vocabulary mapping word -> index
            idf_weights: Optional IDF weights per word
            normalize: Whether to L2 normalize the vector
        """
        # Tokenize
        import re
        tokens = re.findall(r'\b[a-z0-9]+\b', text.lower())

        # Count term frequencies
        from collections import Counter
        tf = Counter(tokens)

        # Build sparse vector
        indices = []
        values = []

        for token, count in tf.items():
            if token in vocab:
                idx = vocab[token]
                # TF-IDF: tf * idf
                tf_val = 1 + np.log(count) if count > 0 else 0  # Log-normalized TF
                idf_val = idf_weights.get(token, 1.0) if idf_weights else 1.0
                value = tf_val * idf_val

                indices.append(idx)
                values.append(value)

        sparse = cls(indices=indices, values=values)

        if normalize and sparse.values:
            sparse = sparse.normalize()

        return sparse

    def to_dict(self) -> Dict[int, float]:
        """Convert to {index: value} dictionary."""
        return dict(zip(self.indices, self.values))

    def to_dense(self, dimension: int) -> np.ndarray:
        """Convert to dense vector of given dimension."""
        dense = np.zeros(dimension)
        for idx, val in zip(self.indices, self.values):
            if idx < dimension:
                dense[idx] = val
        return dense

    def dot(self, other: "SparseVector") -> float:
        """
        Compute dot product with another sparse vector.

        Efficient O(n + m) implementation using sorted indices.
        """
        if not self.indices or not other.indices:
            return 0.0

        result = 0.0
        i, j = 0, 0

        while i < len(self.indices) and j < len(other.indices):
            if self.indices[i] == other.indices[j]:
                result += self.values[i] * other.values[j]
                i += 1
                j += 1
            elif self.indices[i] < other.indices[j]:
                i += 1
            else:
                j += 1

        return result

    def norm(self) -> float:
        """Compute L2 norm."""
        return np.sqrt(sum(v * v for v in self.values))

    def normalize(self) -> "SparseVector":
        """Return L2 normalized version."""
        n = self.norm()
        if n == 0:
            return SparseVector(indices=self.indices.copy(), values=self.values.copy())
        return SparseVector(
            indices=self.indices.copy(),
            values=[v / n for v in self.values]
        )

    def cosine_similarity(self, other: "SparseVector") -> float:
        """Compute cosine similarity with another sparse vector."""
        dot_product = self.dot(other)
        norm_product = self.norm() * other.norm()
        if norm_product == 0:
            return 0.0
        return dot_product / norm_product

    def top_k(self, k: int) -> "SparseVector":
        """Return sparse vector with only top-k values by magnitude."""
        if len(self.values) <= k:
            return SparseVector(indices=self.indices.copy(), values=self.values.copy())

        # Get indices of top-k values
        top_indices = np.argsort(np.abs(self.values))[-k:]

        new_indices = [self.indices[i] for i in sorted(top_indices)]
        new_values = [self.values[i] for i in sorted(top_indices)]

        return SparseVector(indices=new_indices, values=new_values)

    def __len__(self) -> int:
        """Number of non-zero elements."""
        return len(self.indices)

    def __repr__(self) -> str:
        if len(self.indices) <= 5:
            pairs = ", ".join(f"{i}:{v:.3f}" for i, v in zip(self.indices, self.values))
        else:
            first = ", ".join(f"{i}:{v:.3f}" for i, v in zip(self.indices[:3], self.values[:3]))
            pairs = f"{first}, ... ({len(self.indices)} total)"
        return f"SparseVector({{{pairs}}})"


@dataclass
class IndexConfig:
    """Configuration for vector index."""

    index_type: IndexType = IndexType.HNSW
    quantization: QuantizationType = QuantizationType.NONE

    # HNSW parameters
    hnsw_m: int = 16  # Number of connections per layer
    hnsw_ef_construction: int = 200  # Build-time search width
    hnsw_ef_search: int = 100  # Query-time search width

    # IVF parameters
    ivf_nlist: int = 100  # Number of clusters
    ivf_nprobe: int = 10  # Clusters to search

    # PQ parameters
    pq_segments: int = 8  # Number of subvectors
    pq_bits: int = 8  # Bits per subvector

    def to_dict(self) -> dict:
        return {
            "index_type": self.index_type.value,
            "quantization": self.quantization.value,
            "hnsw_m": self.hnsw_m,
            "hnsw_ef_construction": self.hnsw_ef_construction,
            "hnsw_ef_search": self.hnsw_ef_search,
            "ivf_nlist": self.ivf_nlist,
            "ivf_nprobe": self.ivf_nprobe,
            "pq_segments": self.pq_segments,
            "pq_bits": self.pq_bits,
        }


@dataclass
class Point:
    """
    A vector point with ID, dense vector, optional sparse vector, and metadata.

    Supports both dense and sparse vectors for hybrid dense+sparse search.

    Example:
        # Dense only
        point = Point(id="doc1", vector=[0.1, 0.2, 0.3])

        # Dense + sparse for hybrid search
        point = Point(
            id="doc1",
            vector=[0.1, 0.2, 0.3],  # Dense embedding
            sparse_vector=SparseVector.from_dict({0: 0.5, 42: 1.2}),  # Sparse (e.g., SPLADE)
            text="Original document text"
        )
    """

    id: str
    vector: Union[list[float], np.ndarray]
    metadata: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)  # Alias for metadata (Qdrant compat)
    sparse_vector: Optional[Union[SparseVector, dict[int, float]]] = None  # Sparse representation
    text: Optional[str] = None  # Original text for hybrid search
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        # Merge payload into metadata for compatibility
        if self.payload and not self.metadata:
            self.metadata = self.payload
        elif self.payload:
            self.metadata.update(self.payload)

        # Convert dict sparse_vector to SparseVector object
        if isinstance(self.sparse_vector, dict):
            self.sparse_vector = SparseVector.from_dict(self.sparse_vector)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result = {
            "id": self.id,
            "vector": self.vector if isinstance(self.vector, list) else self.vector.tolist(),
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if self.sparse_vector:
            if isinstance(self.sparse_vector, SparseVector):
                result["sparse_vector"] = {
                    "indices": self.sparse_vector.indices,
                    "values": self.sparse_vector.values,
                }
            else:
                result["sparse_vector"] = self.sparse_vector
        if self.text:
            result["text"] = self.text
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Point":
        """Create from dictionary."""
        sparse = data.get("sparse_vector")
        if sparse and isinstance(sparse, dict):
            if "indices" in sparse and "values" in sparse:
                sparse = SparseVector(indices=sparse["indices"], values=sparse["values"])
            else:
                sparse = SparseVector.from_dict(sparse)

        return cls(
            id=data["id"],
            vector=data["vector"],
            metadata=data.get("metadata", data.get("payload", {})),
            sparse_vector=sparse,
            text=data.get("text"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.utcnow(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None,
        )

    def has_sparse(self) -> bool:
        """Check if point has a sparse vector."""
        return self.sparse_vector is not None and len(self.sparse_vector) > 0


@dataclass
class SearchResult:
    """
    A single search result with scores from different search modes.

    Supports:
    - Dense vector score
    - Sparse vector score
    - Text/keyword score
    - Combined/fused score
    """

    id: str
    score: float  # Combined/final score
    vector: Optional[list[float]] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Individual scores for transparency
    dense_score: Optional[float] = None  # Dense vector similarity
    sparse_score: Optional[float] = None  # Sparse vector similarity
    text_score: Optional[float] = None  # BM25/keyword score

    # Text search extras
    highlights: Optional[list[str]] = None  # Text snippets

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result = {
            "id": self.id,
            "score": self.score,
            "metadata": self.metadata,
        }
        if self.vector is not None:
            result["vector"] = self.vector
        if self.dense_score is not None:
            result["dense_score"] = self.dense_score
        if self.sparse_score is not None:
            result["sparse_score"] = self.sparse_score
        if self.text_score is not None:
            result["text_score"] = self.text_score
        if self.highlights:
            result["highlights"] = self.highlights
        return result


@dataclass
class SearchResults:
    """Collection of search results with metadata."""

    results: list[SearchResult]
    query_time_ms: float
    total_searched: int
    search_mode: SearchMode = SearchMode.VECTOR

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "results": [r.to_dict() for r in self.results],
            "query_time_ms": self.query_time_ms,
            "total_searched": self.total_searched,
            "search_mode": self.search_mode.value,
        }


@dataclass
class CollectionInfo:
    """Information about a collection."""

    name: str
    dimension: int
    metric: DistanceMetric
    count: int
    size_bytes: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    description: Optional[str] = None
    index_config: Optional[IndexConfig] = None
    has_text_index: bool = False
    indexed_fields: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    """
    Capability tags for the collection. Common tags:
    - Dense: Uses dense vector search
    - Sparse: Uses sparse vector search (BM25/SPLADE)
    - Hybrid: Combines dense + sparse/keyword
    - Ultimate: Uses all 4 models (dense + sparse + ColBERT + reranker)
    - Graph: Has GraphRAG enabled
    """

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "dimension": self.dimension,
            "metric": self.metric.value,
            "count": self.count,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "description": self.description,
            "index_config": self.index_config.to_dict() if self.index_config else None,
            "has_text_index": self.has_text_index,
            "indexed_fields": self.indexed_fields,
            "tags": self.tags,
        }


@dataclass
class DatabaseInfo:
    """Information about the database."""

    path: str
    version: str
    collections_count: int
    total_vectors: int
    total_size_bytes: int
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "path": self.path,
            "version": self.version,
            "collections_count": self.collections_count,
            "total_vectors": self.total_vectors,
            "total_size_bytes": self.total_size_bytes,
            "created_at": self.created_at.isoformat(),
        }


# =============================================================================
# Advanced Filtering System (Better than Qdrant)
# =============================================================================

class FilterOperator(str, Enum):
    """All supported filter operators."""

    # Comparison
    EQ = "eq"  # Equal
    NE = "ne"  # Not equal
    GT = "gt"  # Greater than
    GTE = "gte"  # Greater than or equal
    LT = "lt"  # Less than
    LTE = "lte"  # Less than or equal

    # Array operations
    IN = "in"  # Value in array
    NIN = "nin"  # Value not in array
    ALL = "all"  # Array contains all values
    ANY = "any"  # Array contains any value

    # String operations
    CONTAINS = "contains"  # String contains
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    REGEX = "regex"  # Regular expression match
    ICONTAINS = "icontains"  # Case-insensitive contains

    # Existence
    EXISTS = "exists"  # Field exists
    IS_NULL = "is_null"  # Field is null
    IS_EMPTY = "is_empty"  # Field is empty string/array

    # Range
    BETWEEN = "between"  # Value between two bounds

    # Geo (for location-based filtering)
    GEO_RADIUS = "geo_radius"  # Within radius of point
    GEO_BOX = "geo_box"  # Within bounding box

    # Date/Time
    DATE_RANGE = "date_range"  # Date within range


@dataclass
class FilterCondition:
    """A filter condition for metadata queries."""

    field: str
    operator: str  # eq, ne, gt, gte, lt, lte, in, nin, contains, etc.
    value: Any

    def matches(self, metadata: dict[str, Any]) -> bool:
        """Check if metadata matches this condition."""
        # Handle nested field access with dot notation
        field_value = self._get_nested_value(metadata, self.field)

        # Handle existence checks
        if self.operator == "exists":
            return (field_value is not None) == self.value

        if self.operator == "is_null":
            return (field_value is None) == self.value

        if self.operator == "is_empty":
            if field_value is None:
                return self.value
            if isinstance(field_value, (str, list, dict)):
                return (len(field_value) == 0) == self.value
            return False

        # For other operators, field must exist
        if field_value is None:
            return False

        # Comparison operators
        if self.operator == "eq":
            return field_value == self.value
        elif self.operator == "ne":
            return field_value != self.value
        elif self.operator == "gt":
            return field_value > self.value
        elif self.operator == "gte":
            return field_value >= self.value
        elif self.operator == "lt":
            return field_value < self.value
        elif self.operator == "lte":
            return field_value <= self.value

        # Array operators
        elif self.operator == "in":
            return field_value in self.value
        elif self.operator == "nin":
            return field_value not in self.value
        elif self.operator == "all":
            if not isinstance(field_value, list):
                return False
            return all(v in field_value for v in self.value)
        elif self.operator == "any":
            if not isinstance(field_value, list):
                return field_value in self.value
            return any(v in self.value for v in field_value)

        # String operators
        elif self.operator == "contains":
            return str(self.value) in str(field_value)
        elif self.operator == "icontains":
            return str(self.value).lower() in str(field_value).lower()
        elif self.operator == "starts_with":
            return str(field_value).startswith(str(self.value))
        elif self.operator == "ends_with":
            return str(field_value).endswith(str(self.value))
        elif self.operator == "regex":
            try:
                return bool(re.search(self.value, str(field_value)))
            except re.error:
                return False

        # Range operator
        elif self.operator == "between":
            if isinstance(self.value, (list, tuple)) and len(self.value) == 2:
                return self.value[0] <= field_value <= self.value[1]
            return False

        # Date range
        elif self.operator == "date_range":
            try:
                if isinstance(field_value, str):
                    field_date = datetime.fromisoformat(field_value.replace("Z", "+00:00"))
                elif isinstance(field_value, datetime):
                    field_date = field_value
                else:
                    return False

                start = datetime.fromisoformat(self.value[0].replace("Z", "+00:00"))
                end = datetime.fromisoformat(self.value[1].replace("Z", "+00:00"))
                return start <= field_date <= end
            except (ValueError, TypeError):
                return False

        # Geo radius
        elif self.operator == "geo_radius":
            return self._geo_radius_match(field_value, self.value)

        # Geo bounding box
        elif self.operator == "geo_box":
            return self._geo_box_match(field_value, self.value)

        else:
            raise ValueError(f"Unknown operator: {self.operator}")

    def _get_nested_value(self, data: dict, field_path: str) -> Any:
        """Get value from nested dict using dot notation."""
        keys = field_path.split(".")
        value = data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            elif isinstance(value, list) and key.isdigit():
                idx = int(key)
                if 0 <= idx < len(value):
                    value = value[idx]
                else:
                    return None
            else:
                return None
        return value

    def _geo_radius_match(self, point: Any, params: dict) -> bool:
        """Check if point is within radius of center."""
        try:
            if not isinstance(point, dict) or "lat" not in point or "lon" not in point:
                return False

            lat1, lon1 = point["lat"], point["lon"]
            lat2, lon2 = params["center"]["lat"], params["center"]["lon"]
            radius_km = params["radius_km"]

            # Haversine formula
            from math import radians, sin, cos, sqrt, atan2

            R = 6371  # Earth's radius in km

            dlat = radians(lat2 - lat1)
            dlon = radians(lon2 - lon1)
            a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
            c = 2 * atan2(sqrt(a), sqrt(1 - a))
            distance = R * c

            return distance <= radius_km
        except (KeyError, TypeError):
            return False

    def _geo_box_match(self, point: Any, box: dict) -> bool:
        """Check if point is within bounding box."""
        try:
            if not isinstance(point, dict) or "lat" not in point or "lon" not in point:
                return False

            lat, lon = point["lat"], point["lon"]
            return (
                box["min_lat"] <= lat <= box["max_lat"] and
                box["min_lon"] <= lon <= box["max_lon"]
            )
        except (KeyError, TypeError):
            return False


@dataclass
class Filter:
    """
    A composite filter with AND/OR/NOT logic.

    Supports nested filters for complex queries like Qdrant's filtering.
    """

    conditions: list[FilterCondition] = field(default_factory=list)
    logic: str = "and"  # "and", "or"
    nested: list["Filter"] = field(default_factory=list)  # Nested sub-filters
    negate: bool = False  # NOT wrapper

    def matches(self, metadata: dict[str, Any]) -> bool:
        """Check if metadata matches the filter."""
        # Evaluate conditions
        condition_results = [c.matches(metadata) for c in self.conditions]

        # Evaluate nested filters
        nested_results = [n.matches(metadata) for n in self.nested]

        # Combine all results
        all_results = condition_results + nested_results

        if not all_results:
            result = True
        elif self.logic == "and":
            result = all(all_results)
        else:  # or
            result = any(all_results)

        # Apply negation
        return not result if self.negate else result

    @classmethod
    def from_dict(cls, filter_dict: dict[str, Any]) -> "Filter":
        """
        Create filter from dictionary.

        Supports multiple formats:

        Simple format:
            {"category": "tech", "price": {"$lt": 100}}

        Qdrant-style format:
            {
                "must": [{"key": "category", "match": {"value": "tech"}}],
                "should": [{"key": "price", "range": {"lt": 100}}],
                "must_not": [{"key": "deleted", "match": {"value": true}}]
            }

        Extended format:
            {
                "$and": [
                    {"field": "category", "op": "eq", "value": "tech"},
                    {"$or": [
                        {"field": "price", "op": "lt", "value": 100},
                        {"field": "premium", "op": "eq", "value": true}
                    ]}
                ]
            }
        """
        # Handle Qdrant-style filter
        if any(k in filter_dict for k in ["must", "should", "must_not"]):
            return cls._from_qdrant_format(filter_dict)

        # Handle extended format with $and/$or
        if "$and" in filter_dict:
            nested = [cls.from_dict(f) for f in filter_dict["$and"]]
            return cls(nested=nested, logic="and")

        if "$or" in filter_dict:
            nested = [cls.from_dict(f) for f in filter_dict["$or"]]
            return cls(nested=nested, logic="or")

        if "$not" in filter_dict:
            inner = cls.from_dict(filter_dict["$not"])
            inner.negate = True
            return inner

        # Handle extended single condition
        if "field" in filter_dict and "op" in filter_dict:
            return cls(conditions=[FilterCondition(
                field=filter_dict["field"],
                operator=filter_dict["op"],
                value=filter_dict.get("value")
            )])

        # Simple format
        conditions = []
        for field, value in filter_dict.items():
            if isinstance(value, dict):
                # Complex condition like {"$lt": 100}
                for op, v in value.items():
                    operator = op.lstrip("$")
                    conditions.append(FilterCondition(field=field, operator=operator, value=v))
            else:
                # Simple equality
                conditions.append(FilterCondition(field=field, operator="eq", value=value))

        return cls(conditions=conditions)

    @classmethod
    def _from_qdrant_format(cls, filter_dict: dict) -> "Filter":
        """Parse Qdrant-style filter format."""
        nested_filters = []

        # Handle "must" (AND conditions)
        if "must" in filter_dict:
            must_conditions = []
            for cond in filter_dict["must"]:
                must_conditions.append(cls._parse_qdrant_condition(cond))
            if must_conditions:
                nested_filters.append(cls(conditions=must_conditions, logic="and"))

        # Handle "should" (OR conditions)
        if "should" in filter_dict:
            should_conditions = []
            for cond in filter_dict["should"]:
                should_conditions.append(cls._parse_qdrant_condition(cond))
            if should_conditions:
                nested_filters.append(cls(conditions=should_conditions, logic="or"))

        # Handle "must_not" (negated AND)
        if "must_not" in filter_dict:
            must_not_conditions = []
            for cond in filter_dict["must_not"]:
                must_not_conditions.append(cls._parse_qdrant_condition(cond))
            if must_not_conditions:
                nested_filters.append(cls(conditions=must_not_conditions, logic="and", negate=True))

        return cls(nested=nested_filters, logic="and")

    @classmethod
    def _parse_qdrant_condition(cls, cond: dict) -> FilterCondition:
        """Parse a single Qdrant-style condition."""
        key = cond.get("key", "")

        if "match" in cond:
            match = cond["match"]
            if "value" in match:
                return FilterCondition(field=key, operator="eq", value=match["value"])
            elif "text" in match:
                return FilterCondition(field=key, operator="contains", value=match["text"])
            elif "any" in match:
                return FilterCondition(field=key, operator="any", value=match["any"])

        if "range" in cond:
            range_cond = cond["range"]
            for op in ["gt", "gte", "lt", "lte"]:
                if op in range_cond:
                    return FilterCondition(field=key, operator=op, value=range_cond[op])

        if "geo_radius" in cond:
            return FilterCondition(field=key, operator="geo_radius", value=cond["geo_radius"])

        if "geo_bounding_box" in cond:
            box = cond["geo_bounding_box"]
            return FilterCondition(field=key, operator="geo_box", value={
                "min_lat": box["bottom_right"]["lat"],
                "max_lat": box["top_left"]["lat"],
                "min_lon": box["top_left"]["lon"],
                "max_lon": box["bottom_right"]["lon"],
            })

        if "is_null" in cond:
            return FilterCondition(field=key, operator="is_null", value=cond["is_null"]["value"])

        if "is_empty" in cond:
            return FilterCondition(field=key, operator="is_empty", value=cond["is_empty"]["value"])

        raise ValueError(f"Unknown Qdrant condition format: {cond}")


@dataclass
class SearchQuery:
    """
    Complete search query configuration.

    Supports vector search, keyword search, and hybrid search.
    """

    # Vector search
    vector: Optional[Union[list[float], np.ndarray]] = None
    sparse_vector: Optional[dict[int, float]] = None

    # Keyword search
    query_text: Optional[str] = None
    search_fields: list[str] = field(default_factory=list)  # Fields to search in

    # Search configuration
    mode: SearchMode = SearchMode.VECTOR
    limit: int = 10
    offset: int = 0
    filter: Optional[Filter] = None

    # Hybrid search weights
    vector_weight: float = 0.7  # Weight for vector similarity
    text_weight: float = 0.3  # Weight for text/keyword match

    # Result options
    include_vectors: bool = False
    include_metadata: bool = True
    score_threshold: Optional[float] = None  # Minimum score cutoff

    # Advanced options
    ef_search: Optional[int] = None  # Override HNSW ef for this query
    rescore: bool = False  # Re-rank with exact distances
    diversity: float = 0.0  # MMR diversity factor (0-1)

    def to_dict(self) -> dict:
        return {
            "vector": self.vector.tolist() if isinstance(self.vector, np.ndarray) else self.vector,
            "sparse_vector": self.sparse_vector,
            "query_text": self.query_text,
            "search_fields": self.search_fields,
            "mode": self.mode.value,
            "limit": self.limit,
            "offset": self.offset,
            "vector_weight": self.vector_weight,
            "text_weight": self.text_weight,
            "include_vectors": self.include_vectors,
            "score_threshold": self.score_threshold,
        }


@dataclass
class BatchResult:
    """Result of a batch operation."""

    success_count: int
    error_count: int
    errors: list[dict] = field(default_factory=list)
    operation_time_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "success_count": self.success_count,
            "error_count": self.error_count,
            "errors": self.errors,
            "operation_time_ms": self.operation_time_ms,
        }
