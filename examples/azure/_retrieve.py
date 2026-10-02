"""Ask the questions through one of the three setups the evaluation picked.

The three scripts beside this one, 10, 11 and 12, are each three lines: they
name a pick and call :func:`retrieve`. The work is here so the three cannot
drift apart, and so that comparing them is comparing the setup and nothing
else.

Searches go **through the running server**, not straight at the index, and
that is deliberate. A search made through the server is written to the access
log, and the access log is what the Overview's charts and the Access page are
drawn from. Each script signs in with a key named after its pick, so
"who reads most" ends up with three rows, one a setup, which is the whole
comparison on one card.

One honest gap. A setup names four things: the engine, the method, the
reranker and which vectors answer. The REST routes carry the first three. They
do not carry the vectors choice, so on an index built with two vectors the
server searches with both and this says so rather than quietly searching
something else. Use the library directly when you need that one pinned.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from _common import Az, begin, collections, done, finish, links, note, settings, state, step, stop


# ============================================================================
# SETTINGS: the routes and the questions
# ============================================================================
#
# The REST route each setup's method goes through, and the questions every
# setup is asked, the same for all three so comparing them compares the setup
# and nothing else.
#: A method, as a run names it, to the route that runs it and what to send.
ROUTES: Dict[str, Tuple[str, bool]] = {
    "dense": ("text-search", False),
    "hybrid": ("text-hybrid-search", False),
    "hybrid_reranked": ("text-hybrid-search", True),
    "hybrid_bedrock": ("text-hybrid-search", True),
    "hybrid_semantic": ("text-hybrid-search", True),
    "keyword": ("keyword-search", False),
    "keyword_semantic": ("keyword-search", False),
}

QUESTIONS = [
    "What was the total revenue for the year?",
    "How much did the bank set aside for credit losses?",
    "What is the common equity tier 1 ratio?",
    "What did the chief executive say about the year?",
    "How many people does the bank employ?",
]


# ============================================================================
# ASKING: one search, the newest run, a key of its own
# ============================================================================
#
# INPUT   a route and its body; the evals container; a pick's name
# OUTPUT  the hits and how long the search took from here; the report the
#         function saved; a key named after the pick, so the Access page shows
#         one row a setup
#
# Searches go through the running server, not straight at the index, so each
# is written to the access log, which is what the Overview's charts and the
# Access page are drawn from.


def post(url: str, body: Dict[str, Any], key: str, timeout: float = 120.0) -> Tuple[Any, float]:
    """One search, and how long it took from here."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "api-key": key},
    )
    began = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310 - localhost, this deployment's
        answer = json.loads(reply.read())
    return answer, (time.perf_counter() - began) * 1000


def newest_run(kept: Dict[str, Any], az: Az, config: Dict[str, str]) -> Dict[str, Any]:
    """The report the function saved, read out of the evals container."""
    from vectrixdb.evaluation import report_store

    where = f"{kept.get('blob_account', '')}/evals"
    keys = az(
        "storage", "account", "keys", "list",
        "--account-name", config["VX_STORAGE"], "--resource-group", config["VX_RESOURCE_GROUP"],
        reads=True, quiet=True, allow_fail=True,
    )
    if not keys:
        stop("Could not read a storage key. Run 01_create_resources.py first.")
    import os

    os.environ.setdefault("AZURE_STORAGE_KEY", keys[0]["value"])
    try:
        store = report_store(where)
        runs = store.list()
    except Exception as exc:  # noqa: BLE001 - the address and the account are the caller's
        stop(f"Could not read the runs at {where}: {exc}\n  Run an evaluation first.")
    if not runs:
        stop(f"There are no runs at {where} yet. Run an evaluation first.")
    return store.read(runs[0]["id"] if isinstance(runs[0], dict) else runs[0])


def key_for(az: Az, config: Dict[str, str], name: str, port: int) -> str:
    """A key of this setup's own, so the Access page shows one row a setup."""
    made, _ = post(
        f"http://localhost:{port}/api/v1/keys",
        {"name": name, "role": "operator"},
        key="",
        timeout=20.0,
    )
    return made["data"]["key"]


# ============================================================================
# RETRIEVING: every question through one setup
# ============================================================================
#
# INPUT   the pick's name
# OUTPUT  what came back for each question, shown, with its relevance
#
# The three scripts beside this one are each three lines: they name a pick and
# call this, so they cannot drift apart. The REST routes carry the engine, the
# method and the reranker; they do not carry the vectors choice, which the
# module docstring names as the one honest gap.


def retrieve(
    pick: str,
    port: int = 8000,
    limit: int = 5,
    questions: Optional[List[str]] = None,
    dry_run: bool = False,
    which: Optional[str] = None,
) -> int:
    """Ask every question through the setup this pick names, and show what came back."""
    config = settings()
    az = Az(dry_run)
    kept = state()
    which = which or collections(config)[0]
    asking = list(questions or QUESTIONS)
    begin(pick[:2].upper(), f"Retrieve: {pick}", f"The questions, through the setup the last run picked as {pick}.")

    if dry_run:
        note("a dry run reads no run and asks nothing")
        return 0

    step("The newest run")
    report = newest_run(kept, az, config)
    chosen = (report.get("picks") or {}).get(pick)
    by_key = {s["key"]: s for s in report.get("setups") or []}
    if not chosen or chosen not in by_key:
        stop(f"The newest run has no {pick}. It has: {', '.join(sorted((report.get('picks') or {}))) or 'no picks at all'}")
    setup = by_key[chosen]
    summary = setup["summary"]
    models = " and ".join(m.get("label") or m.get("name") or "" for m in setup.get("models") or []) or "words only"
    done(f"{setup['method_label']} on {setup['engine']}, {models}")
    print(f"       on the golden questions: {summary['found']['10']:.0%} in the top 10, {summary['median_ms']:.0f} ms a search")

    route, rerank = ROUTES.get(setup["method"], ("text-search", False))
    wanted = setup.get("search") or {}
    if wanted.get("vectors"):
        note(
            f"this setup answers with the {wanted['vectors']} vectors, and the REST routes do not carry that choice, "
            "so the server below searches with every vector the index holds"
        )

    step("A key of its own, so the Access page shows this setup as a caller")
    try:
        key = key_for(az, config, f"retrieve-{pick.replace('_', '-')}", port)
        done(f"retrieve-{pick.replace('_', '-')}")
    except (urllib.error.URLError, TimeoutError):
        stop(
            f"Nothing is answering on http://localhost:{port}.\n"
            "  Start the server first, in another terminal."
        )
    except urllib.error.HTTPError as refused:
        if refused.code in (401, 403):
            stop(
                "The server would not make a key. It is running with sign-in on, so sign in first in the browser, "
                "or set VX_SIGNIN=no in settings.env and start it again."
            )
        raise

    step(f"{len(asking)} questions of {which}, through /{route}")
    url = f"http://localhost:{port}/api/v1/collections/{which}/{route}?snippet=160"
    times: List[float] = []
    for question in asking:
        body: Dict[str, Any] = {"query_text": question, "limit": limit}
        if route != "keyword-search":
            body["rerank"] = rerank
        else:
            body = {"query": question, "limit": limit}
        try:
            answer, took = post(url, body, key)
        except urllib.error.HTTPError as refused:
            print(f"\n  {question}\n       refused {refused.code}: {refused.read().decode('utf-8', 'replace')[:200]}")
            continue
        times.append(took)
        results = answer.get("data", answer).get("results") or answer.get("data", answer).get("items") or []
        print(f"\n  {question}   {took:.0f} ms")
        if not results:
            print("       nothing came back")
            continue
        for place, hit in enumerate(results[:3], start=1):
            meta = hit.get("metadata") or {}
            where = meta.get("_vx_citation") or meta.get("_vx_doc") or hit.get("id", "")
            relevance = hit.get("similarity") or hit.get("relevance") or hit.get("score")
            said = " ".join(str(hit.get("text") or "").split())[:110]
            print(f"       {place}. {str(where)[:54]:<54} {_relevance(relevance)}")
            if said:
                print(f"          {said}")

    if times:
        times.sort()
        step(f"median {times[len(times) // 2]:.0f} ms over {len(times)} searches, from this machine")
        note("that is the network and this laptop too; the run's own figure is the server's alone")
    links(
        ("who reads most, on Access", f"http://localhost:{port}/dashboard/#/access"),
        ("searches a day, on Overview", f"http://localhost:{port}/dashboard/#/overview"),
    )
    finish(
        f"{len(times)} searches are now in the access log, under retrieve-{pick.replace('_', '-')}.",
        "the next retrieve script, then look at Access and Overview in the dashboard",
    )
    return 0


def _relevance(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


# ============================================================================
# OPTIONS
# ============================================================================
#
# INPUT   the command line
# OUTPUT  the options the three scripts share
#
# One parser for all three.


def options(parser) -> None:
    parser.add_argument("--port", type=int, default=8000, help="where the dashboard is serving")
    parser.add_argument("--limit", type=int, default=5, help="results a question")
    parser.add_argument("--question", action="append", default=None, help="ask your own; give it again for more")
    parser.add_argument("--collection", default=None, help="which collection to ask; the first by default")
