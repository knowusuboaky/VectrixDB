"""Where to stop answering, measured per model on a BEIR dataset.

    python scripts/answer_cutoff_bench.py scifact --json benchmarks/answer_cutoff_scifact_bge.json
    python scripts/answer_cutoff_bench.py scifact --dense-model e5-small --json ...

Every judged query is searched once, dense. Its top result is the answer when
the judges marked that document relevant, and a near miss when they did not:
the best the collection had, and wrong. :func:`vectrixdb.evaluation.answer_cutoff`
places the similarity that best separates the two.

It reuses the dense index ``beir_eval.py`` built, under ``VECTRIXDB_BEIR_CACHE``.
``--dense-model`` builds one with another model beside it, the model's name in
the folder's, and the run stops if the collection did not open with the model
asked for: a cut-off quoted for a model has to come from that model.

The number is this model's on these documents. It moves with both, which is
the point of measuring: the docs quote it as an example of the method, and
never as a cut-off to borrow.
"""

from __future__ import annotations

import argparse
import datetime
import json
import platform
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import beir_eval  # noqa: E402


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   a BEIR dataset's name; --json for where the result goes; --dense-
#         model for a model other than the bundled one
# OUTPUT  the similarity that best separates an answer from a near miss, for
#         this model on these documents, printed and written as JSON when
#         asked
#
# Every judged query is searched once, dense, over the index beir_eval.py
# built and cached. Its top result is an answer when the judges marked that
# document relevant and a near miss when they did not, and the library's
# answer_cutoff places the line between the two. The run stops if the
# collection did not open with the model asked for: a cut-off quoted for a
# model has to come from that model. The number is this model's on these
# documents; the docs quote it as an example of the method, never as a cut-off
# to borrow.
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", nargs="?", default="scifact")
    parser.add_argument("--dense-model", default=None, help="a model by its alias, e5-small; left out, the default model")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    import vectrixdb
    from vectrixdb import Vectrix
    from vectrixdb.evaluation import Question, answer_cutoff

    corpus, queries, qrels = beir_eval.load(args.dataset)
    if args.dense_model:
        path = beir_eval.CACHE / "index" / f"{args.dataset}-dense-{args.dense_model}-full-{len(corpus)}"
        db = Vectrix("beir", path=str(path), mode="dense", dense_model=args.dense_model)
        if db.count() != len(corpus):
            db.clear()
            db.add(list(corpus.values()), ids=list(corpus), progress=True)
        if args.dense_model.split("-")[0] not in str(db.model_name):
            print(f"asked for {args.dense_model} and the collection opened with {db.model_name}", file=sys.stderr)
            return 1
    else:
        db, _ = beir_eval.build(args.dataset, "dense", corpus, False, None)
    questions = [Question(text, expected=sorted(qrels[qid]), id=qid) for qid, text in queries.items()]
    try:
        # A BEIR document is one row whose id is the corpus id, so the grain is the row.
        found = answer_cutoff(db, questions, by="chunk", mode="dense")
    finally:
        db.close()

    print(f"{args.dataset}, {found['model']}: {found['answers']} answers and {found['near_misses']} near misses at the top of {found['questions']} queries")
    print(f"an answer outscores a near miss {found['separation']:.1%} of the time; cut-off {found['cutoff']}")
    print("| Cut-off | right when it answers | answers kept | near misses declined |")
    print("| ---: | ---: | ---: | ---: |")
    for row in found["table"]:
        mark = " (chosen)" if row["cutoff"] == found["cutoff"] else ""
        print(f"| {row['cutoff']:.3f}{mark} | {row['precision']:.1%} | {row['answered']:.1%} | {row['declined']:.1%} |")
    if args.json:
        provenance = {
            "dataset": args.dataset,
            "vectrixdb": vectrixdb.__version__,
            "date": datetime.date.today().isoformat(),
            "machine": f"{platform.machine()} {platform.system()} Python {platform.python_version()}",
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({**provenance, **found}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
