"""Two measurements a golden file makes possible: where to stop answering, and how to cut documents.

Both are re-exported from :mod:`vectrixdb.evaluation`, which is where to
import them from.

:func:`answer_cutoff` answers the question a fixed number cannot: below which
relevance is the top result more likely a near miss than the answer? It
depends on the model and on the documents, so it is measured on labelled
questions and never borrowed.

:func:`sweep` answers the one :func:`~vectrixdb.evaluation.evaluate` leaves
alone. That compares ways of *searching* one index. How the documents were
cut, which chunker, how long, whether the headings went to the embedder, is
decided at ingestion, so comparing those means building the index again for
each, and that is what this does, in a scratch folder, from the originals.
"""

from __future__ import annotations

import itertools
import shutil
import tempfile
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

__all__ = ["answer_cutoff", "sweep", "sweep_markdown"]


# ============================================================================
# SETTINGS: the measures, and the fewest questions a cut-off needs
# ============================================================================
#
# Which relevance measures a cut-off can be placed on, and how many questions
# it takes before a number means anything.

MEASURES = ("relevance", "similarity")
#: Fewer than this on either side and a cut-off is a guess with a decimal point.
FEWEST = 5


# ============================================================================
# THE ANSWER CUT-OFF
# ============================================================================
#
# INPUT   a collection and its golden questions
# OUTPUT  the relevance below which the top result is more likely a near miss
#         than the answer, with how well it separates the two
#
# How often a right answer scores above a near miss, ties counting half; 0.5
# is a coin. It answers the question a fixed number cannot: where, on this
# collection with this model, to stop answering.


def _value_of(hit: Any, measure: str) -> Optional[float]:
    value = getattr(hit, measure, None)
    return float(value) if value is not None else None


def _separation(right: Sequence[float], wrong: Sequence[float]) -> Optional[float]:
    """How often a right answer scores above a near miss; ties count half. 0.5 is a coin."""
    if not right or not wrong:
        return None
    above = sum((r > w) + 0.5 * (r == w) for r in right for w in wrong)
    return round(above / (len(right) * len(wrong)), 4)


def _row(cut: float, right: Sequence[float], wrong: Sequence[float]) -> Dict[str, Any]:
    kept_right = sum(1 for v in right if v >= cut)
    kept_wrong = sum(1 for v in wrong if v >= cut)
    answered = kept_right + kept_wrong
    return {
        "cutoff": round(cut, 4),
        # of what it answers at this cut-off, the share it should have answered
        "precision": round(kept_right / answered, 4) if answered else None,
        # of what it could have answered, the share it still does
        "answered": round(kept_right / len(right), 4) if right else None,
        # of the near misses, the share it now declines
        "declined": round(1 - kept_wrong / len(wrong), 4) if wrong else None,
    }


def answer_cutoff(
    db: Any,
    questions: Sequence[Any],
    *,
    unanswerable: Iterable[str] = (),
    by: str = "doc",
    measure: str = "similarity",
    **search: Any,
) -> Dict[str, Any]:
    """The relevance below which the top result is more likely a near miss than the answer.

    Every labelled question is searched, and its top result is either the
    answer, one of the documents the question expects, or a near miss: the
    best the collection had, and wrong. ``unanswerable`` is questions a person
    knows the documents do not answer; whatever comes top for one is a near
    miss by definition, and they are what makes the cut-off honest about
    questions from outside the documents, so give some.

    ``measure`` is the attribute of a result that is read: ``similarity``,
    the cosine alone, which means the same in every mode, or ``relevance``,
    which is the reranker's verdict where one ran. ``search`` goes to
    ``db.search``, and the cut-off belongs to exactly that way of searching.

    The reply holds ``cutoff``: the value that best separates the two, by
    the share of answers kept plus the share of near misses declined. Beside
    it, ``separation``, how often an answer outscores a near miss, where 0.5
    is a coin and nothing under about 0.75 is worth acting on; ``table``,
    the trade at each candidate, to choose a stricter or a looser one from;
    and ``at_cutoff``. With fewer than five answers or five near misses
    ``cutoff`` is ``None`` and ``reason`` says which: a number from three
    examples is not a measurement.
    """
    if measure not in MEASURES:
        raise ValueError(f"measure is one of {MEASURES}, got {measure!r}")
    if by not in ("doc", "chunk", "evidence"):
        raise ValueError(f"by is 'doc', 'chunk' or 'evidence', got {by!r}")
    from .evaluation import _answers

    right: List[float] = []
    wrong: List[float] = []
    unmeasured = 0

    def top_of(text: str) -> Tuple[Optional[Any], Optional[float]]:
        hits = list(db.search(text, limit=1, **search))
        if not hits:
            return None, None
        return hits[0], _value_of(hits[0], measure)

    labelled = [q for q in questions if getattr(q, "expected", None)]
    for q in labelled:
        hit, value = top_of(q.text)
        if hit is None or value is None:
            unmeasured += 1
            continue
        (
            right if _answers(hit, q.expected, by, getattr(q, "evidence", None) or ()) else wrong
        ).append(value)
    outside = [str(text) for text in unanswerable if str(text).strip()]
    for text in outside:
        hit, value = top_of(text)
        if hit is None:
            continue  # nothing came back, which is already a decline
        if value is None:
            unmeasured += 1
            continue
        wrong.append(value)

    reply: Dict[str, Any] = {
        "measure": measure,
        "by": by,
        "model": getattr(db, "model_name", None),
        "questions": len(labelled),
        "unanswerable": len(outside),
        "answers": len(right),
        "near_misses": len(wrong),
        "unmeasured": unmeasured,
        "separation": _separation(right, wrong),
        "cutoff": None,
        "at_cutoff": None,
        "table": [],
        "search": {
            k: v for k, v in search.items() if isinstance(v, (str, int, float, bool, type(None)))
        },
    }
    if unmeasured and not right and not wrong:
        reply["reason"] = (
            f"no result carried a {measure}: a keyword search has none, and a distance that is not cosine has none"
        )
        return reply
    if len(right) < FEWEST or len(wrong) < FEWEST:
        short = "answers" if len(right) < FEWEST else "near misses"
        reply["reason"] = (
            f"{len(right)} answers and {len(wrong)} near misses: too few {short} to place a cut-off. "
            + (
                "Add questions the documents do not answer, as unanswerable=."
                if short == "near misses"
                else "Label more questions."
            )
        )
        return reply

    # A cut-off only changes anything at a value some result has, so those,
    # and the midpoints between neighbours, are every cut-off there is.
    values = sorted(set(right) | set(wrong))
    candidates = sorted({values[0], *((a + b) / 2 for a, b in zip(values, values[1:]))})
    rows = [_row(c, right, wrong) for c in candidates]
    best = max(rows, key=lambda r: ((r["answered"] or 0) + (r["declined"] or 0), -r["cutoff"]))
    reply["cutoff"] = best["cutoff"]
    reply["at_cutoff"] = best
    # The table is the candidates thinned to about a dozen, the chosen one kept.
    step = max(1, len(rows) // 12)
    reply["table"] = sorted(
        {id(r): r for r in [*rows[::step], rows[-1], best]}.values(), key=lambda r: r["cutoff"]
    )
    return reply


# ============================================================================
# THE SWEEP: every way of cutting, ranked
# ============================================================================
#
# INPUT   the documents, the golden questions, and the ways to build
# OUTPUT  the index built every way asked, each asked the golden questions,
#         ranked; and the sweep as a table, best first, for a pull request or
#         a page
#
# Both are re-exported from vectrixdb.evaluation, which is where to import
# them from.


def _sources(documents: Any) -> List[Tuple[Optional[str], Any]]:
    if isinstance(documents, Mapping):
        return [(str(doc_id), source) for doc_id, source in documents.items()]
    return [(None, source) for source in documents]


def _median(values: Sequence[float]) -> Optional[float]:
    ordered = sorted(values)
    return round(ordered[len(ordered) // 2], 1) if ordered else None


def sweep(
    documents: Union[Mapping[str, Any], Iterable[Any]],
    questions: Sequence[Any],
    *,
    chunk: Sequence[str] = ("recursive",),
    chunk_size: Sequence[int] = (1000,),
    overlap: Sequence[int] = (200,),
    embed_heading: Sequence[bool] = (False,),
    search: Sequence[Mapping[str, Any]] = ({"mode": "dense"},),
    by: str = "doc",
    k: Sequence[int] = (1, 3, 5, 10),
    open_with: Optional[Mapping[str, Any]] = None,
    workdir: Union[str, Path, None] = None,
    progress: Optional[Callable[[Dict[str, Any], int, int], None]] = None,
) -> Dict[str, Any]:
    """Build the index every way asked, ask the golden questions of each, and rank them.

    ``documents`` is the originals: paths, or ``{doc_id: path}`` when the
    golden file's ``expected`` names ids the files would not get by
    themselves. Every combination of ``chunk``, ``chunk_size``, ``overlap``
    and ``embed_heading`` is one build in a scratch folder, removed after; an
    overlap that is not smaller than the size is left out. Each build is
    asked every question once for each entry of ``search``, the keyword
    arguments of ``db.search``: ``{"mode": "dense"}``, ``{"mode": "hybrid",
    "rerank": False}``. ``open_with`` goes to ``Vectrix``, for a
    ``dense_model`` or a ``text_language``; the collection is opened in the
    highest mode any search asks for.

    Labels have to survive a change of chunker, so they name documents:
    ``by="doc"`` is the default and the only grain that means the same in
    two builds. The reply's ``rows`` are ranked by nDCG, then MRR, then by
    time, each with its recall at k, the chunks it made, the seconds the
    build took and the median search in milliseconds; ``best`` is the first.
    Builds run one after another on one machine, so the times compare with
    each other and with nothing else.
    """
    from .easy import Vectrix
    from .evaluation import retrieval_report

    labelled = [q for q in questions if getattr(q, "expected", None)]
    if not labelled:
        raise ValueError(
            "no question names the documents that answer it, so there is nothing to score against"
        )
    for name, given in (
        ("chunk", chunk),
        ("chunk_size", chunk_size),
        ("overlap", overlap),
        ("embed_heading", embed_heading),
        ("search", search),
    ):
        if isinstance(given, (str, bytes)) or not list(given):
            raise ValueError(f"{name} is a list of the values to try, got {given!r}")
    sources = _sources(documents)
    if not sources:
        raise ValueError("documents is empty")
    order = {"dense": 1, "sparse": 1, "hybrid": 2, "ultimate": 3}
    asked = [str(s.get("mode") or "dense") for s in search]
    unknown = [m for m in asked if m not in order]
    if unknown:
        raise ValueError(f"a sweep searches dense, sparse, hybrid or ultimate, got {unknown[0]!r}")
    opened_as = max(asked, key=lambda m: order[m])
    opened_as = "hybrid" if opened_as == "sparse" else opened_as

    builds: List[Dict[str, Any]] = [
        {"chunk": c, "chunk_size": int(size), "overlap": int(lap), "embed_heading": bool(heading)}
        for c, size, lap, heading in itertools.product(chunk, chunk_size, overlap, embed_heading)
        if int(lap) < int(size)
    ]
    if not builds:
        raise ValueError("every overlap given is at least as long as every chunk_size given")
    total = len(builds) * len(search)
    scratch = (
        Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="vectrixdb-sweep-"))
    )
    rows: List[Dict[str, Any]] = []
    done = 0
    try:
        for number, build in enumerate(builds):
            home = scratch / f"build-{number}"
            db = Vectrix(
                "sweep",
                path=str(home),
                mode=cast(Any, opened_as),
                embedding_cache=False,
                **dict(open_with or {}),
            )
            try:
                began = time.perf_counter()
                for doc_id, source in sources:
                    db.add_document(
                        source, doc_id=doc_id, progress=False, on_low_quality="allow", **build
                    )
                built = round(time.perf_counter() - began, 2)
                chunks = db.count()
                for how in search:
                    how = dict(how)
                    times: List[float] = []
                    timed = _Timed(db, times)
                    report = retrieval_report(timed, labelled, k=k, by=by, **how)
                    row = {
                        **build,
                        "search": how,
                        "chunks": chunks,
                        "build_s": built,
                        "median_ms": _median(times),
                        "recall": report["recall"],
                        "mrr": report["mrr"],
                        "ndcg": report[f"ndcg@{max(int(x) for x in k)}"],
                        "misses": [m["id"] or m["question"] for m in report["misses"]],
                    }
                    rows.append(row)
                    done += 1
                    if progress is not None:
                        progress(row, done, total)
            finally:
                db.close()
                shutil.rmtree(home, ignore_errors=True)
    finally:
        if workdir is None:
            shutil.rmtree(scratch, ignore_errors=True)
    rows.sort(key=lambda r: (-(r["ndcg"] or 0), -(r["mrr"] or 0), r["median_ms"] or 0, r["chunks"]))
    for place, row in enumerate(rows, start=1):
        row["rank"] = place
    return {
        "questions": len(labelled),
        "documents": len(sources),
        "by": by,
        "k": sorted({int(x) for x in k}),
        "builds": len(builds),
        "rows": rows,
        "best": rows[0],
    }


class _Timed:
    """``db.search`` with each call's milliseconds noted, which is all a report needs of a collection."""

    def __init__(self, db: Any, times: List[float]) -> None:
        self._db, self._times = db, times

    def search(self, *args: Any, **kwargs: Any) -> Any:
        began = time.perf_counter()
        try:
            return self._db.search(*args, **kwargs)
        finally:
            self._times.append(1000 * (time.perf_counter() - began))


def _how(search: Mapping[str, Any]) -> str:
    rest = ", ".join(f"{key}={value}" for key, value in search.items() if key != "mode")
    return str(search.get("mode") or "dense") + (f" ({rest})" if rest else "")


def sweep_markdown(report: Mapping[str, Any]) -> str:
    """The sweep as a table, best first, for a pull request or a page."""
    ks = [f"@{x}" for x in report["k"]]
    head = [
        "#",
        "chunker",
        "size",
        "overlap",
        "headings",
        "search",
        *[f"recall {x}" for x in ks],
        "MRR",
        "nDCG",
        "chunks",
        "build",
        "median",
    ]
    lines = [
        "| " + " | ".join(head) + " |",
        "| " + " | ".join("---" if i in (1, 5) else "---:" for i in range(len(head))) + " |",
    ]
    for row in report["rows"]:
        cells = [
            str(row["rank"]),
            row["chunk"],
            str(row["chunk_size"]),
            str(row["overlap"]),
            "yes" if row["embed_heading"] else "no",
            _how(row["search"]),
            *[f"{row['recall'][x]:.3f}" for x in ks],
            f"{row['mrr']:.3f}",
            f"{row['ndcg']:.3f}",
            str(row["chunks"]),
            f"{row['build_s']:.1f} s",
            f"{row['median_ms']:.0f} ms" if row["median_ms"] is not None else "",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
