"""
Streaming Batch Processor

Memory-efficient streaming for processing large datasets without loading
everything into memory.
"""

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, TypeVar

from .parallel import BatchResult

T = TypeVar('T')


class StreamingBatchProcessor:
    """
    Process data streams without loading everything into memory.

    Maintains bounded memory usage by processing in batches.

    Example:
        >>> processor = StreamingBatchProcessor(batch_size=1000)
        >>> for result in processor.process_stream(data_iterator, process_func):
        ...     print(f"Batch: {result.success_count} items")
    """

    def __init__(
        self,
        batch_size: int = 1000,
        max_buffer_size: int = 10000,
    ):
        """
        Initialize streaming batch processor.

        Args:
            batch_size: Items per batch
            max_buffer_size: Maximum items to buffer
        """
        self.batch_size = batch_size
        self.max_buffer_size = max_buffer_size

    def process_stream(
        self,
        stream: Iterator[T],
        processor: Callable[[List[T]], BatchResult],
        on_batch_complete: Optional[Callable[[BatchResult, int], None]] = None,
    ) -> Iterator[BatchResult]:
        """
        Process a stream of items, yielding results per batch.

        Args:
            stream: Iterator of items
            processor: Function to process a batch
            on_batch_complete: Optional callback after each batch

        Yields:
            BatchResult for each processed batch
        """
        batch = []
        batch_num = 0

        for item in stream:
            batch.append(item)

            if len(batch) >= self.batch_size:
                result = processor(batch)
                batch_num += 1

                if on_batch_complete:
                    on_batch_complete(result, batch_num)

                yield result
                batch = []

        # Process remaining items
        if batch:
            result = processor(batch)
            batch_num += 1

            if on_batch_complete:
                on_batch_complete(result, batch_num)

            yield result

    def process_stream_combined(
        self,
        stream: Iterator[T],
        processor: Callable[[List[T]], BatchResult],
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> BatchResult:
        """
        Process stream and return combined result.

        Args:
            stream: Iterator of items
            processor: Function to process a batch
            on_progress: Progress callback with items processed

        Returns:
            Combined BatchResult
        """
        combined = BatchResult()
        total_processed = 0

        for batch_result in self.process_stream(stream, processor):
            combined = combined + batch_result
            total_processed += batch_result.success_count + batch_result.error_count

            if on_progress:
                on_progress(total_processed)

        return combined


class StreamingReader:
    """
    Read large files in streaming fashion.

    Supports JSONL, CSV, and other formats without loading entire file.
    """

    @staticmethod
    def read_jsonl(
        path: Path,
        batch_size: int = 1000
    ) -> Iterator[List[Dict[str, Any]]]:
        """
        Stream JSONL file in batches.

        Args:
            path: Path to JSONL file
            batch_size: Records per batch

        Yields:
            Batches of parsed JSON objects
        """
        path = Path(path)
        batch = []

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    obj = json.loads(line)
                    batch.append(obj)

                    if len(batch) >= batch_size:
                        yield batch
                        batch = []
                except json.JSONDecodeError:
                    continue

        if batch:
            yield batch

    @staticmethod
    def read_json_array(
        path: Path,
        batch_size: int = 1000
    ) -> Iterator[List[Dict[str, Any]]]:
        """
        Stream JSON array file in batches.

        Uses incremental parsing to avoid loading entire file.

        Args:
            path: Path to JSON file containing an array
            batch_size: Records per batch

        Yields:
            Batches of parsed JSON objects
        """
        path = Path(path)

        # For simplicity, load and iterate
        # For truly huge files, would need ijson library
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            yield [data]
            return

        batch = []
        for item in data:
            batch.append(item)
            if len(batch) >= batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    @staticmethod
    def read_csv(
        path: Path,
        batch_size: int = 1000,
        delimiter: str = ",",
        has_header: bool = True,
    ) -> Iterator[List[Dict[str, Any]]]:
        """
        Stream CSV file in batches.

        Args:
            path: Path to CSV file
            batch_size: Records per batch
            delimiter: Field delimiter
            has_header: Whether first row is header

        Yields:
            Batches of dicts (header keys -> values)
        """
        import csv
        path = Path(path)

        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter=delimiter)

            # Get header
            if has_header:
                try:
                    header = next(reader)
                except StopIteration:
                    return
            else:
                # Use column indices as keys
                header = None

            batch = []
            for i, row in enumerate(reader):
                if header:
                    obj = dict(zip(header, row))
                else:
                    obj = {f"col_{j}": v for j, v in enumerate(row)}

                batch.append(obj)

                if len(batch) >= batch_size:
                    yield batch
                    batch = []

            if batch:
                yield batch

    @staticmethod
    def read_parquet(
        path: Path,
        batch_size: int = 1000,
        columns: Optional[List[str]] = None,
    ) -> Iterator[List[Dict[str, Any]]]:
        """
        Stream Parquet file in batches.

        Requires pyarrow or fastparquet.

        Args:
            path: Path to Parquet file
            batch_size: Records per batch
            columns: Optional list of columns to read

        Yields:
            Batches of dicts
        """
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError("pyarrow required for Parquet support: pip install pyarrow")

        path = Path(path)

        # Read in batches using PyArrow's batch reader
        parquet_file = pq.ParquetFile(path)

        for batch in parquet_file.iter_batches(batch_size=batch_size, columns=columns):
            # Convert to list of dicts
            df_dict = batch.to_pydict()
            n_rows = len(list(df_dict.values())[0]) if df_dict else 0

            records = []
            for i in range(n_rows):
                record = {k: v[i] for k, v in df_dict.items()}
                records.append(record)

            yield records

    @staticmethod
    def read_npy(
        path: Path,
        batch_size: int = 1000,
    ) -> Iterator[Any]:
        """
        Stream NumPy .npy file in batches.

        Args:
            path: Path to .npy file
            batch_size: Vectors per batch

        Yields:
            Batches of numpy arrays
        """
        import numpy as np
        path = Path(path)

        # Load with mmap for memory efficiency
        data = np.load(path, mmap_mode='r')

        for i in range(0, len(data), batch_size):
            # Copy batch to avoid mmap issues
            yield np.array(data[i:i + batch_size])

    @staticmethod
    def count_lines(path: Path) -> int:
        """Count lines in a file efficiently."""
        path = Path(path)
        count = 0

        with open(path, "rb") as f:
            for _ in f:
                count += 1

        return count


class VectorFileReader:
    """
    Specialized reader for vector data files.

    Handles various formats with automatic vector extraction.
    """

    def __init__(
        self,
        vector_field: str = "vector",
        id_field: str = "id",
        metadata_fields: Optional[List[str]] = None,
        text_field: Optional[str] = None,
    ):
        """
        Initialize vector file reader.

        Args:
            vector_field: Name of vector field in records
            id_field: Name of ID field
            metadata_fields: Fields to include as metadata
            text_field: Optional text field name
        """
        self.vector_field = vector_field
        self.id_field = id_field
        self.metadata_fields = metadata_fields
        self.text_field = text_field

    def read_file(
        self,
        path: Path,
        file_format: str = "auto",
        batch_size: int = 1000,
    ) -> Iterator[Dict[str, Any]]:
        """
        Read vector data from file.

        Args:
            path: Path to file
            file_format: Format ("jsonl", "json", "csv", "parquet", "auto")
            batch_size: Records per batch

        Yields:
            Dicts with keys: ids, vectors, metadata, texts
        """
        path = Path(path)

        # Auto-detect format
        if file_format == "auto":
            suffix = path.suffix.lower()
            format_map = {
                ".jsonl": "jsonl",
                ".json": "json",
                ".csv": "csv",
                ".parquet": "parquet",
                ".pq": "parquet",
            }
            file_format = format_map.get(suffix, "jsonl")

        # Get reader
        if file_format == "jsonl":
            reader = StreamingReader.read_jsonl(path, batch_size)
        elif file_format == "json":
            reader = StreamingReader.read_json_array(path, batch_size)
        elif file_format == "csv":
            reader = StreamingReader.read_csv(path, batch_size)
        elif file_format == "parquet":
            reader = StreamingReader.read_parquet(path, batch_size)
        else:
            raise ValueError(f"Unknown format: {file_format}")

        # Process batches
        for records in reader:
            batch = self._process_records(records)
            if batch["ids"]:
                yield batch

    def _process_records(self, records: List[Dict]) -> Dict[str, Any]:
        """Process records into vector batch format."""
        ids = []
        vectors = []
        metadata_list = []
        texts = []

        for i, record in enumerate(records):
            # Get ID
            doc_id = record.get(self.id_field, str(i))
            ids.append(str(doc_id))

            # Get vector
            vector = record.get(self.vector_field)
            if vector is None:
                continue
            vectors.append(vector)

            # Get metadata
            if self.metadata_fields:
                meta = {k: record.get(k) for k in self.metadata_fields if k in record}
            else:
                # Include all non-special fields
                meta = {
                    k: v for k, v in record.items()
                    if k not in [self.vector_field, self.id_field, self.text_field]
                }
            metadata_list.append(meta)

            # Get text
            if self.text_field:
                text = record.get(self.text_field)
                texts.append(text)

        return {
            "ids": ids,
            "vectors": vectors,
            "metadata": metadata_list if metadata_list else None,
            "texts": texts if texts else None,
        }
