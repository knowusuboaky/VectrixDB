"""Compare dense embedding models on the fixture set and a BEIR dataset.

    python scripts/compare_models.py
    python scripts/compare_models.py --dataset nfcorpus --models dense_en bge_small_en

Each model embeds the documents and the queries; retrieval is exact cosine
over the vectors, so the index plays no part and the number is the model's.
The fixture set gives MRR and recall@1 over 48 question and document pairs;
the BEIR dataset gives nDCG@10 and recall@100 over its judged queries,
computed the way trec_eval computes them (see scripts/beir_eval.py).

Models are folders under vectrixdb/models/data with the pooling each was
trained with, and the query or passage prefix its authors recommend. A
model is measured both bare and with its prefixes when it has any, because
the prefix is part of the model: e5 without "query: " is a different, worse
retriever than e5 with it.

Every model is measured with padding to the longest text in each batch,
which is what ``embed()`` does by default; pass the e5 variant below to see
what 512-token padding changes.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np


# ============================================================================
# SETTINGS: where the models are, and which
# ============================================================================
#
# Where the library and its models are, and the models compared: folders under
# vectrixdb/models/data, each with the pooling it was trained with and the
# query or passage prefix its authors recommend.

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

MODELS_DIR = ROOT / "vectrixdb" / "models" / "data"

MODELS: Dict[str, dict] = {
    "bge_small_en": {
        "label": "bge-small-en-v1.5 INT8 (bundled, the default)",
        "pooling": "cls",
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "passage_prefix": "",
    },
    "dense_en": {
        "label": "e5-small-v2 INT8 (bundled, the default before 2.2)",
        "pooling": "mean",
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
}

# To weigh a candidate, put its ONNX export and tokenizer in a folder under
# vectrixdb/models/data and add an entry above with the pooling its authors
# trained (mean, or the [CLS] vector) and the prefixes they recommend. The
# run that chose the 2.2 default compared bge-small against e5-small-v2 and
# snowflake-arctic-embed-xs, in fp32 and in three INT8 exports; the table is
# on the benchmarks page and its files are under benchmarks/archive, since
# those candidates are no longer on this machine and this script cannot
# write them again (benchmarks/archive/README.md says what it ran as).


# ============================================================================
# EMBEDDING: one embedder a model
# ============================================================================
#
# INPUT   a model's name; then the texts, whether to pad fast, and the batch
#         size
# OUTPUT  an embedder, and the vectors it makes of the texts
#
# Every model is measured with padding to the longest text in each batch,
# which is what embed() does by default; the e5 variant shows what 512-token
# padding changes.


def embedder(name: str):
    from vectrixdb.models import DenseEmbedder

    return DenseEmbedder(
        model_dir=MODELS_DIR / name, pooling=MODELS[name]["pooling"], language="en"
    )


def embed(model, texts: Sequence[str], fast: bool, batch: int = 64) -> np.ndarray:
    out = model.embed(list(texts), batch_size=batch, pad_to_longest=fast)
    out = np.asarray(out, dtype=np.float32)
    out /= np.linalg.norm(out, axis=1, keepdims=True) + 1e-9
    return out


# ============================================================================
# SCORING: the fixture pairs, and BEIR
# ============================================================================
#
# INPUT   a model, whether its prefixes are on, and for BEIR the dataset with
#         k and depth
# OUTPUT  MRR and recall@1 over the 48 fixture pairs; nDCG@10 and recall@100
#         over the dataset's judged queries
#
# Retrieval is exact cosine over the vectors, so the index plays no part and
# the number is the model's. A model is measured both bare and with its
# prefixes when it has any, because the prefix is part of the model: e5
# without "query: " is a different, worse retriever than e5 with it.


def fixture_scores(model, prefixed: bool, fast: bool) -> dict:
    pairs = json.loads((ROOT / "tests" / "fixtures" / "qa_pairs.json").read_text(encoding="utf-8"))
    spec = MODELS[model_name_of(model)]
    qp, pp = (spec["query_prefix"], spec["passage_prefix"]) if prefixed else ("", "")
    docs = embed(model, [pp + p["document"] for p in pairs], fast)
    queries = embed(model, [qp + p["question"] for p in pairs], fast)
    sims = queries @ docs.T
    rr = 0.0
    hits = 0
    for i in range(len(pairs)):
        order = np.argsort(-sims[i])
        rank = int(np.where(order == i)[0][0]) + 1
        rr += 1.0 / rank
        hits += rank == 1
    return {"mrr": round(rr / len(pairs), 4), "recall1": round(hits / len(pairs), 4)}


def beir_scores(model, prefixed: bool, fast: bool, dataset: str, k: int, depth: int) -> dict:
    from beir_eval import load, ndcg_at, recall_at

    corpus, queries, qrels = load(dataset)
    spec = MODELS[model_name_of(model)]
    qp, pp = (spec["query_prefix"], spec["passage_prefix"]) if prefixed else ("", "")
    ids = list(corpus)
    t0 = time.perf_counter()
    docs = embed(model, [pp + corpus[i] for i in ids], fast)
    embed_s = time.perf_counter() - t0
    qids = list(queries)
    qvecs = embed(model, [qp + queries[q] for q in qids], fast)
    sims = qvecs @ docs.T
    ndcgs, recalls = [], []
    for row, qid in enumerate(qids):
        top = np.argpartition(-sims[row], depth)[:depth]
        top = top[np.argsort(-sims[row][top])]
        ranked = [ids[i] for i in top]
        ndcgs.append(ndcg_at(ranked, qrels[qid], k))
        recalls.append(recall_at(ranked, qrels[qid], depth))
    return {
        f"ndcg@{k}": round(float(np.mean(ndcgs)), 4),
        f"recall@{depth}": round(float(np.mean(recalls)), 4),
        "embed_s": round(embed_s, 1),
    }


# ============================================================================
# NAMING
# ============================================================================
#
# INPUT   a model
# OUTPUT  its name as the table shows it
#
# One place the name is made, so the rows and the provenance agree.


def model_name_of(model) -> str:
    return Path(model.model_dir).name


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --dataset and --models
# OUTPUT  the table, with when and where it was measured
#
# Each row names its own model; the provenance line says when and where the
# rows were measured.


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", nargs="+", default=list(MODELS), choices=list(MODELS))
    parser.add_argument("--dataset", default="scifact")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--depth", type=int, default=100)
    parser.add_argument("--json", type=Path, default=ROOT / "benchmarks" / "models.json")
    parser.add_argument("--skip-beir", action="store_true")
    args = parser.parse_args(argv)

    rows = []
    for name in args.models:
        spec = MODELS[name]
        model = embedder(name)
        variants = [("bare", False, True)]
        if spec["query_prefix"] or spec["passage_prefix"]:
            variants.append(("prefixed", True, True))
        if name == "dense_en":
            variants.append(("prefixed, 512 padding as shipped", True, False))
        for variant, prefixed, fast in variants:
            print(f"{spec['label']} [{variant}]", file=sys.stderr)
            row = {"model": name, "label": spec["label"], "variant": variant}
            row.update(fixture_scores(model, prefixed, fast))
            if not args.skip_beir:
                row.update(beir_scores(model, prefixed, fast, args.dataset, args.k, args.depth))
            rows.append(row)
            print(
                "  ",
                {k: v for k, v in row.items() if k not in ("model", "label", "variant")},
                file=sys.stderr,
            )

    print(
        f"| Model | Variant | fixture MRR | fixture recall@1 | {args.dataset} nDCG@{args.k} | recall@{args.depth} | embed time |"
    )
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for r in rows:
        nd = r.get(f"ndcg@{args.k}", float("nan"))
        rc = r.get(f"recall@{args.depth}", float("nan"))
        es = r.get("embed_s", float("nan"))
        print(
            f"| {r['label']} | {r['variant']} | {r['mrr']:.3f} | {r['recall1']:.3f} | "
            f"{nd:.3f} | {rc:.3f} | {'' if math.isnan(es) else f'{es:.0f} s'} |"
        )
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps({"dataset": args.dataset, **_provenance(), "rows": rows}, indent=2),
        encoding="utf-8",
    )
    return 0


def _provenance() -> dict:
    """When and where the rows were measured; each row names its own model."""
    import datetime
    import platform

    import onnxruntime

    import vectrixdb

    return {
        "vectrixdb": vectrixdb.__version__,
        "onnxruntime": onnxruntime.__version__,
        "date": datetime.date.today().isoformat(),
        "machine": f"{platform.machine()} {platform.system()} Python {platform.python_version()}",
    }


if __name__ == "__main__":
    sys.exit(main())
