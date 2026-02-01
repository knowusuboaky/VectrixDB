"""
SQLite Storage for VectrixDB GraphRAG Knowledge Graph.

Provides persistent storage for entities, relationships, communities,
and text units with efficient querying support.
"""

import os
import json
import sqlite3
import threading
from typing import Dict, List, Optional, Any, Tuple
from contextlib import contextmanager

from ..extractor.base import Entity, Relationship


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
        if not hasattr(self._local, 'conn') or self._local.conn is None:
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
        os.makedirs(os.path.dirname(self.path) if os.path.dirname(self.path) else '.', exist_ok=True)

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
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_relationships_type ON relationships(type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_communities_level ON communities(level)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_text_units_doc ON text_units(doc_id)")

    # ========== Entity Operations ==========

    def save_entity(self, entity: Entity) -> None:
        """Save or update an entity."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO entities
                (id, name, type, description, importance, source_units, aliases, attributes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                entity.id,
                entity.name,
                entity.type,
                entity.description,
                entity.importance,
                json.dumps(entity.source_units),
                json.dumps(list(entity.aliases)),
                json.dumps(entity.attributes),
            ))

    def save_entities(self, entities: List[Entity]) -> None:
        """Save multiple entities in batch."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO entities
                (id, name, type, description, importance, source_units, aliases, attributes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, [
                (
                    e.id, e.name, e.type, e.description, e.importance,
                    json.dumps(e.source_units), json.dumps(list(e.aliases)),
                    json.dumps(e.attributes)
                )
                for e in entities
            ])

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
            id=row['id'],
            name=row['name'],
            type=row['type'],
            description=row['description'] or "",
            importance=row['importance'] or 0.0,
            source_units=json.loads(row['source_units'] or "[]"),
            aliases=set(json.loads(row['aliases'] or "[]")),
            attributes=json.loads(row['attributes'] or "{}"),
        )

    def delete_entity(self, entity_id: str) -> bool:
        """Delete an entity."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
            return cursor.rowcount > 0

    # ========== Relationship Operations ==========

    def save_relationship(self, relationship: Relationship) -> None:
        """Save or update a relationship."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO relationships
                (id, source_id, target_id, type, description, strength, source_units, bidirectional, attributes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                relationship.id,
                relationship.source_id,
                relationship.target_id,
                relationship.type,
                relationship.description,
                relationship.strength,
                json.dumps(relationship.source_units),
                1 if relationship.bidirectional else 0,
                json.dumps(relationship.attributes),
            ))

    def save_relationships(self, relationships: List[Relationship]) -> None:
        """Save multiple relationships in batch."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO relationships
                (id, source_id, target_id, type, description, strength, source_units, bidirectional, attributes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, [
                (
                    r.id, r.source_id, r.target_id, r.type, r.description,
                    r.strength, json.dumps(r.source_units),
                    1 if r.bidirectional else 0, json.dumps(r.attributes)
                )
                for r in relationships
            ])

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
            id=row['id'],
            source_id=row['source_id'],
            target_id=row['target_id'],
            type=row['type'],
            description=row['description'] or "",
            strength=row['strength'] or 0.5,
            source_units=json.loads(row['source_units'] or "[]"),
            bidirectional=bool(row['bidirectional']),
            attributes=json.loads(row['attributes'] or "{}"),
        )

    def load_relationships_for_entity(self, entity_id: str) -> List[Relationship]:
        """Load all relationships involving an entity."""
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT * FROM relationships
            WHERE source_id = ? OR target_id = ?
        """, (entity_id, entity_id))
        return [self._row_to_relationship(row) for row in cursor.fetchall()]

    # ========== Community Operations ==========

    def save_community(
        self,
        community_id: str,
        level: int,
        summary: str = "",
        importance: float = 0.0,
        parent_id: Optional[str] = None,
        entity_ids: Optional[List[str]] = None
    ) -> None:
        """Save a community and its members."""
        with self._transaction() as conn:
            cursor = conn.cursor()

            # Save community
            cursor.execute("""
                INSERT OR REPLACE INTO communities
                (id, level, summary, importance, parent_id)
                VALUES (?, ?, ?, ?, ?)
            """, (community_id, level, summary, importance, parent_id))

            # Save members
            if entity_ids:
                cursor.execute(
                    "DELETE FROM community_members WHERE community_id = ?",
                    (community_id,)
                )
                cursor.executemany("""
                    INSERT INTO community_members (community_id, entity_id)
                    VALUES (?, ?)
                """, [(community_id, eid) for eid in entity_ids])

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
                "SELECT entity_id FROM community_members WHERE community_id = ?",
                (row['id'],)
            )
            member_ids = [r['entity_id'] for r in cursor.fetchall()]

            communities.append({
                'id': row['id'],
                'level': row['level'],
                'summary': row['summary'],
                'importance': row['importance'],
                'parent_id': row['parent_id'],
                'entity_ids': member_ids,
            })

        return communities

    def update_community_summary(self, community_id: str, summary: str) -> None:
        """Update a community's summary."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE communities SET summary = ? WHERE id = ?",
                (summary, community_id)
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
        metadata: Optional[Dict] = None
    ) -> None:
        """Save a text unit."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO text_units
                (id, text, doc_id, position, token_count, char_start, char_end, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                unit_id, text, doc_id, position, token_count,
                char_start, char_end, json.dumps(metadata or {})
            ))

    def load_text_units_for_doc(self, doc_id: str) -> List[Dict[str, Any]]:
        """Load all text units for a document."""
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT * FROM text_units WHERE doc_id = ? ORDER BY position",
            (doc_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

    # ========== Graph-level Operations ==========

    def save_graph(self, graph: "KnowledgeGraph") -> None:
        """Save entire graph to storage."""
        from .knowledge_graph import KnowledgeGraph

        self.save_entities(graph.get_all_entities())
        self.save_relationships(graph.get_all_relationships())

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
        """Save community hierarchy - stores via save_community for each community."""
        if hierarchy is None:
            return
        # Communities are already saved during detection via save_community
        # This is a placeholder for any additional hierarchy metadata
        pass

    def load_hierarchy(self):
        """Load community hierarchy."""
        # Return None - communities can be loaded separately
        # This would require CommunityHierarchy reconstruction which is complex
        return None

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
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    def get_stats(self) -> Dict[str, int]:
        """Get storage statistics."""
        cursor = self._conn.cursor()
        stats = {}

        for table in ['entities', 'relationships', 'communities', 'text_units']:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cursor.fetchone()[0]

        return stats
