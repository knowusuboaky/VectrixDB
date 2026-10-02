"""Incremental community detection.

A full re-clustering after every add() is the expensive tail of GraphRAG:
under an LLM extractor it is one model call per community, paid again for
communities nothing touched. This module finds the part of the graph a batch
actually changed, re-detects communities only there, and keeps the rest of
the hierarchy, summaries included.

The unit of change is the connected component. A new node or edge can only
move community boundaries inside the component it lands in (communities never
span components), so every community that intersects an affected component
is dropped and re-detected, and every other community is kept as is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Set, Tuple

from ..extractor.base import Entity, Relationship
from .community import Community, CommunityHierarchy, detect_communities
from .knowledge_graph import KnowledgeGraph

__all__ = ["IncrementalResult", "affected_nodes", "subgraph", "update_hierarchy"]


# ============================================================================
# WHAT CHANGED, AND RE-DETECTING ONLY THERE
# ============================================================================
#
# INPUT   a graph, and its nodes and edges before a batch
# OUTPUT  what the update did; every node in a component that gained a node or
#         an edge; a standalone graph over those nodes; the hierarchy with
#         communities re-detected where the graph changed and kept elsewhere,
#         summaries included; a hierarchy holding only named communities; node
#         to component, for diagnostics
#
# A new node or edge can only move community boundaries inside the component
# it lands in, so every other community is kept as is.


@dataclass
class IncrementalResult:
    """What an incremental update did."""

    hierarchy: CommunityHierarchy
    affected: Set[str] = field(default_factory=set)
    removed: List[str] = field(default_factory=list)
    added: List[Community] = field(default_factory=list)
    full: bool = False

    @property
    def new_ids(self) -> Set[str]:
        return {c.id for c in self.added}


def affected_nodes(
    graph: KnowledgeGraph,
    before_nodes: FrozenSet[str],
    before_edges: FrozenSet[str],
) -> Set[str]:
    """Every node in a connected component that gained a node or an edge.

    Seeds are the new nodes and both endpoints of every new edge; the
    closure is over the live edges of the graph as it is now.
    """
    seeds: Set[str] = set(graph.nodes) - set(before_nodes)
    for edge_id in set(graph.edges) - set(before_edges):
        rel = graph.edges.get(edge_id)
        if rel is not None:
            seeds.add(rel.source_id)
            seeds.add(rel.target_id)
    seeds = {s for s in seeds if s in graph.nodes}
    if not seeds:
        return set()
    seen: Set[str] = set()
    stack = list(seeds)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for rel in graph.get_relationships_for_entity(node):
            other = rel.target_id if rel.source_id == node else rel.source_id
            if other in graph.nodes and other not in seen:
                stack.append(other)
    return seen


def subgraph(graph: KnowledgeGraph, node_ids: Iterable[str]) -> KnowledgeGraph:
    """A standalone graph over ``node_ids`` and the live edges among them.

    Entities are added without resolution so ids are preserved exactly;
    the ids are what the caller maps communities back with.
    """
    wanted = set(node_ids)
    part = KnowledgeGraph(similarity_threshold=1.0)
    for node_id in wanted:
        entity = graph.nodes.get(node_id)
        if entity is not None:
            part.add_entity(Entity(**{**entity.__dict__}), merge_if_exists=False)
    for rel in graph.get_all_relationships():
        if rel.source_id in wanted and rel.target_id in wanted:
            part.add_relationship(Relationship(**{**rel.__dict__}), merge_if_exists=False)
    return part


def update_hierarchy(
    graph: KnowledgeGraph,
    hierarchy: CommunityHierarchy,
    before_nodes: FrozenSet[str],
    before_edges: FrozenSet[str],
    *,
    max_levels: int = 3,
    min_community_size: int = 2,
    generation: int = 0,
) -> IncrementalResult:
    """Re-detect communities where the graph changed, keep the rest.

    ``generation`` is folded into new community ids so they can never
    collide with ids from an earlier detection that are still in use.
    Returns ``full=True`` when the change touched everything (or there was
    nothing before), in which case the caller may as well detect from
    scratch; the result is still correct either way.
    """
    affected = affected_nodes(graph, before_nodes, before_edges)
    if not affected:
        return IncrementalResult(hierarchy=hierarchy)
    if not before_nodes or len(affected) >= len(graph.nodes):
        fresh = detect_communities(
            graph, max_levels=max_levels, min_community_size=min_community_size
        )
        return IncrementalResult(
            hierarchy=fresh,
            affected=affected,
            removed=[c.id for c in hierarchy.get_all_communities()],
            added=list(fresh.get_all_communities()),
            full=True,
        )

    kept = CommunityHierarchy()
    removed: List[str] = []
    for community in hierarchy.get_all_communities():
        if affected.intersection(community.entity_ids):
            removed.append(community.id)
        else:
            kept.add_community(community)

    part = subgraph(graph, affected)
    local = detect_communities(part, max_levels=max_levels, min_community_size=min_community_size)
    added: List[Community] = []
    for community in local.get_all_communities():
        renamed = Community(
            id=f"{community.id}_g{generation}",
            level=community.level,
            entity_ids=list(community.entity_ids),
            parent_id=community.parent_id,
            children_ids=list(community.children_ids),
            summary=community.summary,
            importance=community.importance,
            embedding=community.embedding,
        )
        kept.add_community(renamed)
        added.append(renamed)
    return IncrementalResult(hierarchy=kept, affected=affected, removed=removed, added=added)


def only(hierarchy: CommunityHierarchy, ids: Iterable[str]) -> CommunityHierarchy:
    """A hierarchy holding just the named communities, for partial summarising."""
    wanted = set(ids)
    part = CommunityHierarchy()
    for community in hierarchy.get_all_communities():
        if community.id in wanted:
            part.add_community(community)
    return part


def component_map(graph: KnowledgeGraph) -> Dict[str, int]:
    """Node id to connected-component index, for diagnostics and tests."""
    index: Dict[str, int] = {}
    current = 0
    for start in graph.nodes:
        if start in index:
            continue
        stack: List[Tuple[str]] = [(start,)]
        while stack:
            (node,) = stack.pop()
            if node in index:
                continue
            index[node] = current
            for rel in graph.get_relationships_for_entity(node):
                other = rel.target_id if rel.source_id == node else rel.source_id
                if other in graph.nodes and other not in index:
                    stack.append((other,))
        current += 1
    return index
