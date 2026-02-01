"""
Memory-Efficient Batcher

Memory-mapped operations for processing datasets larger than RAM.
"""

import os
import tempfile
from pathlib import Path
from typing import Iterator, Optional, Tuple
import numpy as np


class MemoryEfficientBatcher:
    """
    Memory-efficient batch handling for very large datasets.

    Uses memory-mapped files for processing data larger than available RAM.

    Example:
        >>> batcher = MemoryEfficientBatcher(max_memory_mb=1024)
        >>> vectors_mmap = batcher.create_vector_mmap(1000000, 384)
        >>> for i, chunk in enumerate(batcher.chunk_vectors(vectors, chunk_size_mb=100)):
        ...     process_chunk(chunk)
    """

    def __init__(
        self,
        max_memory_mb: int = 1024,
        temp_dir: Optional[Path] = None,
    ):
        """
        Initialize memory-efficient batcher.

        Args:
            max_memory_mb: Maximum memory to use (MB)
            temp_dir: Directory for temporary files
        """
        self.max_memory_mb = max_memory_mb
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
        self._temp_files = []

    def estimate_memory(
        self,
        n_vectors: int,
        dimension: int,
        dtype: str = "float32",
        include_metadata: bool = True,
        avg_metadata_size: int = 200,
    ) -> int:
        """
        Estimate memory usage in bytes.

        Args:
            n_vectors: Number of vectors
            dimension: Vector dimension
            dtype: Data type
            include_metadata: Include estimated metadata size
            avg_metadata_size: Average metadata size per vector (bytes)

        Returns:
            Estimated memory in bytes
        """
        dtype_sizes = {
            "float32": 4,
            "float64": 8,
            "float16": 2,
            "int32": 4,
            "int64": 8,
            "uint8": 1,
        }

        dtype_size = dtype_sizes.get(dtype, 4)
        vector_bytes = n_vectors * dimension * dtype_size

        if include_metadata:
            metadata_bytes = n_vectors * avg_metadata_size
            return vector_bytes + metadata_bytes

        return vector_bytes

    def create_vector_mmap(
        self,
        n_vectors: int,
        dimension: int,
        dtype: str = "float32",
        filename: Optional[str] = None,
    ) -> np.memmap:
        """
        Create memory-mapped array for vectors.

        Args:
            n_vectors: Number of vectors
            dimension: Vector dimension
            dtype: Data type
            filename: Optional filename (auto-generated if None)

        Returns:
            Memory-mapped numpy array
        """
        if filename is None:
            filename = f"vectrix_mmap_{os.getpid()}_{len(self._temp_files)}.npy"

        filepath = self.temp_dir / filename
        self._temp_files.append(filepath)

        # Create memory-mapped file
        mmap = np.memmap(
            filepath,
            dtype=dtype,
            mode='w+',
            shape=(n_vectors, dimension),
        )

        return mmap

    def load_vectors_mmap(
        self,
        path: Path,
        dtype: str = "float32",
    ) -> np.memmap:
        """
        Load vectors as memory-mapped array.

        Args:
            path: Path to .npy file
            dtype: Expected data type

        Returns:
            Memory-mapped numpy array
        """
        return np.load(path, mmap_mode='r')

    def chunk_vectors(
        self,
        vectors: np.ndarray,
        chunk_size_mb: int = 100,
    ) -> Iterator[np.ndarray]:
        """
        Yield vector chunks that fit in memory.

        Args:
            vectors: Input vectors
            chunk_size_mb: Target chunk size in MB

        Yields:
            Vector chunks
        """
        chunk_size_bytes = chunk_size_mb * 1024 * 1024
        bytes_per_vector = vectors.itemsize * vectors.shape[1]
        vectors_per_chunk = max(1, chunk_size_bytes // bytes_per_vector)

        for i in range(0, len(vectors), vectors_per_chunk):
            yield vectors[i:i + vectors_per_chunk]

    def chunk_by_count(
        self,
        vectors: np.ndarray,
        chunk_size: int = 10000,
    ) -> Iterator[np.ndarray]:
        """
        Yield vector chunks by count.

        Args:
            vectors: Input vectors
            chunk_size: Vectors per chunk

        Yields:
            Vector chunks
        """
        for i in range(0, len(vectors), chunk_size):
            yield vectors[i:i + chunk_size]

    def optimal_chunk_size(
        self,
        dimension: int,
        dtype: str = "float32",
        target_memory_mb: Optional[int] = None,
    ) -> int:
        """
        Calculate optimal chunk size for given parameters.

        Args:
            dimension: Vector dimension
            dtype: Data type
            target_memory_mb: Target memory per chunk (default: max_memory_mb / 4)

        Returns:
            Optimal number of vectors per chunk
        """
        if target_memory_mb is None:
            target_memory_mb = self.max_memory_mb // 4

        target_bytes = target_memory_mb * 1024 * 1024

        dtype_sizes = {"float32": 4, "float64": 8, "float16": 2, "uint8": 1}
        dtype_size = dtype_sizes.get(dtype, 4)

        bytes_per_vector = dimension * dtype_size

        return max(1, target_bytes // bytes_per_vector)

    def merge_chunks(
        self,
        chunk_paths: list,
        output_path: Path,
        delete_chunks: bool = True,
    ) -> np.ndarray:
        """
        Merge multiple chunk files into one.

        Args:
            chunk_paths: Paths to chunk files
            output_path: Output file path
            delete_chunks: Delete chunk files after merge

        Returns:
            Merged array (memory-mapped)
        """
        # Load first chunk to get shape
        first = np.load(chunk_paths[0], mmap_mode='r')
        dimension = first.shape[1] if first.ndim > 1 else first.shape[0]
        dtype = first.dtype

        # Calculate total size
        total_vectors = sum(
            len(np.load(p, mmap_mode='r'))
            for p in chunk_paths
        )

        # Create output mmap
        output = np.memmap(
            output_path,
            dtype=dtype,
            mode='w+',
            shape=(total_vectors, dimension),
        )

        # Copy chunks
        offset = 0
        for chunk_path in chunk_paths:
            chunk = np.load(chunk_path, mmap_mode='r')
            output[offset:offset + len(chunk)] = chunk
            offset += len(chunk)

            if delete_chunks:
                Path(chunk_path).unlink()

        output.flush()
        return output

    def cleanup(self) -> None:
        """Delete temporary files."""
        for filepath in self._temp_files:
            try:
                if filepath.exists():
                    filepath.unlink()
            except Exception:
                pass
        self._temp_files = []

    def __del__(self):
        """Cleanup on deletion."""
        self.cleanup()


class LargeDatasetProcessor:
    """
    Process very large datasets that don't fit in memory.

    Uses memory mapping and streaming to handle datasets of any size.
    """

    def __init__(
        self,
        max_memory_mb: int = 2048,
        temp_dir: Optional[Path] = None,
    ):
        """
        Initialize large dataset processor.

        Args:
            max_memory_mb: Maximum memory to use
            temp_dir: Directory for temporary files
        """
        self.batcher = MemoryEfficientBatcher(max_memory_mb, temp_dir)

    def process_large_file(
        self,
        input_path: Path,
        process_func,
        output_path: Optional[Path] = None,
        chunk_size_mb: int = 100,
    ) -> dict:
        """
        Process a large vector file in chunks.

        Args:
            input_path: Input file path (.npy)
            process_func: Function to process each chunk
            output_path: Optional output path for results
            chunk_size_mb: Chunk size in MB

        Returns:
            Processing statistics
        """
        vectors = self.batcher.load_vectors_mmap(input_path)

        stats = {
            "total_vectors": len(vectors),
            "chunks_processed": 0,
            "errors": [],
        }

        results = []

        for i, chunk in enumerate(self.batcher.chunk_vectors(vectors, chunk_size_mb)):
            try:
                result = process_func(chunk, chunk_index=i)
                results.append(result)
                stats["chunks_processed"] += 1
            except Exception as e:
                stats["errors"].append({"chunk": i, "error": str(e)})

        if output_path and results:
            # Save results
            np.save(output_path, np.concatenate(results))

        return stats

    def batch_insert_large_dataset(
        self,
        vectors_path: Path,
        collection,
        ids_path: Optional[Path] = None,
        metadata_path: Optional[Path] = None,
        batch_size: int = 10000,
    ) -> dict:
        """
        Insert a large dataset into a collection.

        Args:
            vectors_path: Path to vectors file
            collection: Target collection
            ids_path: Optional path to IDs file
            metadata_path: Optional path to metadata file
            batch_size: Vectors per batch

        Returns:
            Insert statistics
        """
        vectors = self.batcher.load_vectors_mmap(vectors_path)
        n_vectors = len(vectors)

        ids = None
        if ids_path:
            ids = np.load(ids_path, allow_pickle=True)

        metadata = None
        if metadata_path:
            metadata = np.load(metadata_path, allow_pickle=True)

        stats = {
            "total_vectors": n_vectors,
            "inserted": 0,
            "errors": 0,
        }

        for i in range(0, n_vectors, batch_size):
            end = min(i + batch_size, n_vectors)

            batch_ids = (
                ids[i:end].tolist() if ids is not None
                else [str(j) for j in range(i, end)]
            )
            batch_vectors = vectors[i:end].copy()  # Copy from mmap
            batch_metadata = (
                metadata[i:end].tolist() if metadata is not None
                else None
            )

            try:
                collection.add(
                    ids=batch_ids,
                    vectors=batch_vectors,
                    metadata=batch_metadata,
                )
                stats["inserted"] += len(batch_ids)
            except Exception as e:
                stats["errors"] += len(batch_ids)

        return stats
