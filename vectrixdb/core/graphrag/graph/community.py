"""
Community Detection for VectrixDB GraphRAG.

Implements hierarchical community detection using the Leiden algorithm
for grouping related entities into thematic clusters.
"""

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any
from collections import defaultdict

from .knowledge_graph import KnowledgeGraph


@dataclass
class Community:
    """A community of related entities."""
    id: str
    level: int
    entity_ids: List[str]
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    summary: str = ""
    importance: float = 0.0
    embedding: Optional[List[float]] = None

    @property
    def size(self) -> int:
        return len(self.entity_ids)

    def __hash__(self):
        return hash(self.id)


@dataclass
class CommunityHierarchy:
    """Hierarchical structure of communities."""
    levels: Dict[int, List[Community]] = field(default_factory=dict)
    entity_to_community: Dict[str, Dict[int, str]] = field(default_factory=lambda: defaultdict(dict))

    def add_community(self, community: Community) -> None:
        """Add a community to the hierarchy."""
        if community.level not in self.levels:
            self.levels[community.level] = []
        self.levels[community.level].append(community)

        # Update entity mapping
        for entity_id in community.entity_ids:
            self.entity_to_community[entity_id][community.level] = community.id

    def get_level(self, level: int) -> List[Community]:
        """Get all communities at a specific level."""
        return self.levels.get(level, [])

    def get_community(self, community_id: str) -> Optional[Community]:
        """Get a community by ID."""
        for communities in self.levels.values():
            for community in communities:
                if community.id == community_id:
                    return community
        return None

    def get_community_for_entity(self, entity_id: str, level: int = 0) -> Optional[Community]:
        """Get the community containing an entity at a specific level."""
        community_id = self.entity_to_community.get(entity_id, {}).get(level)
        if community_id:
            return self.get_community(community_id)
        return None

    def get_all_communities(self) -> List[Community]:
        """Get all communities across all levels."""
        all_communities = []
        for communities in self.levels.values():
            all_communities.extend(communities)
        return all_communities

    @property
    def num_levels(self) -> int:
        return len(self.levels)

    @property
    def total_communities(self) -> int:
        return sum(len(communities) for communities in self.levels.values())


class CommunityDetector:
    """
    Hierarchical community detection using Leiden algorithm.

    Creates a multi-level community structure:
    - Level 0: Fine-grained communities (small clusters)
    - Level 1+: Coarser communities (larger clusters)

    Supports both python-igraph (fast) and networkx (fallback).
    """

    def __init__(self, min_community_size: int = 2, resolution: float = 1.0):
        """
        Initialize community detector.

        Args:
            min_community_size: Minimum entities per community.
            resolution: Leiden resolution parameter (higher = smaller communities).
        """
        self.min_community_size = min_community_size
        self.resolution = resolution

        # Try to import community detection libraries
        self._use_igraph = False
        self._use_networkx = False

        try:
            import igraph
            import leidenalg
            self._use_igraph = True
        except ImportError:
            try:
                import networkx as nx
                from networkx.algorithms import community
                self._use_networkx = True
            except ImportError:
                pass

    def detect(
        self,
        graph: KnowledgeGraph,
        max_levels: int = 3
    ) -> CommunityHierarchy:
        """
        Detect communities in the knowledge graph.

        Args:
            graph: The knowledge graph to analyze.
            max_levels: Maximum hierarchy depth.

        Returns:
            CommunityHierarchy with detected communities.
        """
        if graph.is_empty():
            return CommunityHierarchy()

        if self._use_igraph:
            return self._detect_with_igraph(graph, max_levels)
        elif self._use_networkx:
            return self._detect_with_networkx(graph, max_levels)
        else:
            return self._detect_simple(graph, max_levels)

    def _detect_with_igraph(
        self,
        graph: KnowledgeGraph,
        max_levels: int
    ) -> CommunityHierarchy:
        """Detect communities using python-igraph and leidenalg."""
        import igraph
        import leidenalg

        hierarchy = CommunityHierarchy()

        # Build igraph graph
        entity_ids = list(graph.nodes.keys())
        id_to_idx = {eid: i for i, eid in enumerate(entity_ids)}

        g = igraph.Graph()
        g.add_vertices(len(entity_ids))

        # Add edges with weights
        edges = []
        weights = []
        for rel in graph.get_all_relationships():
            if rel.source_id in id_to_idx and rel.target_id in id_to_idx:
                edges.append((id_to_idx[rel.source_id], id_to_idx[rel.target_id]))
                weights.append(rel.strength)

        if edges:
            g.add_edges(edges)
            g.es['weight'] = weights

        # Detect communities at multiple resolutions
        current_resolution = self.resolution
        for level in range(max_levels):
            partition = leidenalg.find_partition(
                g,
                leidenalg.RBConfigurationVertexPartition,
                weights='weight' if edges else None,
                resolution_parameter=current_resolution
            )

            # Convert to communities
            for cluster_idx, cluster in enumerate(partition):
                if len(cluster) < self.min_community_size:
                    continue

                community_id = f"community_L{level}_{cluster_idx}"
                entity_ids_in_cluster = [entity_ids[idx] for idx in cluster]

                community = Community(
                    id=community_id,
                    level=level,
                    entity_ids=entity_ids_in_cluster
                )
                hierarchy.add_community(community)

            # Increase resolution for next level (coarser communities)
            current_resolution *= 0.5

        return hierarchy

    def _detect_with_networkx(
        self,
        graph: KnowledgeGraph,
        max_levels: int
    ) -> CommunityHierarchy:
        """Detect communities using networkx."""
        import networkx as nx
        from networkx.algorithms.community import louvain_communities

        hierarchy = CommunityHierarchy()

        # Build networkx graph
        G = nx.Graph()

        # Add nodes
        for entity_id in graph.nodes:
            G.add_node(entity_id)

        # Add edges with weights
        for rel in graph.get_all_relationships():
            if rel.source_id in graph.nodes and rel.target_id in graph.nodes:
                G.add_edge(rel.source_id, rel.target_id, weight=rel.strength)

        if G.number_of_edges() == 0:
            # No edges, put all in one community
            community = Community(
                id="community_L0_0",
                level=0,
                entity_ids=list(graph.nodes.keys())
            )
            hierarchy.add_community(community)
            return hierarchy

        # Detect communities using Louvain (similar to Leiden)
        current_resolution = self.resolution
        for level in range(max_levels):
            try:
                communities = louvain_communities(
                    G,
                    weight='weight',
                    resolution=current_resolution
                )

                for cluster_idx, cluster in enumerate(communities):
                    if len(cluster) < self.min_community_size:
                        continue

                    community_id = f"community_L{level}_{cluster_idx}"
                    community = Community(
                        id=community_id,
                        level=level,
                        entity_ids=list(cluster)
                    )
                    hierarchy.add_community(community)

                current_resolution *= 0.5
            except Exception:
                break

        return hierarchy

    def _detect_simple(
        self,
        graph: KnowledgeGraph,
        max_levels: int
    ) -> CommunityHierarchy:
        """Simple community detection without external libraries."""
        hierarchy = CommunityHierarchy()

        # Use connected components as communities
        visited: Set[str] = set()
        cluster_idx = 0

        for start_entity_id in graph.nodes:
            if start_entity_id in visited:
                continue

            # BFS to find connected component
            component: Set[str] = set()
            queue = [start_entity_id]

            while queue:
                entity_id = queue.pop(0)
                if entity_id in visited:
                    continue

                visited.add(entity_id)
                component.add(entity_id)

                # Add neighbors
                for rel in graph.get_relationships_for_entity(entity_id):
                    neighbor_id = rel.target_id if rel.source_id == entity_id else rel.source_id
                    if neighbor_id not in visited:
                        queue.append(neighbor_id)

            if len(component) >= self.min_community_size:
                community = Community(
                    id=f"community_L0_{cluster_idx}",
                    level=0,
                    entity_ids=list(component)
                )
                hierarchy.add_community(community)
                cluster_idx += 1

        return hierarchy


def detect_communities(
    graph: KnowledgeGraph,
    max_levels: int = 3,
    min_community_size: int = 2
) -> CommunityHierarchy:
    """
    Factory function to detect communities.

    Args:
        graph: Knowledge graph to analyze.
        max_levels: Maximum hierarchy depth.
        min_community_size: Minimum entities per community.

    Returns:
        CommunityHierarchy with detected communities.
    """
    detector = CommunityDetector(min_community_size=min_community_size)
    return detector.detect(graph, max_levels)
