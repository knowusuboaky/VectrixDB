"""Record retrieval quality so a drop fails a change.

    python scripts/recall_baseline.py            # print current vs recorded
    python scripts/recall_baseline.py --update   # record the current numbers

The corpus and questions live in tests/fixtures/qa_pairs.json and are shared
with scripts/compare_modes.py. The baseline in tests/fixtures/recall_baseline.json
is checked by tests/unit/test_recall_baseline.py with a small tolerance.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


# ============================================================================
# SETTINGS: the pairs and the baseline
# ============================================================================
#
# The question and document pairs in tests/fixtures/qa_pairs.json, shared with
# compare_modes.py, and the baseline in tests/fixtures/recall_baseline.json.

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectrixdb import Vectrix  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAIRS_FILE = ROOT / "tests" / "fixtures" / "qa_pairs.json"
BASELINE = ROOT / "tests" / "fixtures" / "recall_baseline.json"


# ============================================================================
# LOADING AND MEASURING
# ============================================================================
#
# INPUT   a mode and the pairs
# OUTPUT  MRR and recall@1 over the questions
#
# Each question is asked the way a user asks it, in the mode named.


def load_pairs() -> list:
    return json.loads(PAIRS_FILE.read_text(encoding="utf-8"))


def measure(mode: str, pairs: list) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = Vectrix("baseline", path=tmp, mode=mode)
        docs = [p["document"] for p in pairs]
        db.add(docs)
        rr = 0.0
        hits1 = 0
        for i, pair in enumerate(pairs):
            texts = db.search(pair["question"], limit=10).texts
            if docs[i] in texts:
                rank = texts.index(docs[i]) + 1
                rr += 1.0 / rank
                hits1 += rank == 1
        db.close()
    return {"mrr": round(rr / len(pairs), 4), "recall1": round(hits1 / len(pairs), 4)}


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --update
# OUTPUT  current against recorded, or the baseline rewritten
#
# tests/unit/test_recall_baseline.py checks the baseline with a small
# tolerance, so a drop fails a change.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--modes", nargs="+", default=["dense", "hybrid"])
    args = parser.parse_args(argv)

    pairs = load_pairs()
    current = {mode: measure(mode, pairs) for mode in args.modes}
    recorded = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    for mode, scores in current.items():
        before = recorded.get(mode, {})
        print(
            f"{mode:8} mrr {scores['mrr']:.4f} (recorded {before.get('mrr', '-')})  "
            f"recall@1 {scores['recall1']:.2f} (recorded {before.get('recall1', '-')})"
        )
    if args.update:
        recorded.update(current)
        BASELINE.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
