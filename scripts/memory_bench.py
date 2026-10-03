"""Memory benchmarks: LoCoMo and LongMemEval by one command.

    python scripts/memory_bench.py locomo
    python scripts/memory_bench.py longmemeval            # the oracle split, 15 MB
    python scripts/memory_bench.py longmemeval_s --limit 100

Both benchmarks are long multi-session conversations with questions whose
answers live in specific turns. Answering needs an LLM; this script measures
the part VectrixDB owns, which is whether the memory system puts the right
turns in front of the model. For each question the conversation is stored
with ``remember()`` (one memory session per conversation, one turn per
utterance, dated), the question goes through ``context()`` under a token
budget, and the metric is evidence recall: the share of the question's
evidence turns that made it into the block. A model cannot answer from
evidence it was never shown, so this is the ceiling on any answer accuracy.

Datasets, downloaded on first use and cached under ~/.cache/vectrixdb/memory:

* LoCoMo (Maharana et al., 2024): 10 conversations, up to 32 sessions each,
  1,986 questions with evidence turn ids. Categories 1 to 5 are single-hop,
  multi-hop, temporal, open-domain and adversarial (no evidence, skipped).
* LongMemEval (Wu et al., 2024): 500 questions, each with its own history.
  ``longmemeval`` is the oracle split (only the evidence sessions, quick);
  ``longmemeval_s`` is the real one, about 40 sessions per question and
  278 MB; ``--limit`` takes the first N questions.

Reported per benchmark: evidence recall at the budget, mean block tokens,
questions, and the same for a plain ``search()`` over the turns so the
memory ranking can be compared with vector search alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================================
# SETTINGS: the cache and the archives
# ============================================================================
#
# Where the datasets are cached, ~/.cache/vectrixdb/memory, and where each is
# downloaded from on first use.

CACHE = Path(
    os.environ.get("VECTRIXDB_MEMORY_CACHE", Path.home() / ".cache" / "vectrixdb" / "memory")
)
URLS = {
    "locomo": "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
    "longmemeval": "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_oracle",
    "longmemeval_s": "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s",
}


# ============================================================================
# FETCHING
# ============================================================================
#
# INPUT   a dataset's name
# OUTPUT  its JSON, downloaded once into the cache
#
# LoCoMo is small; the LongMemEval oracle split is 15 MB, and the real one is
# larger, so each is fetched once and kept.


def fetch(name: str) -> Any:
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / f"{name}.json"
    if not target.exists():
        import requests

        url = URLS[name]
        print(f"downloading {url}", file=sys.stderr)
        resp = requests.get(url, timeout=600)
        resp.raise_for_status()
        target.write_bytes(resp.content)
    return json.loads(target.read_text(encoding="utf-8"))


# ============================================================================
# ONE SHAPE FOR BOTH: a list of tasks, each a conversation plus questions
# ============================================================================
#
# INPUT   the raw LoCoMo or LongMemEval data
# OUTPUT  tasks: each a dated conversation, its questions, and the evidence
#         turn ids each answer lives in
#
# Two datasets, one shape, so the run below does not know which it was given.


def _locomo_date(raw: str) -> datetime:
    # "1:56 pm on 8 May, 2023"
    try:
        return datetime.strptime(raw, "%I:%M %p on %d %B, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime(2023, 1, 1, tzinfo=timezone.utc)


def locomo_tasks(data: Any) -> Iterable[Dict[str, Any]]:
    for sample in data:
        conv = sample["conversation"]
        turns: List[Tuple[str, str, str, datetime]] = []  # (id, speaker, text, when)
        n = 1
        while f"session_{n}" in conv:
            when = _locomo_date(conv.get(f"session_{n}_date_time", ""))
            for i, t in enumerate(conv[f"session_{n}"]):
                turns.append((t["dia_id"], t["speaker"], t["text"], when + timedelta(seconds=i)))
            n += 1
        questions = []
        for q in sample["qa"]:
            evidence = [e for e in q.get("evidence", []) if isinstance(e, str)]
            if not evidence or q.get("category") == 5:
                continue
            questions.append(
                {"question": q["question"], "evidence": evidence, "category": q.get("category")}
            )
        yield {"id": sample["sample_id"], "turns": turns, "questions": questions}


def _lme_date(raw: str) -> datetime:
    # "2023/04/10 (Mon) 23:07"
    m = re.match(r"(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2})", raw or "")
    if not m:
        return datetime(2023, 1, 1, tzinfo=timezone.utc)
    y, mo, d, h, mi = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def longmemeval_tasks(data: Any) -> Iterable[Dict[str, Any]]:
    for q in data:
        turns = []
        evidence = []
        sessions = zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"])
        for sid, date, session in sessions:
            when = _lme_date(date)
            for i, t in enumerate(session):
                tid = f"{sid}:{i}"
                turns.append((tid, t["role"], t["content"], when + timedelta(seconds=i)))
                if t.get("has_answer"):
                    evidence.append(tid)
        if not evidence:
            continue
        yield {
            "id": q["question_id"],
            "turns": turns,
            "questions": [
                {"question": q["question"], "evidence": evidence, "category": q["question_type"]}
            ],
        }


# ============================================================================
# RUNNING
# ============================================================================
#
# INPUT   the tasks, a token budget, a limit, how many recent turns to keep,
#         and the dense model
# OUTPUT  evidence recall: the share of each question's evidence turns that
#         made it into the context block
#
# Each conversation is stored with remember(), one memory session a
# conversation and one turn an utterance, dated; each question goes through
# context() under the budget. Answering needs an LLM; this measures the part
# VectrixDB owns, whether the right turns are put in front of the model. A
# model cannot answer from evidence it was never shown, so this is the ceiling
# on any answer accuracy.


def run(
    tasks: Iterable[Dict[str, Any]],
    token_budget: int,
    limit: Optional[int],
    recent_turns: int,
    dense_model: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from vectrixdb import Vectrix

    # One model is the collection's own; more than one is a collection that
    # carries each model's vectors and fuses their lists by rank.
    models: Dict[str, Any] = {}
    if dense_model:
        models["dense_model"] = dense_model[0] if len(dense_model) == 1 else list(dense_model)

    workdir = Path(tempfile.mkdtemp(prefix="vxmem-"))
    totals = {
        "questions": 0,
        "memory_recall": 0.0,
        "search_recall": 0.0,
        "tokens": 0,
        "by_category": {},
    }
    t0 = time.perf_counter()
    try:
        for n, task in enumerate(tasks):
            if limit is not None and n >= limit:
                break
            db = Vectrix(f"t{n}", path=str(workdir), mode="hybrid", embedding_cache=False, **models)
            if n == 0 and dense_model and len(dense_model) > 1:
                # A number quoted for two models has to come from two models.
                assert len(db.vector_names()) == len(dense_model), db.vector_names()
            texts, metas, ids = [], [], []
            for turn_index, (tid, speaker, text, when) in enumerate(task["turns"]):
                texts.append(f"{speaker}: {text}")
                metas.append(
                    {
                        "_vx_kind": "turn",
                        "_vx_session": "conv",
                        "_vx_role": speaker,
                        "_vx_turn": turn_index,
                        "_vx_ts": when.isoformat(),
                    }
                )
                ids.append(tid)
            db.add(texts, metadata=metas, ids=ids)
            for q in task["questions"]:
                block = db.context(
                    q["question"],
                    session="conv",
                    token_budget=token_budget,
                    recent_turns=recent_turns,
                    mode="hybrid",
                )
                got = {r.id for r in block.recent} | {r.id for r in block.retrieved}
                hits = len(set(q["evidence"]) & got) / len(q["evidence"])
                plain = db.search(
                    q["question"],
                    limit=max(1, len(block)),
                    mode="hybrid",
                    rerank=False,
                    token_budget=token_budget,
                )
                plain_hits = len(set(q["evidence"]) & {r.id for r in plain}) / len(q["evidence"])
                totals["questions"] += 1
                totals["memory_recall"] += hits
                totals["search_recall"] += plain_hits
                totals["tokens"] += block.token_estimate
                cat = totals["by_category"].setdefault(
                    str(q["category"]), {"n": 0, "memory": 0.0, "search": 0.0}
                )
                cat["n"] += 1
                cat["memory"] += hits
                cat["search"] += plain_hits
            db.close()
            print(
                f"  {n + 1} conversations, {totals['questions']} questions",
                file=sys.stderr,
                end="\r",
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    n = max(1, totals["questions"])
    return {
        "questions": totals["questions"],
        "evidence_recall_memory": round(totals["memory_recall"] / n, 4),
        "evidence_recall_search": round(totals["search_recall"] / n, 4),
        "mean_block_tokens": round(totals["tokens"] / n),
        "by_category": {
            k: {
                "n": v["n"],
                "memory": round(v["memory"] / v["n"], 3),
                "search": round(v["search"] / v["n"], 3),
            }
            for k, v in totals["by_category"].items()
        },
        "seconds": round(time.perf_counter() - t0, 1),
    }


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   the dataset, --limit and the options
# OUTPUT  the numbers, and what they came from
#
# One command a benchmark, with the model named in the provenance so a number
# is never quoted without it.


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("benchmark", nargs="?", default="locomo", choices=list(URLS))
    parser.add_argument("--token-budget", type=int, default=1500)
    parser.add_argument(
        "--recent-turns",
        type=int,
        default=0,
        help="recent turns always included; 0 measures retrieval alone",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="first N conversations (questions for LongMemEval)"
    )
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--dense-model",
        action="append",
        default=None,
        help="the dense model, by its alias: bge-small, e5-small. Give it twice for a collection that carries both and fuses them",
    )
    args = parser.parse_args(argv)

    data = fetch(args.benchmark)
    tasks = locomo_tasks(data) if args.benchmark == "locomo" else longmemeval_tasks(data)
    result = run(tasks, args.token_budget, args.limit, args.recent_turns, args.dense_model)
    print()
    print(
        f"| Benchmark | questions | evidence recall, memory | evidence recall, search | block tokens |"
    )
    print("| --- | ---: | ---: | ---: | ---: |")
    print(
        f"| {args.benchmark} | {result['questions']} | {result['evidence_recall_memory']:.3f} | "
        f"{result['evidence_recall_search']:.3f} | {result['mean_block_tokens']} |"
    )
    for cat, v in sorted(result["by_category"].items()):
        print(f"  category {cat}: n={v['n']} memory {v['memory']:.3f} search {v['search']:.3f}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "benchmark": args.benchmark,
                    "token_budget": args.token_budget,
                    **_provenance(args.dense_model),
                    **result,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


def _provenance(dense_model: Optional[List[str]] = None) -> dict:
    """What the numbers came from. Results written without the model were once
    quoted for a model that was not the one that produced them."""
    import datetime
    import platform

    import vectrixdb
    from vectrixdb import Vectrix

    return {
        "model": " + ".join(dense_model) if dense_model else Vectrix._default_model,
        "vectrixdb": vectrixdb.__version__,
        "date": datetime.date.today().isoformat(),
        "machine": f"{platform.machine()} {platform.system()} Python {platform.python_version()}",
    }


if __name__ == "__main__":
    sys.exit(main())
