"""Post-ranking cuts shared by ``search()`` and conversation recall.

Two ideas, both borrowed from Graphify's query renderer and retuned for a
list of documents rather than a subgraph:

* **Gap ratio.** A specific question has one or two strong answers and a tail
  of noise; a broad one has many candidates of similar strength. A fixed
  ``limit`` cannot tell them apart, so it pays the full cost on both. Dropping
  everything below a fraction of the top score makes the cost track the
  question instead of the constant.

* **Token budget with an honest cut.** Results are kept in rank order until
  the budget is spent. The top result is never cut, even if it alone exceeds
  the budget, because a caller who asked a question must at least get the
  best answer to it. When anything is cut the caller is told how much, since
  a silently shortened list reads as a complete one.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple, TypeVar

from ._tokens import TokenCounter, estimate_tokens


# ============================================================================
# SETTINGS: the item type
# ============================================================================
#
# Whatever a caller ranks, with a score and a text read through the functions
# it passes.

T = TypeVar("T")


# ============================================================================
# THE TWO CUTS: the score gap, and the token budget
# ============================================================================
#
# INPUT   ranked items, with how to read a score and a text
# OUTPUT  only the items scoring at least ratio times the best; the items
#         trimmed to a token budget
#
# Borrowed from Graphify's query renderer and retuned for a list of documents:
# a specific question has one or two strong answers and the rest is noise, and
# a budget keeps a result from flooding a model's context. Shared by search()
# and conversation recall.


def apply_score_gap(
    items: Sequence[T],
    ratio: Optional[float],
    score_of: Callable[[T], float],
) -> List[T]:
    """Keep only items scoring at least ``ratio`` times the best score.

    ``items`` must already be in descending score order. ``None`` or ``0``
    disables the cut; the first item always survives. Non-positive top scores
    disable it too, because a fraction of nothing is not a threshold.
    """
    items = list(items)
    if not items or not ratio or ratio <= 0:
        return items
    if not 0 < ratio <= 1:
        raise ValueError(f"score_gap must be in (0, 1], got {ratio!r}")
    top = score_of(items[0])
    if top <= 0:
        return items
    floor = top * ratio
    return [items[0]] + [it for it in items[1:] if score_of(it) >= floor]


def fit_to_budget(
    items: Sequence[T],
    budget: Optional[int],
    text_of: Callable[[T], str],
    counter: Optional[TokenCounter] = None,
) -> Tuple[List[T], int, int]:
    """Trim ``items`` to a token budget.

    Returns ``(kept, cut_count, token_estimate)``. Without a budget nothing
    is cut and the estimate still reports what the full list costs, so a
    caller can see the number before deciding to set one.
    """
    items = list(items)
    if budget is not None and budget < 0:
        raise ValueError(f"token_budget must be non-negative, got {budget!r}")

    costs = [estimate_tokens(text_of(it), counter) for it in items]
    if budget is None:
        return items, 0, sum(costs)

    kept: List[T] = []
    spent = 0
    for it, cost in zip(items, costs):
        # The best answer always ships. A budget smaller than one result is
        # still honoured by cutting everything after it.
        if kept and spent + cost > budget:
            break
        kept.append(it)
        spent += cost
    return kept, len(items) - len(kept), spent
