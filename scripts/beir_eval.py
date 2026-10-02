"""Evaluate VectrixDB on a BEIR dataset by one command.

    python scripts/beir_eval.py scifact
    python scripts/beir_eval.py nfcorpus --modes dense hybrid --k 10

BEIR (Thakur et al., 2021) is the standard zero-shot retrieval benchmark: a
corpus, a set of queries and graded relevance judgements per dataset, all
published as one zip. This script downloads the zip on first use (a few
megabytes for scifact or nfcorpus; the larger sets are hundreds), indexes the
corpus with the bundled English model, runs every judged query through the
same `search()` a user calls, and reports nDCG@10 and recall@100, the two
numbers BEIR papers report.

No dependency on the beir package, sentence-transformers or torch: the loader
is forty lines and the metrics are computed here the way trec_eval computes
them (linear gain, log2 discount), so the figures compare with published
tables. Cached under ~/.cache/vectrixdb/beir.

Small datasets, sizes and what to expect:

    scifact    5,183 docs,   300 queries    a few minutes, then seconds
    nfcorpus   3,633 docs,   323 queries
    arguana    8,674 docs, 1,406 queries
    fiqa      57,638 docs,   648 queries    an hour of embedding

The first run embeds the corpus with 512-token padding, which is what a user
gets; it is slow. Pass --fast to pad each batch to its longest document
instead, roughly ten times quicker with slightly different vectors.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional


# ============================================================================
# SETTINGS: the BEIR archive and its cache
# ============================================================================
#
# Where the datasets come from, and where the zips and the indexes built from
# them are kept: VECTRIXDB_BEIR_CACHE, or ~/.cache/vectrixdb/beir. A second
# run of the same dataset is seconds.

BEIR_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{name}.zip"
CACHE = Path(os.environ.get("VECTRIXDB_BEIR_CACHE", Path.home() / ".cache" / "vectrixdb" / "beir"))


# ============================================================================
# FETCHING: the archive, once, into the cache
# ============================================================================
#
# INPUT   a dataset's name
# OUTPUT  its extracted folder, downloaded on the first use only
#
# A zip is a few megabytes for scifact or nfcorpus and hundreds for the larger
# sets, so it is fetched once and kept. requests is used when present, its own
# CA bundle with it, and urllib otherwise.


def fetch(name: str) -> Path:
    """The extracted dataset directory, downloading the zip if needed."""
    target = CACHE / name
    if (target / "corpus.jsonl").exists():
        return target
    CACHE.mkdir(parents=True, exist_ok=True)
    url = BEIR_URL.format(name=name)
    print(f"downloading {url}", file=sys.stderr)
    data = _download(url)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(CACHE)
    if not (target / "corpus.jsonl").exists():
        raise SystemExit(f"{url} did not contain {name}/corpus.jsonl")
    return target


def _download(url: str) -> bytes:
    """requests when present (it ships its own CA bundle, which the Windows
    Store Python lacks), urllib otherwise."""
    try:
        import requests
    except ImportError:
        import urllib.request

        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed host
            return resp.read()
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    return resp.content


def load(name: str, split: str = "test"):
    root = fetch(name)
    corpus: Dict[str, str] = {}
    with open(root / "corpus.jsonl", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            title = (row.get("title") or "").strip()
            text = (row.get("text") or "").strip()
            corpus[row["_id"]] = f"{title}. {text}" if title else text
    queries: Dict[str, str] = {}
    with open(root / "queries.jsonl", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            queries[row["_id"]] = row["text"]
    qrels: Dict[str, Dict[str, int]] = defaultdict(dict)
    with open(root / "qrels" / f"{split}.tsv", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            score = int(row["score"])
            if score > 0:
                qrels[row["query-id"]][row["corpus-id"]] = score
    judged = {q: t for q, t in queries.items() if q in qrels}
    return corpus, judged, qrels


# ============================================================================
# SCORING: nDCG and recall at k
# ============================================================================
#
# INPUT   a ranked list of ids, the judged relevance for the query, and k
# OUTPUT  nDCG at k and recall at k, one number each
#
# Computed here the way trec_eval computes them, linear gain and a log2
# discount, so the figures compare with published tables and nothing depends
# on the beir package.


def ndcg_at(ranked: List[str], rels: Dict[str, int], k: int) -> float:
    dcg = sum(rels.get(d, 0) / math.log2(i + 2) for i, d in enumerate(ranked[:k]))
    ideal = sorted(rels.values(), reverse=True)[:k]
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def recall_at(ranked: List[str], rels: Dict[str, int], k: int) -> float:
    return len(set(ranked[:k]) & set(rels)) / len(rels)


# ============================================================================
# BUILDING AND EVALUATING: the index, then each query against it
# ============================================================================
#
# INPUT   the corpus and the mode, whether to pad fast and how many documents;
#         then the queries and qrels with k, depth and whether to rerank
# OUTPUT  a collection indexed with the bundled English model; nDCG@k and
#         recall@depth over the judged queries
#
# Every query goes through the same search() a user calls. The first run
# embeds the corpus with 512-token padding, which is what a user gets, and is
# slow; --fast pads each batch to its longest document instead, roughly ten
# times quicker with slightly different vectors.


def build(name: str, mode: str, corpus: Dict[str, str], fast: bool, max_docs: Optional[int]):
    from vectrixdb import Vectrix

    ids = list(corpus)[:max_docs] if max_docs else list(corpus)
    # The model is part of the key. Without it, a cache built with one model
    # was reopened after the default changed, the legacy path correctly kept
    # the old model for it, and the "new model" numbers were the old model's.
    model_tag = Vectrix._default_model.rsplit("/", 1)[-1]
    path = CACHE / "index" / f"{name}-{mode}-{model_tag}-{'fast' if fast else 'full'}-{len(ids)}"
    db = Vectrix("beir", path=str(path), mode=mode)
    if db.count() == len(ids):
        return db, 0.0
    if db.count():
        db.clear()
    texts = [corpus[i] for i in ids]
    t0 = time.perf_counter()
    if fast:
        from vectrixdb.models import DenseEmbedder

        vectors = DenseEmbedder(language="en").embed(texts, pad_to_longest=True)
        db.add(texts, ids=ids, vectors=vectors, progress=True)
    else:
        db.add(texts, ids=ids, progress=True)
    db.close()
    db = Vectrix("beir", path=str(path), mode=mode)
    return db, time.perf_counter() - t0


def evaluate(db, queries: Dict[str, str], qrels, k: int, depth: int, rerank: bool) -> dict:
    """nDCG@k and recall@depth over the judged queries.

    Without the reranker one search at ``depth`` serves both metrics. With
    it, recall@depth comes from the same unreranked list and nDCG@k from a
    second search at ``limit=k``, which reranks 3k candidates: reranking a
    hundred long documents per query is a minute of CPU, and no user does it.

    The time is the search a user would run: the one at ``depth`` without the
    reranker, and only the one at ``limit=k`` with it. Timing both put a
    search nobody runs into the reranked row.
    """
    ndcgs, recalls, latencies = [], [], []
    for qid, text in queries.items():
        t0 = time.perf_counter()
        ranked = [r.id for r in db.search(text, limit=depth, rerank=False)]
        elapsed = time.perf_counter() - t0
        if rerank:
            t0 = time.perf_counter()
            top = [r.id for r in db.search(text, limit=k)]
            elapsed = time.perf_counter() - t0
            ranked = top + [d for d in ranked if d not in set(top)]
        latencies.append(elapsed)
        ndcgs.append(ndcg_at(ranked, qrels[qid], k))
        recalls.append(recall_at(ranked, qrels[qid], depth))
    latencies.sort()
    return {
        f"ndcg@{k}": round(sum(ndcgs) / len(ndcgs), 4),
        f"recall@{depth}": round(sum(recalls) / len(recalls), 4),
        "p50_ms": round(1000 * latencies[len(latencies) // 2], 1),
        "queries": len(queries),
    }


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   the dataset, --modes, --k, --fast and the other options
# OUTPUT  one line a mode with its two numbers, printed
#
# One command, one dataset, the two numbers BEIR papers report.


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("dataset", nargs="?", default="scifact")
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["dense", "hybrid", "hybrid+rerank"],
        help="search modes; a '+rerank' suffix adds the cross-encoder on the top 3k",
    )
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--depth", type=int, default=100, help="recall depth and search limit")
    parser.add_argument(
        "--max-docs", type=int, default=None, help="index only the first N documents"
    )
    parser.add_argument("--max-queries", type=int, default=None)
    parser.add_argument("--fast", action="store_true", help="pad batches to their longest document")
    parser.add_argument("--json", type=Path, default=None, help="write the results here too")
    args = parser.parse_args(argv)

    corpus, queries, qrels = load(args.dataset)
    if args.max_docs:
        keep = set(list(corpus)[: args.max_docs])
        qrels = {q: {d: s for d, s in rel.items() if d in keep} for q, rel in qrels.items()}
        queries = {q: t for q, t in queries.items() if qrels.get(q)}
    if args.max_queries:
        queries = dict(list(queries.items())[: args.max_queries])
    print(
        f"{args.dataset}: {len(corpus):,} documents, {len(queries)} judged queries",
        file=sys.stderr,
    )

    results = {}
    for spec in args.modes:
        mode, _, extra = spec.partition("+")
        rerank = extra == "rerank"
        db, build_s = build(args.dataset, mode, corpus, args.fast, args.max_docs)
        try:
            results[spec] = evaluate(db, queries, qrels, args.k, args.depth, rerank)
            results[spec]["build_s"] = round(build_s, 1)
        finally:
            db.close()

    print(f"| Mode | nDCG@{args.k} | recall@{args.depth} | query p50 | build |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for mode, r in results.items():
        build_s = r["build_s"]
        built = f"{build_s / 60:.1f} min" if build_s else "cached"
        print(
            f"| {mode} | {r[f'ndcg@{args.k}']:.3f} | {r[f'recall@{args.depth}']:.3f} | "
            f"{r['p50_ms']:.0f} ms | {built} |"
        )
    if args.json:
        import datetime
        import platform

        import vectrixdb
        from vectrixdb import Vectrix

        # What the numbers came from, since a table quoted e5 numbers as bge once.
        provenance = {
            "model": Vectrix._default_model,
            "vectrixdb": vectrixdb.__version__,
            "date": datetime.date.today().isoformat(),
            "machine": f"{platform.machine()} {platform.system()} Python {platform.python_version()}",
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"dataset": args.dataset, "fast": args.fast, **provenance, "results": results}, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
