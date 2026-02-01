"""
String Index

Index for string fields supporting exact, prefix, and contains queries.
Uses hash index for exact match and trigram index for contains/fuzzy search.
"""

import pickle
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import BasePayloadIndex


class StringIndex(BasePayloadIndex):
    """
    Index for string fields supporting various string operations.

    Uses multiple internal indexes:
    - Hash index for exact match (O(1))
    - Trigram index for contains/fuzzy search

    Supports operators: eq, ne, contains, icontains, starts_with, ends_with, regex

    Example:
        >>> index = StringIndex("title")
        >>> index.add("doc1", "Machine Learning Basics")
        >>> index.query("contains", "learning")  # Returns {"doc1"}
        >>> index.query("starts_with", "Machine")  # Returns {"doc1"}
    """

    def __init__(self, field_name: str, case_sensitive: bool = False):
        """
        Initialize string index.

        Args:
            field_name: Name of the string field
            case_sensitive: Whether queries are case-sensitive
        """
        super().__init__(field_name)
        self.case_sensitive = case_sensitive

        # Exact match index: normalized_value -> set of doc_ids
        self._exact_index: Dict[str, Set[str]] = {}

        # Trigram index for contains: trigram -> set of doc_ids
        self._trigram_index: Dict[str, Set[str]] = {}

        # Prefix index: prefix -> set of doc_ids (first 3 chars)
        self._prefix_index: Dict[str, Set[str]] = {}

        # Forward index: doc_id -> original value
        self._doc_to_value: Dict[str, str] = {}

    @property
    def index_type(self) -> str:
        return "string"

    def _normalize(self, value: str) -> str:
        """Normalize string value."""
        if self.case_sensitive:
            return value
        return value.lower()

    def _get_trigrams(self, value: str) -> Set[str]:
        """Extract trigrams from string."""
        value = self._normalize(value)
        # Pad with spaces for edge trigrams
        padded = f"  {value}  "
        return {padded[i:i+3] for i in range(len(padded) - 2)}

    def add(self, doc_id: str, value: Any) -> None:
        """Add a string value to the index."""
        if value is None:
            return

        value = str(value)

        # Remove if already exists
        if doc_id in self._doc_to_value:
            self.remove(doc_id)

        normalized = self._normalize(value)

        # Exact index
        if normalized not in self._exact_index:
            self._exact_index[normalized] = set()
        self._exact_index[normalized].add(doc_id)

        # Trigram index
        for trigram in self._get_trigrams(value):
            if trigram not in self._trigram_index:
                self._trigram_index[trigram] = set()
            self._trigram_index[trigram].add(doc_id)

        # Prefix index (first 3 characters)
        if len(normalized) >= 3:
            prefix = normalized[:3]
            if prefix not in self._prefix_index:
                self._prefix_index[prefix] = set()
            self._prefix_index[prefix].add(doc_id)

        # Forward index
        self._doc_to_value[doc_id] = value
        self._count += 1

    def remove(self, doc_id: str) -> bool:
        """Remove document from index."""
        if doc_id not in self._doc_to_value:
            return False

        value = self._doc_to_value[doc_id]
        normalized = self._normalize(value)

        # Remove from exact index
        if normalized in self._exact_index:
            self._exact_index[normalized].discard(doc_id)
            if not self._exact_index[normalized]:
                del self._exact_index[normalized]

        # Remove from trigram index
        for trigram in self._get_trigrams(value):
            if trigram in self._trigram_index:
                self._trigram_index[trigram].discard(doc_id)
                if not self._trigram_index[trigram]:
                    del self._trigram_index[trigram]

        # Remove from prefix index
        if len(normalized) >= 3:
            prefix = normalized[:3]
            if prefix in self._prefix_index:
                self._prefix_index[prefix].discard(doc_id)
                if not self._prefix_index[prefix]:
                    del self._prefix_index[prefix]

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
            operator: eq, ne, contains, icontains, starts_with, ends_with, regex
            value: String value to match

        Returns:
            Set of matching document IDs
        """
        if operator == "eq":
            return self._query_eq(value)
        elif operator == "ne":
            return self._query_ne(value)
        elif operator == "contains":
            return self._query_contains(value, case_sensitive=self.case_sensitive)
        elif operator == "icontains":
            return self._query_contains(value, case_sensitive=False)
        elif operator == "starts_with":
            return self._query_starts_with(value)
        elif operator == "ends_with":
            return self._query_ends_with(value)
        elif operator == "regex":
            return self._query_regex(value)
        else:
            raise ValueError(f"Unknown operator: {operator}")

    def _query_eq(self, value: str) -> Set[str]:
        """Find documents with exact value."""
        normalized = self._normalize(str(value))
        return self._exact_index.get(normalized, set()).copy()

    def _query_ne(self, value: str) -> Set[str]:
        """Find documents with different value."""
        normalized = self._normalize(str(value))
        matching = self._exact_index.get(normalized, set())
        return set(self._doc_to_value.keys()) - matching

    def _query_contains(self, substring: str, case_sensitive: bool = False) -> Set[str]:
        """Find documents containing substring."""
        if not case_sensitive:
            substring = substring.lower()

        # Use trigram index to find candidates
        trigrams = self._get_trigrams(substring) if not case_sensitive else {
            f"  {substring}  "[i:i+3] for i in range(len(substring) + 1)
        }

        if not trigrams:
            return set(self._doc_to_value.keys())

        # Find intersection of all trigram matches
        candidates = None
        for trigram in trigrams:
            matches = self._trigram_index.get(trigram, set())
            if candidates is None:
                candidates = matches.copy()
            else:
                candidates &= matches
            if not candidates:
                return set()

        if candidates is None:
            return set()

        # Verify candidates (trigrams can have false positives)
        result = set()
        for doc_id in candidates:
            value = self._doc_to_value.get(doc_id, "")
            check_value = value if case_sensitive else value.lower()
            check_substring = substring if case_sensitive else substring.lower()
            if check_substring in check_value:
                result.add(doc_id)

        return result

    def _query_starts_with(self, prefix: str) -> Set[str]:
        """Find documents starting with prefix."""
        prefix = str(prefix)
        normalized_prefix = self._normalize(prefix)

        result = set()

        # If prefix is at least 3 chars, use prefix index
        if len(normalized_prefix) >= 3:
            candidates = self._prefix_index.get(normalized_prefix[:3], set())
            for doc_id in candidates:
                value = self._doc_to_value.get(doc_id, "")
                if self._normalize(value).startswith(normalized_prefix):
                    result.add(doc_id)
        else:
            # Scan all documents
            for doc_id, value in self._doc_to_value.items():
                if self._normalize(value).startswith(normalized_prefix):
                    result.add(doc_id)

        return result

    def _query_ends_with(self, suffix: str) -> Set[str]:
        """Find documents ending with suffix."""
        suffix = str(suffix)
        normalized_suffix = self._normalize(suffix)

        result = set()
        for doc_id, value in self._doc_to_value.items():
            if self._normalize(value).endswith(normalized_suffix):
                result.add(doc_id)

        return result

    def _query_regex(self, pattern: str) -> Set[str]:
        """Find documents matching regex pattern."""
        try:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            compiled = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}")

        result = set()
        for doc_id, value in self._doc_to_value.items():
            if compiled.search(value):
                result.add(doc_id)

        return result

    def get_unique_values(self) -> List[str]:
        """Get list of unique values."""
        return list(self._exact_index.keys())

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        stats = super().get_stats()
        stats.update({
            "unique_values": len(self._exact_index),
            "trigrams": len(self._trigram_index),
            "case_sensitive": self.case_sensitive,
        })
        return stats

    def save(self, path: Path) -> None:
        """Save index to disk."""
        path = Path(path)
        data = {
            "field_name": self.field_name,
            "case_sensitive": self.case_sensitive,
            "exact_index": {k: list(v) for k, v in self._exact_index.items()},
            "trigram_index": {k: list(v) for k, v in self._trigram_index.items()},
            "prefix_index": {k: list(v) for k, v in self._prefix_index.items()},
            "doc_to_value": self._doc_to_value,
            "count": self._count,
        }
        with open(path, "wb") as f:
            pickle.dump(data, f)

    def load(self, path: Path) -> "StringIndex":
        """Load index from disk."""
        path = Path(path)
        with open(path, "rb") as f:
            data = pickle.load(f)

        self.field_name = data["field_name"]
        self.case_sensitive = data["case_sensitive"]
        self._exact_index = {k: set(v) for k, v in data["exact_index"].items()}
        self._trigram_index = {k: set(v) for k, v in data["trigram_index"].items()}
        self._prefix_index = {k: set(v) for k, v in data["prefix_index"].items()}
        self._doc_to_value = data["doc_to_value"]
        self._count = data["count"]

        return self
