"""
Base Payload Index Abstract Class

Defines the interface for all payload indexes in VectrixDB.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional, Set


class BasePayloadIndex(ABC):
    """
    Abstract base class for payload indexes.

    All payload indexes must implement:
    - add(): Index a value for a document
    - remove(): Remove document from index
    - update(): Update indexed value
    - query(): Query index and return matching document IDs
    """

    def __init__(self, field_name: str):
        """
        Initialize payload index.

        Args:
            field_name: Name of the field being indexed
        """
        self.field_name = field_name
        self._count = 0

    @property
    def count(self) -> int:
        """Number of indexed documents."""
        return self._count

    @property
    @abstractmethod
    def index_type(self) -> str:
        """Type of index (numeric, string, tag, geo, etc.)."""
        pass

    @abstractmethod
    def add(self, doc_id: str, value: Any) -> None:
        """
        Index a value for a document.

        Args:
            doc_id: Document ID
            value: Value to index
        """
        pass

    @abstractmethod
    def remove(self, doc_id: str) -> bool:
        """
        Remove document from index.

        Args:
            doc_id: Document ID to remove

        Returns:
            True if document was found and removed
        """
        pass

    @abstractmethod
    def update(self, doc_id: str, value: Any) -> None:
        """
        Update indexed value for a document.

        Args:
            doc_id: Document ID
            value: New value
        """
        pass

    @abstractmethod
    def query(self, operator: str, value: Any) -> Set[str]:
        """
        Query index and return matching document IDs.

        Args:
            operator: Query operator (eq, ne, gt, gte, lt, lte, contains, etc.)
            value: Value to compare against

        Returns:
            Set of matching document IDs
        """
        pass

    @abstractmethod
    def save(self, path: Path) -> None:
        """
        Persist index to disk.

        Args:
            path: File path to save to
        """
        pass

    @abstractmethod
    def load(self, path: Path) -> "BasePayloadIndex":
        """
        Load index from disk.

        Args:
            path: File path to load from

        Returns:
            self for method chaining
        """
        pass

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        return {
            "field_name": self.field_name,
            "index_type": self.index_type,
            "count": self._count,
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(field={self.field_name}, count={self._count})"
