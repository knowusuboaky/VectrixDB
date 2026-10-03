"""Entity resolution: deciding when two names denote the same thing.

Extraction produces the same entity under several surface forms. A document
mentioning "Marie Curie", then "Curie", then "M. Curie" yields three nodes
unless something recognises them as one, and every edge, community and
centrality score is then computed over a graph that believes in three people.

The previous approach compared names with ``difflib.SequenceMatcher`` against a
0.85 threshold. That metric measures character overlap, which is the wrong
question for names:

    SequenceMatcher("marie curie", "curie")   = 0.625
    SequenceMatcher("marie curie", "m. curie") = 0.737

Both are the same person and both fall short of the threshold. The signal is not
character overlap but *token structure*: one name's tokens being a subset of the
other's, or an initial standing in for a given name.

So matching runs as a short ladder of increasingly loose rules, each with a
reason attached, and a set of guards that veto a merge outright. The guards
matter more than the rules: a false merge silently fuses two real entities and
is far harder to notice than a missed one.

The approach follows Graphify (Apache 2.0), which uses token-swap detection,
an entropy floor and a numeric-token guard for the same problem over code
symbols. The thresholds here are retuned for prose, where names are longer and
less structured.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

__all__ = ["EntityMatch", "EntityResolver"]

try:  # pragma: no cover - depends on the install
    from rapidfuzz.distance import JaroWinkler as _JaroWinkler

    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _JaroWinkler = None
    _HAVE_RAPIDFUZZ = False


# ============================================================================
# JARO AND JARO-WINKLER
# ============================================================================
#
# INPUT   two names
# OUTPUT  similarity from 0 to 1, boosted for a shared prefix
#
# Suits names, where the start matters; character overlap does not.


def _jaro(a: str, b: str) -> float:
    """Jaro similarity, 0-1."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    reach = max(len(a), len(b)) // 2 - 1
    if reach < 0:
        reach = 0

    a_hit = [False] * len(a)
    b_hit = [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - reach), min(len(b), i + reach + 1)):
            if not b_hit[j] and b[j] == ch:
                a_hit[i] = b_hit[j] = True
                matches += 1
                break
    if not matches:
        return 0.0

    # Transpositions: matched characters that appear out of order.
    transpositions = 0
    k = 0
    for i, hit in enumerate(a_hit):
        if not hit:
            continue
        while not b_hit[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1
    transpositions //= 2

    return (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3.0


def _jaro_winkler(a: str, b: str, scaling: float = 0.1) -> float:
    """Jaro, boosted for a shared prefix. Suits names, where the start matters.

    Implemented here rather than relying on rapidfuzz so the same inputs give
    the same answer on every install; rapidfuzz is used when present purely for
    speed. difflib was the previous fallback and rates a one-character typo in
    "radium" at 0.67, low enough to miss real duplicates.
    """
    score = _jaro(a, b)
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return score + prefix * scaling * (1 - score)


# ============================================================================
# SETTINGS: the stopwords, the legal suffixes, the floors, and the token shapes
# ============================================================================
#
# Tokens that identify nothing on their own, the suffixes a company name
# drops, the entropy below which a label is too short to merge on, the typo
# rule's floor, and the shapes of a token and a number.

#: Tokens that carry no identifying information on their own. Two names sharing
#: only one of these are not the same entity.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "de",
        "la",
        "le",
        "van",
        "von",
        "der",
        "den",
    }
)

#: Trailing tokens that name a legal form rather than the organisation.
#: "Acme" and "Acme Corporation" are one company, so these are dropped from the
#: end of a name before anything is compared.
_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "llc",
        "llp",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "ag",
        "sa",
        "nv",
        "bv",
        "oy",
        "ab",
        "as",
        "holdings",
        "group",
    }
)

#: Below this, a label carries too little information to merge on. "AB" and "AC"
#: are close by any string metric and are not the same thing.
_MIN_ENTROPY = 2.0

#: Character-similarity floor for the typo rule, on a 0-1 scale.
_FUZZY_THRESHOLD = 0.88

_TOKEN = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)
_DIGITS = re.compile(r"\d+")


# ============================================================================
# THE MATCH, THE SIGNALS, AND THE RESOLVER
# ============================================================================
#
# INPUT   two entity names
# OUTPUT  why they were judged the same; tokens, entropy, similarity,
#         differing numbers, initials; the decision
#
# Marie Curie, Curie and M. Curie are one person, and the graph should believe
# in one.


@dataclass(frozen=True)
class EntityMatch:
    """Why two names were judged to be the same entity."""

    matched: bool
    score: float
    reason: str

    def __bool__(self) -> bool:
        return self.matched


def _tokens(name: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(name)]


def _content_tokens(name: str) -> list[str]:
    return [t for t in _tokens(name) if t not in _STOPWORDS]


def _entropy(text: str) -> float:
    """Shannon entropy over characters, as a proxy for how informative a label is."""
    text = text.lower().strip()
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(text)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _similarity(a: str, b: str) -> float:
    """Character similarity in 0-1. Jaro-Winkler when available, else a ratio.

    Jaro-Winkler rewards a shared prefix, which suits names; the stdlib fallback
    keeps this dependency-free at a small cost in quality.
    """
    if _HAVE_RAPIDFUZZ:
        return _JaroWinkler.normalized_similarity(a, b)
    return _jaro_winkler(a, b)


def _numeric_tokens_differ(a: str, b: str) -> bool:
    """True when the two names carry different numbers.

    "Phase 1" and "Phase 2" are highly similar as strings and are never the same
    entity. Neither are "GPT-4" and "GPT-5".
    """
    return set(_DIGITS.findall(a)) != set(_DIGITS.findall(b))


def _abbreviates(abbrev: list[str], full: list[str]) -> bool:
    """True when ``abbrev`` is ``full`` with the given names reduced to initials."""
    if len(abbrev) != len(full) or len(abbrev) < 2:
        return False
    if abbrev[-1] != full[-1]:
        return False
    return all(
        len(a) == 1 and len(f) > 1 and f.startswith(a) for a, f in zip(abbrev[:-1], full[:-1])
    )


def _is_initial_form(a: list[str], b: list[str]) -> bool:
    """True when either name abbreviates the other.

    Both orders are tried because the two names have the same token count, so
    there is no shorter side to pick: "m curie" and "marie curie" are both two
    tokens and either may be given first.
    """
    return _abbreviates(a, b) or _abbreviates(b, a)


class EntityResolver:
    """Decides whether two entity names refer to the same thing.

    Args:
        threshold: Character-similarity floor for the typo rule, 0-1. At 1.0
            only exact normalised matches merge, which disables every fuzzy rule.
        allow_subset: Whether a name whose content tokens are a subset of
            another's may merge into it ("Curie" into "Marie Curie"). This is the
            rule that recovers most real duplicates, and also the one most able
            to over-merge, so it can be turned off.
    """

    def __init__(self, threshold: float = _FUZZY_THRESHOLD, allow_subset: bool = True):
        self.threshold = threshold
        self.allow_subset = allow_subset

    def normalize(self, name: str) -> str:
        """Lowercase tokens with any trailing legal suffix removed."""
        tokens = _tokens(name)
        while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
            tokens = tokens[:-1]
        return " ".join(tokens)

    def _blocked(self, a: str, b: str) -> Optional[str]:
        """Return a reason to refuse the merge, or None to allow it."""
        if _numeric_tokens_differ(a, b):
            return "numeric tokens differ"
        if min(_entropy(a), _entropy(b)) < _MIN_ENTROPY:
            return "label too low-information to merge on"
        if not _content_tokens(a) or not _content_tokens(b):
            return "no content tokens"
        return None

    def compare(self, a: str, b: str) -> EntityMatch:
        """Judge whether incoming name ``a`` denotes established entity ``b``.

        The order matters for the subset rule and only for that rule. A shorter
        incoming name may fold into a longer established one, because the longer
        name is the more specific of the two: "Curie" arriving after "Marie
        Curie" is almost certainly her.

        The reverse is not true. If "Curie" is established and "Pierre Curie"
        arrives, merging would assert they are the same person on the strength of
        a shared surname. "Curie" on its own is ambiguous, and the arrival of a
        second full name is evidence of that rather than against it, so the two
        stay separate and "Curie" keeps its own node.
        """
        na, nb = self.normalize(a), self.normalize(b)

        if na == nb:
            return EntityMatch(True, 1.0, "exact")

        if self.threshold >= 1.0:
            return EntityMatch(False, 0.0, "fuzzy matching disabled")

        blocked = self._blocked(na, nb)
        if blocked:
            return EntityMatch(False, 0.0, blocked)

        ta, tb = _content_tokens(na), _content_tokens(nb)

        # Initials work in either direction: the two forms are equally specific.
        if _is_initial_form(ta, tb):
            return EntityMatch(True, 0.95, "initials")

        # Subset only folds the incoming name into the established one.
        if self.allow_subset and set(ta) < set(tb):
            if len(ta) == 1:
                if _entropy(ta[0]) < _MIN_ENTROPY:
                    return EntityMatch(False, 0.0, "shared token too generic")
                if ta[0] != tb[-1]:
                    return EntityMatch(False, 0.0, "shared token is not the head noun")
            return EntityMatch(True, 0.9, "token subset")

        score = _similarity(na, nb)
        if score >= self.threshold:
            return EntityMatch(True, score, "fuzzy")

        return EntityMatch(False, score, "below threshold")

    def find_match(self, name: str, candidates: Iterable[str]) -> Optional[tuple[str, EntityMatch]]:
        """Best candidate for ``name``, or None.

        ``name`` is the incoming name and ``candidates`` the established ones,
        an order the subset rule depends on. The strongest match wins rather than
        the first found, so the iteration order of the graph does not decide
        which entity absorbs which.
        """
        best: Optional[tuple[str, EntityMatch]] = None
        for candidate in candidates:
            match = self.compare(name, candidate)
            if match.matched and (best is None or match.score > best[1].score):
                best = (candidate, match)
                if match.score >= 1.0:
                    break
        return best
