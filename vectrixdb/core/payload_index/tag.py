"""
Tag Index

Inverted index for array/tag fields supporting in, all, any queries.
"""

import pickle
from pathlib import Path
from typing import Any, Dict, List, Set, Union

from .base import BasePayloadIndex


class TagIndex(BasePayloadIndex):
    """
    Inverted index for array/tag fields.

    Efficiently finds documents containing specific tags or combinations.

    Supports operators: in, nin, all, any, eq (exact array match)

    Example:
        >>> index = TagIndex("tags")
        >>> index.add("doc1", ["python", "ml", "data"])
        >>> index.add("doc2", ["python", "web"])
        >>> index.query("any", ["ml", "web"])  # Returns {"doc1", "doc2"}
        >>> index.query("all", ["python", "ml"])  # Returns {"doc1"}
    """

    def __init__(self, field_name: str):
        """
        Initialize tag index.

        Args:
            field_name: Name of the array field
        """
        super().__init__(field_name)

        # Inverted index: tag -> set of doc_ids
        self._tag_to_docs: Dict[Any, Set[str]] = {}

        # Forward index: doc_id -> set of tags
        self._doc_to_tags: Dict[str, Set[Any]] = {}

    @property
    def index_type(self) -> str:
        return "tag"

    def _normalize_tags(self, value: Any) -> Set[Any]:
        """Convert value to set of tags."""
        if value is None:
            return set()

        if isinstance(value, (list, tuple, set)):
            return set(value)

        # Single value becomes a set
        return {value}

    def add(self, doc_id: str, value: Any) -> None:
        """Add tags to the index."""
        tags = self._normalize_tags(value)

        if not tags:
            return

        # Remove if already exists
        if doc_id in self._doc_to_tags:
            self.remove(doc_id)

        # Add to inverted index
        for tag in tags:
            if tag not in self._tag_to_docs:
                self._tag_to_docs[tag] = set()
            self._tag_to_docs[tag].add(doc_id)

        # Add to forward index
        self._doc_to_tags[doc_id] = tags
        self._count += 1

    def remove(self, doc_id: str) -> bool:
        """Remove document from index."""
        if doc_id not in self._doc_to_tags:
            return False

        tags = self._doc_to_tags[doc_id]

        # Remove from inverted index
        for tag in tags:
            if tag in self._tag_to_docs:
                self._tag_to_docs[tag].discard(doc_id)
                if not self._tag_to_docs[tag]:
                    del self._tag_to_docs[tag]

        del self._doc_to_tags[doc_id]
        self._count -= 1
        return True

    def update(self, doc_id: str, value: Any) -> None:
        """Update indexed tags."""
        self.remove(doc_id)
        self.add(doc_id, value)

    def query(self, operator: str, value: Any) -> Set[str]:
        """
        Query index with given operator.

        Args:
            operator: in, nin, all, any, eq
            value: Single tag or list of tags

        Returns:
            Set of matching document IDs
        """
        if operator == "in" or operator == "any":
            return self._query_any(value)
        elif operator == "nin":
            return self._query_nin(value)
        elif operator == "all":
            return self._query_all(value)
        elif operator == "eq":
            return self._query_eq(value)
        elif operator == "contains":
            # Single value contains
            return self._query_any([value])
        else:
            raise ValueError(f"Unknown operator: {operator}")

    def _query_any(self, tags: Any) -> Set[str]:
        """Find documents containing any of the tags."""
        tags = self._normalize_tags(tags)

        if not tags:
            return set()

        result = set()
        for tag in tags:
            result |= self._tag_to_docs.get(tag, set())

        return result

    def _query_nin(self, tags: Any) -> Set[str]:
        """Find documents NOT containing any of the tags."""
        tags = self._normalize_tags(tags)

        if not tags:
            return set(self._doc_to_tags.keys())

        # Get all documents that have any of the tags
        excluded = self._query_any(tags)

        # Return all documents except those
        return set(self._doc_to_tags.keys()) - excluded

    def _query_all(self, tags: Any) -> Set[str]:
        """Find documents containing ALL of the tags."""
        tags = self._normalize_tags(tags)

        if not tags:
            return set(self._doc_to_tags.keys())

        # Start with documents containing first tag
        tag_list = list(tags)
        result = self._tag_to_docs.get(tag_list[0], set()).copy()

        if not result:
            return set()

        # Intersect with documents containing each subsequent tag
        for tag in tag_list[1:]:
            result &= self._tag_to_docs.get(tag, set())
            if not result:
                return set()

        return result

    def _query_eq(self, tags: Any) -> Set[str]:
        """Find documents with exactly these tags (no more, no less)."""
        tags = self._normalize_tags(tags)

        result = set()
        for doc_id, doc_tags in self._doc_to_tags.items():
            if doc_tags == tags:
                result.add(doc_id)

        return result

    def get_all_tags(self) -> Set[Any]:
        """Get set of all unique tags."""
        return set(self._tag_to_docs.keys())

    def get_tag_counts(self) -> Dict[Any, int]:
        """Get count of documents per tag."""
        return {tag: len(docs) for tag, docs in self._tag_to_docs.items()}

    def get_tags_for_doc(self, doc_id: str) -> Set[Any]:
        """Get tags for a specific document."""
        return self._doc_to_tags.get(doc_id, set()).copy()

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        stats = super().get_stats()
        stats.update({
            "unique_tags": len(self._tag_to_docs),
            "avg_tags_per_doc": (
                sum(len(tags) for tags in self._doc_to_tags.values()) / self._count
                if self._count > 0 else 0
            ),
        })
        return stats

    def save(self, path: Path) -> None:
        """Save index to disk."""
        path = Path(path)
        data = {
            "field_name": self.field_name,
            "tag_to_docs": {str(k): list(v) for k, v in self._tag_to_docs.items()},
            "doc_to_tags": {k: list(v) for k, v in self._doc_to_tags.items()},
            "count": self._count,
        }
        with open(path, "wb") as f:
            pickle.dump(data, f)

    def load(self, path: Path) -> "TagIndex":
        """Load index from disk."""
        path = Path(path)
        with open(path, "rb") as f:
            data = pickle.load(f)

        self.field_name = data["field_name"]
        self._tag_to_docs = {k: set(v) for k, v in data["tag_to_docs"].items()}
        self._doc_to_tags = {k: set(v) for k, v in data["doc_to_tags"].items()}
        self._count = data["count"]

        return self
