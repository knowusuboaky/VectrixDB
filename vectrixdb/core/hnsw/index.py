"""
Native HNSW Index Implementation

Hierarchical Navigable Small World graph for approximate nearest neighbor search.
Pure Python/NumPy implementation without external dependencies.
"""

import heapq
import json
import math
import random
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
import numpy as np

from .distance import DistanceFunctions


@dataclass
class HNSWConfig:
    """Configuration for HNSW index."""
    dimension: int
    metric: str = "cosine"
    m: int = 16  # Number of connections per node
    m_max: int = 16  # Max connections at layers > 0
    m_max_0: int = 32  # Max connections at layer 0
    ef_construction: int = 200  # Search width during construction
    ef_search: int = 100  # Search width during query
    ml: float = 0.0  # Level multiplier (0 = auto-compute)
    seed: int = 42

    def __post_init__(self):
        if self.ml == 0.0:
            # Default: 1/ln(M)
            self.ml = 1.0 / math.log(self.m)
        if self.m_max == 0:
            self.m_max = self.m
        if self.m_max_0 == 0:
            self.m_max_0 = self.m * 2


@dataclass
class SearchResult:
    """Result from HNSW search."""
    ids: np.ndarray  # Internal indices
    distances: np.ndarray  # Distances to query


class NativeHNSWIndex:
    """
    Native HNSW implementation with full control over persistence.

    This is a pure Python/NumPy implementation that provides:
    - No external dependencies
    - Full persistence support
    - Incremental updates (add/remove)
    - Thread-safe operations

    Example:
        >>> index = NativeHNSWIndex(dimension=384, metric="cosine")
        >>> index.add(vectors)
        >>> ids, distances = index.search(query, k=10)
        >>> index.save(path)
    """

    def __init__(
        self,
        dimension: int,
        metric: str = "cosine",
        m: int = 16,
        ef_construction: int = 200,
        ef_search: int = 100,
        max_elements: int = 10000,
        path: Optional[Path] = None,
        seed: int = 42,
    ):
        """
        Initialize HNSW index.

        Args:
            dimension: Vector dimension
            metric: Distance metric (cosine, euclidean, dot, manhattan)
            m: Number of bi-directional connections per node (default: 16)
            ef_construction: Search width during construction (default: 200)
            ef_search: Search width during query (default: 100)
            max_elements: Initial capacity (grows automatically)
            path: Optional path for persistence
            seed: Random seed for reproducibility
        """
        self.config = HNSWConfig(
            dimension=dimension,
            metric=metric,
            m=m,
            m_max=m,
            m_max_0=m * 2,
            ef_construction=ef_construction,
            ef_search=ef_search,
            seed=seed,
        )

        self.path = Path(path) if path else None
        self._rng = random.Random(seed)
        self._lock = threading.RLock()

        # Distance function
        self._distance_func = DistanceFunctions.get_distance_func(metric)
        self._batch_distance_func = DistanceFunctions.get_batch_distance_func(metric)

        # Data storage
        self._vectors: np.ndarray = np.zeros((max_elements, dimension), dtype=np.float32)
        self._count: int = 0
        self._capacity: int = max_elements

        # Graph structure
        # neighbors[level][node_id] = list of neighbor node_ids
        self._neighbors: List[Dict[int, List[int]]] = [{}]  # Start with level 0
        self._max_level: int = 0  # Current maximum level
        self._entry_point: int = -1  # Entry point node
        self._node_levels: Dict[int, int] = {}  # node_id -> max level for that node

        # ID mapping
        self._external_ids: Dict[int, str] = {}  # internal_id -> external_id
        self._internal_ids: Dict[str, int] = {}  # external_id -> internal_id
        self._deleted: Set[int] = set()  # Deleted internal IDs

        # Load existing index if path exists
        if self.path and (self.path / "config.json").exists():
            self.load(self.path)

    @property
    def count(self) -> int:
        """Number of vectors in index."""
        return self._count - len(self._deleted)

    @property
    def dimension(self) -> int:
        """Vector dimension."""
        return self.config.dimension

    @property
    def metric(self) -> str:
        """Distance metric."""
        return self.config.metric

    def _get_random_level(self) -> int:
        """Generate random level for new node using exponential distribution."""
        r = self._rng.random()
        return int(-math.log(r) * self.config.ml)

    def _ensure_capacity(self, n_new: int) -> None:
        """Ensure capacity for n_new vectors."""
        required = self._count + n_new
        if required > self._capacity:
            new_capacity = max(required, self._capacity * 2)
            new_vectors = np.zeros((new_capacity, self.config.dimension), dtype=np.float32)
            new_vectors[:self._count] = self._vectors[:self._count]
            self._vectors = new_vectors
            self._capacity = new_capacity

    def _select_neighbors_simple(
        self,
        candidates: List[Tuple[float, int]],
        m: int
    ) -> List[int]:
        """
        Simple neighbor selection: take M nearest.

        Args:
            candidates: List of (distance, node_id) tuples
            m: Number of neighbors to select

        Returns:
            List of selected neighbor node_ids
        """
        # Sort by distance and take top M
        candidates.sort(key=lambda x: x[0])
        return [node_id for _, node_id in candidates[:m]]

    def _select_neighbors_heuristic(
        self,
        query: np.ndarray,
        candidates: List[Tuple[float, int]],
        m: int,
        level: int,
        extend_candidates: bool = True,
        keep_pruned: bool = True,
    ) -> List[int]:
        """
        Heuristic neighbor selection for better graph connectivity.

        Args:
            query: Query vector
            candidates: List of (distance, node_id) tuples
            m: Number of neighbors to select
            level: Current level
            extend_candidates: Whether to extend candidates with neighbors
            keep_pruned: Whether to keep some pruned candidates

        Returns:
            List of selected neighbor node_ids
        """
        if len(candidates) <= m:
            return [node_id for _, node_id in candidates]

        # Sort candidates by distance
        candidates.sort(key=lambda x: x[0])

        # Extend candidates with their neighbors
        if extend_candidates:
            extended = set()
            for dist, node_id in candidates:
                extended.add(node_id)
                if level < len(self._neighbors) and node_id in self._neighbors[level]:
                    for neighbor in self._neighbors[level][node_id]:
                        if neighbor not in extended and neighbor not in self._deleted:
                            neighbor_dist = self._distance_func(
                                query, self._vectors[neighbor]
                            )
                            extended.add(neighbor)
                            candidates.append((neighbor_dist, neighbor))

            candidates.sort(key=lambda x: x[0])

        # Select neighbors using heuristic
        selected = []
        pruned = []

        for dist, node_id in candidates:
            if len(selected) >= m:
                break

            # Check if this candidate is closer than all selected neighbors
            good = True
            for selected_id in selected:
                selected_dist = self._distance_func(
                    self._vectors[node_id], self._vectors[selected_id]
                )
                if selected_dist < dist:
                    good = False
                    break

            if good:
                selected.append(node_id)
            else:
                pruned.append((dist, node_id))

        # Add pruned candidates if needed
        if keep_pruned and len(selected) < m:
            for dist, node_id in pruned:
                if len(selected) >= m:
                    break
                selected.append(node_id)

        return selected

    def _search_layer(
        self,
        query: np.ndarray,
        entry_points: List[int],
        ef: int,
        level: int,
    ) -> List[Tuple[float, int]]:
        """
        Search a single layer of the graph.

        Args:
            query: Query vector
            entry_points: Starting nodes
            ef: Number of candidates to maintain
            level: Layer to search

        Returns:
            List of (distance, node_id) tuples, sorted by distance
        """
        if level >= len(self._neighbors):
            return []

        visited = set(entry_points)

        # Min-heap of candidates (distance, node_id)
        candidates = []
        for ep in entry_points:
            if ep in self._deleted:
                continue
            dist = self._distance_func(query, self._vectors[ep])
            heapq.heappush(candidates, (dist, ep))

        # Max-heap of results (negative distance for max behavior)
        results = []
        for dist, node_id in candidates:
            heapq.heappush(results, (-dist, node_id))

        while candidates:
            c_dist, c_node = heapq.heappop(candidates)

            # Get furthest result distance
            if results:
                f_dist = -results[0][0]
                if c_dist > f_dist:
                    break

            # Explore neighbors
            if c_node in self._neighbors[level]:
                for neighbor in self._neighbors[level][c_node]:
                    if neighbor not in visited and neighbor not in self._deleted:
                        visited.add(neighbor)
                        n_dist = self._distance_func(query, self._vectors[neighbor])

                        f_dist = -results[0][0] if results else float('inf')

                        if n_dist < f_dist or len(results) < ef:
                            heapq.heappush(candidates, (n_dist, neighbor))
                            heapq.heappush(results, (-n_dist, neighbor))

                            if len(results) > ef:
                                heapq.heappop(results)

        # Convert results to sorted list
        result_list = [(-dist, node_id) for dist, node_id in results]
        result_list.sort(key=lambda x: x[0])

        return result_list

    def add(
        self,
        vectors: np.ndarray,
        ids: Optional[List[str]] = None,
    ) -> List[int]:
        """
        Add vectors to the index.

        Args:
            vectors: Vectors to add, shape (n, dimension) or (dimension,)
            ids: Optional external IDs (auto-generated if None)

        Returns:
            List of internal IDs assigned
        """
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        if vectors.shape[1] != self.config.dimension:
            raise ValueError(
                f"Expected dimension {self.config.dimension}, got {vectors.shape[1]}"
            )

        n_vectors = len(vectors)

        # Generate IDs if not provided
        if ids is None:
            ids = [str(self._count + i) for i in range(n_vectors)]

        if len(ids) != n_vectors:
            raise ValueError(f"Number of IDs ({len(ids)}) must match vectors ({n_vectors})")

        with self._lock:
            self._ensure_capacity(n_vectors)

            internal_ids = []

            for i, (vector, ext_id) in enumerate(zip(vectors, ids)):
                internal_id = self._count

                # Store vector
                self._vectors[internal_id] = vector

                # Store ID mapping
                self._external_ids[internal_id] = ext_id
                self._internal_ids[ext_id] = internal_id

                # Get level for this node
                node_level = self._get_random_level()
                self._node_levels[internal_id] = node_level

                # Ensure we have enough levels
                while node_level >= len(self._neighbors):
                    self._neighbors.append({})

                # Initialize empty neighbor lists
                for level in range(node_level + 1):
                    self._neighbors[level][internal_id] = []

                # Insert into graph
                if self._entry_point == -1:
                    # First node
                    self._entry_point = internal_id
                    self._max_level = node_level
                else:
                    # Search for nearest neighbors and connect
                    curr_node = self._entry_point
                    curr_dist = self._distance_func(vector, self._vectors[curr_node])

                    # Traverse from top to node_level + 1
                    for level in range(self._max_level, node_level, -1):
                        changed = True
                        while changed:
                            changed = False
                            if curr_node in self._neighbors[level]:
                                for neighbor in self._neighbors[level][curr_node]:
                                    if neighbor in self._deleted:
                                        continue
                                    n_dist = self._distance_func(vector, self._vectors[neighbor])
                                    if n_dist < curr_dist:
                                        curr_dist = n_dist
                                        curr_node = neighbor
                                        changed = True

                    # For levels <= node_level, do full search and connect
                    entry_points = [curr_node]

                    for level in range(min(node_level, self._max_level), -1, -1):
                        candidates = self._search_layer(
                            vector,
                            entry_points,
                            self.config.ef_construction,
                            level,
                        )

                        # Select neighbors
                        m = self.config.m_max if level > 0 else self.config.m_max_0
                        neighbors = self._select_neighbors_heuristic(
                            vector,
                            candidates,
                            m,
                            level,
                        )

                        # Add bidirectional connections
                        self._neighbors[level][internal_id] = neighbors

                        for neighbor in neighbors:
                            if neighbor not in self._neighbors[level]:
                                self._neighbors[level][neighbor] = []

                            self._neighbors[level][neighbor].append(internal_id)

                            # Prune if too many connections
                            max_conn = self.config.m_max if level > 0 else self.config.m_max_0
                            if len(self._neighbors[level][neighbor]) > max_conn:
                                # Re-select neighbors
                                neighbor_candidates = [
                                    (self._distance_func(
                                        self._vectors[neighbor],
                                        self._vectors[n]
                                    ), n)
                                    for n in self._neighbors[level][neighbor]
                                    if n not in self._deleted
                                ]
                                self._neighbors[level][neighbor] = self._select_neighbors_heuristic(
                                    self._vectors[neighbor],
                                    neighbor_candidates,
                                    max_conn,
                                    level,
                                )

                        # Update entry points for next level
                        entry_points = [n for _, n in candidates[:self.config.ef_construction]]
                        if not entry_points:
                            entry_points = [internal_id]

                    # Update entry point if new node has higher level
                    if node_level > self._max_level:
                        self._entry_point = internal_id
                        self._max_level = node_level

                self._count += 1
                internal_ids.append(internal_id)

            return internal_ids

    def search(
        self,
        query: np.ndarray,
        k: int = 10,
        ef: Optional[int] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Search for k nearest neighbors.

        Args:
            query: Query vector, shape (dimension,)
            k: Number of neighbors to return
            ef: Search width (default: config.ef_search)

        Returns:
            Tuple of (internal_ids, distances)
        """
        query = np.asarray(query, dtype=np.float32).flatten()

        if len(query) != self.config.dimension:
            raise ValueError(
                f"Expected dimension {self.config.dimension}, got {len(query)}"
            )

        if ef is None:
            ef = self.config.ef_search

        # Ensure ef >= k
        ef = max(ef, k)

        with self._lock:
            if self._entry_point == -1 or self._count == 0:
                return np.array([], dtype=np.int64), np.array([], dtype=np.float32)

            # Start from entry point
            curr_node = self._entry_point
            curr_dist = self._distance_func(query, self._vectors[curr_node])

            # Greedy search from top level to level 1
            for level in range(self._max_level, 0, -1):
                changed = True
                while changed:
                    changed = False
                    if curr_node in self._neighbors[level]:
                        for neighbor in self._neighbors[level][curr_node]:
                            if neighbor in self._deleted:
                                continue
                            n_dist = self._distance_func(query, self._vectors[neighbor])
                            if n_dist < curr_dist:
                                curr_dist = n_dist
                                curr_node = neighbor
                                changed = True

            # Search level 0 with ef candidates
            candidates = self._search_layer(query, [curr_node], ef, 0)

            # Return top k
            candidates = candidates[:k]

            if not candidates:
                return np.array([], dtype=np.int64), np.array([], dtype=np.float32)

            ids = np.array([node_id for _, node_id in candidates], dtype=np.int64)
            distances = np.array([dist for dist, _ in candidates], dtype=np.float32)

            return ids, distances

    def search_batch(
        self,
        queries: np.ndarray,
        k: int = 10,
        ef: Optional[int] = None,
    ) -> List[Tuple[np.ndarray, np.ndarray]]:
        """
        Search for k nearest neighbors for multiple queries.

        Args:
            queries: Query vectors, shape (n, dimension)
            k: Number of neighbors per query
            ef: Search width

        Returns:
            List of (ids, distances) tuples
        """
        queries = np.asarray(queries, dtype=np.float32)
        if queries.ndim == 1:
            queries = queries.reshape(1, -1)

        results = []
        for query in queries:
            ids, distances = self.search(query, k, ef)
            results.append((ids, distances))

        return results

    def remove(self, ids: List[str]) -> int:
        """
        Mark vectors as deleted (lazy deletion).

        Args:
            ids: External IDs to remove

        Returns:
            Number of vectors actually removed
        """
        count = 0
        with self._lock:
            for ext_id in ids:
                if ext_id in self._internal_ids:
                    internal_id = self._internal_ids[ext_id]
                    if internal_id not in self._deleted:
                        self._deleted.add(internal_id)
                        count += 1

        return count

    def get_vector(self, id: str) -> Optional[np.ndarray]:
        """Get vector by external ID."""
        with self._lock:
            if id not in self._internal_ids:
                return None
            internal_id = self._internal_ids[id]
            if internal_id in self._deleted:
                return None
            return self._vectors[internal_id].copy()

    def get_vector_by_internal_id(self, internal_id: int) -> Optional[np.ndarray]:
        """Get vector by internal ID."""
        with self._lock:
            if internal_id < 0 or internal_id >= self._count:
                return None
            if internal_id in self._deleted:
                return None
            return self._vectors[internal_id].copy()

    def save(self, path: Optional[Path] = None) -> None:
        """
        Save index to disk.

        Args:
            path: Directory to save to (uses self.path if None)
        """
        path = Path(path) if path else self.path
        if path is None:
            raise ValueError("No path specified")

        path.mkdir(parents=True, exist_ok=True)

        with self._lock:
            # Save configuration
            config = {
                "dimension": self.config.dimension,
                "metric": self.config.metric,
                "m": self.config.m,
                "m_max": self.config.m_max,
                "m_max_0": self.config.m_max_0,
                "ef_construction": self.config.ef_construction,
                "ef_search": self.config.ef_search,
                "ml": self.config.ml,
                "seed": self.config.seed,
                "count": self._count,
                "max_level": self._max_level,
                "entry_point": self._entry_point,
            }
            with open(path / "config.json", "w") as f:
                json.dump(config, f, indent=2)

            # Save vectors
            np.save(path / "vectors.npy", self._vectors[:self._count])

            # Save graph structure
            graph_data = {
                "neighbors": [
                    {str(k): v for k, v in level.items()}
                    for level in self._neighbors
                ],
                "node_levels": {str(k): v for k, v in self._node_levels.items()},
            }
            with open(path / "graph.json", "w") as f:
                json.dump(graph_data, f)

            # Save ID mappings
            id_data = {
                "external_ids": {str(k): v for k, v in self._external_ids.items()},
                "deleted": list(self._deleted),
            }
            with open(path / "ids.json", "w") as f:
                json.dump(id_data, f)

    def load(self, path: Optional[Path] = None) -> "NativeHNSWIndex":
        """
        Load index from disk.

        Args:
            path: Directory to load from (uses self.path if None)

        Returns:
            self for method chaining
        """
        path = Path(path) if path else self.path
        if path is None:
            raise ValueError("No path specified")

        with self._lock:
            # Load configuration
            with open(path / "config.json", "r") as f:
                config = json.load(f)

            self.config = HNSWConfig(
                dimension=config["dimension"],
                metric=config["metric"],
                m=config["m"],
                m_max=config["m_max"],
                m_max_0=config["m_max_0"],
                ef_construction=config["ef_construction"],
                ef_search=config["ef_search"],
                ml=config["ml"],
                seed=config["seed"],
            )

            self._count = config["count"]
            self._max_level = config["max_level"]
            self._entry_point = config["entry_point"]

            # Update distance functions
            self._distance_func = DistanceFunctions.get_distance_func(self.config.metric)
            self._batch_distance_func = DistanceFunctions.get_batch_distance_func(self.config.metric)

            # Load vectors
            self._vectors = np.load(path / "vectors.npy")
            self._capacity = len(self._vectors)

            # Load graph structure
            with open(path / "graph.json", "r") as f:
                graph_data = json.load(f)

            self._neighbors = [
                {int(k): v for k, v in level.items()}
                for level in graph_data["neighbors"]
            ]
            self._node_levels = {int(k): v for k, v in graph_data["node_levels"].items()}

            # Load ID mappings
            with open(path / "ids.json", "r") as f:
                id_data = json.load(f)

            self._external_ids = {int(k): v for k, v in id_data["external_ids"].items()}
            self._internal_ids = {v: int(k) for k, v in id_data["external_ids"].items()}
            self._deleted = set(id_data["deleted"])

        return self

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        with self._lock:
            stats = {
                "count": self.count,
                "total_count": self._count,
                "deleted_count": len(self._deleted),
                "dimension": self.config.dimension,
                "metric": self.config.metric,
                "m": self.config.m,
                "ef_construction": self.config.ef_construction,
                "ef_search": self.config.ef_search,
                "max_level": self._max_level,
                "levels": len(self._neighbors),
                "memory_bytes": self._vectors.nbytes,
            }

            # Connections per level
            level_stats = []
            for level, neighbors in enumerate(self._neighbors):
                n_nodes = len(neighbors)
                if n_nodes > 0:
                    avg_connections = sum(len(v) for v in neighbors.values()) / n_nodes
                else:
                    avg_connections = 0
                level_stats.append({
                    "level": level,
                    "nodes": n_nodes,
                    "avg_connections": avg_connections,
                })

            stats["level_stats"] = level_stats

            return stats

    def __len__(self) -> int:
        """Return number of vectors."""
        return self.count

    def __repr__(self) -> str:
        return (
            f"NativeHNSWIndex("
            f"count={self.count}, "
            f"dimension={self.config.dimension}, "
            f"metric={self.config.metric}, "
            f"m={self.config.m})"
        )
