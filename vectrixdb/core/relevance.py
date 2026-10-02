"""One number, from 0 to 1, that means the same thing whichever engine answered.

A search result has always had a ``score``, and the score has always meant
something different depending on where it came from: a cosine similarity from
the local index, ``1 / (1 + distance)`` from Azure AI Search, ``(2 - d) / 2``
or ``1 / (1 + d)`` from OpenSearch depending on its version and engine, a
BM25 weight with no ceiling from a keyword search, and from a hybrid search a
sum of reciprocal ranks that tops out near 0.018 and says nothing about how
good the match is. ``score`` is left exactly as it was, because programs read
it and the order of results comes from it.

``relevance`` is the other question: how well does this chunk match, on a
scale that can carry a threshold. It is what lets a caller say "the best
thing I found is not good enough, so do not answer from it", which a rank
can never say, because the top of a ranking is the top whatever is in it.

``relevance_kind`` says what the number is, because the honest sources of it
are not interchangeable:

``similarity``
    The cosine similarity between the query and the chunk, with anything
    below zero shown as zero. Comparable across engines, and what a
    threshold is normally set on. Typical values depend on the embedding
    model.
``reranker``
    A cross-encoder read the query and the chunk together and judged the
    pair. The best absolute signal there is when it is available. A model
    that returns a logit is passed through a sigmoid; one that already
    returns 0 to 1 is left alone.
``distance``
    The collection's metric is not cosine, so the number is
    ``1 / (1 + distance)``: bounded and ordered, and not comparable with a
    cosine similarity.
``relative``
    A keyword score has no ceiling, so there is no honest absolute number.
    This is the score as a share of the best hit in the same result list:
    the top hit is always 1.0, which says it won and not that it is good.

When no honest number exists, ``relevance`` is None. A backend whose scoring
is not known here reports None and not a guess.

Where the engine formulas come from. Azure AI Search documents
``@search.score = 1 / (1 + cosine_distance)`` and the conversion back
("Vector relevance and ranking", read 2026-09-18). OpenSearch documented
``1 / (1 + d)`` for nmslib and faiss and ``(2 - d) / 2`` for Lucene through
2.17, and ``(2 - d) / 2`` for every engine from 2.19 ("k-NN vector, Spaces").
Because that depends on a cluster's version and engine, the OpenSearch store
does not trust a table: it checks both formulas against a hit whose vector it
can read, and uses the one that reproduces the true cosine.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence

__all__ = [
    "DISTANCE",
    "MATCHED_KEYWORDS",
    "MATCHED_MEANING",
    "RELATIVE",
    "RERANKER",
    "SIMILARITY",
    "cosine",
    "from_azure_cosine_score",
    "from_cosine",
    "from_opensearch_cosine_score",
    "from_reranker",
    "pick_opensearch_formula",
    "relative",
]


# ============================================================================
# SETTINGS: the kinds of relevance, and the OpenSearch formulas
# ============================================================================
#
# The kinds a relevance can be, what a result matched on, and the two
# documented formulas an OpenSearch cosine score may have come from.

SIMILARITY = "similarity"
RERANKER = "reranker"
DISTANCE = "distance"
RELATIVE = "relative"

MATCHED_MEANING = "meaning"
MATCHED_KEYWORDS = "keywords"

#: OpenSearch's two documented ways of turning a cosine distance into a score.
OPENSEARCH_FORMULAS = ("half", "reciprocal")


# ============================================================================
# FROM EACH ENGINE, ONE NUMBER
# ============================================================================
#
# INPUT   a cosine similarity, an Azure or OpenSearch score, a cross-encoder's
#         scores, or scores with no ceiling
# OUTPUT  a relevance from 0 to 1 that means the same thing whichever engine
#         answered
#
# Opposite and unrelated are both not a match: zero. A search result has
# always had a score, and the score has always meant something different each
# time; this is the one number the pages show.


def _unit(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


def from_cosine(similarity: Optional[float]) -> Optional[float]:
    """A cosine similarity as a relevance. Opposite and unrelated are both "not a match": zero."""
    if similarity is None or math.isnan(similarity):
        return None
    return round(_unit(similarity), 6)


def from_azure_cosine_score(score: Optional[float]) -> Optional[float]:
    """Azure AI Search's ``@search.score`` for a cosine field, turned back into the cosine.

    Microsoft's own conversion: ``cosine_distance = (1 - score) / score`` and
    ``similarity = 1 - cosine_distance``. A score at or below zero is not one
    this formula produced, so it gets no answer.
    """
    if score is None or score <= 0.0 or math.isnan(score):
        return None
    return from_cosine(1.0 - (1.0 - score) / score)


def from_opensearch_cosine_score(score: Optional[float], formula: Optional[str]) -> Optional[float]:
    """An OpenSearch ``_score`` for a ``cosinesimil`` field, turned back into the cosine.

    ``half`` is ``score = (2 - d) / 2``, so ``cos = 2 * score - 1``.
    ``reciprocal`` is ``score = 1 / (1 + d)``, so ``cos = 2 - 1 / score``.
    With no formula established there is no answer.
    """
    if score is None or formula is None or math.isnan(score):
        return None
    if formula == "half":
        return from_cosine(2.0 * score - 1.0)
    if formula == "reciprocal":
        return None if score <= 0.0 else from_cosine(2.0 - 1.0 / score)
    return None


def pick_opensearch_formula(
    score: float, true_cosine: float, tolerance: float = 0.01
) -> Optional[str]:
    """Which documented formula turns this score into this cosine, or None if neither does."""
    fits = []
    for formula in OPENSEARCH_FORMULAS:
        got = from_opensearch_cosine_score(score, formula)
        if got is not None and abs(got - _unit(true_cosine)) <= tolerance:
            fits.append(formula)
    # Both agree only where the two curves cross (a perfect match), which settles nothing.
    return fits[0] if len(fits) == 1 else None


def cosine(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """The cosine similarity of two vectors, or None when either has no length."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return None
    return dot / (na * nb)


def from_reranker(scores: Iterable[float]) -> List[float]:
    """A cross-encoder's scores for one result list, as relevances.

    Decided for the list as a whole: if every score is already between 0 and
    1 the model gives probabilities and they are kept; otherwise they are
    logits and go through a sigmoid. Deciding per score would treat a logit
    of 0.4 as a probability and the logit of 3.1 beside it as a logit.
    """
    values = [float(s) for s in scores]
    if not values:
        return []
    if all(0.0 <= v <= 1.0 for v in values):
        return [round(v, 6) for v in values]
    return [round(1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, v)))), 6) for v in values]


def relative(scores: Iterable[Optional[float]]) -> List[Optional[float]]:
    """Each score as a share of the best in the list. For scores with no ceiling."""
    values = list(scores)
    present = [v for v in values if v is not None and v > 0.0]
    if not present:
        return [None for _ in values]
    best = max(present)
    return [None if v is None else round(_unit(v / best), 6) for v in values]
