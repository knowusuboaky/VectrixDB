"""A golden row's evidence: the exact words on its pages that answer it, found again wherever they are.

The golden writer keeps a question only when every quote it gave is on its
page word for word, and it writes those quotes into the row as
``evidence``. A page says where the answer is; the quotes say what it is.
Scoring by them asks the sharper question: did the chunks handed over, or
the chunk that came back, hold the words that answer, and not only a piece
of the right page, which on a dense page may be the paragraph beside the
answer.

Word for word means what the writer meant by it: case, spacing,
punctuation and a table's bars aside, a number without its thousands
commas, and a gap marked with an ellipsis, each piece after the last. A
quote of fewer than three words is found anywhere and proves nothing, so it
is not used, and a row with no usable quote is scored by its pages as
before.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ._eval_writer import _ELLIPSIS, _QUOTE_WORDS, _words

__all__ = ["evidence_found", "evidence_span", "holds", "usable"]

Words = Tuple[Tuple[str, ...], Tuple[int, ...], Tuple[int, ...], Dict[str, List[int]]]


# ============================================================================
# A QUOTE IN A TEXT: found word for word
# ============================================================================
#
# INPUT   a quote, a text, and where to start
# OUTPUT  whether the quote proves anything, three words in one piece at
#         least; where it sits in the text, its first character and just past
#         its last; whether a chunk holds it, all of it or a share of its
#         words in a row
#
# Words are compared, not characters, so a hyphen or a line break does not
# lose a quote, and the search is by word index so a long text is not walked
# twice.


def _pieces(quote: str) -> List[List[str]]:
    return [
        p for p in ([w for w, _, _ in _words(piece)] for piece in _ELLIPSIS.split(str(quote))) if p
    ]


def usable(quote: Any) -> bool:
    """Whether a quote proves anything: at least three words in one of its pieces."""
    pieces = _pieces(str(quote or ""))
    return bool(pieces) and max(len(p) for p in pieces) >= _QUOTE_WORDS


@lru_cache(maxsize=128)
def _indexed(text: str) -> Words:
    """A text's words as compared, where each starts and ends, and where each word is, for finding a quote fast."""
    folded, starts, ends = [], [], []
    where: Dict[str, List[int]] = {}
    for n, (word, raw, at) in enumerate(_words(text)):
        folded.append(word)
        starts.append(at)
        ends.append(at + len(raw))
        where.setdefault(word, []).append(n)
    return tuple(folded), tuple(starts), tuple(ends), where


def _run_at(piece: Sequence[str], words: Words, cursor: int) -> int:
    """The first word at or after ``cursor`` where the words of ``piece`` run in order; -1 when they do not."""
    folded, _, _, where = words
    size = len(piece)
    for i in where.get(piece[0], ()):
        if i >= cursor and folded[i : i + size] == tuple(piece):
            return i
    return -1


@lru_cache(maxsize=4096)
def evidence_span(quote: str, text: str, start: int = 0) -> Optional[Tuple[int, int]]:
    """Where a quote sits in a text, word for word, at or after character ``start``: its first character and just past its last.

    None when it is not there, or when it is too short to prove anything.
    """
    if not usable(quote):
        return None
    words = _indexed(text)
    _, starts, ends, _ = words
    cursor = next((i for i, at in enumerate(starts) if at >= start), len(starts))
    first: Optional[int] = None
    for piece in _pieces(quote):
        at = _run_at(piece, words, cursor)
        if at < 0:
            return None
        first = at if first is None else first
        cursor = at + len(piece)
    return (starts[first], ends[cursor - 1]) if first is not None else None


def holds(text: str, quote: str, share: float = 0.5) -> bool:
    """Whether a chunk holds a quote: all of it word for word, or, for a quote longer than the chunk, ``share`` of its words in a row.

    A chunk cut through the middle of the answer still holds the answer's
    words, half of them in a row by default, and a quote is never matched
    on fewer than three.
    """
    if not usable(quote):
        return False
    if evidence_span(quote, text) is not None:
        return True
    wanted = [w for piece in _pieces(quote) for w in piece]
    need = max(_QUOTE_WORDS, math.ceil(len(wanted) * float(share)))
    folded, _, _, where = _indexed(text)
    for j in range(0, len(wanted) - need + 1):
        for i in where.get(wanted[j], ()):
            run = 0
            while (
                j + run < len(wanted)
                and i + run < len(folded)
                and folded[i + run] == wanted[j + run]
            ):
                run += 1
            if run >= need:
                return True
    return False


# ============================================================================
# EVIDENCE FOUND: in what was handed over
# ============================================================================
#
# INPUT   a question, what was handed over, and the documents
# OUTPUT  where each quote is placed, on the page its row names when it is
#         there, else anywhere in that document; whether what was handed holds
#         every quote of the evidence; None when it has no usable quote, so
#         its pages decide
#
# The golden writer keeps a question only when every quote it gave is on its
# page word for word; this is where those quotes are found again.


def _page_start(doc: Any, page: int) -> Optional[int]:
    for at, number in getattr(doc, "pages", None) or ():
        if int(number) == int(page):
            return int(at)
    return None


def _placed(
    quote: str, expected: Iterable[Any], docs: Mapping[str, Any]
) -> Optional[Tuple[str, int, int]]:
    """The document a quote is in and where: on the page its row names when it is there, else anywhere in that document."""
    from .evaluation import _where

    fallback: Optional[Tuple[str, int, int]] = None
    for entry in expected:
        name, page = _where(entry)
        doc = docs.get(name)
        text = str(getattr(doc, "text", "") or "") if doc is not None else ""
        if not text:
            continue
        if page is not None:
            begin = _page_start(doc, page)
            if begin is not None:
                span = evidence_span(quote, text, begin)
                if span is not None and doc is not None and doc.page_at(span[0]) == page:
                    return name, span[0], span[1]
        span = evidence_span(quote, text)
        if span is not None and fallback is None:
            fallback = (name, span[0], span[1])
    return fallback


def _offsets(hit: Any, handed: str) -> Optional[Tuple[str, int, int]]:
    meta = getattr(hit, "metadata", None) or {}
    try:
        start, end = int(meta["_vx_start"]), int(meta["_vx_end"])
    except (KeyError, TypeError, ValueError):
        return None
    doc = str(meta.get("_vx_doc") or str(getattr(hit, "id", "")).rsplit(":", 1)[0])
    whole = str(getattr(hit, "text", "") or "")
    # What was cut short by the budget reaches only as far as it was handed.
    if whole and len(handed) < len(whole):
        end = min(end, start + len(handed))
    return doc, start, end


def _covered(spans: Sequence[Tuple[int, int]], start: int, end: int) -> bool:
    reach = start
    for first, last in sorted(spans):
        if first > reach:
            break
        reach = max(reach, last)
        if reach >= end:
            return True
    return reach >= end


def evidence_found(
    question: Any, handed: Sequence[Tuple[Any, str]], docs: Mapping[str, Any]
) -> Optional[bool]:
    """Whether what was handed over holds every quote of a question's evidence; None when it has no usable quote, so its pages decide.

    A quote is found where it is in its document, and counts as handed over
    when the chunks handed cover that stretch between them, so a quote cut
    across two chunks is found when both were handed. A chunk with no
    offsets counts when its own text holds the quote.
    """
    quotes = [str(q) for q in getattr(question, "evidence", None) or () if usable(q)]
    if not quotes:
        return None
    expected = list(getattr(question, "expected", None) or ())
    for quote in quotes:
        placed = _placed(quote, expected, docs)
        if placed is not None:
            name, start, end = placed
            spans = [
                (s, e)
                for s_doc, s, e in (
                    o for o in (_offsets(hit, text) for hit, text in handed) if o is not None
                )
                if s_doc == name
            ]
            if _covered(spans, start, end):
                continue
        if any(evidence_span(quote, text) is not None for _, text in handed):
            continue
        return False
    return True
