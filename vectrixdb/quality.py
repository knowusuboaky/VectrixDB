"""Is this text the output of an extraction that worked?

A scanned PDF that OCRs badly produces chunks that pass every schema check
and destroy retrieval: the fields are all there, the text is nonsense, and
nothing downstream can tell. This is closer to signal reliability than to
validation, and the roadmap held it back until it could be measured against
a labelled set rather than shipped as a heuristic nobody trusts.

So it is measured. :func:`extraction_quality` scores a text on five signals
that OCR failure moves and clean prose does not, and the default threshold
comes from :func:`calibrate` run over a labelled set: real prose from this
repository's own documentation, and the same prose degraded by a simulator of
the substitutions, joins and glyph noise OCR actually produces, at a range of
noise levels. ``scripts/quality_eval.py`` rebuilds that set and prints the
separation; the numbers it printed when the threshold was set are in the
docstring of :data:`DEFAULT_THRESHOLD`.

What it is not: a language model, a dictionary, or a judgement about
meaning. A page of clean text about nothing scores well. What it catches is
the failure that reads like a permissions problem or a ranking problem when
it is an ingestion problem, and it says so at the write, where the stack
trace points at the pipeline.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "DEFAULT_THRESHOLD",
    "ExtractionQuality",
    "extraction_quality",
    "Calibration",
    "calibrate",
    "degrade",
]


# ============================================================================
# SETTINGS: the shipped threshold, the common words, and the token shapes
# ============================================================================
#
# The threshold set from scripts/quality_eval.py, the common words clean prose
# is full of, and the shapes a token, a letter run and a vowel take.

#: Score at or above which a text is treated as a working extraction.
#:
#: Set by ``scripts/quality_eval.py`` over 524 labelled samples: 262 prose
#: paragraphs from these docs and the same 262 degraded at 5%, 10%, 20% and
#: 40% of characters. The line is defined operationally. Five percent noise
#: is a page a person can still read and counts as clean; twenty percent and
#: above destroys retrieval and counts as bad; ten percent is reported and
#: left out of the calibration as the ambiguous band.
#:
#: The run that set it: clean mean 0.931 (min 0.707); 5% noise mean 0.890
#: (min 0.772); 10% mean 0.827; 20% mean 0.726 (max 0.820); 40% mean 0.544
#: (max 0.713). Separation against clean, as AUC: 0.854 at 5%, 0.974 at
#: 10%, 0.996 at 20%, 1.000 at 40%. At this threshold on the decisive set:
#: precision 0.948, recall 0.988, balanced accuracy 0.926, AUC 0.994. The
#: best threshold for that set was 0.81; 0.78 gives up a little precision to
#: keep every readable page. Measured again on 2026-09-20, the docs having
#: grown to 613 paragraphs and the ``varied`` signal being in: precision
#: 0.957, recall 0.987, balanced accuracy 0.938, AUC 0.995, and the same to
#: the third decimal with ``varied`` switched off, which is the point of it
#: scaling the score rather than being weighed in. The first cut of that
#: signal did cost recall: it took a table's row of "yes yes yes yes" for a
#: loop. Re-run the script before changing this, and
#: calibrate() against your own labelled set if your documents are not
#: English prose.
DEFAULT_THRESHOLD = 0.78

# Common English words. Function words and the most frequent content words:
# clean prose hits this list often, OCR garbage almost never, and it needs no
# dictionary dependency. Deliberately short, since the signal is the hit
# rate rather than coverage.
_COMMON = frozenset(
    """
    the of and to in a is that for it as was with be by on not he this are or
    his from at which but have an had they you were their one all we can her
    has there been if more when will would who so no out up into than them
    these some could its then two other do time may only new like now over
    such our any many made after also did before must through back years where
    much your way well down should because each just those people how too
    little state good very make world still own see men work long get here
    between both life being under never day same another know while last
    might us great old year off come since against go came right used take
    three states himself few house use during without again place american
    around however home small found mrs thought went say part once general
    high upon school every don't does got united left number course war until
    always away something fact though water less public put think almost hand
    enough far took head yet government system better set told nothing night
    end why called didn't eyes find going look asked later knew point next
    program city business give group toward young days let room president side
    social given present several order national possible rather second face
    per among form important often things looked early white case become
    large need big four within felt along children saw best church ever least
    power development light thing seemed family interest want members mind
    country area others turned although open god problem service certain kind
    began different door thus help sense whole matter perhaps itself times
    human law line above name example action company local show whether five
    history gave today either act feet across taken quite anything having
    seen death week experience really field hands nature past table report
    data first policy value record total change result question account
    """.split()
)

_TOKEN = re.compile(r"\S+")
_ALPHA = re.compile(r"^[A-Za-z]+$")
_MIXED = re.compile(r"(?=.*\d)(?=.*[A-Za-z])")
_PLAIN = re.compile(r"[A-Za-z0-9\s.,;:!?'\"()\-‘’“”/&%$]")
_VOWELS = set("aeiouyAEIOUY")


# ============================================================================
# THE SCORE
# ============================================================================
#
# INPUT   a text, and a threshold
# OUTPUT  the score, the signals it came from, and the verdict at the
#         threshold; whether the text goes somewhere or loops
#
# Five signals OCR failure moves and clean prose does not, scaled by a sixth.


@dataclass(frozen=True)
class ExtractionQuality:
    """The score, the signals it came from, and the verdict at a threshold.

    ``score`` is in [0, 1], higher is cleaner. ``signals`` are the five
    components, each in [0, 1], so a low score can be read: a text that is
    all symbols fails ``plain_characters``; one that is words nobody has seen
    fails ``known_words``; one cut into fragments fails ``whole_words``.
    """

    score: float
    threshold: float
    signals: Mapping[str, float] = field(default_factory=dict)
    tokens: int = 0

    @property
    def usable(self) -> bool:
        return self.score >= self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "threshold": self.threshold,
            "usable": self.usable,
            "tokens": self.tokens,
            "signals": dict(self.signals),
        }


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def _varied(tokens: Sequence[str]) -> float:
    """1.0 for text that goes somewhere, towards 0 for text that loops.

    OCR fails this way on noise, the same word a hundred times, and a vision
    model fails this way on a page it cannot read, the same sentence over and
    over. Two measures, the worse one counts: the longest run of one token,
    which is fine up to eight, because a table has rows of "yes yes yes yes"
    and a rule of dashes under its header, and gone by twenty; and,
    past thirty tokens, how many of the four-token windows are different.
    A list or a table of rows stays well over half. A sentence said three
    times is about a third and is let through, since a refrain or a repeated
    disclaimer is somebody's real text; by five times it is under a quarter,
    and a model that loops says its sentence dozens of times.
    """
    if not tokens:
        return 1.0
    longest = run = 1
    for before, after in zip(tokens, tokens[1:]):
        run = run + 1 if before == after else 1
        longest = max(longest, run)
    by_run = 1.0 if longest <= 8 else _clip(1.0 - (longest - 8) / 12.0)
    windows = [tuple(tokens[i : i + 4]) for i in range(len(tokens) - 3)]
    by_windows = _clip(len(set(windows)) / len(windows) / 0.3) if len(windows) >= 30 else 1.0
    return min(by_run, by_windows)


def _is_row(line: str) -> bool:
    """Whether a line is one the readers write for a table's row, or a figure line.

    ``Region: EMEA; Revenue: 1200`` or ``Net income; 2025: 300; 2024: 330``:
    parts split by "; ", every part short, every part but a leading label
    written ``key: value``, and three parts or more, or a figure among them.
    Prose that happens to hold a semicolon and a colon, "rates rose; however:
    ...", has long parts or fails one of the others. A Markdown pipe row, a
    ``[Figure: ...]`` line and the ``Words in the picture: ...`` line under
    one count too: a list of what a picture prints is not sentences.
    """
    s = line.strip()
    if not s:
        return False
    if s.startswith("|") and s.endswith("|") and s.count("|") >= 3:
        return True
    if (s.startswith("[Figure:") and s.endswith("]")) or s.startswith("Words in the picture:"):
        return True
    parts = s.split("; ")
    if len(parts) < 2 or any(len(part) > 120 for part in parts):
        return False
    keyed = sum(1 for part in parts if ": " in part and 0 < len(part.split(": ", 1)[0]) <= 60)
    if keyed < len(parts) - 1:
        return False
    return len(parts) >= 3 or any(c.isdigit() for c in s)


def extraction_quality(text: str, threshold: float = DEFAULT_THRESHOLD) -> ExtractionQuality:
    """Score a text's prose on five signals OCR failure moves and clean prose does not, scaled by a sixth.

    A table's rows are not prose. ``Region: EMEA; Revenue: 1200`` has more
    figures than words, repeats its column names on every line and uses no
    common words, which is everything a failed OCR pass looks like, and a
    clean spreadsheet scored 0.20 against a line of 0.78. So the lines a
    reader writes for rows, and its figure lines, are set aside, and what
    is left is judged. A text that is nearly all rows has too little prose
    to judge, and is usable, as a two-word heading is.
    """
    lines = text.split("\n")
    rows = [line for line in lines if _is_row(line)]
    if not rows:
        return _prose_quality(text, threshold)
    prose = "\n".join(line for line in lines if not _is_row(line))
    judged = _prose_quality(prose, threshold)
    total = len(_TOKEN.findall(text))
    if judged.tokens < 5:
        # Too little prose to judge: usable, with the signals of the whole text reported.
        whole = _prose_quality(text, threshold)
        return ExtractionQuality(score=max(whole.score, threshold), threshold=threshold, signals=whole.signals, tokens=total)
    return ExtractionQuality(score=judged.score, threshold=threshold, signals=judged.signals, tokens=total)


def _prose_quality(text: str, threshold: float = DEFAULT_THRESHOLD) -> ExtractionQuality:
    """Score a text on five signals OCR failure moves and clean prose does not, scaled by a sixth.

    * ``plain_characters``: the share of characters that are letters, digits,
      whitespace or ordinary punctuation. Glyph noise fails it.
    * ``whole_words``: the share of tokens that are alphabetic and not a lone
      letter, minus tokens that mix digits into letters. Broken words fail it.
    * ``pronounceable``: the share of alphabetic tokens with a vowel ratio a
      word can have. Consonant runs fail it.
    * ``known_words``: the hit rate against a short list of common English
      words, scaled so that ordinary prose saturates it. Nonsense fails it.
    * ``word_length``: how far the mean alphabetic token length is from the
      range prose lives in. Joined words and shredded words both fail it.
    * ``varied``: 1 unless the text loops, the same word or the same sentence
      over and over. It multiplies the score: see :func:`_varied`.

    Fewer than five tokens is not enough to judge, and scores as usable with
    the signals it could compute, because a two-word heading is not an OCR
    failure and refusing it would be the heuristic nobody trusts.
    """
    tokens = _TOKEN.findall(text)
    count = len(tokens)
    if count == 0:
        return ExtractionQuality(score=0.0, threshold=threshold, signals={}, tokens=0)

    characters = [c for c in text if not c.isspace()]
    plain = sum(1 for c in characters if _PLAIN.match(c)) / max(len(characters), 1)

    alpha = [t for t in tokens if _ALPHA.match(t)]
    lone = sum(1 for t in alpha if len(t) == 1 and t.lower() not in ("a", "i"))
    mixed = sum(1 for t in tokens if _MIXED.match(t) and len(t) > 1)
    whole = _clip((len(alpha) - lone - mixed) / count)

    if alpha:
        pronounceable = sum(
            1
            for t in alpha
            if len(t) == 1 or 0.15 <= sum(1 for c in t if c in _VOWELS) / len(t) <= 0.8
        ) / len(alpha)
        known = sum(1 for t in alpha if t.lower() in _COMMON) / len(alpha)
        # Ordinary prose hits the list on roughly 45% of its words; OCR at
        # 20% noise still hits "the" and "of", so the bar is set where prose
        # saturates rather than where garbage starts to score.
        known = _clip(known / 0.45)
        mean_length = sum(len(t) for t in alpha) / len(alpha)
        # Prose sits between about 3.5 and 6.5 characters per word; shredded
        # text falls under, joined text runs over.
        if 3.0 <= mean_length <= 7.0:
            length = 1.0
        elif mean_length < 3.0:
            length = _clip(mean_length / 3.0)
        else:
            length = _clip(1.0 - (mean_length - 7.0) / 8.0)
    else:
        pronounceable = known = length = 0.0

    varied = _varied([t.lower() for t in tokens])
    signals = {
        "plain_characters": round(plain, 4),
        "whole_words": round(whole, 4),
        "pronounceable": round(pronounceable, 4),
        "known_words": round(known, 4),
        "word_length": round(length, 4),
        "varied": round(varied, 4),
    }
    # Weighted by how much each moved on the labelled set. Broken words and
    # the hit rate carry it; pronounceability barely moves under OCR noise,
    # because shape-alike substitutions keep the vowels, so it is reported
    # and nearly unweighted.
    weights = {
        "plain_characters": 0.2,
        "whole_words": 0.4,
        "pronounceable": 0.05,
        "known_words": 0.25,
        "word_length": 0.1,
    }
    # Repetition is not weighed with the rest, it scales them: text that
    # loops is made of perfectly good words, so every other signal is at its
    # best exactly when this one is at its worst. Ordinary text has it at 1,
    # which leaves the score, and the line it was calibrated on, where it was.
    score = sum(signals[k] * w for k, w in weights.items()) * varied

    if count < 5:
        # Not enough to judge. Report what was computed, and do not fail it.
        return ExtractionQuality(
            score=max(round(score, 4), threshold),
            threshold=threshold,
            signals=signals,
            tokens=count,
        )
    return ExtractionQuality(
        score=round(score, 4), threshold=threshold, signals=signals, tokens=count
    )


# ============================================================================
# THE LABELLED SET: prose, and prose put through what OCR does to it
# ============================================================================
#
# INPUT   a text, a level and a seed; (text, is_clean) pairs and a threshold
# OUTPUT  the text put through a simulation of a bad OCR pass; what the set
#         says about a threshold: the AUC, and precision, recall and balanced
#         accuracy at it
#
# A scanned PDF that OCRs badly produces chunks that pass every schema check
# and destroy retrieval; this is how the detector is measured.

#: What OCR gets wrong, as substitutions seen in real output: shape-alike
#: glyphs, the rn/m and cl/d confusions, and dropped or inserted spaces.
_CONFUSIONS = {
    "l": ["1", "I", "|"],
    "I": ["l", "1"],
    "O": ["0", "Q"],
    "o": ["0", "c"],
    "0": ["O"],
    "e": ["c", "o"],
    "a": ["o", "@"],
    "s": ["5", "$"],
    "S": ["5"],
    "B": ["8"],
    "g": ["q", "9"],
    "t": ["f", "+"],
    "h": ["b", "n"],
    "u": ["v", "ll"],
    "n": ["ri", "m"],
    "m": ["rn", "nn"],
    "d": ["cl"],
    "w": ["vv"],
    "i": ["j", ";"],
}
_NOISE = "~^`*#|\\{}[]<>°¬§¦"


def degrade(text: str, level: float, seed: int = 0) -> str:
    """Put ``text`` through a simulation of a bad OCR pass.

    ``level`` is the share of characters touched, from 0 (untouched) to 1.
    Each touched character is swapped for a shape-alike, dropped, joined to
    its neighbour by losing the space, or replaced with glyph noise, in the
    proportions those failures show up in real output. Deterministic for a
    given ``seed``, so a labelled set can be rebuilt.
    """
    rng = random.Random(seed)
    out = []
    for c in text:
        if rng.random() >= level:
            out.append(c)
            continue
        roll = rng.random()
        if c.isspace():
            # Lost spaces join words; extra spaces split them.
            out.append("" if roll < 0.7 else "  ")
        elif c in _CONFUSIONS and roll < 0.6:
            out.append(rng.choice(_CONFUSIONS[c]))
        elif roll < 0.75:
            out.append(rng.choice(_NOISE))
        elif roll < 0.9:
            out.append("")
        else:
            out.append(c + " ")
    return "".join(out)


@dataclass(frozen=True)
class Calibration:
    """What a labelled set says about a threshold."""

    threshold: float
    samples: int
    positives: int
    precision: float
    recall: float
    balanced_accuracy: float
    auc: float
    clean_mean: float
    degraded_mean: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "samples": self.samples,
            "positives": self.positives,
            "precision": self.precision,
            "recall": self.recall,
            "balanced_accuracy": self.balanced_accuracy,
            "auc": self.auc,
            "clean_mean": self.clean_mean,
            "degraded_mean": self.degraded_mean,
        }


def _auc(scored: Sequence[tuple[float, bool]]) -> float:
    """Probability a random clean sample outscores a random degraded one."""
    clean = [s for s, ok in scored if ok]
    bad = [s for s, ok in scored if not ok]
    if not clean or not bad:
        return float("nan")
    wins = 0.0
    for c in clean:
        for b in bad:
            wins += 1.0 if c > b else 0.5 if c == b else 0.0
    return wins / (len(clean) * len(bad))


def calibrate(
    labelled: Iterable[tuple[str, bool]],
    threshold: Optional[float] = None,
) -> Calibration:
    """Measure the detector against ``(text, is_clean)`` pairs.

    With ``threshold`` given, reports precision and recall at it. Without,
    picks the threshold that maximises balanced accuracy over the set, which
    is how :data:`DEFAULT_THRESHOLD` was chosen. Precision and recall are for
    the clean class: precision is how often "usable" is right, recall is how
    much clean text is kept.
    """
    scored = [(extraction_quality(text).score, bool(ok)) for text, ok in labelled]
    if not scored:
        raise ValueError("calibrate() needs at least one labelled sample")

    def at(cut: float) -> tuple[float, float, float]:
        tp = sum(1 for s, ok in scored if ok and s >= cut)
        fp = sum(1 for s, ok in scored if not ok and s >= cut)
        fn = sum(1 for s, ok in scored if ok and s < cut)
        tn = sum(1 for s, ok in scored if not ok and s < cut)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        specificity = tn / (tn + fp) if tn + fp else 0.0
        return precision, recall, (recall + specificity) / 2

    if threshold is None:
        candidates = sorted({round(s, 2) for s, _ in scored})
        threshold = max(candidates, key=lambda c: (at(c)[2], -c))
    precision, recall, balanced = at(threshold)
    clean = [s for s, ok in scored if ok]
    bad = [s for s, ok in scored if not ok]
    return Calibration(
        threshold=threshold,
        samples=len(scored),
        positives=len(clean),
        precision=round(precision, 4),
        recall=round(recall, 4),
        balanced_accuracy=round(balanced, 4),
        auc=round(_auc(scored), 4),
        clean_mean=round(sum(clean) / len(clean), 4) if clean else float("nan"),
        degraded_mean=round(sum(bad) / len(bad), 4) if bad else float("nan"),
    )
