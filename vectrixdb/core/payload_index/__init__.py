"""
VectrixDB Payload Index Module

Provides fast metadata filtering through specialized indexes:
- NumericRangeIndex: For numeric fields with range queries
- StringIndex: For string fields with exact/prefix/contains queries
- TagIndex: For array fields with in/all/any queries
- GeoIndex: For location fields with radius/box queries
- PayloadIndexManager: Manages all indexes for a collection
"""

from .base import BasePayloadIndex
from .numeric import NumericRangeIndex
from .string import StringIndex
from .tag import TagIndex
from .geo import GeoIndex
from .manager import PayloadIndexManager

__all__ = [
    "BasePayloadIndex",
    "NumericRangeIndex",
    "StringIndex",
    "TagIndex",
    "GeoIndex",
    "PayloadIndexManager",
]
