"""The golden file changes, every setup is searched again, and the run is saved for the Evaluate page.

Reference code for an Azure Function App on the Flex Consumption or Premium
plan, with Durable Functions. It is not run by the test suite, because every
line of it talks to Azure; what it calls in the library is tested there.

The flow, in order:

1. ``golden.jsonl`` is saved in a Blob container that keeps versions, by a
   person, a pipeline, or ``vectrixdb golden template`` and some typing.
2. Event Grid sends ``BlobCreated``. The subscription is filtered to names
   ending in ``golden.jsonl``, and :func:`golden_changed` checks again.
3. :func:`golden_changed` reads the file's sha256 and starts one
   orchestration named by the collection and the hash, so the same event
   delivered twice, or the same bytes saved twice, starts one run.
4. :func:`evaluate_golden` waits a minute and reads the hash again: when a
   newer save replaced the file, that save's own run covers it, so three
   saves in a row cost one run. Then one activity per setup, in parallel,
   each only searching, since the index already exists. One index gives
   every setup: each set of vectors alone and fused, keyword, dense and
   hybrid, and Azure's semantic ranker, the cross-encoder or neither,
   chosen search by search.
5. :func:`save_report` looks up every document the golden file expects,
   builds the report and saves it, one file, ``retrieval/runs/<id>/report.json``
   beside the golden file, where the server's Evaluate pages read it (point
   ``VECTRIXDB_EVALUATIONS`` there). The report holds the three picks, and
   under ``missing`` any expected document the index does not hold.
6. A person chooses. Nothing here changes what production searches with.

:func:`evaluate_now` starts the same run on demand, which is what an
ingestion pipeline calls when it finishes: new documents can change the
picks as much as new questions can.

Settings, in the Function App's configuration; the secrets as Key Vault
references, and the storage reached with the app's managed identity:

``EVAL_COLLECTION``           the collection's name, in run names and the message
``EVAL_GOLDEN_URL``           the golden file, for evaluate_now
``EVAL_REPORTS``              where runs go; default: beside the golden file
``AZURE_SEARCH_ENDPOINT``     the Azure AI Search service
``AZURE_SEARCH_KEY``          optional; left out, the managed identity is used
``AZURE_SEARCH_SEMANTIC``     "false" on a search service with the semantic ranker turned off
``AZURE_OPENAI_ENDPOINT``     with ``_DEPLOYMENT`` and ``_DIMENSIONS``: the second vector
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

import azure.durable_functions as df
import azure.functions as func

from vectrixdb import Vectrix, VectrixDB
from vectrixdb.evaluation import (
    Target,
    build_report,
    describe_target,
    missing_documents,
    read_golden,
    report_store,
    run_setup,
    setups_of,
)

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

COLLECTION = os.environ.get("EVAL_COLLECTION", "docs")
GOLDEN_NAME = "golden.jsonl"
DEBOUNCE = timedelta(seconds=int(os.environ.get("EVAL_DEBOUNCE_SECONDS", "60")))
RUNNING = ("Running", "Pending", "ContinuedAsNew")

_targets = None


def targets() -> list:
    """The indexes to compare, opened once per worker.

    This is the part to edit. Each target is the same documents on one
    engine; the library asks each handle what it can do and runs all of it.
    Open an index with the vectors it was built with. The semantic ranker
    is chosen per search, so one index is compared with it and without it:
    opened this way, an index with no semantic configuration gets one, and
    one it already has, made here or in the portal, is kept as it is.

    A collection kept in SQLite on a VectrixDB server's own disk cannot be
    reached from here. Run ``vectrixdb evaluate`` on that server instead,
    on a timer or from the same event.
    """
    global _targets
    if _targets is None:
        second = {
            "endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
            "deployment": os.environ["AZURE_OPENAI_DEPLOYMENT"],
            "dimensions": int(os.environ.get("AZURE_OPENAI_DIMENSIONS", "1536")),
        }
        store = VectrixDB.with_azure_search(
            os.environ["AZURE_SEARCH_ENDPOINT"],
            key=os.environ.get("AZURE_SEARCH_KEY"),
            semantic=os.environ.get("AZURE_SEARCH_SEMANTIC", "true").lower() != "false",
            embeddings="both",
            azure_embedding=second,
        )
        handle = Vectrix(COLLECTION, storage_backend=store, path="/tmp/vectrixdb", mode="hybrid")
        _targets = [Target(handle, name="Azure AI Search", collection=COLLECTION)]
    return _targets


def _instance(sha256: str, suffix: str = "") -> str:
    """One run per collection and golden file: the same bytes twice start nothing new."""
    name = re.sub(r"[^a-z0-9]+", "-", COLLECTION.lower()).strip("-") or "docs"
    return f"eval-{name}-{sha256[:20]}{suffix}"


def _reports_for(golden_url: str) -> str:
    return os.environ.get("EVAL_REPORTS") or golden_url.rsplit("/", 1)[0]


async def _start(
    client: df.DurableOrchestrationClient, url: str, instance: str, sha256: str
) -> str:
    status = await client.get_status(instance)
    if (
        status is not None
        and status.runtime_status is not None
        and status.runtime_status.name in RUNNING + ("Completed",)
    ):
        return f"{instance} already ran or is running"
    await client.start_new("evaluate_golden", instance, {"url": url, "sha256": sha256})
    return f"started {instance}"


@app.event_grid_trigger(arg_name="event")
@app.durable_client_input(client_name="client")
async def golden_changed(event: func.EventGridEvent, client: df.DurableOrchestrationClient) -> None:
    """A golden file was saved: start its run unless these bytes already have one."""
    data = event.get_json() or {}
    url = str(data.get("url") or "")
    if not url.endswith(GOLDEN_NAME) or event.event_type != "Microsoft.Storage.BlobCreated":
        return
    sha256 = read_golden(url).sha256
    print(
        json.dumps(
            {
                "golden": url,
                "sha256": sha256,
                "then": await _start(client, url, _instance(sha256), sha256),
            }
        )
    )


@app.route(route="evaluate-now", methods=["POST"])
@app.durable_client_input(client_name="client")
async def evaluate_now(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Run again with the golden file as it is: what an ingestion calls when it finishes."""
    url = os.environ["EVAL_GOLDEN_URL"]
    sha256 = read_golden(url).sha256
    stamp = datetime.now(timezone.utc).strftime("-%Y%m%d%H%M%S")
    said = await _start(client, url, _instance(sha256, stamp), sha256)
    return func.HttpResponse(
        json.dumps({"said": said}), mimetype="application/json", status_code=202
    )


@app.orchestration_trigger(context_name="context")
def evaluate_golden(context: df.DurableOrchestrationContext):
    """Wait out a burst of saves, then run every setup at once and report."""
    job = context.get_input()
    yield context.create_timer(context.current_utc_datetime + DEBOUNCE)
    now = yield context.call_activity("golden_sha", job["url"])
    if now != job["sha256"]:
        return {"skipped": "a newer save replaced the file, and its own run covers it"}
    setups = yield context.call_activity("list_setups", job)
    results = yield context.task_all(
        [context.call_activity("run_one_setup", {"url": job["url"], "setup": s}) for s in setups]
    )
    return (yield context.call_activity("save_report", {"job": job, "results": results}))


@app.activity_trigger(input_name="url")
def golden_sha(url: str) -> str:
    return read_golden(url).sha256


@app.activity_trigger(input_name="job")
def list_setups(job: dict) -> list:
    return [setup for target in targets() for setup in setups_of(target)]


@app.activity_trigger(input_name="work")
def run_one_setup(work: dict) -> dict:
    """One setup, every question: searches only. The file is small, so each activity reads it."""
    golden = read_golden(work["url"])
    setup = work["setup"]
    target = next(t for t in targets() if (t.name or "") == setup["target"])
    return run_setup(target, setup, golden.labelled)


@app.activity_trigger(input_name="work")
def save_report(work: dict) -> dict:
    """Save the run beside the golden file, with the three picks and any expected document the index lacks."""
    job = work["job"]
    golden = read_golden(job["url"])
    if golden.sha256 != job["sha256"]:
        # Saved again while the setups ran, so some may have read the new
        # questions. That save started its own run, which covers it.
        return {"skipped": "the golden file changed during the run, and its own run covers it"}
    described = {}
    for target in targets():
        info = describe_target(target)
        described[info["name"]] = info
    # A document removed or renamed since the golden file was written is a
    # miss on every setup. Named here, in the run and in the function's output.
    missing = missing_documents(targets(), golden.labelled)
    report = build_report(
        work["results"],
        golden.describe(),
        question_ids=[str(q.id) for q in golden.labelled],
        targets=described,
        missing=missing,
    )
    run = report_store(_reports_for(job["url"])).save(report, golden=golden.raw)
    return {"run": run, "picks": report["picks"], "missing": missing}
