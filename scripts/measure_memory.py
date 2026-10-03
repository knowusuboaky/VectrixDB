"""Measure what a vector costs, on disk and in each quantiser, and write it up.

    python scripts/measure_memory.py                      # print a markdown table
    python scripts/measure_memory.py --doc docs/explanation/memory.md

Numbers are measured, not derived: a collection of random 384-d vectors is
written to disk and its files are sized, and each quantiser is fitted and asked
to encode the same vectors. The doc page is regenerated from this script so
the figures in it are never hand-typed.
"""

from __future__ import annotations

import argparse
import platform
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectrixdb import VectrixDB  # noqa: E402
from vectrixdb.core.quantization import BinaryQuantizer, ProductQuantizer, ScalarQuantizer  # noqa: E402


# ============================================================================
# SETTINGS: the dimension and the count
# ============================================================================
#
# The random vectors measured: how many, and how wide.

DIM = 384
N = 5000


# ============================================================================
# MEASURING: on disk, and each quantiser
# ============================================================================
#
# INPUT   the vectors
# OUTPUT  the size of a collection's files on disk; what each quantiser makes
#         of the same vectors
#
# Measured, not derived: a collection is written to disk and its files are
# sized, and each quantiser is fitted and asked to encode the same vectors.


def on_disk(vectors: np.ndarray) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = VectrixDB(path=tmp)
        collection = db.create_collection("m", dimension=DIM)
        ids = [f"d{i}" for i in range(len(vectors))]
        texts = [f"document number {i}" for i in range(len(vectors))]
        collection.add(ids=ids, vectors=vectors, metadata=[{} for _ in ids], texts=texts)
        collection.save()
        db.close()
        sizes = {}
        for path in Path(tmp).rglob("*"):
            if path.is_file():
                key = "index" if path.suffix in (".usearch", ".hnsw") else "sqlite"
                sizes[key] = sizes.get(key, 0) + path.stat().st_size
    n = len(vectors)
    return {k: v / n for k, v in sizes.items()}


def quantisers(vectors: np.ndarray) -> list:
    rows = []
    for name, q in (
        ("float32 (none)", None),
        ("scalar 8-bit", ScalarQuantizer(dimension=DIM)),
        ("binary 1-bit", BinaryQuantizer(dimension=DIM)),
        ("product (8 subvectors, 256 clusters)", ProductQuantizer(dimension=DIM, n_subvectors=8)),
    ):
        if q is None:
            rows.append((name, vectors.nbytes / len(vectors), 1.0))
            continue
        encoded = q.fit(vectors).encode(vectors)
        per_vector = encoded.nbytes / len(vectors)
        rows.append((name, per_vector, (DIM * 4) / per_vector))
    return rows


# ============================================================================
# RENDERING
# ============================================================================
#
# INPUT   the disk figure and the quantiser rows
# OUTPUT  the Markdown table
#
# The doc page is regenerated from this script so the figures in it are never
# hand-typed.


def render(disk: dict, rows: list) -> str:
    lines = [
        "# Memory per vector",
        "",
        f"Measured by `scripts/measure_memory.py` on {N:,} random {DIM}-dimensional",
        f"float32 vectors ({platform.system()}, Python {platform.python_version()},",
        f"NumPy {np.__version__}). Regenerate the page with the script; the numbers",
        "are not typed by hand.",
        "",
        "## On disk, per vector",
        "",
        "| Store | Bytes per vector |",
        "| --- | ---: |",
        f"| ANN index (`usearch`, float32) | {disk.get('index', 0):,.0f} |",
        f"| SQLite (ids, text, metadata, timestamps) | {disk.get('sqlite', 0):,.0f} |",
        f"| Total | {sum(disk.values()):,.0f} |",
        "",
        f"The raw vector is {DIM * 4:,} bytes. What the index adds is the graph: neighbour",
        "lists for every node.",
        "",
        "The SQLite figure is mostly a second copy of the vector, in the local",
        "document store (the `documents` table in the collection-level `.db`),",
        "beside the ids, text, metadata and timestamps. That column used to hold",
        "the vector as JSON, at about 5.7 times what float32 costs; it is a",
        "float32 blob now, which is where most of the drop from the figure this",
        "page used to carry has come from.",
        "",
        f"So budget about {sum(disk.values()) / (DIM * 4):.1f}x the raw vector size on disk for a local",
        f"collection: {sum(disk.values()):,.0f} bytes against the {DIM * 4:,} the vector itself is.",
        "",
        "## Quantisers, per vector",
        "",
        "The collection index stores float32 today. The quantisers ship as standalone",
        "classes (`vectrixdb.core.quantization`) for callers who store or transmit",
        "vectors themselves; this is what each costs after `fit()` and `encode()`.",
        "",
        "| Quantiser | Bytes per vector | Compression |",
        "| --- | ---: | ---: |",
    ]
    for name, per_vector, ratio in rows:
        lines.append(f"| {name} | {per_vector:,.0f} | {ratio:.1f}x |")
    lines += [
        "",
        "Recall under each quantiser is measured separately in",
        "`tests/unit/test_quantization_accuracy.py`; smaller is not free.",
        "",
    ]
    return "\n".join(lines)


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --doc, the page to rewrite
# OUTPUT  the table printed, and the page rewritten when asked
#
# What a vector costs, written up.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--doc", type=Path, help="write the markdown page here")
    args = parser.parse_args(argv)

    rng = np.random.default_rng(0)
    vectors = rng.standard_normal((N, DIM), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)

    page = render(on_disk(vectors), quantisers(vectors))
    print(page)
    if args.doc:
        args.doc.write_text(page, encoding="utf-8")
        print(f"\nwrote {args.doc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
