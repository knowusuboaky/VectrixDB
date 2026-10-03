"""What fan-out across sealed shards costs, measured.

The sealed-shard design claims that searching N shards of M vectors costs
about what searching one shard of N times M costs, because HNSW search is
logarithmic in the size of the graph, so splitting it pays the constant
factor and not the exponent. That is a claim, and this is the script that
decides whether it is true on your machine.

It builds the same vectors as one index and as several sharded ones, then
for each reports build time, query latency and recall against exact search,
so a drop in recall cannot hide behind a faster number. Deletions are
included because tombstones in a sealed shard are filtered after the search
rather than skipped during it, which is the design's known drag.

    python scripts/shard_bench.py                       # 20,000 x 128, quick
    python scripts/shard_bench.py --vectors 200000 --dims 384
    python scripts/shard_bench.py --shards 1 10 50 --deleted 0.2
    python scripts/shard_bench.py --json benchmarks/shards.json

Nothing here downloads or embeds: the vectors are random, because what is
being measured is the index and not a model.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectrixdb.core.collection import Collection  # noqa: E402
from vectrixdb.core.types import DistanceMetric, IndexConfig  # noqa: E402


# ============================================================================
# NEIGHBOURS, AND ONE CASE RUN
# ============================================================================
#
# INPUT   the corpus, the queries and k; then a case: how many shards, how
#         much deleted, cold or warm, and ef
# OUTPUT  the exact neighbours by brute-force cosine; for the case, build
#         time, query latency and recall against them
#
# Recall is reported beside the latency so a drop cannot hide behind a faster
# number. Deletions are included because tombstones in a sealed shard are
# filtered after the search rather than skipped during it, which is the
# design's known drag.


def exact_neighbours(corpus: np.ndarray, queries: np.ndarray, k: int) -> List[List[int]]:
    """The right answer, by brute force cosine similarity."""
    normed = corpus / np.linalg.norm(corpus, axis=1, keepdims=True)
    q = queries / np.linalg.norm(queries, axis=1, keepdims=True)
    scores = q @ normed.T
    return [list(np.argsort(-row)[:k]) for row in scores]


def _build(path, dims, shard_size, ef):
    from vectrixdb.core.types import IndexConfig, IndexType

    return Collection(
        "bench",
        dimension=dims,
        path=path,
        metric=DistanceMetric.COSINE,
        enable_text_index=False,
        shard_size=shard_size,
        index_config=IndexConfig(index_type=IndexType.HNSW, hnsw_ef_search=ef),
    )


def run_case(
    vectors: np.ndarray,
    queries: np.ndarray,
    truth: List[List[int]],
    shards: int,
    k: int,
    deleted: float,
    cold: bool,
    ef: int = IndexConfig().hnsw_ef_search,
) -> Dict[str, float]:
    """Build one collection and measure it."""
    count = len(vectors)
    shard_size = None if shards <= 1 else max(1, count // shards)
    directory = Path(tempfile.mkdtemp(prefix="shardbench_"))

    try:
        collection = _build(directory, vectors.shape[1], shard_size, ef)
        ids = [str(i) for i in range(count)]

        started = time.perf_counter()
        batch = 2000
        for start in range(0, count, batch):
            stop = min(start + batch, count)
            collection.add(
                ids=ids[start:stop],
                vectors=vectors[start:stop],
                metadata=[{} for _ in range(stop - start)],
            )
        build_s = time.perf_counter() - started

        seal_s = 0.0
        sizes: List[int] = []
        if shard_size:
            sizes = collection._index.shard_sizes()
            # Sealing happens inside add, so its cost is inside build_s. What
            # can be timed on its own is one more seal of a full head.
            seal_started = time.perf_counter()
            collection.save()
            seal_s = time.perf_counter() - seal_started

        removed: set = set()
        if deleted > 0:
            step = max(1, int(1 / deleted))
            removed = set(range(0, count, step))
            collection.delete([str(i) for i in sorted(removed)])

        if cold:
            # Reopen so nothing is warm: the sealed shards are mapped again
            # and the operating system has to fault their pages back in.
            collection.close()
            collection = _build(directory, vectors.shape[1], shard_size, ef)

        latencies: List[float] = []
        hits = 0
        wanted = 0
        for query, correct in zip(queries, truth):
            expected = [i for i in correct if i not in removed][:k]
            if not expected:
                continue
            started = time.perf_counter()
            found = collection.search(query=query, limit=k)
            latencies.append((time.perf_counter() - started) * 1000)
            got = {int(r.id) for r in found.results}
            hits += len(got & set(expected))
            wanted += len(expected)

        latencies.sort()
        result = {
            "shards": len(sizes) if sizes else 1,
            "shard_size": shard_size or count,
            "ef": ef,
            "build_s": round(build_s, 2),
            "seal_s": round(seal_s, 3),
            "p50_ms": round(latencies[len(latencies) // 2], 3) if latencies else 0.0,
            "p99_ms": round(latencies[int(len(latencies) * 0.99)], 3) if latencies else 0.0,
            f"recall@{k}": round(hits / wanted, 4) if wanted else 0.0,
            "live": collection.count(),
        }
        collection.close()
        return result
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --vectors, --dims, --shards, --deleted, --json
# OUTPUT  one row a case, and what the numbers came from
#
# The claim is that searching N shards of M vectors costs about what one shard
# of N times M costs; this decides whether it is true on your machine. Nothing
# downloads or embeds: the vectors are random, because what is measured is the
# index and not a model.


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--vectors", type=int, default=20000)
    parser.add_argument("--dims", type=int, default=128)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--shards",
        type=int,
        nargs="+",
        default=[1, 10, 50, 100],
        help="shard counts to compare; 1 is the single unsharded index",
    )
    parser.add_argument(
        "--deleted",
        type=float,
        default=0.0,
        help="fraction of documents to delete before querying, for tombstone drag",
    )
    parser.add_argument("--cold", action="store_true", help="reopen before querying")
    # The collection's own default, so the table is what a collection does
    # unless told otherwise. It was 50 here while collections search at 100.
    parser.add_argument(
        "--ef",
        type=int,
        default=IndexConfig().hnsw_ef_search,
        help="HNSW search width, the collection's default unless given. Raising it buys "
        "recall on a single index, which is what fan-out buys for free, so match recall "
        "before comparing latency.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    rng = np.random.default_rng(args.seed)
    vectors = rng.normal(size=(args.vectors, args.dims)).astype(np.float32)
    queries = rng.normal(size=(args.queries, args.dims)).astype(np.float32)

    print(
        f"{args.vectors:,} vectors of {args.dims} dimensions, {args.queries} queries, "
        f"k={args.k}, ef={args.ef}, {args.deleted:.0%} deleted, "
        f"{'cold' if args.cold else 'warm'}"
    )
    truth = exact_neighbours(vectors, queries, args.k)

    rows = []
    for shards in args.shards:
        row = run_case(vectors, queries, truth, shards, args.k, args.deleted, args.cold, args.ef)
        rows.append(row)
        print(
            f"  {row['shards']:>4} shards of {row['shard_size']:>7,}  "
            f"build {row['build_s']:>7.2f}s  p50 {row['p50_ms']:>7.3f} ms  "
            f"p99 {row['p99_ms']:>8.3f} ms  recall@{args.k} {row[f'recall@{args.k}']:.4f}"
        )

    print()
    print(f"| Shards | Vectors each | Build | Query p50 | Query p99 | recall@{args.k} |")
    print("| ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        print(
            f"| {row['shards']} | {row['shard_size']:,} | {row['build_s']:.1f} s | "
            f"{row['p50_ms']:.3f} ms | {row['p99_ms']:.3f} ms | {row[f'recall@{args.k}']:.4f} |"
        )

    baseline = rows[0]
    if len(rows) > 1 and baseline["p50_ms"]:
        worst = max(r["p50_ms"] for r in rows[1:]) / baseline["p50_ms"]
        drop = baseline[f"recall@{args.k}"] - min(r[f"recall@{args.k}"] for r in rows[1:])
        print()
        print(
            f"Worst sharded p50 is {worst:.1f}x the single index, "
            f"and recall falls at most {drop:.4f}."
        )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "vectors": args.vectors,
                    "dims": args.dims,
                    "queries": args.queries,
                    "k": args.k,
                    "deleted": args.deleted,
                    "cold": args.cold,
                    "ef": args.ef,
                    "seed": args.seed,
                    **_provenance(),
                    "rows": rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {args.json}")
    return 0


def _provenance() -> dict:
    """What the numbers came from: no model is involved, so the index library
    and the machine are what a reader needs to compare a rerun with."""
    import datetime
    import platform
    from importlib.metadata import PackageNotFoundError, version

    import vectrixdb

    try:
        usearch = version("usearch")
    except PackageNotFoundError:  # pragma: no cover - the index is usearch
        usearch = None
    return {
        "vectrixdb": vectrixdb.__version__,
        "usearch": usearch,
        "numpy": np.__version__,
        "date": datetime.date.today().isoformat(),
        "machine": f"{platform.machine()} {platform.system()} Python {platform.python_version()}",
    }


if __name__ == "__main__":
    raise SystemExit(main())
