"""Measure the extraction quality detector against a labelled set.

The set is real prose from this repository's documentation, paragraph by
paragraph with the markdown stripped, and the same paragraphs put through
``vectrixdb.quality.degrade`` at a range of noise levels. Half the samples
are clean, half are degraded, and the seed is fixed so the set rebuilds the
same way every time.

Prints the mean score per class at each noise level, the separation (AUC),
and precision, recall and balanced accuracy at the shipped threshold and at
the best one for this set. ``vectrixdb.quality.DEFAULT_THRESHOLD`` is set
from this output and its docstring quotes it; re-run before changing either.

    python scripts/quality_eval.py
    python scripts/quality_eval.py --threshold 0.6
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path


# ============================================================================
# SETTINGS: the levels and the markup
# ============================================================================
#
# Where the docs are, the noise levels the paragraphs are degraded at, and the
# markdown stripped from them before they are scored.

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vectrixdb.quality import DEFAULT_THRESHOLD, calibrate, degrade, extraction_quality  # noqa: E402

LEVELS = (0.05, 0.10, 0.20, 0.40)
_FENCE = re.compile(r"```.*?```", re.S)
_MARKUP = re.compile(r"[#*_`>|\[\]()]|https?://\S+")


# ============================================================================
# PARAGRAPHS AND THE LABELLED SET
# ============================================================================
#
# INPUT   a minimum word count; then a seed
# OUTPUT  prose paragraphs from the docs, markdown stripped and fences
#         dropped; (text, is_clean, noise_level) triples, balanced across
#         classes
#
# Half the samples are clean and half are put through
# vectrixdb.quality.degrade, with the seed fixed so the set rebuilds the same
# way every time.


def paragraphs(minimum_words: int = 25) -> list[str]:
    """Prose paragraphs from the docs, markdown stripped, fences dropped."""
    found = []
    for page in sorted((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]:
        text = _FENCE.sub("", page.read_text(encoding="utf-8"))
        for block in text.split("\n\n"):
            block = _MARKUP.sub("", block).replace("\n", " ").strip()
            words = block.split()
            # Prose, not a CLI transcript or a table that survived the strip:
            # the label is "clean prose", so that is what the sample has to be.
            wordish = sum(1 for w in words if re.fullmatch(r"[A-Za-z][A-Za-z'-]*[.,;:!?)]?", w))
            if (
                len(words) >= minimum_words
                and not block.startswith("|")
                and wordish / len(words) >= 0.85
            ):
                found.append(block)
    return found


def labelled_set(seed: int = 7) -> list[tuple[str, bool, float]]:
    """``(text, is_clean, noise_level)`` triples, balanced across classes.

    Every paragraph appears once clean and once degraded, the noise level
    assigned round-robin so each level gets the same share of the prose.
    """
    rng = random.Random(seed)
    prose = paragraphs()
    rng.shuffle(prose)
    samples: list[tuple[str, bool, float]] = [(text, True, 0.0) for text in prose]
    for i, text in enumerate(prose):
        level = LEVELS[i % len(LEVELS)]
        samples.append((degrade(text, level, seed=rng.randrange(1 << 30)), False, level))
    return samples


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --threshold
# OUTPUT  the mean score per class at each noise level, the AUC, and
#         precision, recall and balanced accuracy at the shipped threshold and
#         at the best one for this set
#
# vectrixdb.quality.DEFAULT_THRESHOLD is set from this output and its
# docstring quotes it; re-run before changing either.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    samples = labelled_set()
    clean = [s for s in samples if s[1]]
    print(f"{len(samples)} samples: {len(clean)} clean, {len(samples) - len(clean)} degraded\n")

    scores = {
        level: [extraction_quality(t).score for t, _, lv in samples if lv == level]
        for level in (0.0, *LEVELS)
    }
    print(f"{'noise':>8}  {'mean':>6}  {'min':>6}  {'max':>6}  {'AUC vs clean':>12}")
    for level in (0.0, *LEVELS):
        row = scores[level]
        if level == 0.0:
            auc = ""
        else:
            paired = [(s, True) for s in scores[0.0]] + [(s, False) for s in row]
            auc = (
                f"{calibrate([(t, ok) for t, ok, lv in samples if lv in (0.0, level)]).auc:>12.3f}"
            )
        print(
            f"{level:>8.0%}  {sum(row) / len(row):>6.3f}  {min(row):>6.3f}  {max(row):>6.3f}  {auc:>12}"
        )

    # The operational definition of the line. Five percent noise is a page a
    # person can still read and counts as clean; twenty percent and above
    # destroys retrieval and counts as bad; ten percent is reported above
    # and left out of the calibration, because it is the ambiguous band.
    decisive = [(t, lv <= 0.05) for t, ok, lv in samples if lv != 0.10]
    shipped = calibrate(decisive, threshold=args.threshold)
    best = calibrate(decisive)
    print()
    for label, c in (
        ("at the shipped threshold", shipped),
        ("at the best threshold for this set", best),
    ):
        print(
            f"{label}: threshold {c.threshold:.2f}  precision {c.precision:.3f}  "
            f"recall {c.recall:.3f}  balanced accuracy {c.balanced_accuracy:.3f}  AUC {c.auc:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
