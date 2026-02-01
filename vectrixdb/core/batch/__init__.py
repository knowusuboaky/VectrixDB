"""
VectrixDB Batch Operations Module

Provides optimized batch processing for large-scale operations:
- ParallelBatchProcessor: Thread/process-based parallel processing
- StreamingBatchProcessor: Memory-efficient streaming for large files
- MemoryEfficientBatcher: Memory-mapped operations for huge datasets
"""

from .parallel import ParallelBatchProcessor, ParallelVectorInserter, BatchResult
from .streaming import StreamingBatchProcessor, StreamingReader
from .memory import MemoryEfficientBatcher, LargeDatasetProcessor

__all__ = [
    "ParallelBatchProcessor",
    "ParallelVectorInserter",
    "BatchResult",
    "StreamingBatchProcessor",
    "StreamingReader",
    "MemoryEfficientBatcher",
    "LargeDatasetProcessor",
]
