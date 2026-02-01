"""
Numeric Range Index

Sorted index for numeric fields supporting efficient range queries.
Uses bisect for O(log n) lookups.
"""

import bisect
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import pickle

from .base import BasePayloadIndex


class NumericRangeIndex(BasePayloadIndex):
    """
    Sorted index for numeric fields supporting range queries.

    Uses a sorted list of (value, doc_id) tuples with binary search
    for efficient range queries.

    Supports operators: eq, ne, gt, gte, lt, lte, between

    Example:
        >>> index = NumericRangeIndex("price")
        >>> index.add("doc1", 99.99)
        >>> index.add("doc2", 149.99)
        >>> index.query("lt", 100)  # Returns {"doc1"}
        >>> index.query("between", (100, 200))  # Returns {"doc2"}
    """

    def __init__(self, field_name: str):
        """
        Initialize numeric range index.

        Args:
            field_name: Name of the numeric field
        """
        super().__init__(field_name)

        # Sorted list of (value, doc_id) tuples
        self._entries: List[Tuple[float, str]] = []

        # Fast lookup: doc_id -> value
        self._doc_to_value: Dict[str, float] = {}

    @property
    def index_type(self) -> str:
        return "numeric"

    def add(self, doc_id: str, value: Any) -> None:
        """
        Add a numeric value to the index.

        Args:
            doc_id: Document ID
            value: Numeric value (int or float)
        """
        if value is None:
            return

        # Convert to float
        try:
            value = float(value)
        except (TypeError, ValueError):
            return

        # Remove if already exists
        if doc_id in self._doc_to_value:
            self.remove(doc_id)

        # Insert in sorted order
        entry = (value, doc_id)
        bisect.insort(self._entries, entry)

        self._doc_to_value[doc_id] = value
        self._count += 1

    def remove(self, doc_id: str) -> bool:
        """Remove document from index."""
        if doc_id not in self._doc_to_value:
            return False

        value = self._doc_to_value[doc_id]

        # Find and remove from sorted list
        # Binary search to find position
        idx = bisect.bisect_left(self._entries, (value, ""))
        while idx < len(self._entries):
            if self._entries[idx][0] != value:
                break
            if self._entries[idx][1] == doc_id:
                self._entries.pop(idx)
                break
            idx += 1

        del self._doc_to_value[doc_id]
        self._count -= 1
        return True

    def update(self, doc_id: str, value: Any) -> None:
        """Update indexed value."""
        self.remove(doc_id)
        self.add(doc_id, value)

    def query(self, operator: str, value: Any) -> Set[str]:
        """
        Query index with given operator.

        Args:
            operator: eq, ne, gt, gte, lt, lte, between
            value: Value or (min, max) tuple for between

        Returns:
            Set of matching document IDs
        """
        if operator == "eq":
            return self._query_eq(value)
        elif operator == "ne":
            return self._query_ne(value)
        elif operator == "gt":
            return self._query_gt(value)
        elif operator == "gte":
            return self._query_gte(value)
        elif operator == "lt":
            return self._query_lt(value)
        elif operator == "lte":
            return self._query_lte(value)
        elif operator == "between":
            if isinstance(value, (list, tuple)) and len(value) == 2:
                return self._query_range(value[0], value[1])
            raise ValueError("between operator requires (min, max) tuple")
        else:
            raise ValueError(f"Unknown operator: {operator}")

    def _query_eq(self, value: float) -> Set[str]:
        """Find documents with exact value."""
        value = float(value)
        result = set()

        # Binary search to find first occurrence
        idx = bisect.bisect_left(self._entries, (value, ""))

        # Collect all entries with this value
        while idx < len(self._entries) and self._entries[idx][0] == value:
            result.add(self._entries[idx][1])
            idx += 1

        return result

    def _query_ne(self, value: float) -> Set[str]:
        """Find documents with different value."""
        value = float(value)
        return set(
            doc_id for doc_id, v in self._doc_to_value.items()
            if v != value
        )

    def _query_gt(self, value: float) -> Set[str]:
        """Find documents with value > threshold."""
        value = float(value)
        result = set()

        # Find first entry > value
        idx = bisect.bisect_right(self._entries, (value, "\xff"))

        # Collect all entries after this point
        for i in range(idx, len(self._entries)):
            result.add(self._entries[i][1])

        return result

    def _query_gte(self, value: float) -> Set[str]:
        """Find documents with value >= threshold."""
        value = float(value)
        result = set()

        # Find first entry >= value
        idx = bisect.bisect_left(self._entries, (value, ""))

        # Collect all entries from this point
        for i in range(idx, len(self._entries)):
            result.add(self._entries[i][1])

        return result

    def _query_lt(self, value: float) -> Set[str]:
        """Find documents with value < threshold."""
        value = float(value)
        result = set()

        # Find first entry >= value
        idx = bisect.bisect_left(self._entries, (value, ""))

        # Collect all entries before this point
        for i in range(idx):
            result.add(self._entries[i][1])

        return result

    def _query_lte(self, value: float) -> Set[str]:
        """Find documents with value <= threshold."""
        value = float(value)
        result = set()

        # Find first entry > value
        idx = bisect.bisect_right(self._entries, (value, "\xff"))

        # Collect all entries before this point
        for i in range(idx):
            result.add(self._entries[i][1])

        return result

    def _query_range(
        self,
        min_val: float,
        max_val: float,
        min_inclusive: bool = True,
        max_inclusive: bool = True,
    ) -> Set[str]:
        """Find documents with value in range."""
        min_val = float(min_val)
        max_val = float(max_val)
        result = set()

        # Find start index
        if min_inclusive:
            start_idx = bisect.bisect_left(self._entries, (min_val, ""))
        else:
            start_idx = bisect.bisect_right(self._entries, (min_val, "\xff"))

        # Find end index
        if max_inclusive:
            end_idx = bisect.bisect_right(self._entries, (max_val, "\xff"))
        else:
            end_idx = bisect.bisect_left(self._entries, (max_val, ""))

        # Collect entries in range
        for i in range(start_idx, end_idx):
            result.add(self._entries[i][1])

        return result

    def get_min(self) -> Optional[float]:
        """Get minimum value in index."""
        if not self._entries:
            return None
        return self._entries[0][0]

    def get_max(self) -> Optional[float]:
        """Get maximum value in index."""
        if not self._entries:
            return None
        return self._entries[-1][0]

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        stats = super().get_stats()
        if self._entries:
            values = [e[0] for e in self._entries]
            stats.update({
                "min": min(values),
                "max": max(values),
                "unique_values": len(set(values)),
            })
        return stats

    def save(self, path: Path) -> None:
        """Save index to disk."""
        path = Path(path)
        data = {
            "field_name": self.field_name,
            "entries": self._entries,
            "doc_to_value": self._doc_to_value,
            "count": self._count,
        }
        with open(path, "wb") as f:
            pickle.dump(data, f)

    def load(self, path: Path) -> "NumericRangeIndex":
        """Load index from disk."""
        path = Path(path)
        with open(path, "rb") as f:
            data = pickle.load(f)

        self.field_name = data["field_name"]
        self._entries = data["entries"]
        self._doc_to_value = data["doc_to_value"]
        self._count = data["count"]

        return self
