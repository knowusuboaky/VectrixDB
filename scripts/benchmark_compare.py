"""Compare VectrixDB with Chroma, LanceDB and Qdrant on the same vectors.

    python scripts/benchmark_compare.py --size 10000 --queries 200 --k 10
    python scripts/benchmark_compare.py --size 100000 --json benchmarks/compare_100k.json --chart -
    python scripts/benchmark_compare.py --engines vectrixdb chroma --size 2000
    python scripts/benchmark_compare.py --size 100000 --vectors-cache ~/.cache/vectrixdb/compare

Every engine gets the identical corpus: sentences people wrote, embedded once
with the bundled English model, then handed over as vectors so the comparison
is of the index and the storage, not of four different embedding models. The
sentences come from SciFact's scientific abstracts and from the LongMemEval and
LoCoMo conversations, about 158,000 distinct ones, read through the loaders of
beir_eval.py and memory_bench.py, which download each dataset once. ``--corpus
patterns`` uses the sentences benchmark_recall.py makes instead: 3,072 patterns
told apart by a number, which is what this script used before 2.2 and is far
more clustered than text is. Recall is measured against exhaustive cosine
search over those same vectors, latency is one thread issuing one query at a
time, and each engine runs in a fresh directory with its defaults.

Embedding is not timed, and at 100,000 sentences it is most of a run.
``--vectors-cache`` keeps the vectors in a folder, named for the model and
the sentences, and a rerun reads them back and goes straight to the engines.

Output is a Markdown table on stdout, a JSON file, and a chart, so the numbers
in the docs are whatever this script last printed and nothing else. Engines
that are not installed are skipped and named in the output.

What is and is not being compared:

* VectrixDB: usearch HNSW through the public Collection API, defaults
  (M=16, ef_construction=200, ef_search=100).
* Chroma: PersistentClient, HNSW with cosine space, defaults.
* LanceDB: a table with an IVF_PQ index at the library's defaults, handed
  the vectors as an Arrow table; below 256 rows it has no index and searches
  exhaustively.
* Qdrant: qdrant-client in local mode (path=...). Local mode is exhaustive
  NumPy search, not the server's HNSW, so its recall is 1.0 by construction
  and its latency grows linearly. It is here because it is what "pip install
  qdrant-client" gives an embedded user; the server is a different product.
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np


# ============================================================================
# SETTINGS: where the library is found, the dimension, the corpus's names
# ============================================================================
#
# Where the library is imported from, the dimension of the bundled English
# model, which is what every engine is handed, and how the report names each
# corpus.

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from benchmark_recall import make_corpus  # noqa: E402

DIM = 384

#: Where the sentences come from, as the report and the page name them.
SENTENCES = "sentences from SciFact's abstracts and the LongMemEval and LoCoMo conversations"
PATTERNS = "patterned sentences: 3,072 patterns told apart by a number"


# ============================================================================
# THE CORPUS: sentences people wrote
# ============================================================================
#
# INPUT   how many sentences, and the seed that shuffles them
# OUTPUT  that many distinct sentences
#
# Scientific abstracts and people talking, so the vectors spread the way a
# real collection's do. The datasets are downloaded once, by the loaders the
# BEIR and memory benchmarks already use, and cached where they cache them.


def real_sentences(n: int, seed: int) -> List[str]:
    """``n`` distinct sentences people wrote, shuffled with ``seed``.

    SciFact's abstracts and every turn of the LongMemEval and LoCoMo
    conversations, split where a sentence ends, whitespace tidied, kept when it
    is 30 to 300 characters and mostly letters, each one once.
    """
    import random
    import re

    import beir_eval
    import memory_bench

    corpus, _, _ = beir_eval.load("scifact")
    texts = list(corpus.values())
    for question in memory_bench.fetch("longmemeval"):
        for session in question.get("haystack_sessions", []):
            texts += [str(turn.get("content", "")) for turn in session]
    for conversation in memory_bench.fetch("locomo"):
        for turns in conversation.get("conversation", {}).values():
            if isinstance(turns, list):
                texts += [str(t.get("text", "")) for t in turns if isinstance(t, dict)]
    ends = re.compile(r"(?<=[.!?])\s+|\n+")
    seen: Dict[str, None] = {}
    for text in texts:
        for part in ends.split(text):
            sentence = " ".join(part.split()).strip(" -*#>\"'")
            letters = sum(c.isalpha() for c in sentence)
            if 30 <= len(sentence) <= 300 and letters > 0.6 * len(sentence):
                seen.setdefault(sentence, None)
    pool = sorted(seen)
    if len(pool) < n:
        raise SystemExit(
            f"--size and --queries ask for {n:,} sentences; the datasets hold {len(pool):,}"
        )
    random.Random(seed).shuffle(pool)
    return pool[:n]


# ============================================================================
# EMBEDDING AND GROUND TRUTH
# ============================================================================
#
# INPUT   the sentences of the corpus, and a folder to keep the vectors in;
#         then the query vectors and k
# OUTPUT  one vector a sentence; for each query, the k nearest by exhaustive
#         cosine over those vectors
#
# Embedding happens once and the vectors are handed over, so the comparison is
# of the index and the storage, not of four embedding models. Recall is
# measured against the exhaustive answer over the same vectors. A kept file is
# named for the model's weights and every sentence, so a changed model or a
# changed corpus embeds afresh.


def embed(texts: List[str], cache: Optional[Path] = None) -> np.ndarray:
    from vectrixdb.models import DenseEmbedder

    model = DenseEmbedder(language="en")
    kept = None
    if cache is not None:
        import hashlib

        weights = (model.model_dir / model.onnx_file).stat()
        stamp = f"{model.model_dir.name} {weights.st_size} {weights.st_mtime_ns}"
        key = hashlib.sha256("\n".join([stamp, *texts]).encode("utf-8")).hexdigest()[:16]
        kept = cache.expanduser() / f"vectors-{len(texts)}-{key}.npy"
        if kept.exists():
            return np.load(kept)
    # Padding to the longest text in a batch is what makes ten thousand
    # sentences take half a minute instead of half an hour. The vectors are
    # a little different from the 512-padded default, but every engine gets
    # the same ones, which is all a comparison needs.
    vectors = model.embed(texts, pad_to_longest=True).astype(np.float32)
    if kept is not None:
        kept.parent.mkdir(parents=True, exist_ok=True)
        np.save(kept, vectors)
    return vectors


def ground_truth(corpus: np.ndarray, queries: np.ndarray, k: int) -> List[set]:
    sims = queries @ corpus.T
    top = np.argpartition(-sims, k, axis=1)[:, :k]
    return [set(row.tolist()) for row in top]


# ============================================================================
# ENGINES: one class an engine, the same calls on each
# ============================================================================
#
# INPUT   ids and vectors to ingest; then a query vector and k
# OUTPUT  the k ids the engine returns, from a fresh directory of its own
#
# Build in ingest, answer with query, clean up in close. VectrixDB goes
# through the public Collection API at its defaults; Chroma, LanceDB and
# Qdrant run in local mode at theirs, and what that does and does not compare
# is set out in the module docstring.


class Engine:
    """One engine: build in `ingest`, answer with `query`, clean up in `close`."""

    name = ""
    note = ""

    def __init__(self, workdir: Path):
        self.workdir = workdir

    def ingest(self, ids: List[str], vectors: np.ndarray) -> None:
        raise NotImplementedError

    def query(self, vector: np.ndarray, k: int) -> List[str]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class VectrixEngine(Engine):
    name = "VectrixDB"
    note = "usearch HNSW, defaults"

    def ingest(self, ids, vectors):
        from vectrixdb import VectrixDB

        self.client = VectrixDB(path=str(self.workdir))
        self.coll = self.client.create_collection("bench", dimension=DIM)
        self.coll.add(ids=ids, vectors=vectors)

    def query(self, vector, k):
        return [r.id for r in self.coll.search(query=vector, limit=k).results]

    def close(self):
        self.client.close()


class ChromaEngine(Engine):
    name = "Chroma"
    note = "PersistentClient, HNSW cosine, defaults"

    def ingest(self, ids, vectors):
        import chromadb

        self.client = chromadb.PersistentClient(path=str(self.workdir))
        self.coll = self.client.create_collection("bench", metadata={"hnsw:space": "cosine"})
        step = 5000  # Chroma caps a single add
        for start in range(0, len(ids), step):
            self.coll.add(
                ids=ids[start : start + step], embeddings=vectors[start : start + step].tolist()
            )

    def query(self, vector, k):
        return self.coll.query(query_embeddings=[vector.tolist()], n_results=k, include=[])["ids"][
            0
        ]


class LanceEngine(Engine):
    name = "LanceDB"
    note = "IVF_PQ index, defaults"

    def ingest(self, ids, vectors):
        import lancedb
        import pyarrow as pa

        self.db = lancedb.connect(str(self.workdir))
        # An Arrow table over the same float32 buffer. A list of dicts would
        # turn 100,000 vectors into 38 million Python floats, over a
        # gigabyte, before LanceDB saw them.
        flat = pa.array(np.ascontiguousarray(vectors, dtype=np.float32).ravel())
        rows = pa.table(
            {"id": pa.array(ids), "vector": pa.FixedSizeListArray.from_arrays(flat, DIM)}
        )
        self.tbl = self.db.create_table("bench", data=rows)
        if len(ids) >= 256:
            from lancedb.index import IvfPq

            self.tbl.create_index(
                "vector",
                config=IvfPq(distance_type="cosine", num_partitions=max(1, int(len(ids) ** 0.5))),
            )
        else:
            self.note = "no index under 256 rows, exhaustive"

    def query(self, vector, k):
        return [
            r["id"] for r in self.tbl.search(vector.tolist()).metric("cosine").limit(k).to_list()
        ]


class QdrantEngine(Engine):
    name = "Qdrant (local mode)"
    note = "qdrant-client path mode, exhaustive NumPy search"

    def ingest(self, ids, vectors):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams

        self.ids = ids
        self.client = QdrantClient(path=str(self.workdir))
        self.client.create_collection(
            "bench", vectors_config=VectorParams(size=DIM, distance=Distance.COSINE)
        )
        step = 1000
        for start in range(0, len(ids), step):
            self.client.upsert(
                "bench",
                points=[
                    PointStruct(id=start + j, vector=vectors[start + j].tolist())
                    for j in range(min(step, len(ids) - start))
                ],
            )

    def query(self, vector, k):
        hits = self.client.query_points("bench", query=vector.tolist(), limit=k).points
        return [self.ids[p.id] for p in hits]

    def close(self):
        self.client.close()


# ============================================================================
# RUNNING: which engines are here, and each one's numbers
# ============================================================================
#
# INPUT   an engine's factory, the corpus, the queries and the truth
# OUTPUT  its recall, build time and query latency, one thread issuing one
#         query at a time
#
# An engine that is not installed is skipped and named in the output rather
# than failing the run.

ENGINES: Dict[str, tuple] = {
    "vectrixdb": (VectrixEngine, "vectrixdb"),
    "chroma": (ChromaEngine, "chromadb"),
    "lancedb": (LanceEngine, "lancedb"),
    "qdrant": (QdrantEngine, "qdrant_client"),
}


def available(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def run_engine(
    factory: Callable[[Path], Engine],
    ids: List[str],
    vectors: np.ndarray,
    queries: np.ndarray,
    truth: List[set],
    k: int,
) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="vxbench-"))
    engine = factory(workdir)
    try:
        t0 = time.perf_counter()
        engine.ingest(ids, vectors)
        ingest_s = time.perf_counter() - t0

        # Warm-up: the first query pays for lazy opens and caches.
        engine.query(queries[0], k)

        index_of = {i: n for n, i in enumerate(ids)}
        latencies = []
        hits = 0
        t0 = time.perf_counter()
        for q, want in zip(queries, truth):
            s = time.perf_counter()
            got = engine.query(q, k)
            latencies.append((time.perf_counter() - s) * 1000)
            hits += len({index_of[g] for g in got} & want)
        total_s = time.perf_counter() - t0
        latencies.sort()
        return {
            "name": engine.name,
            "note": engine.note,
            "ingest_s": round(ingest_s, 2),
            "recall": round(hits / (k * len(queries)), 4),
            "p50_ms": round(statistics.median(latencies), 2),
            "p95_ms": round(latencies[int(0.95 * (len(latencies) - 1))], 2),
            "qps": round(len(queries) / total_s, 1),
        }
    finally:
        try:
            engine.close()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# ============================================================================
# REPORTING: the table and the chart
# ============================================================================
#
# INPUT   the rows measured
# OUTPUT  a Markdown table on stdout, a JSON file, and a chart
#
# The numbers in the docs are whatever this script last printed and nothing
# else.


def markdown(rows: List[dict], size: int, k: int, skipped: List[str]) -> str:
    out = [
        f"| Engine | ingest {size:,} | recall@{k} | p50 | p95 | QPS |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        out.append(
            f"| {r['name']} | {r['ingest_s']:.1f} s | {r['recall']:.3f} | "
            f"{r['p50_ms']:.2f} ms | {r['p95_ms']:.2f} ms | {r['qps']:,.0f} |"
        )
    if skipped:
        out.append("")
        out.append("Not installed, skipped: " + ", ".join(skipped) + ".")
    return "\n".join(out)


def chart(rows: List[dict], path: Path, size: int, k: int) -> Optional[Path]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    names = [r["name"] for r in rows]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.5, 3.4), dpi=150)
    colours = ["#534ab7" if n == "VectrixDB" else "#b8b5ad" for n in names]
    ax1.barh(names, [r["p50_ms"] for r in rows], color=colours)
    ax1.set_xlabel(f"p50 query latency, ms ({size:,} vectors, one thread)")
    ax1.invert_yaxis()
    for i, r in enumerate(rows):
        ax1.text(r["p50_ms"], i, f" {r['p50_ms']:.2f}", va="center", fontsize=8)
    ax2.barh(names, [r["recall"] for r in rows], color=colours)
    ax2.set_xlim(0, 1.05)
    ax2.set_xlabel(f"recall@{k} against exhaustive search")
    ax2.invert_yaxis()
    ax2.set_yticklabels([])
    for i, r in enumerate(rows):
        ax2.text(r["recall"], i, f" {r['recall']:.3f}", va="center", fontsize=8)
    for ax in (ax1, ax2):
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    return path


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --size, --queries, --k, --engines, and where the JSON and the chart
#         go
# OUTPUT  the table, the JSON and the chart
#
# Every engine gets the identical corpus.


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--size", type=int, default=10000, help="corpus size")
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--engines", nargs="+", default=list(ENGINES), choices=list(ENGINES))
    parser.add_argument("--json", type=Path, default=ROOT.parent / "benchmarks" / "compare.json")
    parser.add_argument(
        "--chart", type=Path, default=ROOT.parent / "docs" / "images" / "benchmark.png"
    )
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument(
        "--corpus",
        choices=("sentences", "patterns"),
        default="sentences",
        help="sentences people wrote (the default), or the patterned ones benchmark_recall.py makes",
    )
    parser.add_argument(
        "--vectors-cache",
        type=Path,
        default=None,
        help="keep the embedded vectors in this folder and read them back on a rerun",
    )
    args = parser.parse_args(argv)

    wanted = args.size + args.queries
    texts = (
        real_sentences(wanted, args.seed)
        if args.corpus == "sentences"
        else make_corpus(wanted, seed=args.seed)
    )
    print(f"embedding {len(texts):,} sentences with the bundled English model", file=sys.stderr)
    t0 = time.perf_counter()
    vectors = embed(texts, args.vectors_cache)
    print(f"  {time.perf_counter() - t0:.1f} s", file=sys.stderr)
    corpus, queries = vectors[: args.size], vectors[args.size :]
    ids = [f"d{i}" for i in range(args.size)]
    truth = ground_truth(corpus, queries, args.k)

    rows, skipped = [], []
    for key in args.engines:
        cls, module = ENGINES[key]
        if not available(module):
            skipped.append(cls.name)
            continue
        print(f"{cls.name}: ingest {args.size:,}, {args.queries} queries", file=sys.stderr)
        rows.append(run_engine(cls, ids, corpus, queries, truth, args.k))

    import datetime
    import platform
    from importlib.metadata import PackageNotFoundError, version

    def installed(name):
        try:
            return version(name)
        except PackageNotFoundError:
            return None

    # The versions the page quotes, read here rather than typed there.
    report = {
        "corpus": SENTENCES if args.corpus == "sentences" else PATTERNS,
        "size": args.size,
        "queries": args.queries,
        "k": args.k,
        "dimension": DIM,
        "date": datetime.date.today().isoformat(),
        "machine": f"{platform.machine()} {platform.system()} Python {platform.python_version()}",
        "versions": {
            name: installed(name)
            for name in ("vectrixdb", "usearch", "numpy", "chromadb", "lancedb", "qdrant-client")
        },
        "engines": rows,
        "skipped": skipped,
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    saved = chart(rows, args.chart, args.size, args.k) if str(args.chart) not in ("", "-") else None

    print(markdown(rows, args.size, args.k, skipped))
    print(f"\nwrote {args.json}" + (f" and {saved}" if saved else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
