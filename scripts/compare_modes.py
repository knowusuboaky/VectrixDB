"""Does ultimate mode (ColBERT) beat hybrid, and does hybrid beat dense?

    python scripts/compare_modes.py [--doc docs/explanation/search-modes.md]

An honest, small evaluation: 48 short documents and 48 questions written by
hand, one question per document, phrased so the answer shares few words with
the question. Each mode is scored by MRR@10 and recall@1 over all questions.
It is not BEIR; it is enough to say whether the extra models earn their cost
on ordinary English, and to notice a regression. The docs page is regenerated
from the output so its numbers are measured, never typed.

Hybrid and ultimate rerank with the cross-encoder unless told not to, so the
table has a row for hybrid with ``rerank=False`` too: without it, the cost of
the reranker reads as the cost of BM25.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectrixdb import Vectrix  # noqa: E402


# ============================================================================
# SETTINGS: the pairs, the setups and the modes
# ============================================================================
#
# The 48 hand-written question and document pairs from
# tests/fixtures/qa_pairs.json, the setups compared, and the modes, so the
# table and the docs page come from one list.

_PAIRS_FILE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qa_pairs.json"
PAIRS = [
    (item["document"], item["question"])
    for item in json.loads(_PAIRS_FILE.read_text(encoding="utf-8"))
]

#: (label, mode, rerank). None is the mode's own default: the cross-encoder
#: for hybrid and ultimate, nothing for dense.
SETUPS = [
    ("dense", "dense", None),
    ("hybrid, rerank=False", "hybrid", False),
    ("hybrid", "hybrid", None),
    ("ultimate", "ultimate", None),
]
MODES = [label for label, _, _ in SETUPS]


# ============================================================================
# EVALUATING: each setup on the pairs
# ============================================================================
#
# INPUT   a mode, and whether to rerank
# OUTPUT  MRR@10 and recall@1 over every question, and how long it took
#
# Hybrid and ultimate rerank with the cross-encoder unless told not to, so
# hybrid without it has a row too: otherwise the cost of the reranker reads as
# the cost of BM25.


def evaluate(mode: str, rerank=None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = Vectrix("eval", path=tmp, mode=mode)
        docs = [d for d, _ in PAIRS]
        db.add(docs)
        rr = 0.0
        hits1 = 0
        started = time.perf_counter()
        for i, (_, question) in enumerate(PAIRS):
            ids = db.search(question, limit=10, rerank=rerank).texts
            if docs[i] in ids:
                rank = ids.index(docs[i]) + 1
                rr += 1.0 / rank
                hits1 += rank == 1
        elapsed = (time.perf_counter() - started) / len(PAIRS) * 1000
        db.close()
    return {"mrr": rr / len(PAIRS), "recall1": hits1 / len(PAIRS), "ms": elapsed}


# ============================================================================
# RENDERING: the table, and what it says
# ============================================================================
#
# INPUT   the results
# OUTPUT  the Markdown table, and the paragraph under it, written from the
#         numbers
#
# The reading is composed from the figures, so it cannot disagree with them.


def render(results: dict) -> str:
    lines = [
        "",
        "## Which mode earns its cost",
        "",
        f"Measured by `scripts/compare_modes.py`: {len(PAIRS)} hand-written documents and",
        "questions phrased to share few words with their answers, scored by mean",
        "reciprocal rank and recall at 1. Small on purpose, and regenerated rather",
        "than typed.",
        "",
        "| Mode | MRR@10 | Recall@1 | ms per query |",
        "| --- | ---: | ---: | ---: |",
    ]
    for mode in MODES:
        r = results[mode]
        lines.append(f"| {mode} | {r['mrr']:.3f} | {r['recall1']:.2f} | {r['ms']:.0f} |")
    lines += ["", *_reading(results), ""]
    return "\n".join(lines)


def _times(a: float, b: float) -> str:
    """How many times b costs a, in words a reader can say."""
    ratio = b / a if a else float("inf")
    return f"{ratio:.1f} times" if ratio < 10 else f"{ratio:.0f} times"


#: How a row is said in a sentence, where its label's comma would be misread.
_SAID = {"hybrid, rerank=False": "hybrid without the reranker"}


def _reading(results: dict) -> list[str]:
    """The paragraph under the table, written from the numbers, so it cannot
    keep a ratio the last run no longer has."""
    best = max(MODES, key=lambda m: (results[m]["mrr"], -results[m]["ms"]))
    cheapest = min(MODES, key=lambda m: results[m]["ms"])
    base = results[cheapest]["ms"]

    def said(mode: str) -> str:
        return _SAID.get(mode, mode)

    def cost(mode: str) -> str:
        return _times(base, results[mode]["ms"])

    if best == cheapest:
        opening = f"On this set {said(best)} ranks best and costs least."
    else:
        opening = f"On this set {said(best)} ranks best and {said(cheapest)} costs least."
    first, *others = [m for m in MODES if m != cheapest]
    costs = f"Per query, {said(first)} costs {cost(first)} what {said(cheapest)} does"
    if others:
        tail = [f"{said(m)} {cost(m)}" for m in others]
        costs += ", " + (", ".join(tail[:-1]) + " and " if len(tail) > 1 else "") + tail[-1]
    costs += "."

    plain, reranked = results["hybrid, rerank=False"], results["hybrid"]
    change = reranked["mrr"] - plain["mrr"]
    if change > 0.0005:
        effect = f"raises MRR by {change:.3f}"
    elif change < -0.0005:
        effect = f"lowers MRR by {-change:.3f}"
    else:
        effect = "leaves MRR where it was"
    share = (reranked["ms"] - plain["ms"]) / reranked["ms"] if reranked["ms"] else 0.0
    return [
        f"{opening} {costs}",
        "",
        "The two hybrid rows differ only by the cross-encoder, which hybrid and",
        f"ultimate run by default: on the same candidates it {effect} and takes",
        f"{max(share, 0.0):.0%} of hybrid's time. `search(rerank=False)` turns it off.",
        "",
        "That is one small set of short documents, so it argues for measuring on",
        "your own corpus rather than for any one mode everywhere: the case hybrid",
        "exists for, an exact identifier in prose, is exactly what forty-eight",
        "hand-written questions are least likely to contain.",
    ]


# ============================================================================
# WRITING: the section replaced in the doc
# ============================================================================
#
# INPUT   the doc's text and the new section
# OUTPUT  the text with the section in place of the old one, up to the next
#         heading
#
# The docs page is regenerated from the output so its numbers are measured,
# never typed.


def replace_section(text: str, section: str) -> str:
    """Put the section in the page: in place of the old one, up to the next
    heading of the same level, or at the end. Cutting at the marker and
    dropping the rest deleted every section written after this one."""
    marker = "\n## Which mode earns its cost"
    if marker not in text:
        return text.rstrip("\n") + "\n" + section
    start = text.index(marker)
    end = text.find("\n## ", start + len(marker))
    rest = text[end:] if end != -1 else ""
    return text[:start].rstrip("\n") + "\n" + section.rstrip("\n") + "\n" + rest


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --doc, the page to rewrite
# OUTPUT  the table printed, and the page rewritten when asked
#
# It is not BEIR; it is enough to say whether the extra models earn their cost
# on ordinary English, and to notice a regression.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--doc", type=Path, help="append or replace the section in this page")
    args = parser.parse_args(argv)

    results = {label: evaluate(mode, rerank) for label, mode, rerank in SETUPS}
    section = render(results)
    print(section)

    if args.doc:
        text = args.doc.read_text(encoding="utf-8") if args.doc.exists() else "# Search modes\n"
        args.doc.write_text(replace_section(text, section), encoding="utf-8")
        print(f"wrote {args.doc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
