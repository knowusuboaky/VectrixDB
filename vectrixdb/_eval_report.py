"""What an evaluation run adds up to: each setup's numbers, the three picks, and the saved report.

Everything here is arithmetic over results that are already in hand, so it
never searches and never needs a model. :mod:`vectrixdb.evaluation` runs the
searches and hands the results over; the Evaluate pages, the command line and
the reference Function App read the report this writes.

A setup's result is a mapping with, at least, ``key``, ``engine``,
``method``, ``models`` (a list of ``{"key", "label", "name", "kind"}``),
``ranks`` (for every labelled question, the position of the first right
result within the depth searched, or ``None``) and ``times_ms`` (one search
time per question). A setup that could not run carries ``error`` instead.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "FORMAT",
    "PICKS",
    "ReportStore",
    "build_report",
    "choose",
    "frontier",
    "neighbours",
    "summarise",
]


# ============================================================================
# SETTINGS: the format, the depth, the ks, the picks and the buckets
# ============================================================================
#
# What a report records, how deep a search is read, which ks are scored, the
# three picks by name, and the time buckets the pages show.

FORMAT = 1
DEPTH = 20
KS = (1, 3, 5, 10, 20)
PICKS = ("finds_the_most", "best_for_balance", "best_for_time")
# Where the answer landed: first, 2nd to 3rd, 4th to 5th, 6th to 10th, 11th to 20th, not in
# the top 20. A run saved before the top 5 had a band of its own has five numbers here and
# not six, with 4th to 10th as one; the pages draw either.
BUCKETS = ((1, 1), (2, 3), (4, 5), (6, 10), (11, 20))


# ============================================================================
# EACH SETUP'S NUMBERS, THE FRONTIER, AND THE THREE PICKS
# ============================================================================
#
# INPUT   the setups' ranks and times; the balance and the time points
# OUTPUT  the numbers the pages show for one setup, from a nearest-rank
#         percentile; the setups nothing beats on both counts; the three
#         picks, by key
#
# Everything here is arithmetic over results already in hand: it never
# searches and never needs a model.


def _percentile(sorted_values: Sequence[float], share: float) -> float:
    """Nearest rank: the smallest value at or above ``share`` of them."""
    if not sorted_values:
        return 0.0
    index = max(0, math.ceil(share * len(sorted_values)) - 1)
    return float(sorted_values[min(index, len(sorted_values) - 1)])


def summarise(result: Mapping[str, Any]) -> Dict[str, Any]:
    """The numbers the pages show for one setup, from its ranks and times.

    ``found`` is the share of labelled questions whose first right result
    was at or above each k; ``counts`` holds the same as whole questions, so
    "480 of 500" is never rebuilt from a rounded share. ``landed`` counts the
    questions by how far down the answer was.
    """
    ranks: List[Optional[int]] = list(result.get("ranks") or [])
    times = sorted(float(t) for t in (result.get("times_ms") or []))
    n = len(ranks)
    counts = {str(k): sum(1 for r in ranks if r is not None and r <= k) for k in KS}
    landed = [sum(1 for r in ranks if r is not None and low <= r <= high) for low, high in BUCKETS]
    landed.append(n - sum(landed))
    reciprocal = sum(1.0 / r for r in ranks if r)
    return {
        "questions": n,
        "counts": counts,
        "found": {k: round(v / n, 4) if n else None for k, v in counts.items()},
        "landed": landed,
        "mrr": round(reciprocal / n, 4) if n else None,
        "median_ms": round(statistics.median(times), 1) if times else None,
        "p95_ms": round(_percentile(times, 0.95), 1) if times else None,
    }


def _ok(setups: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return [s for s in setups if not s.get("error") and s.get("summary", {}).get("questions")]


def _top10(s: Mapping[str, Any]) -> int:
    return int(s["summary"]["counts"]["10"])


def _first(s: Mapping[str, Any]) -> int:
    return int(s["summary"]["counts"]["1"])


def _ms(s: Mapping[str, Any]) -> float:
    value = s["summary"].get("median_ms")
    return float(value) if value is not None else math.inf


def frontier(setups: Sequence[Mapping[str, Any]]) -> List[str]:
    """The setups nothing beats on both counts: none finds as many and is faster.

    Returned fastest first, which is the order the line is drawn in.
    """
    ok = _ok(setups)
    keep = []
    for s in ok:
        beaten = any(
            (_top10(t) >= _top10(s) and _ms(t) <= _ms(s))
            and (_top10(t) > _top10(s) or _ms(t) < _ms(s))
            for t in ok
            if t is not s
        )
        if not beaten:
            keep.append(s)
    keep.sort(key=lambda s: (_ms(s), -_top10(s)))
    return [str(s["key"]) for s in keep]


def choose(
    setups: Sequence[Mapping[str, Any]], balance: float = 4, time_points: float = 8
) -> Dict[str, Optional[str]]:
    """The three picks, by key.

    ``finds_the_most`` finds the right answer in the top 10 for the most
    questions; a tie goes to the one with more right answers first, then to
    the faster. ``best_for_balance`` is the fastest setup within ``balance``
    percentage points of that, and ``best_for_time`` the fastest within
    ``time_points``. Within is inclusive: at 4 points, 92% counts when the
    most is 96%. All three can be the same setup, and when nothing ran they
    are all ``None``.
    """
    ok = _ok(setups)
    if not ok:
        return {name: None for name in PICKS}
    n = int(ok[0]["summary"]["questions"])
    most = max(ok, key=lambda s: (_top10(s), _first(s), -_ms(s)))

    def fastest_within(points: float) -> Mapping[str, Any]:
        # In whole questions, so 92.0% against 96.0% is never lost to a float.
        floor = _top10(most) * 100 - points * n
        near = [s for s in ok if _top10(s) * 100 >= floor - 1e-9]
        return min(near, key=lambda s: (_ms(s), -_top10(s), -_first(s)))

    return {
        "finds_the_most": str(most["key"]),
        "best_for_balance": str(fastest_within(balance)["key"]),
        "best_for_time": str(fastest_within(time_points)["key"]),
    }


# ============================================================================
# NEIGHBOURS: one change away
# ============================================================================
#
# INPUT   a setup among the setups
# OUTPUT  the setups one change away from it: the ranker, a model, the engine
#
# So a page can say what one change would cost or win.

# Two methods that differ by one step, and how the step reads from each side.
# (from, to): (what changes, how the other one works)
_METHOD_STEPS: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("keyword_semantic", "keyword"): (
        "Without the semantic ranker",
        "the words, in the engine's order",
    ),
    ("keyword", "keyword_semantic"): ("With the semantic ranker", "Azure reorders the words"),
    ("hybrid_semantic", "hybrid_reranked"): (
        "Without the semantic ranker",
        "MiniLM reranks instead",
    ),
    ("hybrid_reranked", "hybrid_semantic"): (
        "With the semantic ranker",
        "Azure reranks instead of MiniLM",
    ),
    ("hybrid_bedrock", "hybrid_reranked"): ("Without Bedrock's reranker", "MiniLM reranks instead"),
    ("hybrid_reranked", "hybrid_bedrock"): (
        "With Bedrock's reranker",
        "Bedrock reranks instead of MiniLM",
    ),
    ("hybrid_reranked", "hybrid"): ("Without the reranker", "the fused list as it is"),
    ("hybrid", "hybrid_reranked"): ("With the reranker", "MiniLM reranks the fused list"),
    ("ultimate", "hybrid_reranked"): ("Without ColBERT", "the reranker alone"),
    ("hybrid_reranked", "ultimate"): ("With ColBERT", "late interaction before the reranker"),
    ("hybrid", "dense"): ("Without the words", "meaning only"),
    ("dense", "hybrid"): ("With the words", "keywords fused in"),
}


def _model_phrase(model: Mapping[str, Any]) -> str:
    label = str(model.get("label") or model.get("key") or "the model")
    if model.get("kind") == "builtin":
        return "the built-in model"
    if model.get("kind") == "hf":
        return "the Hugging Face model"
    return label


def _models_key(s: Mapping[str, Any]) -> Tuple[str, ...]:
    return tuple(sorted(str(m.get("key")) for m in s.get("models") or []))


def neighbours(
    setup: Mapping[str, Any], setups: Sequence[Mapping[str, Any]]
) -> List[Dict[str, str]]:
    """The setups one change away from ``setup``: the ranker, a model, the engine.

    Each is ``{"key", "change", "detail"}``, where ``change`` is what is
    different in words ("Without the built-in model") and ``detail`` how the
    other one works ("Azure OpenAI's vectors only"). The order is the
    ranker, then the models, then the engine, because that is the order of
    what is cheapest to change.
    """
    ok = [s for s in _ok(setups) if s["key"] != setup["key"]]
    engine, method, models = setup.get("engine_kind"), setup.get("method"), _models_key(setup)
    out: List[Dict[str, str]] = []

    for s in ok:
        step = _METHOD_STEPS.get((str(method), str(s.get("method"))))
        if step and s.get("engine_kind") == engine and _models_key(s) == models:
            out.append({"key": str(s["key"]), "change": step[0], "detail": step[1]})

    mine = {str(m.get("key")): m for m in setup.get("models") or []}
    for s in ok:
        if s.get("engine_kind") != engine or s.get("method") != method:
            continue
        theirs = {str(m.get("key")): m for m in s.get("models") or []}
        if not mine or not theirs or set(theirs) == set(mine):
            continue
        gone, added = (
            [mine[k] for k in mine if k not in theirs],
            [theirs[k] for k in theirs if k not in mine],
        )
        if gone and not added:
            kept = [theirs[k] for k in theirs]
            only = kept[0]
            detail = (
                f"{only.get('label')}'s vectors only"
                if only.get("kind") != "builtin"
                else "the built-in model only"
            )
            out.append(
                {
                    "key": str(s["key"]),
                    "change": f"Without {_model_phrase(gone[0])}",
                    "detail": detail,
                }
            )
        elif added and not gone:
            out.append(
                {
                    "key": str(s["key"]),
                    "change": f"With {_model_phrase(added[0])} as well",
                    "detail": "a vector from each, merged",
                }
            )
        elif len(gone) == 1 and len(added) == 1 and len(mine) == 1:
            out.append(
                {
                    "key": str(s["key"]),
                    "change": f"{added[0].get('label')} instead",
                    "detail": f"{added[0].get('name') or added[0].get('label')}",
                }
            )

    others = sorted({str(s.get("engine_kind")) for s in ok if s.get("engine_kind") != engine})
    for other in others:
        same_models = [s for s in ok if s.get("engine_kind") == other and _models_key(s) == models]
        if not same_models:
            continue
        same_method = [s for s in same_models if s.get("method") == method]
        target = (
            same_method[0]
            if same_method
            else max(same_models, key=lambda s: (_top10(s), _first(s), -_ms(s)))
        )
        how = (
            "the same two models"
            if len(models) > 1
            else "the same model"
            if models
            else "the words only"
        )
        out.append(
            {
                "key": str(target["key"]),
                "change": f"On {target.get('engine')} instead",
                "detail": f"{target.get('method_label')}, {how}",
            }
        )
    return out


# ============================================================================
# THE REPORT: one run
# ============================================================================
#
# INPUT   the results and the golden file
# OUTPUT  one run as the pages and the Function App read it, its id from when
#         it was made and the golden file's hash
#
# The id carries the golden file's hash, so a report is never read against
# questions it was not asked.


def _run_id(created: float, golden_sha: str) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(created)) + (
        "-" + golden_sha[:6] if golden_sha else ""
    )


def build_report(
    results: Sequence[Mapping[str, Any]],
    golden: Mapping[str, Any],
    *,
    question_ids: Sequence[str] = (),
    targets: Optional[Mapping[str, Any]] = None,
    missing: Optional[Mapping[str, Any]] = None,
    balance: float = 4,
    time_points: float = 8,
    created: Optional[float] = None,
) -> Dict[str, Any]:
    """One run, as the pages and the Function App read it.

    ``golden`` describes the questions: ``source``, ``sha256``,
    ``questions``, ``labelled`` and ``unfilled`` (template rows nobody has
    written a question for yet). ``targets`` describes what was searched,
    per engine, so a later run can tell whether the index changed between
    them. ``missing`` is what :func:`vectrixdb.evaluation.missing_documents`
    found: by target, the expected documents it does not hold.
    """
    created = time.time() if created is None else created
    setups: List[Dict[str, Any]] = []
    for result in results:
        entry = dict(result)
        entry["summary"] = summarise(result) if not result.get("error") else {"questions": 0}
        setups.append(entry)
    ok = _ok(setups)
    ranked = sorted(ok, key=lambda s: (-_top10(s), -_first(s), _ms(s), str(s["key"])))
    order = {str(s["key"]): i for i, s in enumerate(ranked, start=1)}
    for entry in setups:
        entry["rank"] = order.get(str(entry["key"]))
        entry["neighbours"] = neighbours(entry, setups) if entry["rank"] else []
    setups.sort(key=lambda s: (s["rank"] is None, s["rank"] or 0, str(s["key"])))
    picks = choose(setups, balance, time_points)
    sha = str(golden.get("sha256") or "")
    return {
        "format": FORMAT,
        "id": _run_id(created, sha),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)),
        "golden": dict(golden),
        # Which collections the run searched, read off the targets: a run is over one collection, or a few
        # sampled into one golden file, and the list of runs has to say which without opening each report.
        "collections": collections_of(targets),
        "depth": DEPTH,
        "question_ids": list(question_ids),
        "rules": {"balance_points": balance, "time_points": time_points},
        "targets": dict(targets or {}),
        "missing": dict(missing or {}),
        "picks": picks,
        "frontier": frontier(setups),
        "setups": setups,
    }


# ============================================================================
# THE STORE: where runs are kept
# ============================================================================
#
# INPUT   a place
# OUTPUT  one file a run, retrieval/runs/<id>/report.json, and the history the
#         pages list
#
# Reads are cached by hash and bounded, so a page that polls does not read a
# file it has already seen.

# A saved run never changes, so what the history needs from each is read
# once per process and kept. Keyed by where the store is, which kind of run
# (an evaluation's and a chunking run's ids can be the same) and the run's id.
_SEEN: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
_SEEN_LOCK = threading.Lock()
_SEEN_MAX = 256
# Golden files known to be kept, by where the store is and the fingerprint.
_KEPT: set = set()
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ReportStore:
    """Where runs are kept: one file a run, ``retrieval/runs/<id>/report.json``.

    The report holds everything: every setup's numbers, the three picks and
    the rules they were chosen by, so nothing is written beside it. The one
    other thing kept is the golden file itself, once a version and not once
    a run, as ``golden_dataset/<sha256>.jsonl``, beside the golden dataset
    when that is kept in the same store: the report names the questions by
    their fingerprint and never holds their text. Chunking runs are kept in
    the same store under ``chunking/runs/``, by
    :class:`~vectrixdb.evaluation.ChunkingStore`.

    A run saved before 2.2 is at ``runs/<id>/report.json``, and a golden file
    kept before at ``golden/<sha256>.jsonl``. Both are still read, and
    nothing new is written to either.

    ``files`` is anything with ``read``, ``write``, ``exists`` and ``list``,
    the same small interface the document store uses, so a folder
    (:class:`vectrixdb.documents.LocalFiles`), a bucket
    (:class:`~vectrixdb.documents.S3Files`) or a container
    (:class:`~vectrixdb.documents.BlobFiles`) all work. A path is a folder.
    """

    PREFIX = "retrieval/runs/"
    #: Where runs were saved before, still read so none is lost.
    EARLIER: Tuple[str, ...] = ("runs/",)
    #: The golden file each run used, once a version, beside the golden dataset.
    GOLDEN = "golden_dataset/"
    #: Where those were kept before, still read so no run loses its questions.
    GOLDEN_EARLIER: Tuple[str, ...] = ("golden/",)

    def __init__(self, files: Any) -> None:
        if isinstance(files, str) or hasattr(files, "__fspath__"):
            from .documents import LocalFiles

            files = LocalFiles(files)
        self.files = files

    def __repr__(self) -> str:
        describe = getattr(self.files, "describe", None)
        return f"ReportStore({describe() if callable(describe) else self.files!r})"

    def save(self, report: Mapping[str, Any], golden: Optional[bytes] = None) -> str:
        """Write a run's report and return its id.

        ``golden`` is the golden file's bytes as they were read. They are kept
        once per version, before the report, so a run's own questions can be
        downloaded after the file has changed, and only when they are the
        bytes the report's fingerprint names.
        """
        run = str(report["id"])
        sha = str((report.get("golden") or {}).get("sha256") or "")
        if (
            golden
            and sha
            and hashlib.sha256(golden).hexdigest() == sha
            and not self.has_golden(sha)
        ):
            self.files.write(f"{self.GOLDEN}{sha}.jsonl", bytes(golden))
        self.files.write(
            f"{self.PREFIX}{run}/report.json",
            json.dumps(report, ensure_ascii=False).encode("utf-8"),
        )
        return run

    def _read(self, key: str, missing: str) -> bytes:
        try:
            return bytes(self.files.read(key))
        except (FileNotFoundError, KeyError, OSError) as exc:
            raise KeyError(missing) from exc
        except Exception as exc:  # an SDK's own not-found error
            if "NoSuchKey" in type(exc).__name__ or "NotFound" in type(exc).__name__:
                raise KeyError(missing) from exc
            raise

    def golden(self, sha256: str) -> bytes:
        """The golden file a run used, by its fingerprint; KeyError when it was not kept."""
        if not _SHA256.fullmatch(str(sha256 or "")):
            raise KeyError(sha256)
        for folder in (self.GOLDEN, *self.GOLDEN_EARLIER):
            try:
                return self._read(f"{folder}{sha256}.jsonl", sha256)
            except KeyError:
                continue
        raise KeyError(sha256)

    def has_golden(self, sha256: str) -> bool:
        """Whether this version of the golden file was kept. A kept one never goes, so a yes is remembered."""
        if not _SHA256.fullmatch(str(sha256 or "")):
            return False
        key = (self._where(), str(sha256))
        if key in _KEPT:
            return True
        try:
            # Kept before, in the folder it used to go in, is kept: not written twice.
            kept = any(
                bool(self.files.exists(f"{folder}{sha256}.jsonl"))
                for folder in (self.GOLDEN, *self.GOLDEN_EARLIER)
            )
        except Exception:  # a store that cannot say is a store that has not got it
            kept = False
        if kept:
            _KEPT.add(key)
        return kept

    def _where(self) -> str:
        describe = getattr(self.files, "describe", None)
        return (
            str(describe())
            if callable(describe)
            else f"{type(self.files).__name__}@{id(self.files)}"
        )

    def _folders(self) -> Tuple[str, ...]:
        """Where runs are looked for: the folder new ones go in, then where they went before."""
        return (self.PREFIX, *self.EARLIER)

    def ids(self) -> List[str]:
        """Every run that has a report, newest first."""
        found = set()
        for folder in self._folders():
            for rel in self.files.list(folder):
                parts = str(rel)[len(folder) :].split("/")
                if len(parts) == 2 and parts[1] == "report.json" and parts[0]:
                    found.add(parts[0])
        return sorted(found, reverse=True)

    def get(self, run: str) -> Dict[str, Any]:
        """One run's report. ``"latest"`` is the newest; a missing run raises KeyError."""
        if run == "latest":
            ids = self.ids()
            if not ids:
                raise KeyError("no runs yet")
            run = ids[0]
        if "/" in run or "\\" in run or run.startswith("."):
            raise KeyError(run)
        for folder in self._folders():
            try:
                data = self._read(f"{folder}{run}/report.json", run)
            except KeyError:
                continue
            report: Dict[str, Any] = json.loads(data.decode("utf-8"))
            return report
        raise KeyError(run)

    def latest(self) -> Optional[Dict[str, Any]]:
        try:
            return self.get("latest")
        except KeyError:
            return None

    def history(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        """Newest first: each run's id, time, golden file and every setup's top 10 count.

        Small on purpose: it is what "run by run" is drawn from, and what the
        list of runs to open shows. ``offset`` skips the newest, for the next
        page. Each run's report is read once per process and what the line
        needs is kept, because a saved run never changes.
        """
        out = []
        where = self._where()
        start = max(0, int(offset))
        for run in self.ids()[start : start + max(1, int(limit))]:
            with _SEEN_LOCK:
                entry = _SEEN.get((where, self.PREFIX, run))
            if entry is None:
                try:
                    entry = self._entry(self.get(run))
                except KeyError:
                    continue
                with _SEEN_LOCK:
                    if len(_SEEN) >= _SEEN_MAX:
                        _SEEN.pop(next(iter(_SEEN)))
                    _SEEN[(where, self.PREFIX, run)] = entry
            out.append(entry)
        return out

    def _entry(self, report: Mapping[str, Any]) -> Dict[str, Any]:
        """What the history keeps of one run."""
        return _history_entry(report)


def collections_of(targets: Optional[Mapping[str, Any]]) -> List[str]:
    """The collections a run's targets name, sorted, each once; a target with no collection counts under its name."""
    return sorted(
        {
            str(t.get("collection") or name)
            for name, t in (targets or {}).items()
            if isinstance(t, Mapping)
        }
    )


def _history_entry(report: Mapping[str, Any]) -> Dict[str, Any]:
    ok = [
        s
        for s in report.get("setups") or []
        if not s.get("error") and s.get("summary", {}).get("questions")
    ]
    return {
        "id": report.get("id"),
        "created_at": report.get("created_at"),
        "golden": report.get("golden"),
        # A run saved before it recorded them is read off its targets, so an older run names its collection too.
        "collections": list(report.get("collections") or collections_of(report.get("targets"))),
        "targets": report.get("targets"),
        "picks": report.get("picks"),
        "top10": {str(s["key"]): s["summary"]["counts"]["10"] for s in ok},
        "questions": {str(s["key"]): s["summary"]["questions"] for s in ok},
    }
