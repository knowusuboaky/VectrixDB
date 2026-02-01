"""
Parallel Batch Processor

Thread and process-based parallel processing for batch operations.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, Iterator, List, Optional, TypeVar
import numpy as np

T = TypeVar('T')


@dataclass
class BatchResult:
    """Result of a batch operation."""
    success_count: int = 0
    error_count: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0
    items_per_second: float = 0.0

    def __add__(self, other: "BatchResult") -> "BatchResult":
        """Combine two batch results."""
        return BatchResult(
            success_count=self.success_count + other.success_count,
            error_count=self.error_count + other.error_count,
            errors=self.errors + other.errors,
            duration_ms=self.duration_ms + other.duration_ms,
            items_per_second=0,  # Will be recalculated
        )

    def finalize(self, total_items: int, total_duration_ms: float) -> None:
        """Finalize metrics after processing."""
        self.duration_ms = total_duration_ms
        if total_duration_ms > 0:
            self.items_per_second = (total_items * 1000) / total_duration_ms


class ParallelBatchProcessor(Generic[T]):
    """
    Parallel batch processing with configurable concurrency.

    Uses ThreadPoolExecutor for I/O-bound tasks or ProcessPoolExecutor
    for CPU-bound tasks.

    Example:
        >>> processor = ParallelBatchProcessor(max_workers=4, batch_size=1000)
        >>> result = processor.process_batch(
        ...     items=vectors,
        ...     processor=lambda batch: collection.add(batch),
        ...     on_progress=lambda done, total: print(f"{done}/{total}")
        ... )
    """

    def __init__(
        self,
        max_workers: Optional[int] = None,
        use_processes: bool = False,
        batch_size: int = 1000,
    ):
        """
        Initialize parallel batch processor.

        Args:
            max_workers: Maximum concurrent workers (default: CPU count)
            use_processes: Use processes instead of threads (for CPU-bound)
            batch_size: Items per batch
        """
        self.max_workers = max_workers or os.cpu_count() or 4
        self.use_processes = use_processes
        self.batch_size = batch_size

    def _chunk_items(self, items: List[T]) -> Iterator[List[T]]:
        """Split items into batches."""
        for i in range(0, len(items), self.batch_size):
            yield items[i:i + self.batch_size]

    def process_batch(
        self,
        items: List[T],
        processor: Callable[[List[T]], BatchResult],
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> BatchResult:
        """
        Process items in parallel batches.

        Args:
            items: Items to process
            processor: Function to process a batch, returns BatchResult
            on_progress: Optional progress callback (done, total)

        Returns:
            Combined BatchResult
        """
        if not items:
            return BatchResult()

        start_time = time.perf_counter()
        total_items = len(items)
        batches = list(self._chunk_items(items))
        total_batches = len(batches)

        # Choose executor type
        executor_class = ProcessPoolExecutor if self.use_processes else ThreadPoolExecutor

        combined_result = BatchResult()
        completed_batches = 0

        with executor_class(max_workers=self.max_workers) as executor:
            # Submit all batches
            futures = {
                executor.submit(processor, batch): i
                for i, batch in enumerate(batches)
            }

            # Process results as they complete
            for future in as_completed(futures):
                try:
                    batch_result = future.result()
                    combined_result = combined_result + batch_result
                except Exception as e:
                    # Handle batch failure
                    batch_idx = futures[future]
                    batch_size = len(batches[batch_idx])
                    combined_result.error_count += batch_size
                    combined_result.errors.append({
                        "batch": batch_idx,
                        "error": str(e),
                        "type": type(e).__name__,
                    })

                completed_batches += 1

                if on_progress:
                    processed = min(completed_batches * self.batch_size, total_items)
                    on_progress(processed, total_items)

        # Finalize metrics
        duration_ms = (time.perf_counter() - start_time) * 1000
        combined_result.finalize(total_items, duration_ms)

        return combined_result

    def map_parallel(
        self,
        items: List[T],
        func: Callable[[T], Any],
    ) -> List[Any]:
        """
        Map function over items in parallel.

        Args:
            items: Items to process
            func: Function to apply to each item

        Returns:
            List of results in order
        """
        executor_class = ProcessPoolExecutor if self.use_processes else ThreadPoolExecutor

        with executor_class(max_workers=self.max_workers) as executor:
            results = list(executor.map(func, items))

        return results


class ParallelVectorInserter:
    """
    Specialized parallel inserter for vectors.

    Optimizations:
    - Pre-allocates numpy arrays
    - Batches database operations
    - Progress tracking
    """

    def __init__(
        self,
        add_func: Callable,
        batch_size: int = 1000,
        max_workers: int = 4,
    ):
        """
        Initialize parallel vector inserter.

        Args:
            add_func: Function to add vectors (ids, vectors, metadata) -> BatchResult
            batch_size: Vectors per batch
            max_workers: Number of parallel workers
        """
        self.add_func = add_func
        self.batch_size = batch_size
        self.max_workers = max_workers

    def insert(
        self,
        ids: List[str],
        vectors: np.ndarray,
        metadata: Optional[List[Dict]] = None,
        texts: Optional[List[str]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> BatchResult:
        """
        Insert vectors with parallel processing.

        Args:
            ids: Vector IDs
            vectors: Vector data, shape (n, dimension)
            metadata: Optional metadata per vector
            texts: Optional text per vector
            on_progress: Progress callback

        Returns:
            BatchResult
        """
        vectors = np.asarray(vectors, dtype=np.float32)
        n_vectors = len(vectors)

        if len(ids) != n_vectors:
            raise ValueError(f"IDs ({len(ids)}) must match vectors ({n_vectors})")

        if metadata is not None and len(metadata) != n_vectors:
            raise ValueError(f"Metadata ({len(metadata)}) must match vectors ({n_vectors})")

        if texts is not None and len(texts) != n_vectors:
            raise ValueError(f"Texts ({len(texts)}) must match vectors ({n_vectors})")

        start_time = time.perf_counter()

        # Create batches
        batches = []
        for i in range(0, n_vectors, self.batch_size):
            end = min(i + self.batch_size, n_vectors)
            batch = {
                "ids": ids[i:end],
                "vectors": vectors[i:end],
                "metadata": metadata[i:end] if metadata else None,
                "texts": texts[i:end] if texts else None,
            }
            batches.append(batch)

        # Process batches
        combined_result = BatchResult()
        completed = 0

        # Use ThreadPoolExecutor for I/O-bound database operations
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._process_batch, batch): i
                for i, batch in enumerate(batches)
            }

            for future in as_completed(futures):
                try:
                    batch_result = future.result()
                    combined_result = combined_result + batch_result
                except Exception as e:
                    batch_idx = futures[future]
                    batch_size = len(batches[batch_idx]["ids"])
                    combined_result.error_count += batch_size
                    combined_result.errors.append({
                        "batch": batch_idx,
                        "error": str(e),
                    })

                completed += 1
                if on_progress:
                    processed = min(completed * self.batch_size, n_vectors)
                    on_progress(processed, n_vectors)

        duration_ms = (time.perf_counter() - start_time) * 1000
        combined_result.finalize(n_vectors, duration_ms)

        return combined_result

    def _process_batch(self, batch: Dict) -> BatchResult:
        """Process a single batch."""
        try:
            # Call the add function
            result = self.add_func(
                ids=batch["ids"],
                vectors=batch["vectors"],
                metadata=batch["metadata"],
                texts=batch["texts"],
            )

            if isinstance(result, BatchResult):
                return result

            # Assume success if no result returned
            return BatchResult(success_count=len(batch["ids"]))

        except Exception as e:
            return BatchResult(
                error_count=len(batch["ids"]),
                errors=[{"error": str(e)}]
            )
