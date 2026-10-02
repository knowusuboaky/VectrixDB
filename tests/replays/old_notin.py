"""Restore the parameter-bound NOT IN deletes, to prove the scale tests catch them."""

from vectrixdb.core.graphrag.graph.storage import GraphStorage


def _old_save_hierarchy(self, hierarchy):
    if hierarchy is None:
        return
    communities = hierarchy.get_all_communities()
    keep = {c.id for c in communities}
    with self._transaction() as conn:
        cursor = conn.cursor()
        if keep:
            placeholders = ",".join("?" * len(keep))
            cursor.execute(
                f"DELETE FROM community_members WHERE community_id NOT IN ({placeholders})",
                tuple(keep),
            )
            cursor.execute(f"DELETE FROM communities WHERE id NOT IN ({placeholders})", tuple(keep))
        else:
            cursor.execute("DELETE FROM community_members")
            cursor.execute("DELETE FROM communities")
        for community in communities:
            cursor.execute(
                "INSERT OR REPLACE INTO communities (id, level, summary, importance, parent_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    community.id,
                    community.level,
                    community.summary,
                    community.importance,
                    community.parent_id,
                ),
            )
            cursor.execute("DELETE FROM community_members WHERE community_id = ?", (community.id,))
            if community.entity_ids:
                cursor.executemany(
                    "INSERT OR REPLACE INTO community_members (community_id, entity_id) "
                    "VALUES (?, ?)",
                    [(community.id, eid) for eid in community.entity_ids],
                )


def pytest_configure(config):
    GraphStorage.save_hierarchy = _old_save_hierarchy
