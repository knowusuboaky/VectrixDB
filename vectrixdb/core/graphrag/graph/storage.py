"""
SQLite Storage for VectrixDB GraphRAG Knowledge Graph.

Provides persistent storage for entities, relationships, communities,
and text units with efficient querying support.
"""

import os
import json
import sqlite3
import threading
from typing import TYPE_CHECKING, Dict, List, Optional, Any, Tuple
from contextlib import contextmanager

from ..extractor.base import Entity, Relationship


__all__ = [
    "GraphStorage",
]

if TYPE_CHECKING:
    from .knowledge_graph import KnowledgeGraph


# ============================================================================
# SQLITE PERSISTENCE
# ============================================================================
#
# INPUT   a graph
# OUTPUT  entities, relationships, communities and text units saved and
#         queried
#
# One file beside the collection.


class GraphStorage:
    """
    SQLite-based persistence for the knowledge graph.

    Tables:
    - entities: Entity nodes with embeddings
    - relationships: Edges between entities
    - communities: Detected communities with summaries
    - community_members: Entity-community mapping
    - text_units: Source text chunks
    - entity_sources: Entity-text unit mapping

    Features:
    - Thread-safe operations
    - Batch insert/update
    - Efficient querying with indexes
    - JSON serialization for complex fields
    """

    def __init__(self, path: str):
        """
        Initialize graph storage.

        Args:
            path: Path to the SQLite database file.
        """
        self.path = path
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def _transaction(self):
        """Context manager for transactions."""
        conn = self._conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self):
        """Initialize database schema."""
        # Ensure directory exists
        os.makedirs(
            os.path.dirname(self.path) if os.path.dirname(self.path) else ".", exist_ok=True
        )

        with self._transaction() as conn:
            cursor = conn.cursor()

            # Entities table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT,
                    importance REAL DEFAULT 0.0,
                    source_units TEXT,
                    aliases TEXT,
                    attributes TEXT,
                    embedding BLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Relationships table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT,
                    strength REAL DEFAULT 0.5,
                    source_units TEXT,
                    bidirectional INTEGER DEFAULT 0,
                    attributes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    valid_from TEXT,
                    valid_to TEXT,
                    superseded_by TEXT,
                    FOREIGN KEY (source_id) REFERENCES entities(id),
                    FOREIGN KEY (target_id) REFERENCES entities(id)
                )
            """)

            # Communities table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS communities (
                    id TEXT PRIMARY KEY,
                    level INTEGER NOT NULL,
                    summary TEXT,
                    importance REAL DEFAULT 0.0,
                    parent_id TEXT,
                    embedding BLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (parent_id) REFERENCES communities(id)
                )
            """)

            # Community members table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS community_members (
                    community_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    PRIMARY KEY (community_id, entity_id),
                    FOREIGN KEY (community_id) REFERENCES communities(id),
                    FOREIGN KEY (entity_id) REFERENCES entities(id)
                )
            """)

            # Text units table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS text_units (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    position INTEGER,
                    token_count INTEGER,
                    char_start INTEGER,
                    char_end INTEGER,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Entity sources table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entity_sources (
                    entity_id TEXT NOT NULL,
                    text_unit_id TEXT NOT NULL,
                    PRIMARY KEY (entity_id, text_unit_id),
                    FOREIGN KEY (entity_id) REFERENCES entities(id),
                    FOREIGN KEY (text_unit_id) REFERENCES text_units(id)
                )
            """)

            # Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_relationships_type ON relationships(type)"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_communities_level ON communities(level)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_text_units_doc ON text_units(doc_id)")

    # ========== Entity Operations ==========

    def save_entity(self, entity: Entity) -> None:
        """Save or update an entity."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO entities
                (id, name, type, description, importance, source_units, aliases, attributes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
                (
                    entity.id,
                    entity.name,
                    entity.type,
                    entity.description,
                    entity.importance,
                    json.dumps(entity.source_units),
                    json.dumps(list(entity.aliases)),
                    json.dumps(entity.attributes),
                ),
            )

    def save_entities(self, entities: List[Entity]) -> None:
        """Save multiple entities in batch."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR REPLACE INTO entities
                (id, name, type, description, importance, source_units, aliases, attributes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
                [
                    (
                        e.id,
                        e.name,
                        e.type,
                        e.description,
                        e.importance,
                        json.dumps(e.source_units),
                        json.dumps(list(e.aliases)),
                        json.dumps(e.attributes),
                    )
                    for e in entities
                ],
            )

    def load_entity(self, entity_id: str) -> Optional[Entity]:
        """Load an entity by ID."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM entities WHERE id = ?", (entity_id,))
        row = cursor.fetchone()
        if row:
            return self._row_to_entity(row)
        return None

    def load_all_entities(self) -> List[Entity]:
        """Load all entities."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM entities")
        return [self._row_to_entity(row) for row in cursor.fetchall()]

    def _row_to_entity(self, row: sqlite3.Row) -> Entity:
        """Convert a database row to an Entity."""
        return Entity(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            description=row["description"] or "",
            importance=row["importance"] or 0.0,
            source_units=json.loads(row["source_units"] or "[]"),
            aliases=set(json.loads(row["aliases"] or "[]")),
            attributes=json.loads(row["attributes"] or "{}"),
        )

    def delete_entity(self, entity_id: str) -> bool:
        """Delete an entity."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
            return cursor.rowcount > 0

    # ========== Relationship Operations ==========

    def _ensure_relationship_columns(self, cursor) -> None:
        """Add the supersession columns to a database written before 2.2."""
        cursor.execute("PRAGMA table_info(relationships)")
        have = {row[1] for row in cursor.fetchall()}
        for column in ("valid_from", "valid_to", "superseded_by"):
            if column not in have:
                cursor.execute(f"ALTER TABLE relationships ADD COLUMN {column} TEXT")

    @staticmethod
    def _relationship_row(relationship: Relationship) -> tuple:
        return (
            relationship.id,
            relationship.source_id,
            relationship.target_id,
            relationship.type,
            relationship.description,
            relationship.strength,
            json.dumps(relationship.source_units),
            1 if relationship.bidirectional else 0,
            json.dumps(relationship.attributes),
            relationship.valid_from,
            relationship.valid_to,
            relationship.superseded_by,
        )

    _RELATIONSHIP_INSERT = """
        INSERT OR REPLACE INTO relationships
        (id, source_id, target_id, type, description, strength, source_units, bidirectional,
         attributes, valid_from, valid_to, superseded_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """

    def save_relationship(self, relationship: Relationship) -> None:
        """Save or update a relationship."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            self._ensure_relationship_columns(cursor)
            cursor.execute(self._RELATIONSHIP_INSERT, self._relationship_row(relationship))

    def save_relationships(self, relationships: List[Relationship]) -> None:
        """Save many relationships in one transaction."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            self._ensure_relationship_columns(cursor)
            cursor.executemany(
                self._RELATIONSHIP_INSERT, [self._relationship_row(r) for r in relationships]
            )

    def load_relationship(self, relationship_id: str) -> Optional[Relationship]:
        """Load a relationship by ID."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM relationships WHERE id = ?", (relationship_id,))
        row = cursor.fetchone()
        if row:
            return self._row_to_relationship(row)
        return None

    def load_all_relationships(self) -> List[Relationship]:
        """Load all relationships."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM relationships")
        return [self._row_to_relationship(row) for row in cursor.fetchall()]

    def _row_to_relationship(self, row: sqlite3.Row) -> Relationship:
        """Convert a database row to a Relationship."""
        return Relationship(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            type=row["type"],
            description=row["description"] or "",
            strength=row["strength"] or 0.5,
            source_units=json.loads(row["source_units"] or "[]"),
            bidirectional=bool(row["bidirectional"]),
            attributes=json.loads(row["attributes"] or "{}"),
            valid_from=row["valid_from"] if "valid_from" in row.keys() else None,
            valid_to=row["valid_to"] if "valid_to" in row.keys() else None,
            superseded_by=row["superseded_by"] if "superseded_by" in row.keys() else None,
        )

    def load_relationships_for_entity(self, entity_id: str) -> List[Relationship]:
        """Load all relationships involving an entity."""
        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT * FROM relationships
            WHERE source_id = ? OR target_id = ?
        """,
            (entity_id, entity_id),
        )
        return [self._row_to_relationship(row) for row in cursor.fetchall()]

    # ========== Community Operations ==========

    def save_community(
        self,
        community_id: str,
        level: int,
        summary: str = "",
        importance: float = 0.0,
        parent_id: Optional[str] = None,
        entity_ids: Optional[List[str]] = None,
    ) -> None:
        """Save a community and its members."""
        with self._transaction() as conn:
            cursor = conn.cursor()

            # Save community
            cursor.execute(
                """
                INSERT OR REPLACE INTO communities
                (id, level, summary, importance, parent_id)
                VALUES (?, ?, ?, ?, ?)
            """,
                (community_id, level, summary, importance, parent_id),
            )

            # Save members. ``is not None``: the delete used to sit inside a
            # truthiness check, so saving a community whose membership had
            # emptied updated the summary and left the old members in place.
            if entity_ids is not None:
                cursor.execute(
                    "DELETE FROM community_members WHERE community_id = ?", (community_id,)
                )
                cursor.executemany(
                    """
                    INSERT INTO community_members (community_id, entity_id)
                    VALUES (?, ?)
                """,
                    [(community_id, eid) for eid in entity_ids],
                )

    def load_communities(self, level: Optional[int] = None) -> List[Dict[str, Any]]:
        """Load communities, optionally filtered by level."""
        cursor = self._conn.cursor()
        if level is not None:
            cursor.execute("SELECT * FROM communities WHERE level = ?", (level,))
        else:
            cursor.execute("SELECT * FROM communities")

        communities = []
        for row in cursor.fetchall():
            # Get members
            cursor.execute(
                "SELECT entity_id FROM community_members WHERE community_id = ?", (row["id"],)
            )
            member_ids = [r["entity_id"] for r in cursor.fetchall()]

            communities.append(
                {
                    "id": row["id"],
                    "level": row["level"],
                    "summary": row["summary"],
                    "importance": row["importance"],
                    "parent_id": row["parent_id"],
                    "entity_ids": member_ids,
                }
            )

        return communities

    def update_community_summary(self, community_id: str, summary: str) -> None:
        """Update a community's summary."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE communities SET summary = ? WHERE id = ?", (summary, community_id)
            )

    # ========== Text Unit Operations ==========

    def save_text_unit(
        self,
        unit_id: str,
        text: str,
        doc_id: str,
        position: int = 0,
        token_count: int = 0,
        char_start: int = 0,
        char_end: int = 0,
        metadata: Optional[Dict] = None,
    ) -> None:
        """Save a text unit."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO text_units
                (id, text, doc_id, position, token_count, char_start, char_end, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    unit_id,
                    text,
                    doc_id,
                    position,
                    token_count,
                    char_start,
                    char_end,
                    json.dumps(metadata or {}),
                ),
            )

    def load_text_units_for_doc(self, doc_id: str) -> List[Dict[str, Any]]:
        """Load all text units for a document."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM text_units WHERE doc_id = ? ORDER BY position", (doc_id,))
        return [dict(row) for row in cursor.fetchall()]

    # ========== Graph-level Operations ==========

    def save_graph(self, graph: "KnowledgeGraph") -> None:
        """Save entire graph to storage."""
        from .knowledge_graph import KnowledgeGraph

        self.save_entities(graph.get_all_entities())
        self.save_relationships(graph.get_all_relationships(include_superseded=True))

    def load_graph(self) -> "KnowledgeGraph":
        """Load entire graph from storage."""
        from .knowledge_graph import KnowledgeGraph

        graph = KnowledgeGraph()

        # Load entities
        for entity in self.load_all_entities():
            graph.add_entity(entity, merge_if_exists=False)

        # Load relationships
        for rel in self.load_all_relationships():
            graph.add_relationship(rel, merge_if_exists=False)

        return graph

    def save_hierarchy(self, hierarchy) -> None:
        """Persist every community in the hierarchy.

        The previous implementation returned early and then did nothing, with a
        comment claiming communities were "already saved during detection via
        save_community". They were not: nothing in the package ever called
        save_community, so the communities and community_members tables were
        never written by any code path.

        Both tables are cleared first rather than relying on INSERT OR REPLACE.
        Community ids are positional (``community_L{level}_{index}``) and get
        reused, so after a rebuild that produces fewer communities the leftovers
        would survive as phantoms pointing at entities that may no longer exist.
        A full replace is what the caller means in any case: this writes the
        whole hierarchy, not a delta.
        """
        if hierarchy is None:
            return

        communities = hierarchy.get_all_communities()

        with self._transaction() as conn:
            cursor = conn.cursor()
            # Not `DELETE ... WHERE id NOT IN (?, ?, ...)`: that binds one
            # parameter per surviving community and a large graph exceeds
            # SQLITE_LIMIT_VARIABLE_NUMBER, which is 999 on SQLite before 3.32.
            # It fails with "too many SQL variables" exactly when the graph is
            # big enough to matter. Members go first, since they reference
            # communities.
            cursor.execute("DELETE FROM community_members")
            cursor.execute("DELETE FROM communities")

            for community in communities:
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO communities
                    (id, level, summary, importance, parent_id)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (
                        community.id,
                        community.level,
                        community.summary,
                        community.importance,
                        community.parent_id,
                    ),
                )
                if community.entity_ids:
                    cursor.executemany(
                        "INSERT OR REPLACE INTO community_members (community_id, entity_id) "
                        "VALUES (?, ?)",
                        [(community.id, eid) for eid in community.entity_ids],
                    )

    def load_hierarchy(self):
        """Rebuild the community hierarchy from storage, or None if empty.

        This used to return None unconditionally, with a comment that
        reconstruction "would require CommunityHierarchy reconstruction which is
        complex". It does not: ``add_community`` rebuilds the level index and the
        entity lookup itself, so the rows are enough.

        Returning None here was not inert. The pipeline gates both the global and
        hybrid searchers behind a truthy hierarchy, and the default search type is
        HYBRID, so after any restart graph search raised and the caller silently
        fell back to vector-only results.
        """
        from .community import Community, CommunityHierarchy

        rows = self.load_communities()
        if not rows:
            # A graph whose entities never formed a community (too few to meet
            # min_community_size, say) still has a hierarchy: an empty one. That
            # is exactly the state the first run leaves in memory, so returning
            # None here made a reopened graph behave unlike the run that built
            # it, and the pipeline gates its searchers on this value.
            cursor = self._conn.cursor()
            cursor.execute("SELECT 1 FROM entities LIMIT 1")
            return CommunityHierarchy() if cursor.fetchone() else None

        hierarchy = CommunityHierarchy()
        for row in rows:
            hierarchy.add_community(
                Community(
                    id=row["id"],
                    level=row["level"],
                    entity_ids=list(row.get("entity_ids") or []),
                    parent_id=row.get("parent_id"),
                    summary=row.get("summary") or "",
                    importance=row.get("importance") or 0.0,
                )
            )

        # children_ids is derived, not stored, so rebuild it from parent links.
        by_id = {c.id: c for c in hierarchy.get_all_communities()}
        for community in by_id.values():
            parent = by_id.get(community.parent_id) if community.parent_id else None
            if parent is not None and community.id not in parent.children_ids:
                parent.children_ids.append(community.id)

        return hierarchy

    def clear(self) -> None:
        """Clear all data from storage."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM entity_sources")
            cursor.execute("DELETE FROM community_members")
            cursor.execute("DELETE FROM text_units")
            cursor.execute("DELETE FROM communities")
            cursor.execute("DELETE FROM relationships")
            cursor.execute("DELETE FROM entities")

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    def get_stats(self) -> Dict[str, int]:
        """Get storage statistics."""
        cursor = self._conn.cursor()
        stats = {}

        for table in ["entities", "relationships", "communities", "text_units"]:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cursor.fetchone()[0]

        return stats
