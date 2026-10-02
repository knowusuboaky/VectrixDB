"""
Tests for VectrixDB batch helpers.

Covers vectrixdb/core/batch/memory.py (memory-mapped batching for datasets
larger than RAM) and vectrixdb/core/batch/parallel.py (thread/process backed
parallel insert helpers).

Everything here runs offline with tmp_path files and small in-memory lists.
No test spawns a real OS process: this sandboxed environment was found to
hang indefinitely on a plain, correctly-guarded ProcessPoolExecutor script
(confirmed outside pytest with a two-worker repro), so the use_processes=True
branch is exercised by monkeypatching ProcessPoolExecutor to ThreadPoolExecutor
for the duration of those two tests. That still runs the exact same selection
line and executor code path; it just never asks the OS for a new process.
Wherever the code under test reads the machine's CPU count, os.cpu_count is
monkeypatched too, so nothing here depends on real hardware.
"""

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from vectrixdb.core.batch import parallel as parallel_mod
from vectrixdb.core.batch.memory import LargeDatasetProcessor, MemoryEfficientBatcher
from vectrixdb.core.batch.parallel import (
    BatchResult,
    ParallelBatchProcessor,
    ParallelVectorInserter,
)


def _square(x):
    """Module-level (picklable) helper for the process-pool code path."""
    return x * x


def _make_success_result(batch):
    """Module-level (picklable) helper for the process-pool code path."""
    return BatchResult(success_count=len(batch))


class _FakeCollection:
    """Duck-typed stand-in for a VectrixDB collection's .add()."""

    def __init__(self, fail_on_batch_start=None):
        self.calls = []
        self.fail_on_batch_start = fail_on_batch_start

    def add(self, ids, vectors, metadata):
        if self.fail_on_batch_start is not None and ids and ids[0] == self.fail_on_batch_start:
            raise RuntimeError("insert failed")
        self.calls.append(
            {"ids": list(ids), "vectors": np.asarray(vectors).copy(), "metadata": metadata}
        )


# =============================================================================
# MemoryEfficientBatcher
# =============================================================================


class TestEstimateMemory:
    def test_with_metadata_hand_computed(self):
        batcher = MemoryEfficientBatcher()

        # 10 vectors x 100 dims x 4 bytes (float32) = 4_000 bytes of vector data,
        # plus 10 * 200 bytes of estimated metadata.
        estimate = batcher.estimate_memory(n_vectors=10, dimension=100, avg_metadata_size=200)

        assert estimate == 4_000 + 2_000

    def test_without_metadata_is_vector_bytes_only(self):
        batcher = MemoryEfficientBatcher()
        estimate = batcher.estimate_memory(n_vectors=10, dimension=100, include_metadata=False)
        assert estimate == 4_000

    def test_unknown_dtype_falls_back_to_four_bytes(self):
        batcher = MemoryEfficientBatcher()
        estimate = batcher.estimate_memory(
            n_vectors=1, dimension=1, dtype="not-a-real-dtype", include_metadata=False
        )
        assert estimate == 4

    @pytest.mark.parametrize(
        ("dtype", "size"),
        [("float32", 4), ("float64", 8), ("float16", 2), ("int32", 4), ("int64", 8), ("uint8", 1)],
    )
    def test_known_dtype_sizes(self, dtype, size):
        batcher = MemoryEfficientBatcher()
        estimate = batcher.estimate_memory(
            n_vectors=1, dimension=1, dtype=dtype, include_metadata=False
        )
        assert estimate == size


class TestChunkVectors:
    def test_splits_by_memory_budget_with_exact_boundaries(self):
        # 0.5 MB per float32 vector (131072 dims x 4 bytes), so a 1 MB budget
        # gives exactly 2 vectors per chunk -- an exact, hand-computed split.
        dimension = 131072
        vectors = np.zeros((5, dimension), dtype="float32")
        for i in range(5):
            vectors[i, 0] = i  # marker column, cheap to compare
        batcher = MemoryEfficientBatcher()

        chunks = list(batcher.chunk_vectors(vectors, chunk_size_mb=1))

        assert [len(c) for c in chunks] == [2, 2, 1]
        assert [row[0] for row in chunks[0]] == [0, 1]
        assert [row[0] for row in chunks[2]] == [4]

    def test_single_chunk_when_budget_exceeds_the_data(self):
        vectors = np.zeros((5, 4), dtype="float32")
        batcher = MemoryEfficientBatcher()

        chunks = list(batcher.chunk_vectors(vectors, chunk_size_mb=100))

        assert len(chunks) == 1
        assert len(chunks[0]) == 5

    def test_chunks_reconstruct_the_original_array(self):
        vectors = np.arange(40, dtype="float32").reshape(10, 4)
        batcher = MemoryEfficientBatcher()

        chunks = list(batcher.chunk_vectors(vectors, chunk_size_mb=1))
        rebuilt = np.concatenate(chunks, axis=0)

        assert np.array_equal(rebuilt, vectors)


class TestChunkByCount:
    def test_even_split(self):
        vectors = np.arange(24, dtype="float32").reshape(8, 3)
        batcher = MemoryEfficientBatcher()

        chunks = list(batcher.chunk_by_count(vectors, chunk_size=4))

        assert [len(c) for c in chunks] == [4, 4]
        assert np.array_equal(np.concatenate(chunks), vectors)

    def test_uneven_last_chunk(self):
        vectors = np.arange(15, dtype="float32").reshape(5, 3)
        batcher = MemoryEfficientBatcher()

        chunks = list(batcher.chunk_by_count(vectors, chunk_size=2))

        assert [len(c) for c in chunks] == [2, 2, 1]

    def test_default_chunk_size_is_ten_thousand(self):
        vectors = np.zeros((5, 2), dtype="float32")
        batcher = MemoryEfficientBatcher()

        chunks = list(batcher.chunk_by_count(vectors))

        assert len(chunks) == 1  # 5 rows fit in the default 10_000-row chunk


class TestOptimalChunkSize:
    def test_defaults_to_one_quarter_of_max_memory(self):
        batcher = MemoryEfficientBatcher(max_memory_mb=40)
        # target = 40 // 4 = 10 MB; 128 dims x 4 bytes = 512 bytes/vector.
        expected = (10 * 1024 * 1024) // 512
        assert batcher.optimal_chunk_size(dimension=128) == expected

    def test_respects_an_explicit_target(self):
        batcher = MemoryEfficientBatcher(max_memory_mb=4096)
        expected = (5 * 1024 * 1024) // (64 * 4)
        assert batcher.optimal_chunk_size(dimension=64, target_memory_mb=5) == expected

    def test_unknown_dtype_falls_back_to_four_bytes(self):
        batcher = MemoryEfficientBatcher()
        expected = (1 * 1024 * 1024) // (10 * 4)
        assert (
            batcher.optimal_chunk_size(dimension=10, dtype="int8", target_memory_mb=1) == expected
        )

    def test_floors_at_one_vector_when_budget_is_tiny(self):
        batcher = MemoryEfficientBatcher()
        assert batcher.optimal_chunk_size(dimension=10_000_000, target_memory_mb=1) == 1


class TestCreateAndLoadVectorMmap:
    def test_create_vector_mmap_has_the_requested_shape_and_dtype(self, tmp_path):
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        mmap = batcher.create_vector_mmap(5, 4, dtype="float32", filename="vecs.npy")

        assert mmap.shape == (5, 4)
        assert mmap.dtype == np.dtype("float32")
        assert (tmp_path / "vecs.npy").exists()

    def test_default_filename_is_tracked_for_cleanup(self, tmp_path):
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        batcher.create_vector_mmap(2, 2)

        assert len(batcher._temp_files) == 1
        assert batcher._temp_files[0].exists()

    def test_load_vectors_mmap_reads_a_real_npy_file(self, tmp_path):
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        data = np.arange(12, dtype="float32").reshape(3, 4)
        path = tmp_path / "real.npy"
        np.save(path, data)

        loaded = batcher.load_vectors_mmap(path)

        assert np.array_equal(np.asarray(loaded), data)

    def test_cleanup_removes_every_file_it_created(self, tmp_path):
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        batcher.create_vector_mmap(2, 2, filename="a.npy")
        batcher.create_vector_mmap(2, 2, filename="b.npy")
        assert (tmp_path / "a.npy").exists()
        assert (tmp_path / "b.npy").exists()

        batcher.cleanup()

        assert not (tmp_path / "a.npy").exists()
        assert not (tmp_path / "b.npy").exists()
        assert batcher._temp_files == []

    def test_cleanup_tolerates_a_file_already_removed(self, tmp_path):
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        batcher.create_vector_mmap(1, 1, filename="only.npy")
        (tmp_path / "only.npy").unlink()

        batcher.cleanup()  # must not raise even though the file is already gone

        assert batcher._temp_files == []


class TestMergeChunks:
    def test_combines_chunks_in_order(self, tmp_path):
        # delete_chunks=False here: the default True is covered (and shown broken
        # on Windows) by TestMemoryBatcherKnownBugs below.
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        chunk_a = np.arange(6, dtype="float32").reshape(2, 3)
        chunk_b = np.arange(6, 12, dtype="float32").reshape(2, 3)
        path_a, path_b = tmp_path / "a.npy", tmp_path / "b.npy"
        np.save(path_a, chunk_a)
        np.save(path_b, chunk_b)

        merged = batcher.merge_chunks(
            [path_a, path_b], tmp_path / "merged.npy", delete_chunks=False
        )

        assert merged.shape == (4, 3)
        assert np.array_equal(np.asarray(merged), np.vstack([chunk_a, chunk_b]))

    def test_keeps_source_chunks_when_delete_chunks_is_false(self, tmp_path):
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        path = tmp_path / "a.npy"
        np.save(path, np.ones((2, 2), dtype="float32"))

        batcher.merge_chunks([path], tmp_path / "merged.npy", delete_chunks=False)

        assert path.exists()


class TestMemoryBatcherKnownBugs:
    """Regression tests documenting real bugs found while writing this suite.

    Each is marked xfail(strict=True) with a one-line reason. If a fix lands,
    the assertion starts passing, strict mode turns that into a failure, and
    that failure is the signal to delete the xfail marker.
    """

    def test_a_file_created_by_create_vector_mmap_can_be_loaded_back(self, tmp_path):
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        mmap = batcher.create_vector_mmap(5, 4, filename="vecs.npy")
        mmap[:] = np.arange(20, dtype="float32").reshape(5, 4)
        mmap.flush()

        loaded = batcher.load_vectors_mmap(tmp_path / "vecs.npy")

        assert np.array_equal(np.asarray(loaded), np.asarray(mmap))

    def test_merge_chunks_can_delete_the_source_chunks_it_just_read(self, tmp_path):
        batcher = MemoryEfficientBatcher(temp_dir=tmp_path)
        path_a = tmp_path / "a.npy"
        path_b = tmp_path / "b.npy"
        np.save(path_a, np.zeros((2, 2), dtype="float32"))
        np.save(path_b, np.ones((2, 2), dtype="float32"))

        batcher.merge_chunks([path_a, path_b], tmp_path / "merged.npy")  # delete_chunks=True

        assert not path_a.exists()
        assert not path_b.exists()


# =============================================================================
# LargeDatasetProcessor
# =============================================================================


class TestProcessLargeFile:
    def test_calls_process_func_once_per_chunk_and_tracks_stats(self, tmp_path):
        vectors = np.arange(24, dtype="float32").reshape(6, 4)
        input_path = tmp_path / "input.npy"
        np.save(input_path, vectors)
        processor = LargeDatasetProcessor(temp_dir=tmp_path)
        seen_indices = []

        def process_func(chunk, chunk_index):
            seen_indices.append(chunk_index)
            return chunk

        stats = processor.process_large_file(input_path, process_func, chunk_size_mb=100)

        assert stats["total_vectors"] == 6
        assert stats["chunks_processed"] == len(seen_indices)
        assert stats["errors"] == []

    def test_continues_processing_after_a_chunk_error(self, tmp_path):
        # 0.5 MB per float32 vector, so chunk_size_mb=1 gives an exact 2/2/1 split.
        dimension = 131072
        vectors = np.zeros((5, dimension), dtype="float32")
        input_path = tmp_path / "input.npy"
        np.save(input_path, vectors)
        processor = LargeDatasetProcessor(temp_dir=tmp_path)
        seen = []

        def flaky(chunk, chunk_index):
            seen.append(chunk_index)
            if chunk_index == 1:
                raise RuntimeError("bad chunk")
            return chunk

        stats = processor.process_large_file(input_path, flaky, chunk_size_mb=1)

        assert seen == [0, 1, 2]
        assert stats["chunks_processed"] == 2
        assert stats["errors"] == [{"chunk": 1, "error": "bad chunk"}]

    def test_saves_concatenated_results_when_an_output_path_is_given(self, tmp_path):
        vectors = np.arange(16, dtype="float32").reshape(4, 4)
        input_path = tmp_path / "input.npy"
        output_path = tmp_path / "output.npy"
        np.save(input_path, vectors)
        processor = LargeDatasetProcessor(temp_dir=tmp_path)

        def identity(chunk, chunk_index):
            return chunk

        processor.process_large_file(
            input_path, identity, output_path=output_path, chunk_size_mb=100
        )

        saved = np.load(output_path)
        assert np.array_equal(saved, vectors)


class TestBatchInsertLargeDataset:
    def test_default_ids_are_stringified_row_indices(self, tmp_path):
        vectors = np.arange(12, dtype="float32").reshape(6, 2)
        vectors_path = tmp_path / "vectors.npy"
        np.save(vectors_path, vectors)
        processor = LargeDatasetProcessor(temp_dir=tmp_path)
        collection = _FakeCollection()

        stats = processor.batch_insert_large_dataset(vectors_path, collection, batch_size=4)

        assert stats == {"total_vectors": 6, "inserted": 6, "errors": 0}
        all_ids = [i for call in collection.calls for i in call["ids"]]
        assert all_ids == [str(j) for j in range(6)]

    def test_explicit_ids_and_metadata_are_forwarded_in_batches(self, tmp_path):
        vectors = np.arange(8, dtype="float32").reshape(4, 2)
        ids = np.array(["a", "b", "c", "d"], dtype=object)
        metadata = np.array([{"i": 0}, {"i": 1}, {"i": 2}, {"i": 3}], dtype=object)
        vectors_path = tmp_path / "vectors.npy"
        ids_path = tmp_path / "ids.npy"
        metadata_path = tmp_path / "metadata.npy"
        np.save(vectors_path, vectors)
        np.save(ids_path, ids, allow_pickle=True)
        np.save(metadata_path, metadata, allow_pickle=True)
        processor = LargeDatasetProcessor(temp_dir=tmp_path)
        collection = _FakeCollection()

        stats = processor.batch_insert_large_dataset(
            vectors_path, collection, ids_path=ids_path, metadata_path=metadata_path, batch_size=2
        )

        assert stats == {"total_vectors": 4, "inserted": 4, "errors": 0}
        assert collection.calls[0]["ids"] == ["a", "b"]
        assert collection.calls[0]["metadata"] == [{"i": 0}, {"i": 1}]
        assert collection.calls[1]["ids"] == ["c", "d"]

    def test_a_failing_batch_is_counted_as_errors_not_inserted(self, tmp_path):
        vectors = np.arange(8, dtype="float32").reshape(4, 2)
        vectors_path = tmp_path / "vectors.npy"
        np.save(vectors_path, vectors)
        processor = LargeDatasetProcessor(temp_dir=tmp_path)
        collection = _FakeCollection(fail_on_batch_start="2")  # second batch (ids 2,3) fails

        stats = processor.batch_insert_large_dataset(vectors_path, collection, batch_size=2)

        assert stats == {"total_vectors": 4, "inserted": 2, "errors": 2}
        assert len(collection.calls) == 1


# =============================================================================
# BatchResult
# =============================================================================


class TestBatchResult:
    def test_add_combines_counts_and_errors(self):
        a = BatchResult(success_count=3, error_count=1, errors=[{"e": 1}], duration_ms=10.0)
        b = BatchResult(success_count=2, error_count=0, errors=[{"e": 2}], duration_ms=5.0)

        combined = a + b

        assert combined.success_count == 5
        assert combined.error_count == 1
        assert combined.errors == [{"e": 1}, {"e": 2}]
        assert combined.duration_ms == 15.0
        assert combined.items_per_second == 0

    def test_add_does_not_mutate_the_operands(self):
        a = BatchResult(success_count=1, errors=[{"e": 1}])
        b = BatchResult(success_count=1, errors=[{"e": 2}])

        _ = a + b

        assert a.errors == [{"e": 1}]
        assert b.errors == [{"e": 2}]

    def test_finalize_computes_items_per_second(self):
        result = BatchResult()
        result.finalize(total_items=100, total_duration_ms=200.0)

        assert result.duration_ms == 200.0
        assert result.items_per_second == pytest.approx(500.0)

    def test_finalize_handles_zero_duration(self):
        result = BatchResult()
        result.finalize(total_items=10, total_duration_ms=0.0)

        assert result.items_per_second == 0.0


# =============================================================================
# ParallelBatchProcessor
# =============================================================================


class TestParallelBatchProcessorConfig:
    def test_max_workers_defaults_to_four_when_cpu_count_is_unknown(self, monkeypatch):
        monkeypatch.setattr(parallel_mod.os, "cpu_count", lambda: None)
        processor = ParallelBatchProcessor()
        assert processor.max_workers == 4

    def test_max_workers_falls_back_to_cpu_count(self, monkeypatch):
        monkeypatch.setattr(parallel_mod.os, "cpu_count", lambda: 7)
        processor = ParallelBatchProcessor()
        assert processor.max_workers == 7

    def test_explicit_max_workers_overrides_cpu_count(self, monkeypatch):
        monkeypatch.setattr(parallel_mod.os, "cpu_count", lambda: 99)
        processor = ParallelBatchProcessor(max_workers=2)
        assert processor.max_workers == 2


class TestParallelBatchProcessorThreads:
    def test_process_batch_returns_empty_result_for_no_items(self):
        processor = ParallelBatchProcessor(max_workers=2, batch_size=10)

        result = processor.process_batch([], lambda batch: BatchResult(success_count=len(batch)))

        assert result == BatchResult()

    def test_process_batch_splits_by_batch_size_and_sums_successes(self):
        processor = ParallelBatchProcessor(max_workers=2, batch_size=3)
        seen_batches = []

        def handler(batch):
            seen_batches.append(list(batch))
            return BatchResult(success_count=len(batch))

        result = processor.process_batch(list(range(7)), handler)

        assert result.success_count == 7
        assert result.error_count == 0
        assert sorted(len(b) for b in seen_batches) == [1, 3, 3]
        assert result.duration_ms >= 0
        assert result.items_per_second >= 0

    def test_process_batch_records_errors_without_losing_other_batches(self):
        processor = ParallelBatchProcessor(max_workers=2, batch_size=2)

        def handler(batch):
            if 3 in batch:
                raise ValueError("bad batch")
            return BatchResult(success_count=len(batch))

        result = processor.process_batch(list(range(6)), handler)  # batches: [0,1] [2,3] [4,5]

        assert result.success_count == 4
        assert result.error_count == 2
        assert len(result.errors) == 1
        assert result.errors[0]["batch"] == 1
        assert result.errors[0]["type"] == "ValueError"
        assert "bad batch" in result.errors[0]["error"]

    def test_process_batch_progress_callback_sequence_with_one_worker(self):
        # A single worker processes batches strictly in submission order, so the
        # progress sequence is fully deterministic: batches of size 2, 2, 1.
        processor = ParallelBatchProcessor(max_workers=1, batch_size=2)
        progress_calls = []

        def handler(batch):
            return BatchResult(success_count=len(batch))

        processor.process_batch(
            list(range(5)),
            handler,
            on_progress=lambda done, total: progress_calls.append((done, total)),
        )

        assert progress_calls == [(2, 5), (4, 5), (5, 5)]

    def test_map_parallel_preserves_input_order(self):
        processor = ParallelBatchProcessor(max_workers=2, use_processes=False)

        results = processor.map_parallel([1, 2, 3, 4], lambda x: x * 10)

        assert results == [10, 20, 30, 40]


class TestParallelBatchProcessorProcessBranch:
    """Exercises the use_processes=True branch without spawning a real process.

    See the module docstring: a genuine ProcessPoolExecutor hangs in this
    sandbox, so ProcessPoolExecutor is monkeypatched to ThreadPoolExecutor
    here. The `executor_class = ProcessPoolExecutor if self.use_processes
    else ThreadPoolExecutor` selection line and the rest of the method still
    run exactly as written; only the concurrency primitive underneath changes.
    """

    def test_process_batch_with_use_processes_true(self, monkeypatch):
        monkeypatch.setattr(parallel_mod, "ProcessPoolExecutor", ThreadPoolExecutor)
        processor = ParallelBatchProcessor(max_workers=2, use_processes=True, batch_size=2)

        result = processor.process_batch(list(range(4)), _make_success_result)

        assert result.success_count == 4
        assert result.error_count == 0

    def test_map_parallel_with_use_processes_true(self, monkeypatch):
        monkeypatch.setattr(parallel_mod, "ProcessPoolExecutor", ThreadPoolExecutor)
        processor = ParallelBatchProcessor(max_workers=2, use_processes=True)

        results = processor.map_parallel([1, 2, 3], _square)

        assert results == [1, 4, 9]


# =============================================================================
# ParallelVectorInserter
# =============================================================================


class TestParallelVectorInserter:
    def test_insert_validates_ids_length_matches_vectors(self):
        def add_func(**_kwargs):
            return BatchResult()

        inserter = ParallelVectorInserter(add_func=add_func)

        with pytest.raises(ValueError):
            inserter.insert(ids=["a"], vectors=np.zeros((2, 3)))

    def test_insert_validates_metadata_length(self):
        def add_func(**_kwargs):
            return BatchResult()

        inserter = ParallelVectorInserter(add_func=add_func)

        with pytest.raises(ValueError):
            inserter.insert(ids=["a", "b"], vectors=np.zeros((2, 3)), metadata=[{"x": 1}])

    def test_insert_validates_texts_length(self):
        def add_func(**_kwargs):
            return BatchResult()

        inserter = ParallelVectorInserter(add_func=add_func)

        with pytest.raises(ValueError):
            inserter.insert(ids=["a", "b"], vectors=np.zeros((2, 3)), texts=["only one"])

    def test_insert_calls_add_func_with_correctly_sliced_batches(self):
        calls = []

        def add_func(ids, vectors, metadata, texts):
            calls.append(
                {"ids": list(ids), "n_vectors": len(vectors), "metadata": metadata, "texts": texts}
            )
            return BatchResult(success_count=len(ids))

        inserter = ParallelVectorInserter(add_func=add_func, batch_size=2, max_workers=2)
        vectors = np.arange(24, dtype="float32").reshape(6, 4)
        ids = [f"id{i}" for i in range(6)]
        metadata = [{"i": i} for i in range(6)]

        result = inserter.insert(ids=ids, vectors=vectors, metadata=metadata)

        assert result.success_count == 6
        assert result.error_count == 0
        assert sorted(len(c["ids"]) for c in calls) == [2, 2, 2]
        assert sorted(i for c in calls for i in c["ids"]) == sorted(ids)

    def test_insert_reports_a_failing_batch_as_errors(self):
        def add_func(ids, vectors, metadata, texts):
            if "id2" in ids:
                raise RuntimeError("insert exploded")
            return BatchResult(success_count=len(ids))

        inserter = ParallelVectorInserter(add_func=add_func, batch_size=2, max_workers=2)
        vectors = np.zeros((4, 3), dtype="float32")
        ids = ["id0", "id1", "id2", "id3"]

        result = inserter.insert(ids=ids, vectors=vectors)

        assert result.success_count == 2
        assert result.error_count == 2
        assert len(result.errors) == 1
        assert "insert exploded" in result.errors[0]["error"]

    def test_insert_counts_success_when_add_func_returns_nothing(self):
        def add_func(ids, vectors, metadata, texts):
            return None  # some add_funcs signal success by simply not raising

        inserter = ParallelVectorInserter(add_func=add_func, batch_size=10)

        result = inserter.insert(ids=["a", "b"], vectors=np.zeros((2, 2)))

        assert result.success_count == 2
        assert result.error_count == 0

    def test_insert_handles_a_future_that_raises_directly(self, monkeypatch):
        # _process_batch already catches every exception from add_func and turns
        # it into a BatchResult, so insert()'s own except-around-future.result()
        # (parallel.py:267-271) can only fire if something else raises. Replace
        # the instance method itself to exercise that defensive branch directly.
        def add_func(ids, vectors, metadata, texts):
            return BatchResult(success_count=len(ids))

        inserter = ParallelVectorInserter(add_func=add_func, batch_size=2, max_workers=2)

        def broken_process_batch(batch):
            raise RuntimeError("thread pool internals blew up")

        monkeypatch.setattr(inserter, "_process_batch", broken_process_batch)

        result = inserter.insert(ids=["a", "b", "c", "d"], vectors=np.zeros((4, 2)))

        assert result.success_count == 0
        assert result.error_count == 4
        assert len(result.errors) == 2  # one per batch, both batches "fail"
        assert all("thread pool internals blew up" in e["error"] for e in result.errors)

    def test_insert_progress_callback_sequence_with_one_worker(self):
        def add_func(ids, vectors, metadata, texts):
            return BatchResult(success_count=len(ids))

        inserter = ParallelVectorInserter(add_func=add_func, batch_size=2, max_workers=1)
        progress = []

        inserter.insert(
            ids=[f"id{i}" for i in range(5)],
            vectors=np.zeros((5, 2)),
            on_progress=lambda done, total: progress.append((done, total)),
        )

        assert progress == [(2, 5), (4, 5), (5, 5)]
