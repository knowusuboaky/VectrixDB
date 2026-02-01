"""
Payload Index Manager

Manages all payload indexes for a collection.
Provides unified interface for creating, querying, and persisting indexes.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Type

from .base import BasePayloadIndex
from .numeric import NumericRangeIndex
from .string import StringIndex
from .tag import TagIndex
from .geo import GeoIndex


class PayloadIndexManager:
    """
    Manages payload indexes for a collection.

    Automatically creates and maintains indexes on specified fields.
    Supports combined queries across multiple indexes.

    Example:
        >>> manager = PayloadIndexManager(path=Path("./indexes"))
        >>> manager.create_index("price", "numeric")
        >>> manager.create_index("tags", "tag")
        >>> manager.index_document("doc1", {"price": 99.99, "tags": ["sale", "new"]})
        >>> candidates = manager.query({"price": {"$lt": 100}, "tags": {"$in": ["sale"]}})
    """

    # Index type mapping
    INDEX_TYPES: Dict[str, Type[BasePayloadIndex]] = {
        "numeric": NumericRangeIndex,
        "string": StringIndex,
        "tag": TagIndex,
        "geo": GeoIndex,
    }

    def __init__(self, path: Optional[Path] = None):
        """
        Initialize payload index manager.

        Args:
            path: Directory for persisting indexes
        """
        self.path = Path(path) if path else None

        # Active indexes: field_name -> index
        self._indexes: Dict[str, BasePayloadIndex] = {}

        # Field type hints for auto-detection
        self._field_types: Dict[str, str] = {}

        # Load existing indexes if path exists
        if self.path and self.path.exists():
            self._load_config()

    def _detect_type(self, value: Any) -> str:
        """Auto-detect index type from value."""
        if value is None:
            return "string"

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return "numeric"

        if isinstance(value, (list, tuple, set)):
            return "tag"

        if isinstance(value, dict):
            # Check for geo coordinates
            if any(k in value for k in ["lat", "latitude", "lon", "lng", "longitude"]):
                return "geo"
            return "string"

        return "string"

    def create_index(
        self,
        field: str,
        index_type: str = "auto",
        **kwargs
    ) -> BasePayloadIndex:
        """
        Create an index on a field.

        Args:
            field: Field name (supports dot notation for nested fields)
            index_type: "numeric", "string", "tag", "geo", or "auto"
            **kwargs: Additional arguments for specific index types

        Returns:
            Created index instance
        """
        if field in self._indexes:
            return self._indexes[field]

        # Auto-detect type if needed
        if index_type == "auto":
            index_type = self._field_types.get(field, "string")

        if index_type not in self.INDEX_TYPES:
            raise ValueError(
                f"Unknown index type: {index_type}. "
                f"Available: {list(self.INDEX_TYPES.keys())}"
            )

        # Create index
        index_class = self.INDEX_TYPES[index_type]
        index = index_class(field, **kwargs)

        self._indexes[field] = index
        self._field_types[field] = index_type

        return index

    def drop_index(self, field: str) -> bool:
        """
        Drop an index on a field.

        Args:
            field: Field name

        Returns:
            True if index was found and dropped
        """
        if field not in self._indexes:
            return False

        del self._indexes[field]
        self._field_types.pop(field, None)

        # Delete index file if persisted
        if self.path:
            index_file = self.path / f"{field}.idx"
            if index_file.exists():
                index_file.unlink()

        return True

    def has_index(self, field: str) -> bool:
        """Check if an index exists for a field."""
        return field in self._indexes

    def get_index(self, field: str) -> Optional[BasePayloadIndex]:
        """Get index for a field."""
        return self._indexes.get(field)

    def _get_nested_value(self, data: Dict[str, Any], field: str) -> Any:
        """Get value from nested dict using dot notation."""
        parts = field.split(".")
        value = data

        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None

            if value is None:
                return None

        return value

    def index_document(self, doc_id: str, metadata: Dict[str, Any]) -> None:
        """
        Index all fields of a document.

        Args:
            doc_id: Document ID
            metadata: Document metadata
        """
        for field, index in self._indexes.items():
            value = self._get_nested_value(metadata, field)

            if value is not None:
                # Update type hint based on actual value
                if field not in self._field_types or self._field_types[field] == "auto":
                    self._field_types[field] = self._detect_type(value)

                index.add(doc_id, value)

    def remove_document(self, doc_id: str) -> None:
        """
        Remove a document from all indexes.

        Args:
            doc_id: Document ID
        """
        for index in self._indexes.values():
            index.remove(doc_id)

    def update_document(self, doc_id: str, metadata: Dict[str, Any]) -> None:
        """
        Update a document in all indexes.

        Args:
            doc_id: Document ID
            metadata: New metadata
        """
        for field, index in self._indexes.items():
            value = self._get_nested_value(metadata, field)
            if value is not None:
                index.update(doc_id, value)
            else:
                index.remove(doc_id)

    def query(self, filter_dict: Dict[str, Any]) -> Optional[Set[str]]:
        """
        Query indexes to get candidate document IDs.

        Supports MongoDB-style query operators:
        - $eq, $ne: Equality
        - $gt, $gte, $lt, $lte: Comparison
        - $in, $nin: Array membership
        - $all, $any: Array operations
        - $contains, $starts_with, $ends_with: String operations
        - $geo_radius, $geo_box: Geo queries

        Args:
            filter_dict: Query filter, e.g., {"price": {"$lt": 100}, "category": "electronics"}

        Returns:
            Set of matching document IDs, or None if no indexes can satisfy the query
        """
        if not filter_dict:
            return None

        results = []

        for field, condition in filter_dict.items():
            if field not in self._indexes:
                # No index for this field, can't use it
                continue

            index = self._indexes[field]

            # Parse condition
            if isinstance(condition, dict):
                for operator, value in condition.items():
                    # Convert MongoDB-style operators
                    op = operator.lstrip("$")
                    try:
                        field_results = index.query(op, value)
                        results.append(field_results)
                    except ValueError:
                        # Unsupported operator for this index type
                        continue
            else:
                # Simple equality
                try:
                    field_results = index.query("eq", condition)
                    results.append(field_results)
                except ValueError:
                    continue

        if not results:
            return None

        # Intersect all results
        final_result = results[0]
        for result in results[1:]:
            final_result &= result

        return final_result

    def list_indexes(self) -> List[Dict[str, Any]]:
        """List all indexes with statistics."""
        return [
            {
                "field": field,
                "type": self._field_types.get(field, "unknown"),
                **index.get_stats()
            }
            for field, index in self._indexes.items()
        ]

    def _save_config(self) -> None:
        """Save index configuration."""
        if not self.path:
            return

        self.path.mkdir(parents=True, exist_ok=True)

        config = {
            "indexes": list(self._indexes.keys()),
            "field_types": self._field_types,
        }

        with open(self.path / "config.json", "w") as f:
            json.dump(config, f, indent=2)

    def _load_config(self) -> None:
        """Load index configuration."""
        config_path = self.path / "config.json"
        if not config_path.exists():
            return

        with open(config_path, "r") as f:
            config = json.load(f)

        self._field_types = config.get("field_types", {})

        # Recreate indexes and load data
        for field in config.get("indexes", []):
            index_type = self._field_types.get(field, "string")
            self.create_index(field, index_type)

            # Load index data
            index_file = self.path / f"{field}.idx"
            if index_file.exists():
                self._indexes[field].load(index_file)

    def save(self) -> None:
        """Persist all indexes to disk."""
        if not self.path:
            return

        self.path.mkdir(parents=True, exist_ok=True)

        # Save config
        self._save_config()

        # Save each index
        for field, index in self._indexes.items():
            index_file = self.path / f"{field}.idx"
            index.save(index_file)

    def load(self) -> "PayloadIndexManager":
        """Load all indexes from disk."""
        if self.path:
            self._load_config()
        return self

    def get_stats(self) -> Dict[str, Any]:
        """Get overall statistics."""
        return {
            "total_indexes": len(self._indexes),
            "indexes": self.list_indexes(),
        }

    def __repr__(self) -> str:
        return f"PayloadIndexManager(indexes={list(self._indexes.keys())})"
