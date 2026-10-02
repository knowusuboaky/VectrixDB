"""Graph questions a person asks: how are these two connected, and why is this here.

``path()`` is the shortest chain of relationships between two entities.
``explain()`` is everything the graph knows about one entity, or about the
pair: direct relationships live and superseded, shared neighbours, the
communities each sits in with their summaries, and the path. Both take names
as a user would type them and resolve through the graph's own matching, so
"Curie" finds "Marie Curie".
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional

from .extractor.base import Entity, Relationship
from .graph.community import CommunityHierarchy
from .graph.knowledge_graph import KnowledgeGraph

__all__ = ["find_entity", "path", "explain"]


# ============================================================================
# FINDING, THE PATH, AND THE EXPLANATION
# ============================================================================
#
# INPUT   a name as a person types it; two names
# OUTPUT  the entity, by id, name, alias or fuzzy match; the shortest chain of
#         live relationships between two; everything the graph knows about one
#         entity or the pair
#
# Two questions a person asks: how are these connected, and why is this here.


def find_entity(graph: KnowledgeGraph, name: str) -> Optional[Entity]:
    """The entity ``name`` denotes, by id, exact name, alias, or fuzzy match."""
    if name in graph.nodes:
        return graph.nodes[name]
    entity = graph.get_entity_by_name(name)
    if entity is not None:
        return entity
    entity_id = graph._find_similar_entity(name)
    return graph.nodes.get(entity_id) if entity_id else None


def _rel_dict(graph: KnowledgeGraph, rel: Relationship) -> Dict[str, Any]:
    source = graph.nodes.get(rel.source_id)
    target = graph.nodes.get(rel.target_id)
    return {
        "id": rel.id,
        "source": source.name if source else rel.source_id,
        "target": target.name if target else rel.target_id,
        "type": rel.type,
        "description": rel.description,
        "strength": rel.strength,
        "valid_from": getattr(rel, "valid_from", None),
        "valid_to": getattr(rel, "valid_to", None),
        "superseded_by": getattr(rel, "superseded_by", None),
    }


def path(graph: KnowledgeGraph, a: str, b: str, max_depth: int = 4) -> Dict[str, Any]:
    """Shortest chain of live relationships from ``a`` to ``b``, either direction.

    Returns ``found``, the entity names in order, the relationships between
    them in order, and ``depth``. When either name is unknown, ``found`` is
    False and ``missing`` names it, so a caller can tell "no path" from "no
    such entity".
    """
    start = find_entity(graph, a)
    goal = find_entity(graph, b)
    missing = [n for n, e in ((a, start), (b, goal)) if e is None]
    if missing:
        return {"found": False, "missing": missing, "entities": [], "relationships": [], "depth": 0}
    assert start is not None and goal is not None
    if start.id == goal.id:
        return {"found": True, "entities": [start.name], "relationships": [], "depth": 0}

    previous: Dict[str, Optional[tuple]] = {start.id: None}
    queue = deque([(start.id, 0)])
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for rel in graph.get_relationships_for_entity(node):
            other = rel.target_id if rel.source_id == node else rel.source_id
            if other not in graph.nodes or other in previous:
                continue
            previous[other] = (node, rel)
            if other == goal.id:
                queue.clear()
                break
            queue.append((other, depth + 1))
    if goal.id not in previous:
        return {"found": False, "missing": [], "entities": [], "relationships": [], "depth": 0}

    chain: List[Relationship] = []
    nodes: List[str] = [goal.id]
    cursor = goal.id
    while previous[cursor] is not None:
        prev_node, rel = previous[cursor]  # type: ignore[misc]
        chain.append(rel)
        nodes.append(prev_node)
        cursor = prev_node
    chain.reverse()
    nodes.reverse()
    return {
        "found": True,
        "entities": [graph.nodes[n].name for n in nodes],
        "relationships": [_rel_dict(graph, r) for r in chain],
        "depth": len(chain),
    }


def _communities_of(
    hierarchy: Optional[CommunityHierarchy], entity_id: str
) -> List[Dict[str, Any]]:
    if hierarchy is None:
        return []
    out = []
    for level, community_id in sorted(hierarchy.entity_to_community.get(entity_id, {}).items()):
        community = hierarchy.get_community(community_id)
        if community is None:
            continue
        out.append(
            {
                "id": community.id,
                "level": level,
                "size": community.size,
                "summary": community.summary,
            }
        )
    return out


def explain(
    graph: KnowledgeGraph,
    a: str,
    b: Optional[str] = None,
    hierarchy: Optional[CommunityHierarchy] = None,
    max_depth: int = 4,
    limit: int = 20,
) -> Dict[str, Any]:
    """What the graph knows about ``a``, or about how ``a`` and ``b`` relate."""
    start = find_entity(graph, a)
    if start is None:
        return {"found": False, "missing": [a]}
    live = graph.get_relationships_for_entity(start.id)
    everything = graph.get_relationships_for_entity(start.id, include_superseded=True)
    superseded = [r for r in everything if getattr(r, "superseded_by", None)]
    result: Dict[str, Any] = {
        "found": True,
        "entity": {
            "id": start.id,
            "name": start.name,
            "type": start.type,
            "description": start.description,
            "aliases": sorted(start.aliases),
            "importance": start.importance,
            "mentions": len(start.source_units),
        },
        "communities": _communities_of(hierarchy, start.id),
        "relationships": [
            _rel_dict(graph, r)
            for r in sorted(live, key=lambda r: r.strength, reverse=True)[:limit]
        ],
        "superseded": [_rel_dict(graph, r) for r in superseded[:limit]],
    }
    if b is None:
        return result

    other = find_entity(graph, b)
    if other is None:
        result["found"] = False
        result["missing"] = [b]
        return result
    direct = [r for r in everything if {r.source_id, r.target_id} == {start.id, other.id}]
    mine = {(r.target_id if r.source_id == start.id else r.source_id) for r in live}
    theirs = {
        (r.target_id if r.source_id == other.id else r.source_id)
        for r in graph.get_relationships_for_entity(other.id)
    }
    shared = [
        graph.nodes[n].name
        for n in mine & theirs
        if n in graph.nodes and n not in (start.id, other.id)
    ]
    result["other"] = {"id": other.id, "name": other.name, "type": other.type}
    result["direct"] = [_rel_dict(graph, r) for r in direct]
    result["shared_neighbours"] = sorted(shared)
    result["same_community"] = any(
        c["id"] in {d["id"] for d in _communities_of(hierarchy, other.id)}
        for c in result["communities"]
    )
    result["path"] = path(graph, start.name, other.name, max_depth=max_depth)
    return result
