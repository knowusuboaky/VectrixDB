"""Measure recall and latency against brute-force ground truth.

An approximate index is only trustworthy if someone has checked how much it
approximates. This builds a collection with the bundled embedding model, then
compares what the index returns against exhaustive cosine search over the same
vectors.

    python scripts/benchmark_recall.py --sizes 1000 5000 --k 10

Numbers are written to stdout as a Markdown table so they can go straight into
the README. Nothing here is mocked: the vectors come from the real model and the
queries go through the real search path.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import tempfile
import time
from pathlib import Path
from typing import List

import numpy as np

from vectrixdb import Vectrix


# ============================================================================
# SETTINGS: the words the corpus is made from
# ============================================================================
#
# The subjects, verbs, objects and qualifiers the sentences are put together
# from, so a corpus of any size is real English for the real model to embed.
# Sentence fragments combined into a synthetic corpus. Real text rather than
# random vectors, so the embedding distribution matches actual use.

SUBJECTS = [
    "the storage engine",
    "a vector index",
    "the query planner",
    "this release",
    "the embedding model",
    "a background worker",
    "the cache layer",
    "the API server",
]
VERBS = [
    "handles",
    "rejects",
    "batches",
    "compresses",
    "replicates",
    "validates",
    "streams",
    "reorders",
]
OBJECTS = [
    "concurrent writes",
    "malformed payloads",
    "large documents",
    "quantized vectors",
    "index checkpoints",
    "search requests",
    "metadata filters",
    "sparse embeddings",
]
QUALIFIERS = [
    "without blocking readers",
    "under memory pressure",
    "on a cold start",
    "across process restarts",
    "at a million rows",
    "with WAL enabled",
]


# ============================================================================
# THE CORPUS AND RECALL AT K
# ============================================================================
#
# INPUT   a size and a seed; then the collection, its vectors, the queries and
#         k
# OUTPUT  the sentences; then recall against exhaustive search, and each
#         query's latency
#
# Nothing is mocked: the vectors come from the bundled model and the queries
# go through the real search path, which is the only way to know how much an
# approximate index approximates.


def make_corpus(n: int, seed: int = 20260908) -> List[str]:
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        # The trailing ordinal keeps every document distinct. Without it the
        # limited vocabulary repeats, duplicates collapse into a single
        # content-addressed document, and recall is measured against a ground
        # truth larger than the index: an artefact of the corpus, not the index.
        out.append(
            f"{SUBJECTS[rng.integers(len(SUBJECTS))]} "
            f"{VERBS[rng.integers(len(VERBS))]} "
            f"{OBJECTS[rng.integers(len(OBJECTS))]} "
            f"{QUALIFIERS[rng.integers(len(QUALIFIERS))]}, case {i}"
        )
    return out


def recall_at_k(db: Vectrix, vectors: np.ndarray, queries: List[str], k: int):
    """Recall against exhaustive search, plus per-query latency."""
    hits = 0
    latencies = []

    # Ground truth: exhaustive cosine over the same vectors the index holds.
    normed = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    for text in queries:
        q = np.asarray(db._embed([text])[0], dtype=np.float32)
        q /= np.linalg.norm(q)
        truth = set(np.argsort(-(normed @ q))[:k].tolist())

        started = time.perf_counter()
        results = list(db.search(text, limit=k, mode="dense"))
        latencies.append((time.perf_counter() - started) * 1000)

        returned = {db._id_to_row[r.id] for r in results if r.id in db._id_to_row}
        hits += len(truth & returned)

    return hits / (len(queries) * k), latencies


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --sizes and --k
# OUTPUT  a Markdown table on stdout, ready for the README
#
# One collection a size, built with the bundled embedding model and measured.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[500, 2000])
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--queries", type=int, default=25)
    args = parser.parse_args()

    print(f"| Vectors | recall@{args.k} | p50 query | p95 query | build |")
    print("| ------: | --------: | --------: | --------: | ----: |")

    for size in args.sizes:
        workdir = Path(tempfile.mkdtemp(prefix="vectrix_bench_"))
        try:
            corpus = make_corpus(size)
            db = Vectrix("bench", path=str(workdir))

            started = time.perf_counter()
            db.add(corpus)
            build = time.perf_counter() - started

            # Map ids back to corpus positions so recall can be scored.
            vectors = np.asarray(db._embed(corpus), dtype=np.float32)
            db._id_to_row = {}
            for row, text in enumerate(corpus):
                for doc_id, stored in db._texts.items():
                    if stored == text and doc_id not in db._id_to_row:
                        db._id_to_row[doc_id] = row
                        break

            queries = corpus[:: max(1, size // args.queries)][: args.queries]
            recall, latencies = recall_at_k(db, vectors, queries, args.k)

            print(
                f"| {size:,} | {recall:.3f} | "
                f"{statistics.median(latencies):.1f} ms | "
                f"{sorted(latencies)[int(0.95 * len(latencies)) - 1]:.1f} ms | "
                f"{build:.1f} s |"
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
