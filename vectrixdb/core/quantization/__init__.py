"""
VectrixDB Quantization Module

Provides vector quantization for memory-efficient storage:
- ScalarQuantizer: 8-bit quantization (4x compression)
- BinaryQuantizer: 1-bit quantization (32x compression)
- ProductQuantizer: Codebook-based quantization (configurable compression)
"""

from .base import BaseQuantizer, QuantizationConfig, QuantizationType
from .scalar import ScalarQuantizer
from .binary import BinaryQuantizer
from .product import ProductQuantizer

__all__ = [
    "BaseQuantizer",
    "QuantizationConfig",
    "QuantizationType",
    "ScalarQuantizer",
    "BinaryQuantizer",
    "ProductQuantizer",
]
