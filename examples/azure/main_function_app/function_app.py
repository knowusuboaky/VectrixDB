"""The main function app: the library's API, hosted, plus the queue trigger that fills it.

Read it top to bottom. First SETTINGS, every variable this app reads and what
it is for. Then the ten steps, each under its own heading, and each with what
goes in, what comes out, and its code:

    STEP ONE: EVENTS                handle_events         the message that says a file arrived
    STEP TWO: READING               file_readers          who reads the file, and masks it as it reads
    STEP THREE: MARKDOWN            blob_folders          where its Markdown is kept; it waits there until a cut is picked
    STEP FOUR: GOLDEN DATA          write_golden_dataset  a hundred questions whose answers are known, from the Markdown
    STEP FIVE: CHUNKING COMPARED    compare_techniques    every way of cutting, the same characters handed over, one picked
    STEP SIX: CHUNKS                chunking              the cut in force: step five's pick, or the one pinned
                                    apply_the_cut         every kept document brought to a new cut
    STEP SEVEN: EMBEDDING           embedding_models      the models each chunk is embedded with
    STEP EIGHT: INDEXING            search_index          the index the chunks are written to
    STEP NINE: RETRIEVAL COMPARED   evaluate_collections  every way of searching, ranked, and three named
    STEP TEN: RETRIEVAL             retrieve              a question searched the way in force, and answered

A file goes one, two, three, then six, seven, eight. Four, five and nine run
for every collection when you ask for them, and five and nine each make a
pick, which is what six and ten then use:

    INGEST_CHUNKING=auto        step five's best cut, over checked questions, until a new run beats it by more than luck
    INGEST_CHUNKING=<build>     that cut, markdown-1000-h say, whatever the runs say
    RETRIEVAL_SETUP=auto        each collection's best_for_balance from step nine
    RETRIEVAL_SETUP=<pick>      finds_the_most or best_for_time instead, still following the runs
    RETRIEVAL_SETUP=<way>       that way of searching, hybrid_semantic.both say, in every collection

Until step five has picked a cut, or one is pinned, files wait at step three
with their Markdown kept, and nothing is indexed: a cut is chosen once rather
than indexed twice. A new cut, picked or pinned, is brought to every kept
document from its Markdown, THE CUT APPLIED under step six, and step nine is
then asked again, because what it measured was the old cut.

Then THE API, the library's routes this app serves and the few of its own,
and last the MAIN SCRIPT, which is what the Functions host runs: it opens
each collection with the steps, builds the API, and starts the queue trigger.

WHERE THE DEPLOYED CODE IS
--------------------------

You cannot read this code in the portal, and there is no setting to turn that
on. Flex Consumption runs the app from a package rather than from files, so
there is no Kudu console, no App Service Editor and no Code + Test pane.
(Apps on the older plans report ``functionAppContentEditingState``, saying
whether the editor is allowed. A Flex app does not report it at all, which is
its own answer.) What is deployed is ``released-package.zip`` in the storage
account's ``app-package-<app>-<id>`` container, and reading it needs Storage
Blob Data Reader: being Owner of the subscription does not include the data
plane. What you edit is this folder, and ``06_create_main_function_app.py
--code-only`` publishes it again.

``<your app>.scm.azurewebsites.net`` exists and is tempting. On Flex it
serves the deployment API only; there is no debug console behind it.
"""

import json
import logging
import os
import shutil
import threading
import time
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Optional, Tuple

from urllib.parse import unquote

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

import breaker
import collection_setup
import dead_letters
from collections_of import collection_of, doc_id_of, metadata_for, named, use_records, written_to
from golden_files import (
    ANSWERS,
    APPLY_STATUS,
    AUTO,
    AUTO_ROLE,
    CHUNKING_STATUS,
    EVALUATION_STATUS,
    GOLDEN,
    MOST,
    ROLES,
    GoldenFolder,
    apply_message,
    apply_request,
    chunking_in_force,
    chunking_job,
    chunking_message,
    chunking_request,
    chunking_turn,
    evaluation_message,
    evaluation_request,
    golden_message,
    golden_request,
    queue_account,
    recorded_cut,
    retrieval_in_force,
    with_chunking_pick,
    with_retrieval_picks,
)
from ingest_queue import events_in, outcome_line, should_go_round_again
from readers import (
    describer_from_environment,
    extraction_app_from_environment,
    extractors_from_environment,
    page_reader_from_environment,
    unread,
    what_reads_what,
)
from vectrixdb import Vectrix, VectrixDB
from vectrixdb.evaluation import ChatWriter, chunking_build, chunking_options, handed_over
from vectrixdb.exceptions import ConfigurationError, ExtractionError, PolicyError
from vectrixdb.worker import BlobFetcher, IngestWorker

#: Every line this app logs, under one name, so a host can turn it up or down on its own.
log = logging.getLogger("vectrixdb.azure")
#: Each collection's worker, with the cut it was made with: made again when the cut in force changes.
_workers: Dict[str, Tuple[str, IngestWorker]] = {}
#: Each collection, opened once for the life of the process.
_open: Dict[str, Vectrix] = {}
#: Held while a collection is opened, because a question and a file can arrive at the same moment.
_opening = threading.Lock()


# ============================================================================
# SETTINGS: every variable this app reads, and what it is for
# ============================================================================
#
# 06_create_main_function_app.py sets them, from settings.env and from what
# 01 to 05 made. The keys among them are read from Azure by 06 and sent
# straight to the app; none is written into this code or into a file.
#
# The Functions host
#   FUNCTIONS_WORKER_RUNTIME         python
#   AzureWebJobsStorage              the host's own storage, which the queue trigger listens through
#
# Where things are
#   INGEST_ROLE                      what this deployment of the folder does: both (the default, one app), ingest (the queue
#                                    trigger and health, no API), or query (the API and the dashboard, never a file); step 06
#                                    deploys the folder twice, so a burst of files never slows a search
#   INGEST_QUEUE                     the queue Event Grid writes to when a file lands, where this app's own jobs wait too: ingest
#   INGEST_BREAKER_FAILURES          reads in a row that could not reach the extraction app before ingestion pauses; left out, 5
#   INGEST_BREAKER_SECONDS           how long it pauses before one probe goes through; left out, 300
#   INGEST_CHUNKS_PER_SECOND         a pace on index writes for a burst of files, chunks a second; left out, none
#   VECTRIXDB_EXTRACTOR_RETRIES      the library's: how many more times a file is asked of the extraction app after a timeout or a 429/503; left out, 3
#   INGEST_BLOB_ACCOUNT              the storage account's Blob address: https://<account>.blob.core.windows.net
#   INGEST_COLLECTIONS               the collections, comma separated, each a folder under ingestion/raw/: financial,media,misc;
#                                    a collection made on the dashboard is known from its record instead
#   INGEST_PATH                      this instance's scratch folder; a function app may write under /tmp only: /tmp/vectrixdb
#
# Step two, reading (the extraction app of 05 reads every file)
#   VECTRIXDB_EXTRACTOR_URL          the extraction app's address
#   VECTRIXDB_EXTRACTOR_KEY          its key, which 06 reads from that app's own settings
#   VECTRIXDB_EXTRACTOR_KEY_HEADER   the header the key goes in: api-key
#   VECTRIXDB_EXTRACTOR_BODY         how a file is sent: raw, its bytes as the body and its name in X-Filename
#   VECTRIXDB_EXTRACTOR_TIMEOUT      seconds to wait for it: 230, the most Azure lets an HTTP request run
#   VECTRIXDB_EXTRACTOR_MASK         1: every file is masked as it is read, so the Markdown kept and the index never held an
#                                    identifier; what was masked comes back with the document and the setup spinner counts it
#   EXTRACTION_PREFIX                the path every extraction route lives under, as 05 was given it; empty for the root
#   EXTRACTION_GATEWAY_PATHS         the paths a gateway publishes each extraction route under, as 05 was given them
#   EXTRACTION_BATCH_MINUTES         a long recording goes in pieces of this many minutes: 10
#   EXTRACTION_BATCH_PAGES           a long PDF goes this many pages at a time: 20
#   AZURE_DOCINTEL_ENDPOINT, _KEY    without an extraction app only: Document Intelligence, for scanned pages
#   AZURE_SPEECH_ENDPOINT, _KEY      without an extraction app only: Speech, for recordings
#   AZURE_VISION_ENDPOINT, _KEY      without an extraction app only: Vision, which describes pictures
#
# Step three, Markdown
#   VECTRIXDB_KEEP_SOURCE            where the hosted Documents page opens the Markdown: <blob account>/ingestion/markdown
#   VECTRIXDB_GRAPH_STORE            where a collection's knowledge graph is kept, one a collection: <blob account>/ingestion/graph
#
# Steps four, five and nine: the golden questions and the two comparisons
#   VECTRIXDB_EVALUATIONS            the evals container: the golden dataset, the runs, and picks.json
#   EVAL_GOLDEN_URL                  a golden file somewhere else to ask; left out, evals/golden_dataset/golden.jsonl
#   AZURE_OPENAI_ENDPOINT            the Azure OpenAI resource, whose chat model writes the questions and the answers
#   AZURE_OPENAI_WRITER_DEPLOYMENT   that chat model's deployment: gpt-5.4-mini
#   AZURE_OPENAI_KEY                 its key; left out, the app's managed identity, which needs Cognitive Services OpenAI User
#   AZURE_OPENAI_API_VERSION         the API version it is called with; left out, 2024-10-21
#
# Step six, the cut
#   INGEST_CHUNKING                  auto follows step five's pick; a build key, markdown-1000-h say, pins one. Left out, auto
#
# Step seven, embedding
#   AZURE_OPENAI_EMBED_DEPLOYMENT    the second vector's model, text-embedding-3-small; left out, the built-in vector alone
#   AZURE_OPENAI_EMBED_DIMENSIONS    how many numbers that model gives; left out, 1536
#
# Step eight, indexing
#   AZURE_SEARCH_ENDPOINT            the Azure AI Search service: https://<service>.search.windows.net
#   AZURE_SEARCH_KEY                 its admin key, which 06 reads from Azure
#   AZURE_SEARCH_SEMANTIC            false when the tier has no semantic ranker; left out, true, which basic has
#   VECTRIXDB_PARENT_STORE           Cosmos data_db/parent_sections, where the sections a search returns live
#   VECTRIXDB_CHUNK_STORE            Cosmos data_db/chunk_records, the copy of every chunk the collection pages count
#
# Who may see what, read by every step that opens a collection and by the hosted API alike
#   VECTRIXDB_COLLECTION_STORE       Cosmos access/collection_records: each collection's policy, who may see it and
#                                    whether it is masked, one record each, written by steps 04 and 07
#   VECTRIXDB_SIGNIN_STORE           Cosmos access/signin_records: the people, their sessions and their keys
#   VECTRIXDB_AUDIT_STORE            <blob account>/audit/decisions: a line for each decision an entitlement policy makes
#                                    each, appended to one blob a day in a container nothing can rewrite
#   VECTRIXDB_AUDIT_QUERY_KEY        the key each recorded query is fingerprinted with, which 06 keeps the same
#   VECTRIXDB_ACCESS_LOG             <blob account>/audit/access: every sign-in and read, the same way
#
# Step ten, retrieval
#   RETRIEVAL_SETUP                  auto follows each collection's best_for_balance; finds_the_most or best_for_time
#                                    follow that pick instead; a way of searching, hybrid_semantic.both say, pins one
#                                    in every collection. Left out, auto
#
# The hosted API, which reads these itself
#   VECTRIXDB_STORAGE_BACKEND        azure_search: the API reads the index the steps write
#   AZURE_SEARCH_INDEX_PREFIX        empty: the API names each index after its collection, as step eight does; Azure drops an empty setting, so the app puts it back
#   VECTRIXDB_PATH                   the API's scratch folder: /tmp/vectrixdb/_api
#   VECTRIXDB_DASHBOARD              no leaves the dashboard unpublished; left out, it is served at /
#   VECTRIXDB_EXTRACTOR_ROUTES       set here from the extraction app's table, so an upload goes where a dropped file goes
#   and every other VECTRIXDB_ setting the library's server reads, sign-in among them: docs/reference/settings.md


def _setting(name: str) -> str:
    """INGEST_CHUNKING or RETRIEVAL_SETUP as the app was given it: ``auto`` when it was left out."""
    return os.environ.get(name, "").strip() or AUTO


@lru_cache(maxsize=1)
def _blobs() -> BlobServiceClient:
    """The storage account, through the app's managed identity: one client for the process, so one credential and its tokens."""
    return BlobServiceClient(os.environ["INGEST_BLOB_ACCOUNT"], credential=DefaultAzureCredential())


@lru_cache(maxsize=1)
def _queue() -> Any:
    """The app's one queue, beside the blobs. Its long work waits there, because the queue trigger is the only function declared."""
    from azure.storage.queue import QueueClient

    return QueueClient(
        queue_account(os.environ["INGEST_BLOB_ACCOUNT"]),
        os.environ["INGEST_QUEUE"],
        credential=DefaultAzureCredential(),
    )


@lru_cache(maxsize=1)
def _poison_queue() -> Any:
    """Where the host moves a message after five tries: the files that would not read, which the Ingest page lists."""
    from azure.storage.queue import QueueClient

    return QueueClient(
        queue_account(os.environ["INGEST_BLOB_ACCOUNT"]),
        dead_letters.poison_queue_name(os.environ["INGEST_QUEUE"]),
        credential=DefaultAzureCredential(),
    )


#: What one deployment of this folder does. Both is one app doing everything, the default; ingest and query are the
#: same folder deployed twice, on separate instances, so reading files never contends with answering questions.
#: Not ROLES: that name is the picks' roles, best_for_balance and the others, further up.
APP_ROLES = ("both", "ingest", "query")


def role() -> str:
    """``INGEST_ROLE``: both, ingest or query; anything else is read as both, with a warning."""
    asked = (os.environ.get("INGEST_ROLE") or "both").strip().lower()
    if asked not in APP_ROLES:
        log.warning(
            "INGEST_ROLE is %r, which is none of %s: running as both", asked, ", ".join(APP_ROLES)
        )
        return "both"
    return asked


@lru_cache(maxsize=1)
def _breaker() -> Any:
    """The circuit breaker for the extraction app, its state in the ingestion container where every instance reads it."""
    return breaker.Breaker(
        breaker.state_files(_blobs()),
        "extraction app",
        threshold=int(os.environ.get("INGEST_BREAKER_FAILURES") or 5),
        open_for=float(os.environ.get("INGEST_BREAKER_SECONDS") or 300),
    )


@lru_cache(maxsize=1)
def _collection_store() -> Any:
    """Each collection's record, through the app's managed identity: one for the process, None without the setting.

    Every collection this app opens reads who may retrieve from it, and its
    entitlement, from here, and so does the hosted API, so the two can never
    enforce different rules. A record is held for thirty seconds, so a search
    is not a round trip to Cosmos. The records also name the collections
    made on the dashboard, which INGEST_COLLECTIONS does not know.
    """
    from vectrixdb.collection_records import open_collection_store

    store = open_collection_store(os.environ.get("VECTRIXDB_COLLECTION_STORE", "").strip() or None)
    use_records(store)
    return store


@lru_cache(maxsize=1)
def _audit_sink() -> Any:
    """Where each decision an entitlement policy makes is recorded: VECTRIXDB_AUDIT_STORE, or None without it or its key.

    The same store and the same key the hosted API writes its decisions
    with, so the Audit page shows the steps' searches beside the API's. A
    decision that cannot be recorded is not answered: the search is refused.
    """
    where = os.environ.get("VECTRIXDB_AUDIT_STORE", "").strip()
    key = os.environ.get("VECTRIXDB_AUDIT_QUERY_KEY", "").strip()
    if not where or not key:
        return None
    from vectrixdb.audit import DENY, audit_sink_at

    return audit_sink_at(where, query_key=key.encode(), on_failure=DENY)


def _golden_folder() -> GoldenFolder:
    """The evals container, where the golden dataset, the runs and the picks live."""
    return GoldenFolder.at(os.environ["VECTRIXDB_EVALUATIONS"], _blobs())


def _scratch(*parts: str) -> str:
    """A folder on this instance's disk, under INGEST_PATH: /tmp, the only place a function app may write."""
    return os.path.join(os.environ.get("INGEST_PATH", "/tmp/vectrixdb"), *parts)


# ============================================================================
# STEP ONE: EVENTS
# ============================================================================


def handle_events(message: func.QueueMessage) -> None:
    """What one queue message says arrived, handed to the worker of each file's collection.

    INPUT
    -----
    One message on the ``ingest`` queue, which Event Grid writes when a blob
    appears under ``ingestion/raw/`` or leaves it, as JSON or as base64::

        {"eventType": "Microsoft.Storage.BlobCreated",           or BlobDeleted
         "subject": "/blobServices/default/containers/ingestion/blobs/raw/financial/td/ar2025.pdf",
         "data": {"url": "https://<account>.blob.core.windows.net/ingestion/raw/financial/td/ar2025.pdf",
                  "eTag": "0x8DC...", "contentLength": 1048576}}

    OUTPUT
    ------
    Each file through its collection's worker: steps two and three, then six
    to eight once a cut is in force. One log line a file::

        financial -> created | updated | unchanged | deleted | failed | ignored  <blob>  | <n> chunks

    and ExtractionError raised when a file failed to read, so the message
    comes back; after five tries it is in ``ingest-poison``.

    ``ingest_one``, the queue trigger at the bottom of this file, calls this
    with every message that is not a job. **It has no URL and cannot be called
    over HTTP**: looking for one is the usual first confusion. To make it run,
    upload a file under ``ingestion/raw/``.

    The blob's folder says where it goes, and nothing else is configured:
    ``ingestion/raw/financial/td/ar2025.pdf`` is written to the financial
    collection as ``td/ar2025.pdf``. A blob whose folder names no
    collection is left alone rather than guessed at, and so is a file whose
    suffix the extraction app does not read, with one line in the log rather
    than five tries into ``ingest-poison``. One message at a time, with the
    function's whole timeout for one file, so a two hundred page report is
    never cut off half way. Put an alert on the poison queue's length and no
    file ever vanishes quietly.

    It is deliberately built apart from the API. If the API cannot be built
    at all, a bad setting say, this app still serves a 503 that says why and
    still ingests every file, rather than failing to load and taking
    ingestion down with it.
    """
    events = events_in(message.get_body())
    if not events:
        log.info("nothing to read in message %s", message.id)
        return
    # A message can only hold events for one blob, but the batch shape is the
    # worker's, so they are grouped by collection and each goes to its own.
    by_collection: Dict[str, List[Any]] = {}
    for event in events:
        name = collection_of(event.uri)
        if name is None:
            log.info("no collection named in %s, so it is left alone", event.uri)
            continue
        suffix = unread(event.uri)
        if suffix:
            # Not raised: nothing reads it, so five tries would only put it in
            # ingest-poison, the list of files that failed to read.
            log.info("the extraction app reads no %s file, so %s is left alone", suffix, event.uri)
            continue
        by_collection.setdefault(name, []).append(event)
    if not by_collection:
        return

    # The extraction app is down: the message waits instead of burning one of
    # its five tries, and one probe a window goes through to see if it is back.
    gate = _breaker()
    if not gate.allow():
        body = message.get_body()
        _queue().send_message(
            body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body),
            visibility_timeout=int(gate.open_for),
        )
        log.info(
            "ingestion paused: %s; message %s waits %ss",
            gate.status()["said"],
            message.id,
            int(gate.open_for),
        )
        return

    # Step six's cut, read once a message, so a new pick reaches the next
    # file without a restart.
    cut = chunking()
    if cut.get("index") is False:
        log.info(
            "no cut is in force yet, so the Markdown is kept and nothing is indexed: GET /api/v1/picks says why"
        )
    failed, read = [], 0
    for name, theirs in by_collection.items():
        outcomes = worker(name, cut).handle_all(theirs)
        read += len(outcomes)
        for outcome in outcomes:
            log.info("%s -> %s (try %s)", name, outcome_line(outcome), message.dequeue_count)
            _note_outcome(outcome, message.dequeue_count)
        _note_dependency(outcomes)
        failed += [o for o in outcomes if should_go_round_again(o)]
    if failed:
        raise ExtractionError(f"{len(failed)} of {read} did not read: {outcome_line(failed[0])}")


def _note_outcome(outcome: Any, tries: Any) -> None:
    """A file that failed to read keeps a record beside the poison queue, in words; one that read has its record cleared.

    Bookkeeping for the Ingest page's list of files that would not read.
    It never stops a read: a record that cannot be written is a warning.
    """
    try:
        if should_go_round_again(outcome):
            dead_letters.record_failure(_blobs(), outcome, int(tries or 0))
        else:
            dead_letters.clear_failure(_blobs(), str(getattr(outcome, "uri", "") or ""))
    except Exception as exc:  # noqa: BLE001 - bookkeeping never stops a read
        log.warning("could not note the outcome of %s: %s", getattr(outcome, "uri", "?"), exc)


def _note_dependency(outcomes: Any) -> None:
    """What the reads said about the extraction app: one that could not reach it counts against it, one that got through clears the count."""
    try:
        gate = _breaker()
        unreachable = [
            o
            for o in outcomes
            if should_go_round_again(o) and breaker.is_transient(getattr(o, "error", ""))
        ]
        if unreachable:
            gate.record_failure(getattr(unreachable[0], "error", ""))
        elif any(
            str(getattr(o, "action", "")) in ("created", "updated", "unchanged") for o in outcomes
        ):
            gate.record_success()
    except Exception as exc:  # noqa: BLE001 - bookkeeping never stops a read
        log.warning("could not note the extraction app's state: %s", exc)


# ============================================================================
# STEP TWO: READING
# ============================================================================


def file_readers() -> Dict[str, Any]:
    """Who reads a file: the extraction app, a picture describer, and OCR for scanned pages.

    INPUT
    -----
    The settings of step two; what each reads is a file's bytes and its name,
    fetched from the blob.

    OUTPUT
    ------
    What reads a file, as ``Vectrix`` options::

        {"extractors": {".pdf": <HttpExtractor at /extract/pdf>, ".wav": <...>, ...},
         "describe_figures": <a picture describer> or None,
         "page_ocr": <Document Intelligence's reader> or None}

    and, when a file is read, its Markdown: its pages, its headings, each
    picture with what it shows written under it, a recording's phrases with
    their times.

    A file is sent to the extraction app of step 05, at the route its suffix
    names: the library's table, worked out from the prefix and gateway paths
    the extraction app answers at, so the two apps agree on every path. The
    extraction app reads it, whatever it is. Its pictures are described by
    Vision where they sit, when it has any; its pages with no text layer are
    read by Document Intelligence, and only those; a recording comes back
    with its timed phrases. A long file goes in pieces, because Azure ends
    every HTTP request at 230 seconds: sound in pieces of about ten minutes,
    cut at a pause, a video's sound the same way, a PDF twenty pages at a
    time, several pieces at once, each put back with its times and page
    numbers moved on.

    Without an extraction app, no ``VECTRIXDB_EXTRACTOR_URL``, the services
    this app is given read the files here instead, as it once did. All of it
    is the library's; this file only says who to hand it to.
    """
    return {
        "extractors": extractors_from_environment(),
        "describe_figures": describer_from_environment(),
        # Only the pages of a PDF that have no text layer, and never a page
        # with no ink, so a born-digital report costs nothing here.
        "page_ocr": page_reader_from_environment(),
    }


# ============================================================================
# STEP THREE: MARKDOWN
# ============================================================================


def blob_folders(blobs: Any, name: str) -> Dict[str, Any]:
    """The blob folders a collection's Markdown and chunks are kept in.

    INPUT
    -----
    The storage account and the collection's name.

    OUTPUT
    ------
    Where each step's work is written down, as ``Vectrix`` options::

        {"keep_source": <DocumentStore at ingestion/markdown/<collection>/>,
         "keep_chunks": <BlobFiles at ingestion/chunks/<collection>/>,
         "markdown_first": True}

    and in that folder, a file a document with its front matter first, and
    an index of them all::

        ingestion/markdown/financial/td/ar2025.pdf.md
            ---
            vectrixdb: extracted
            doc_id: td/ar2025.pdf
            version: 3f9a1c0e2b7d4a55                        a hash of the text
            source_version: 0x8DC...                         the blob's ETag
            extracted_at: 2026-09-22T10:15:00+00:00
            ---
            # Annual report 2025 ...
        ingestion/markdown/financial/_index.json
            {"documents": {"td/ar2025.pdf": {"file": "td/ar2025.pdf.md", "version": ..., "source_version": ...,
                                             "user_metadata": {"collection": "financial"},
                                             "chunking": {"chunk": "markdown", "chunk_size": 1000, ...}}}}

    ``chunking`` is there once the document is cut, and is how a document
    at the cut in force is told from one that is not.

    The Markdown is kept before anything is cut. **Until a cut is in force,
    a file stops here**: step six has nothing to cut it with, its Markdown
    waits with no ``chunking``, and nothing is indexed. When a cut is picked
    or pinned, THE CUT APPLIED cuts every waiting document from its Markdown,
    and the file is never fetched or read again, which is the part that
    costs money. From here each step's output is written down before the
    next step begins, so a step that fails sends the message round again,
    and the next try starts from the kept Markdown.

    A file deleted from ``raw/`` takes its Markdown, its chunks and its place
    in the index with it, and no copy is kept.
    """
    return written_to(blobs, name)


# ============================================================================
# STEP FOUR: GOLDEN DATA
# ============================================================================


def golden_questions() -> Dict[str, Any]:
    """How many golden questions, and of which kinds: a hundred, from a search of three words to a long question.

    OUTPUT
    ------
    ``write_golden``'s plan::

        {"n": 100, "mix": {"search": 20, "fact": 30, "why": 20, "two_page": 15, "long": 15}, "evolve": 1}

        20  searches         3 to 8 words, as typed into a search box
        30  facts            a short question after one figure, date, name or value in a table
        20  how or why       a reason, a cause, a process
        15  two places       comparing or combining what two pages say
        15  long questions   20 to 45 words, the asker's situation first, then the question

    Half are short, because that is most of what people type. A third ask
    how or why or across two pages, which is where one way of searching pulls
    ahead of another. The long ones are how a person writes who explains
    themselves first, and what a search tuned on short queries finds hardest.
    Every question but a search is then rewritten once to be harder: more
    concrete, a comparison, or needing both of its pages.

    They are spread over every collection by the size of each part of each
    document, at least one a part, so a two hundred page report is asked
    about all the way through and a lone picture still gets its question.
    """
    return {
        "n": 100,
        "mix": {"search": 20, "fact": 30, "why": 20, "two_page": 15, "long": 15},
        "evolve": 1,
    }


def golden_writer() -> Optional[ChatWriter]:
    """The chat model that drafts the questions, judges them, and writes step ten's answers: an Azure OpenAI deployment, or None.

    ``AZURE_OPENAI_WRITER_DEPLOYMENT`` names the deployment on the resource
    at ``AZURE_OPENAI_ENDPOINT``, the resource the second vector comes from.
    ``AZURE_OPENAI_KEY`` is its key; left out, the function's managed
    identity is used, which needs the Cognitive Services OpenAI User role on
    the resource. ``AZURE_OPENAI_API_VERSION`` changes the API version,
    2024-10-21 when it is not set.
    """
    return ChatWriter.from_environment()


#: How the kept Markdown is cut to write questions from: at headings, into
#: sections of up to 3,000 characters, nothing carried over. No cut step five
#: compares is this one, so the questions favour none of them.
PASSAGES: Dict[str, Any] = {"chunk": "markdown", "chunk_size": 3000, "overlap": 0}
#: What is said when no model is named.
NO_WRITER = (
    "No model is named to write with. Set AZURE_OPENAI_WRITER_DEPLOYMENT to a chat deployment on the resource "
    "at AZURE_OPENAI_ENDPOINT, with AZURE_OPENAI_KEY, or give this app the Cognitive Services OpenAI User role."
)
#: What is said when there is a golden file already.
THERE_ALREADY = f"{GOLDEN} is already there, and questions somebody checked are not written over. Move it aside and ask again."
#: What is said when nothing has been kept to ask about.
NOTHING_KEPT = "No collection keeps any Markdown yet, so there is nothing to ask about. Drop files in ingestion/raw/ first."


def write_golden_dataset(asked: Dict[str, Any]) -> None:
    """Draft the golden dataset from every collection's kept Markdown into ``evals/golden_dataset/golden.jsonl``.

    INPUT
    -----
    The queue message ``POST /api/v1/golden/write`` puts on the queue::

        {"vectrixdb": "golden", "n": 100}

    with every collection's kept Markdown from step three, and
    ``golden_dataset/examples.txt``, a few questions people really ask, one a
    line, when it is there.

    OUTPUT
    ------
    In the evals container::

        golden_dataset/golden.jsonl            a question a line, every row a draft to check:
            {"id": "q1", "question": "How much did net income rise in 2025?",
             "expected": ["td/ar2025.pdf#page=41"], "reference": "By 12%, to $4.1 billion.",
             "evidence": ["Net income rose 12% to $4.1 billion"],
             "hint": "page 41: Net income rose 12% ...", "draft": true}
        golden_dataset/golden.answers.jsonl    every answer the model gave, so a stopped run starts where it stopped
        golden_dataset/golden.status.json      {"state": "asked | writing | done | refused | failed",
                                                "at": "2026-09-22T10:15:00+00:00", "written": 40, "wanted": 100, ...}

    It runs in the queue trigger, never in a request: a hundred questions are
    some six hundred calls to the model, ten to twenty minutes, and Azure
    ends an HTTP request at 230 seconds. ``GET /api/v1/golden/write`` reads
    the status.

    It runs before anything is cut, so it reads the Markdown step three kept
    rather than chunks: each collection's documents are cut into
    :data:`PASSAGES` in a throwaway collection on this instance's disk, by
    :func:`_passages`, and thrown away once the questions are written. The
    labels name pages and the words that answer, not chunks, so they hold
    for whichever cut is picked. The library does the writing,
    ``write_golden``: passages spread over every section and scored,
    questions of the kinds :func:`golden_questions` says, each quote found
    on its page, copied wording and repeats sent back, a critic, and a
    rewrite to make each harder.

    Never over a golden file that is there: it may hold questions somebody
    checked. Every row is a draft until a person reads it against its page
    and deletes ``"draft": true``, and draft questions never move a pick. A
    model that refuses, a key it will not take or a deployment that is not
    there, is said in the status and not tried five more times; anything else
    is said and raised, so the queue tries again from the answers kept.
    """
    from vectrixdb.evaluation import WriterUnavailable, write_golden

    folder = _golden_folder()
    if folder.exists(GOLDEN):
        folder.status("refused", message=THERE_ALREADY)
        return
    writer = golden_writer()
    if writer is None:
        folder.status("failed", message=NO_WRITER)
        return
    plan = {**golden_questions(), **asked}
    scratch = _scratch("_golden")
    drafts, answers = os.path.join(scratch, GOLDEN), os.path.join(scratch, ANSWERS)
    # The same golden_dataset folder here as in the container.
    os.makedirs(os.path.dirname(drafts), exist_ok=True)
    if os.path.exists(drafts):
        os.remove(drafts)
    folder.fetch_answers(answers)
    folder.status("writing", written=0, wanted=plan["n"])

    def progress(made: int, wanted: int) -> None:
        if made % 10 == 0 or made == wanted:
            folder.keep_answers(answers)
            folder.status("writing", written=made, wanted=wanted)

    opened: List[Vectrix] = []
    try:
        opened, handles = _passages(named())
        if not handles:
            folder.status("failed", message=NOTHING_KEPT)
            return
        written = write_golden(
            handles,
            drafts,
            writer=writer,
            examples=folder.examples(),
            cache=answers,
            progress=progress,
            **plan,
        )
    except WriterUnavailable as exc:
        folder.keep_answers(answers)
        folder.status("failed", message=str(exc))
        log.warning("the golden dataset was not written: %s", exc)
        return
    except Exception as exc:
        folder.keep_answers(answers)
        folder.status(
            "failed",
            message=f"{type(exc).__name__}: {exc}. The queue tries again, from the answers kept.",
        )
        raise
    finally:
        for db in opened:
            db.close()
        shutil.rmtree(_scratch("_golden", "passages"), ignore_errors=True)
    folder.keep_answers(answers)
    with open(drafts, "rb") as made:
        folder.write(GOLDEN, made.read(), overwrite=False)
    folder.status(
        "done",
        written=len(written.rows),
        wanted=written.wanted,
        file=folder.url(GOLDEN),
        summary=written.summary().splitlines(),
    )
    log.info("the golden dataset: %s", written.summary().splitlines()[0])


def _passages(names: List[str]) -> Tuple[List[Vectrix], List[Any]]:
    """Each collection's kept Markdown cut into :data:`PASSAGES`, in a throwaway collection: ``(opened, handles)``.

    ``opened`` are the throwaway collections, to close; ``handles`` are what
    ``write_golden`` reads, one a collection that keeps any Markdown. Each
    keeps a copy of the Markdown of its own, so the writer sees every
    document's sections, and carries the metadata each document came with.
    """
    home = _scratch("_golden", "passages")
    shutil.rmtree(home, ignore_errors=True)
    opened: List[Vectrix] = []
    handles: List[Any] = []
    for name in names:
        kept = written_to(_blobs(), name)["keep_source"]
        ids = kept.ids()
        if not ids:
            continue
        db = Vectrix(
            name,
            path=os.path.join(home, name, "index"),
            embedding_cache=False,
            keep_source=os.path.join(home, name, "markdown"),
            collection_store=_collection_store(),
        )
        opened.append(db)
        for doc_id in ids:
            db.add_document(
                kept.get(doc_id),
                doc_id=doc_id,
                metadata=(kept.entry(doc_id) or {}).get("user_metadata") or None,
                progress=False,
                # Every document asked about, however it read: a person checks the questions.
                on_low_quality="allow",
                **PASSAGES,
            )
        handles.append(db)
    return opened, handles


def _kept_markdown(names: List[str]) -> Dict[str, Dict[str, Any]]:
    """Every document step three kept, collection by collection, as the library read it: ``{collection: {doc_id: document}}``."""
    kept: Dict[str, Dict[str, Any]] = {}
    for name in names:
        store = written_to(_blobs(), name)["keep_source"]
        kept[name] = {doc_id: store.get(doc_id) for doc_id in store.ids()}
    return kept


# ============================================================================
# STEP FIVE: CHUNKING COMPARED
# ============================================================================


def chunking_rules() -> Dict[str, Any]:
    """What is compared: every technique, three sizes each, headings in and out, the same characters handed over.

    OUTPUT
    ------
    ``{"budget": 6000}``, the characters every build hands the model for each
    question, which step ten hands over too::

        Structure-aware   at headings, then long sections                     500, 1,000, 2,000
        Parent-child      small chunks find, their sections are handed over   250, 500, 1,000
        Recursive         paragraphs, then lines, sentences and words         500, 1,000, 2,000
        Sentence          whole sentences, up to the size                     500, 1,000, 2,000
        Semantic          a new chunk where the meaning shifts                up to 500, 1,000, 2,000
        Fixed-size        every so many characters, at a space                500, 1,000, 2,000
        LLM-based         the model says where each topic starts              up to 500, 1,000, 2,000

    Each at every size with its heading path embedded in front of each chunk,
    and without, a fifth of the size carried over. With the model step four
    writes with, LLM-based is built too, and each technique once more at its
    middle size with a note from the model in front of every chunk; without
    it, both are left out and the run says so. Late chunking needs an
    embedding model that pools the mean of its tokens, which the built-in one
    does not, so it is said and left out. For every golden question each
    build hands over its best hits until the budget is used, so a technique
    that cuts small hands over more chunks and one that cuts large fewer, and
    none has more to read.
    """
    return {"budget": 6000}


def compare_techniques(asked: Dict[str, Any]) -> None:
    """Cut the collections' kept Markdown every way, hand each golden question the same characters, and pick one.

    INPUT
    -----
    The queue message ``POST /api/v1/chunking/run`` puts on the queue, and
    that this puts on it again after each build, for the next::

        {"vectrixdb": "chunking", "golden": "https://<account>.blob.core.windows.net/evals/golden_dataset/golden.jsonl",
         "collections": ["financial", "media", "misc"], "job": "20260922-101500", "at": 0}

    with the golden file and every collection's kept Markdown.

    OUTPUT
    ------
    In the evals container::

        chunking/work/<job>/<build>.json        each build's outcome, kept until the run is saved:
            {"key": "markdown-1000-h", "technique": "markdown", "size": 1000, "questions": 100,
             "found": 81, "answered": 74, "chunks": 1210, "outcomes": {"q1": [true, true], ...}}
        chunking/runs/<id>/report.json          the run, which the Evaluate page's Chunking tab draws:
            {"kind": "chunking", "id": "20260922-104012-ab12cd", "scored": "answered", "questions": 100,
             "techniques": [{"key": "markdown", "name": "Structure-aware", "rank": 1, "best": "markdown-1000-h",
                             "right": 74, "wrong": 7, "missed": 19, "tie": false}, ...],
             "builds": [...]}
        chunking.status.json                    {"state": "asked | running | done | refused | failed", "build": 12, "of": 49,
                                                 "run": ..., "best": ..., "picked": {"state": "switched", "key": ..., "why": ...}}
        picks.json, when the cut in force moves {"chunking": {"key": "markdown-1000-h", "run": ..., "why": ..., "at": ...}}

    It runs in the queue trigger, but one build a message: forty builds are
    more than the function's hour, so each message makes one, keeps it, and
    asks for the next. ``GET /api/v1/chunking/run/status`` reads the status.
    A build is made on this instance's disk, a scratch collection with the
    built-in model, and thrown away: nothing is written to the index and no
    collection changes. With a model named, every answer is written from the
    characters handed over and judged against the golden answer; without
    one, the run counts what was found in them.

    Then :func:`_pick_a_cut` says what the run does to the cut in force, and
    the status says it as ``picked``. A bad golden file is refused, a model
    that refuses is said, and anything else is said and not raised, so the
    queue does not start a comparison again on its own.
    """
    from vectrixdb.evaluation import (
        WriterUnavailable,
        answer_with,
        check_golden,
        chunking_report,
        chunking_store,
        judge_with,
        plan_chunking,
        run_chunking_build,
    )

    folder = _golden_folder()
    where = asked.get("golden") or os.environ.get("EVAL_GOLDEN_URL") or folder.url(GOLDEN)
    names = [name for name in (asked.get("collections") or named()) if name in named()]
    asked = {**asked, "golden": where, "collections": names}
    rules = chunking_rules()
    try:
        check = check_golden(where, fetcher=BlobFetcher(_blobs()))
        if not check.ok:
            folder.status(
                "refused",
                into=CHUNKING_STATUS,
                golden=where,
                message=f"{check.summary().splitlines()[0]}. Nothing was run.",
                problems=[str(p) for p in check.errors],
            )
            return
        gold = check.golden
        assert gold is not None, "a check that is ok has read the file"
        chat = golden_writer()
        answer, judge = (answer_with(chat), judge_with(chat)) if chat is not None else (None, None)
        # The same plan every message: what the model can add, and what is left out and why.
        plan, skipped, cut_with, context_with = plan_chunking(chat=chat)
        scratch = _scratch("_chunking")
        os.makedirs(scratch, exist_ok=True)
        documents: Dict[str, Dict[str, Any]] = {}

        def build(one: Dict[str, Any]) -> Dict[str, Any]:
            # Read once a message, and only when there is a build to make.
            if not documents:
                documents.update(_kept_markdown(names))
            try:
                return run_chunking_build(
                    documents,
                    gold.labelled,
                    one,
                    budget=rules["budget"],
                    answer=answer,
                    judge=judge,
                    workdir=scratch,
                    cut_with=cut_with,
                    context_with=context_with,
                )
            except WriterUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - one technique that cannot run leaves the others to be compared
                log.warning("the chunking build %s could not run: %s", one["key"], exc)
                return {**one, "questions": 0, "error": f"{type(exc).__name__}: {exc}"}

        def finish(results: List[Dict[str, Any]]) -> Dict[str, Any]:
            report = chunking_report(
                results, gold.describe(), budget=rules["budget"], skipped=skipped
            )
            chunking_store(os.environ["VECTRIXDB_EVALUATIONS"]).save(
                report, golden=gold.raw or None
            )
            # Said in the status rather than saved in the run: what it did to the cut in force.
            return {
                **report,
                "picked": _pick_a_cut(folder, results, report, drafts=gold.drafts, compared=names),
            }

        said = chunking_turn(
            folder, asked, plan, build=build, finish=finish, send=_queue().send_message
        )
    except WriterUnavailable as exc:
        folder.status(
            "failed", into=CHUNKING_STATUS, golden=where, job=asked.get("job"), message=str(exc)
        )
        log.warning("the chunking comparison stopped: %s", exc)
        return
    except Exception as exc:
        # Said, and not raised: the builds made are kept, and a comparison
        # asked for again once it is put right starts afresh.
        folder.status(
            "failed",
            into=CHUNKING_STATUS,
            golden=where,
            job=asked.get("job"),
            message=f"{type(exc).__name__}: {exc}. Ask again once it is put right.",
        )
        log.exception("the chunking comparison stopped")
        return
    if said["state"] == "done":
        log.info(
            "chunking compared, run %s: %s best; %s",
            said.get("run"),
            said.get("best"),
            (said.get("picked") or {}).get("why"),
        )


def _pick_a_cut(
    folder: GoldenFolder,
    results: List[Dict[str, Any]],
    report: Dict[str, Any],
    *,
    drafts: int,
    compared: List[str],
) -> Dict[str, Any]:
    """What a finished run does to the cut in force.

    INPUT
    -----
    Every build the run made, with its outcomes; the run's report; how many
    of its golden questions are drafts; and which collections it compared.

    OUTPUT
    ------
    ::

        {"state": "switched", "key": "recursive-500-n", "was": "markdown-1000-h", "why": "... more than luck"}
        {"state": "kept | pinned | not moved", "why": "..."}

    and, when it switched, ``picks.json`` written and THE CUT APPLIED queued.

    Only with ``INGEST_CHUNKING=auto``: a pinned cut stays whatever a run
    says. Only over checked questions, because a question nobody checked is
    not a measurement, and only when every collection was compared, because
    the one cut is every collection's. The run's best build replaces the one
    in force unless the one in force did as well by luck, the library's
    ``chunking_choice``, because every switch cuts and embeds every document
    again.
    """
    from vectrixdb.evaluation import chunking_choice

    setting = _setting("INGEST_CHUNKING")
    if setting != AUTO:
        return {"state": "pinned", "why": f"INGEST_CHUNKING pins {setting}, so no run moves it"}
    if drafts:
        return {
            "state": "not moved",
            "why": f"{drafts} of the golden questions are drafts nobody checked, so the run moves no cut",
        }
    missing = [name for name in named() if name not in compared]
    if missing:
        return {
            "state": "not moved",
            "why": f"{', '.join(missing)} were not compared, and the cut is every collection's",
        }
    picks = folder.read_picks()
    current = chunking_in_force(AUTO, picks)["key"]
    choice = chunking_choice(results, current)
    if not choice["switch"] or not choice["pick"]:
        return {"state": "kept", "key": current, "why": choice["why"]}
    folder.write_picks(
        with_chunking_pick(
            picks,
            key=choice["pick"],
            run=report.get("id"),
            why=choice["why"],
            questions=int(report.get("questions") or 0),
        )
    )
    log.info("the cut in force is now %s: %s", choice["pick"], choice["why"])
    moved = {"state": "switched", "key": choice["pick"], "was": current, "why": choice["why"]}
    try:
        _queue().send_message(apply_message(choice["pick"], named()))
    except Exception as exc:  # noqa: BLE001 - the pick is made; applying it can be asked for again
        return {
            **moved,
            "apply": f"not queued, {type(exc).__name__}: {exc}. POST /api/v1/chunking/apply asks again.",
        }
    folder.status(
        "asked", into=APPLY_STATUS, key=choice["pick"], collections=named(), run=report.get("id")
    )
    return moved


# ============================================================================
# STEP SIX: CHUNKS
# ============================================================================


class NothingIsCut(Exception):
    """No cut is in force yet, so nothing is indexed and there is nothing to answer from."""


def the_cut(picks: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The cut in force, and why.

    INPUT
    -----
    ``INGEST_CHUNKING``, and ``picks.json`` when it says ``auto``: read here
    when ``picks`` is not given, and not at all when a cut is pinned.

    OUTPUT
    ------
    ::

        {"key": "markdown-1000-h", "by": "auto", "run": "20260922-104012-ab12cd", "why": "...", "at": "..."}
        {"key": "markdown-1000-h", "by": "pinned", "why": "INGEST_CHUNKING names it"}
        {"key": None, "by": "auto", "why": "no chunking run over checked questions has picked a cut yet, ..."}

    and ValueError when ``INGEST_CHUNKING`` names no build.
    """
    setting = _setting("INGEST_CHUNKING")
    if picks is None and setting == AUTO:
        picks = _golden_folder().read_picks()
    return chunking_in_force(setting, picks)


def chunking() -> Dict[str, Any]:
    """How the Markdown is cut: the cut in force, or nothing yet, so the file waits at step three.

    INPUT
    -----
    The cut in force, from :func:`the_cut`.

    OUTPUT
    ------
    What the worker hands ``add_document``. Structure-aware at a thousand
    characters with its headings in front is::

        {"chunk": "markdown", "chunk_size": 1000, "overlap": 200, "embed_heading": True, "parent_size": None, "late": False}

    and with no cut yet ``{"index": False}``: the Markdown is kept and nothing
    is cut. Once a document is cut, its chunks are written, a JSON line
    each::

        ingestion/chunks/financial/td/ar2025.pdf.jsonl
            {"id": "td/ar2025.pdf:0", "text": "Net income rose 12% ...",
             "metadata": {"_vx_doc": "td/ar2025.pdf", "_vx_chunk": 0, "page": 41, "heading": "Results > 2025",
                          "collection": "financial", "_vx_quality": 0.93}}
    """
    key = the_cut()["key"]
    return _cutting(key) if key else {"index": False}


def _cutting(key: str) -> Dict[str, Any]:
    """The options that cut a document the way build ``key`` does, with the model for a cut a model makes."""
    build = chunking_build(key)
    if build["chunk"] != "llm" and build["adds"] != "context":
        return chunking_options(build)
    chat = golden_writer()
    if chat is None:
        raise ConfigurationError(
            f"{key} is cut with a model, and none is named: set AZURE_OPENAI_WRITER_DEPLOYMENT, or pin a cut that needs none"
        )
    from vectrixdb.chunk_models import context_writer, llm_cutter

    return chunking_options(build, cut_with=llm_cutter(chat), context_with=context_writer(chat))


# ----------------------------------------------------------------------------
# STEP SIX, THE CUT APPLIED: every kept document brought to a new cut
# ----------------------------------------------------------------------------

#: How long one message brings documents to a cut before it hands the rest
#: on to the next message: well inside the function's hour.
APPLY_SECONDS = 45 * 60


def apply_the_cut(asked: Dict[str, Any]) -> None:
    """Every kept document of the collections cut the way ``key`` says, from its Markdown, and indexed.

    INPUT
    -----
    The queue message :func:`_pick_a_cut` puts on the queue when step five's
    pick moves, or ``POST /api/v1/chunking/apply`` does after a cut is
    pinned::

        {"vectrixdb": "apply", "key": "markdown-1000-h", "collections": ["financial", "media", "misc"]}

    OUTPUT
    ------
    Each kept document not yet at that cut, through steps six to eight again
    with ``rechunk()``: nothing is fetched or read again, only cut, embedded
    and indexed. Then, in the evals container::

        apply.status.json   {"state": "asked | running | done | failed", "key": "markdown-1000-h",
                             "cut": 12, "last": "td/ar2025.pdf", "failed": [...], "at": "..."}

    and step nine asked for again when there is a golden file, because the
    run it had measured the old cut.

    A document whose recorded chunking is already the cut is left alone, so
    a message the host hands over twice, or one that runs out of time and
    hands the rest on, cuts each document once. One document that will not
    cut leaves the others to be cut, and is named in the status; the message
    is then raised, so the queue tries those again. A model that refuses, or
    a cut that needs a model when none is named, is said and not tried five
    more times.
    """
    from vectrixdb.evaluation import WriterUnavailable

    folder = _golden_folder()
    key = asked["key"]
    names = [name for name in (asked.get("collections") or named()) if name in named()]
    began, cut = time.monotonic(), 0
    failed: List[str] = []
    try:
        options, wanted = _cutting(key), recorded_cut(key)
        for name in names:
            db = collection(name)
            for doc_id in db.documents.ids():
                if (db.documents.entry(doc_id) or {}).get("chunking") == wanted:
                    continue
                if time.monotonic() - began > APPLY_SECONDS:
                    # Handed on rather than cut off: the next message starts where this one stopped.
                    _queue().send_message(apply_message(key, names))
                    folder.status(
                        "running",
                        into=APPLY_STATUS,
                        key=key,
                        cut=cut,
                        failed=failed[:20],
                        message="the rest go on in the next message",
                    )
                    return
                try:
                    db.rechunk(doc_id, **options)
                    cut += 1
                except WriterUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - one document that will not cut leaves the others to be cut
                    failed.append(f"{name}/{doc_id}: {type(exc).__name__}: {exc}")
                folder.status(
                    "running",
                    into=APPLY_STATUS,
                    key=key,
                    collection=name,
                    cut=cut,
                    last=doc_id,
                    failed=failed[:20],
                )
    except (WriterUnavailable, ConfigurationError) as exc:
        folder.status("failed", into=APPLY_STATUS, key=key, cut=cut, message=str(exc))
        log.warning("the cut %s was not applied: %s", key, exc)
        return
    except Exception as exc:
        folder.status(
            "failed",
            into=APPLY_STATUS,
            key=key,
            cut=cut,
            message=f"{type(exc).__name__}: {exc}. The queue tries again from where it stopped.",
        )
        raise
    if failed:
        folder.status(
            "failed",
            into=APPLY_STATUS,
            key=key,
            cut=cut,
            failed=failed[:20],
            message=f"{len(failed)} did not cut. The queue tries them again.",
        )
        raise RuntimeError(f"{len(failed)} documents did not cut the way {key} says: {failed[0]}")
    folder.status("done", into=APPLY_STATUS, key=key, cut=cut, collections=names)
    log.info("every kept document is cut the way %s says; %s were cut now", key, cut)
    if folder.exists(GOLDEN) and not folder.busy(into=EVALUATION_STATUS):
        _queue().send_message(evaluation_message(folder.url(GOLDEN), named()))
        folder.status(
            "asked",
            into=EVALUATION_STATUS,
            golden=folder.url(GOLDEN),
            collections=named(),
            because=f"the cut is now {key}",
        )


# ============================================================================
# STEP SEVEN: EMBEDDING
# ============================================================================


def embedding_models() -> Dict[str, Any]:
    """The models each chunk is embedded with: the collection's own, and Azure OpenAI's when named.

    INPUT
    -----
    ``AZURE_OPENAI_ENDPOINT`` and ``AZURE_OPENAI_EMBED_DEPLOYMENT``, with
    ``AZURE_OPENAI_EMBED_DIMENSIONS`` and ``AZURE_OPENAI_KEY``.

    OUTPUT
    ------
    What the index is built with, for :func:`search_index`::

        {"embeddings": "both",
         "azure_embedding": {"endpoint": "https://<resource>.openai.azure.com/", "deployment": "text-embedding-3-small",
                             "dimensions": 1536, "api_key": "..." or None}}

    or ``{"embeddings": "vectrixdb", "azure_embedding": None}``, the built-in
    vector alone.

    The collection's own is the library's default, bge-small-en-v1.5, which
    ships inside the wheel and reads English. Naming an Azure OpenAI
    embedding deployment adds a second vector: the index then holds our
    vector and theirs, fused by the service under one filter, and step nine
    can compare all three ways of asking. Both are made at once, so step ten
    can follow any of them without anything being embedded again.
    """
    if not (
        os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT")
    ):
        return {"embeddings": "vectrixdb", "azure_embedding": None}
    return {
        "embeddings": "both",
        "azure_embedding": {
            "endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
            "deployment": os.environ["AZURE_OPENAI_EMBED_DEPLOYMENT"],
            "dimensions": int(os.environ.get("AZURE_OPENAI_EMBED_DIMENSIONS", "1536")),
            "api_key": os.environ.get("AZURE_OPENAI_KEY"),
        },
    }


# ============================================================================
# STEP EIGHT: INDEXING
# ============================================================================


def search_index(name: str, models: Dict[str, Any]) -> Dict[str, Any]:
    """Azure AI Search built with those models, plus the local copy, where parents live and the chunks the pages count.

    INPUT
    -----
    The collection's name and step seven's models.

    OUTPUT
    ------
    As ``Vectrix`` options::

        {"storage_backend": <Azure AI Search, the index "financial">,
         "path": "/tmp/vectrixdb/financial", "mode": "hybrid",
         "parent_store": "cosmos://<account>.documents.azure.com/data_db/parent_sections",
         "chunk_store": "cosmos://<account>.documents.azure.com/data_db/chunk_records"}

    and, once written, each chunk in three places: the index, which searches
    it; its parent section in ``data_db/parent_sections``, which a search
    returns; and its record in ``data_db/chunk_records``, which the
    collection pages count::

        {"id": "<sha256 of the chunk's id>", "collection": "financial", "point": "td/ar2025.pdf:0",
         "doc": "td/ar2025.pdf", "build": "build_1f2e...", "quality": 0.93,
         "written": "2026-09-22T10:20:00+00:00", "text": "...", "metadata": {...}}

    One index a collection, named after it, and the API reads it too: the
    API server builds the same thing from the same settings, so the two are
    two readers of one index rather than two databases. That is what
    ``VECTRIXDB_STORAGE_BACKEND=azure_search`` is for. The collection pages
    count chunks, builds, days and scores, which the index does not answer,
    and the table beside this process holds only what this instance wrote:
    ``VECTRIXDB_CHUNK_STORE`` is the copy every instance writes.
    """
    return {
        "storage_backend": VectrixDB.with_azure_search(
            os.environ["AZURE_SEARCH_ENDPOINT"],
            key=os.environ.get("AZURE_SEARCH_KEY"),
            # No prefix: the index is the collection, financial, media, misc.
            index_prefix="",
            semantic=os.environ.get("AZURE_SEARCH_SEMANTIC", "true").lower() != "false",
            **models,
        ),
        # The function's own disk. The index of record is the service; this
        # is the copy beside the process, which every write reaches.
        "path": _scratch(name),
        "mode": "hybrid",
        # Parents are read by whichever instance answers the question, which
        # is not the one that wrote them. Given nothing, they fall back to a
        # file on this instance and the next one sees none.
        "parent_store": os.environ.get("VECTRIXDB_PARENT_STORE") or None,
        # The same for what the collection pages count: every chunk this
        # instance writes, deletes or changes reaches the shared copy, and
        # the dashboard, wherever it runs, reads that.
        "chunk_store": os.environ.get("VECTRIXDB_CHUNK_STORE") or None,
    }


# ============================================================================
# STEP NINE: RETRIEVAL COMPARED
# ============================================================================


def picking_rules() -> Dict[str, Any]:
    """How the three are named from a run: the library's rules, in points of the questions found.

    OUTPUT
    ------
    ``{"balance": 4, "time_points": 8}``::

        finds_the_most     the most questions with the right answer in the top 10
        best_for_balance   the fastest within 4 points of that
        best_for_time      the fastest within 8 points of it

    Within is inclusive: at 4 points, 92% counts when the most is 96%. A tie
    goes to more right answers first, then to the faster, and all three can
    be the same setup. Step ten follows the one ``RETRIEVAL_SETUP`` names.
    """
    return {"balance": 4, "time_points": 8}


def evaluate_collections(asked: Dict[str, Any]) -> None:
    """Ask each collection its own golden questions every way it can be searched, and name three for each.

    INPUT
    -----
    The queue message ``POST /api/v1/evaluations/run`` puts on the queue, or
    that THE CUT APPLIED puts on it once every document is at a new cut::

        {"vectrixdb": "evaluate", "golden": "https://<account>.blob.core.windows.net/evals/golden_dataset/golden.jsonl",
         "collections": ["financial", "media", "misc"]}

    OUTPUT
    ------
    In the evals container::

        retrieval/runs/<id>/report.json   one run a collection, which the Evaluate page's Retrieval tab draws:
            {"id": "20260922-111500-ab12cd", "targets": [...], "golden": {"sha256": ..., "drafts": 0, ...},
             "setups": [{"key": "financial.hybrid_semantic.bge-small-en-v1-5+text-embedding-3-small",
                         "method": "hybrid_semantic", "search": {"mode": "hybrid", "rerank": "semantic", "vectors": "both"},
                         "summary": {...}}, ...],
             "picks": {"finds_the_most": "<setup key>", "best_for_balance": "<setup key>", "best_for_time": "<setup key>"}}
        evaluation.status.json            {"state": "asked | running | done | refused | failed",
                                           "runs": [{"collection": "financial", "run": ..., "picks": {...}, "step_ten": {...}}]}
        picks.json, when step ten follows {"retrieval": {"financial": {"run": ..., "questions": 100, "at": ...,
                                           "picks": {"best_for_balance": {"key": ..., "search": {...}}, ...}}}}

    It runs in the queue trigger, as step five does: a hundred questions
    asked every way is hundreds of timed searches a collection, and a
    request ends at 230 seconds. ``GET /api/v1/evaluations/run/status``
    reads the status.

    Every way means every setup the index can do, the library's
    ``setups_of``: with the built-in vector alone, keyword and dense and
    hybrid, hybrid reranked, and each with Azure's semantic ranker where the
    index has one, six in all; a second vector makes it fourteen. Each is
    timed as a caller would see it, the setups are ranked, and three are
    named by :func:`picking_rules`.

    The golden dataset is one file for every collection, so each collection
    is asked only the questions whose documents it holds. The others can
    never be found there, and would lower every setup alike and make the
    numbers mean less. How many were left out is said. What each run does to
    how step ten searches is :func:`_pick_a_way`'s.
    """
    import warnings

    from vectrixdb.evaluation import (
        Golden,
        MissingDocumentsWarning,
        Target,
        check_golden,
        evaluate,
        missing_documents,
    )

    folder = _golden_folder()
    where = asked.get("golden") or os.environ.get("EVAL_GOLDEN_URL") or folder.url(GOLDEN)
    names = [name for name in (asked.get("collections") or named()) if name in named()]
    runs: List[Dict[str, Any]] = []
    try:
        check = check_golden(where, fetcher=BlobFetcher(_blobs()))
        if not check.ok:
            folder.status(
                "refused",
                into=EVALUATION_STATUS,
                golden=where,
                message=f"{check.summary().splitlines()[0]}. Nothing was run.",
                problems=[str(p) for p in check.errors],
            )
            return
        gold = check.golden
        assert gold is not None, "a check that is ok has read the file"
        for name in names:
            db = collection(name)
            target = Target(db, name="Azure AI Search", collection=name)
            # Each collection its own questions: those whose documents it holds.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MissingDocumentsWarning)
                gone = missing_documents([target], gold.labelled).get("Azure AI Search") or {}
            elsewhere = {str(q) for q in gone.get("questions") or []}
            own = [q for q in gold.labelled if str(q.id) not in elsewhere]
            if not own:
                runs.append(
                    {
                        "collection": name,
                        "questions": 0,
                        "left_out": len(elsewhere),
                        "note": "no golden question names a document it holds",
                    }
                )
                continue

            def progress(setup: Dict[str, Any], number: int, of: int, name: str = name) -> None:
                folder.status(
                    "running",
                    into=EVALUATION_STATUS,
                    golden=where,
                    collection=name,
                    setup=number,
                    of=of,
                    done=runs,
                )

            report = evaluate(
                [target],
                Golden(
                    questions=own,
                    source=gold.source,
                    sha256=gold.sha256,
                    unfilled=gold.unfilled,
                    drafts=gold.drafts,
                    raw=gold.raw,
                ),
                save_to=os.environ.get("VECTRIXDB_EVALUATIONS") or None,
                fetcher=BlobFetcher(_blobs()),
                progress=progress,
                **picking_rules(),
            )
            by_key = {s["key"]: s for s in report["setups"]}
            runs.append(
                {
                    "collection": name,
                    "run": report["id"],
                    "questions": len(own),
                    "left_out": len(elsewhere),
                    "setups": len(report["setups"]),
                    "picks": {
                        p: _said(by_key[k])
                        for p, k in (report.get("picks") or {}).items()
                        if k in by_key
                    },
                    "step_ten": _pick_a_way(
                        folder, name, report, questions=len(own), drafts=gold.drafts
                    ),
                }
            )
            log.info(
                "evaluated %s setups over %s questions on %s, %s left out",
                len(report["setups"]),
                len(own),
                name,
                len(elsewhere),
            )
    except Exception as exc:
        # Said, and not raised: a run tried again would run every collection
        # again, and one that failed for a reason is asked for again once it
        # is put right.
        folder.status(
            "failed",
            into=EVALUATION_STATUS,
            golden=where,
            done=runs,
            message=f"{type(exc).__name__}: {exc}. Ask again once it is put right.",
        )
        log.exception("the evaluation stopped")
        return
    folder.status("done", into=EVALUATION_STATUS, golden=where, runs=runs)


def _pick_a_way(
    folder: GoldenFolder, name: str, report: Dict[str, Any], *, questions: int, drafts: int
) -> Dict[str, Any]:
    """What a finished run does to how step ten searches one collection.

    INPUT
    -----
    The collection's run, how many questions it was asked, and how many of
    the golden questions are drafts.

    OUTPUT
    ------
    ::

        {"state": "followed", "role": "best_for_balance", "why": "step ten searches financial the way this run names best_for_balance"}
        {"state": "pinned | not moved", "why": "..."}

    and, when followed, ``picks.json`` written with the collection's three.

    Followed when ``RETRIEVAL_SETUP`` is ``auto`` or one of the three names,
    and no question is a draft. Every collection is its own run and its own
    pick, so each is written as it finishes, into the picks as they are just
    then, which keeps a cut step five picked meanwhile.
    """
    setting = _setting("RETRIEVAL_SETUP")
    if setting not in (AUTO, *ROLES):
        return {"state": "pinned", "why": f"RETRIEVAL_SETUP pins {setting}, so no run moves it"}
    if drafts:
        return {
            "state": "not moved",
            "why": f"{drafts} of the golden questions are drafts nobody checked, so the run moves no pick",
        }
    folder.write_picks(with_retrieval_picks(folder.read_picks(), name, report, questions))
    role = AUTO_ROLE if setting == AUTO else setting
    return {
        "state": "followed",
        "role": role,
        "why": f"step ten searches {name} the way this run names {role}",
    }


# ============================================================================
# STEP TEN: RETRIEVAL
# ============================================================================


def retrieval_rules() -> Dict[str, Any]:
    """How much a question is handed: ``{"budget": 6000}``, the characters step five measured every cut with."""
    return {"budget": chunking_rules()["budget"]}


def the_way(name: str, picks: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """How step ten searches one collection, and why.

    INPUT
    -----
    ``RETRIEVAL_SETUP``, and ``picks.json`` when it follows the runs: read
    here when ``picks`` is not given, and not at all when a way is pinned.

    OUTPUT
    ------
    ::

        {"search": {"mode": "hybrid", "rerank": "semantic", "vectors": "both"}, "setup": "<setup key>",
         "by": "auto", "role": "best_for_balance", "run": "20260922-111500-ab12cd", "at": "..."}
        {"search": {...}, "setup": "hybrid_semantic.both", "by": "pinned", "why": "RETRIEVAL_SETUP names it"}
        {"search": None, "setup": None, "by": "auto", "role": "best_for_balance", "why": "no retrieval run ... yet"}

    ``search`` None is the way step five searched every cut, hybrid with no
    reranker. ValueError when ``RETRIEVAL_SETUP`` names no way of searching.
    """
    setting = _setting("RETRIEVAL_SETUP")
    if picks is None and setting in (AUTO, *ROLES):
        picks = _golden_folder().read_picks()
    return retrieval_in_force(setting, picks, name)


def retrieve(
    name: str, question: str, principal: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """A question of one collection, searched the way in force, and answered from what was found.

    INPUT
    -----
    What ``POST /api/v1/collections/<name>/search/answer`` was sent, and who
    asked, for the collection that carries a policy::

        {"question": "How much did net income rise in 2025?"}

    with the cut and the way of searching in force, :func:`the_cut` and
    :func:`the_way`.

    OUTPUT
    ------
    ::

        {"question": "How much did net income rise in 2025?",
         "answer": "By 12%, to $4.1 billion.",                 None when no model is named
         "sources": [{"id": "td/ar2025.pdf:57", "doc": "td/ar2025.pdf", "page": 41,
                      "citation": "td/ar2025.pdf#page=41", "cited_as": "td/ar2025.pdf, p. 41",
                      "relevance": 0.83, "text": "Net income rose 12% ..."}, ...],
         "searched": {"cut": {"key": "markdown-1000-h", "by": "auto", "run": ...},
                      "way": {"setup": "<setup key>", "by": "auto", "role": "best_for_balance", "run": ...,
                              "search": {"mode": "hybrid", "rerank": "semantic", "vectors": "both"}}}}

    NothingIsCut when no cut is in force, PolicyError when a collection whose
    record carries an entitlement is asked by nobody in particular, and
    ConfigurationError when a setting names no pick.

    What is handed to the model is the library's ``handed_over``, the same
    function step five measured every cut with: the best results until
    :func:`retrieval_rules` says the characters are used, a Parent-child
    cut's sections rather than its chunks, and the last cut to fit. So what
    is served is what was measured. The model of step four answers from
    those alone.
    """
    from vectrixdb.evaluation import WriterUnavailable, answer_with

    try:
        cut, way = the_cut(), the_way(name)
    except ValueError as exc:
        raise ConfigurationError(
            f"INGEST_CHUNKING or RETRIEVAL_SETUP names no pick: {exc}"
        ) from exc
    if cut["key"] is None:
        raise NothingIsCut(cut["why"])
    db: Any = collection(name)
    if db.policy is not None:
        if principal is None:
            raise PolicyError(
                f"collection {name!r} carries an entitlement policy, so a question of it needs somebody who signed in"
            )
        db = db.as_principal(principal)
    handed = handed_over(
        db, question, cut["key"], budget=retrieval_rules()["budget"], search=way.get("search")
    )
    answer: Optional[str] = None
    why: Optional[str] = None
    chat = golden_writer()
    if chat is None:
        why = "no model is named to answer with: AZURE_OPENAI_WRITER_DEPLOYMENT"
    elif handed:
        try:
            answer = answer_with(chat)(question, [text for _, text in handed]) or None
        except WriterUnavailable as exc:
            why = str(exc)
    return {
        "question": question,
        "answer": answer,
        **({"no_answer": why} if why else {}),
        "sources": [_source(hit, text) for hit, text in handed],
        "searched": {
            "cut": {k: cut.get(k) for k in ("key", "by", "run")},
            "way": {
                k: way.get(k)
                for k in ("setup", "by", "role", "run", "search", "why")
                if way.get(k) is not None
            },
        },
    }


def _source(hit: Any, text: str) -> Dict[str, Any]:
    """One source of an answer as a caller shows it: where it is, how well it matched, and the words handed over."""
    metadata = hit.metadata or {}
    return {
        "id": hit.id,
        "doc": metadata.get("_vx_doc"),
        "page": metadata.get("page"),
        "citation": hit.citation,
        "cited_as": hit.readable_citation,
        "relevance": None if hit.relevance is None else round(float(hit.relevance), 3),
        "text": text,
    }


# ============================================================================
# THE API
# ============================================================================


def _api() -> Any:
    """The library's API, configured for this deployment, or one that explains.

    This app hosts :func:`vectrixdb.api.server.create_app`, which is about
    sixty routes, so the surface is the library's and stays the library's: a
    route added there is live here the next time this is published, and
    there is no second implementation to drift.

    WHAT IS PUBLISHED
    -----------------

        GET  https://<your app>.azurewebsites.net/health          is it up
        GET  https://<your app>.azurewebsites.net/health/wiring   what it was given
        GET  https://<your app>.azurewebsites.net/api/v1/info     did it reach Azure
        ...  https://<your app>.azurewebsites.net/api/v1/...
        GET  https://<your app>.azurewebsites.net/                the dashboard

    Try those three in that order. ``/health`` is the library's, takes no key and
    no arguments, and the useful thing about it is not the 200: a reply of any
    kind proves the Python worker indexed this file. A 404 there means the app
    loaded no functions at all, which is what one function failing to load looks
    like, and it is the failure this deployment actually hit. Point an
    availability test at it.

    ``/health/wiring`` is ours and says what this deployment was given: the
    collections, which one carries a policy, which of them this instance has
    already opened, who reads which file type, the two picks' settings, and a
    yes or no for each service. It opens nothing and calls nothing, so a
    Search outage is never reported there as an app outage. It is not called
    ``/health`` because the library already serves that, and a route
    registered second does not win.

    Everything under ``/api/v1`` is the library's but for the few of ours in
    :func:`_ours`. The groups, in the order you are likely to want them:

        search       search, hybrid-search, keyword-search, sparse-search,
                     dense-sparse-search, text-search, text-hybrid-search,
                     search/rerank, search/facets, search/acl, search/enterprise,
                     and search/answer, which is ours: step ten
        documents    documents, documents/{id}, documents/{id}/chunks, points,
                     provenance/{point_id}
        collections  the list, one of them, and its health, quality, policy,
                     policies, builds, growth, graph, rebuild
        evaluations  evaluations, evaluations/{run}, evaluations/{run}/golden,
                     chunking, chunking/{run}, chunking/{run}/golden, and
                     golden/write, chunking/run, chunking/apply,
                     evaluations/run and picks, which are ours: see below
        who did what audit, access, access/daily, access/readers
        keys         keys, keys/{id}
        about        info, info/extended, models, extractors, resources,
                     cache/stats

    ``GET /api/v1/info`` is the one that matters most when something is wrong.
    It answers from the database, so ``"storage_backend": "azure_search"`` in its
    reply is the proof that the app reached the service rather than opening an
    empty file beside itself, which is what it does if
    ``VECTRIXDB_STORAGE_BACKEND`` is not set.

    HOW A CALLER GETS IN
    --------------------

    Not with a Functions key. ``auth_level`` is ANONYMOUS on purpose, because
    Azure's ``?code=`` is one shared secret with no scope and no expiry, and the
    library already has better: scoped keys that expire, Bearer tokens, an
    identity provider, one refusal shape, a shared rate limit, and
    ``/api/v1/keys`` to issue and revoke. The door is the library's. Azure is
    only the building.

    One thing follows from that and catches people out. Who may retrieve from
    a collection is its record's to say, and a key is nobody in particular, so
    a bare key is refused, 403, unless it is the server's own or one made for
    that collection. A caller who signed in is served when the record names
    them, by security group from their token or by the address they signed
    in with, on the collection's own list. So a collection whose record names people needs sign-in,
    and the rule is stored on the collection rather than applied by whoever
    opened it, which is why this holds for every caller and not only
    for the ones that came through here.

    WHAT WILL NOT WORK
    ------------------

    ``/api/v1/ws/status`` is a WebSocket and Azure Functions does not serve one.
    Anything wanting live status polls instead.

    And the first call after a quiet spell takes eight or nine seconds. That is
    not this code: a request for a route that does not exist is just as slow just
    as often, because Flex Consumption keeps no always-ready instance and a call
    arriving at a scaled-in app waits for one to start. Do not set a five second
    timeout. To buy it away, give the plan an always-ready instance, which costs
    money by the hour rather than by the call.
    """
    # The API and the worker read one index. Settable, so a host can point
    # the API somewhere else deliberately, but never left to a default that
    # would quietly open an empty SQLite file next to the process.
    os.environ.setdefault("VECTRIXDB_STORAGE_BACKEND", "azure_search")
    # Azure hands the app no setting whose value is empty, and the API reads
    # an absent prefix as "vectrix": it would list vectrix-collections and
    # search vectrix-financial, neither of which step eight writes. Step eight
    # names each index after its collection, so the empty prefix is put back.
    os.environ.setdefault("AZURE_SEARCH_INDEX_PREFIX", "")
    # Somewhere writable. A function app's own file system is read-only
    # apart from /tmp, and the default is a folder beside the process, so
    # without this the API falls over at start-up on a permission error and
    # the reason looks like nothing to do with the API.
    os.environ.setdefault("VECTRIXDB_PATH", _scratch("_api"))
    if role() == "ingest":
        # The ingest app answers health and its own routes, and nothing a query would wait on: no library API, no dashboard.
        return _lean()
    try:
        # A file uploaded to the documents route goes where the same file
        # dropped in raw/ goes. That route reads VECTRIXDB_EXTRACTOR_ROUTES,
        # and without it knows only ten suffixes, so it is given this app's
        # table here rather than through az, where JSON is not safe to pass.
        service = extraction_app_from_environment()
        if service is not None:
            os.environ.setdefault("VECTRIXDB_EXTRACTOR_ROUTES", json.dumps(service.routes))
        from vectrixdb.api.server import create_app

        # The same collection records the steps read, so the API and the
        # steps hold one copy of each collection's rules between them.
        api = create_app(
            enable_dashboard=os.environ.get("VECTRIXDB_DASHBOARD", "yes").lower() != "no",
            collection_store=_collection_store(),
        )
        _ours(api)
        log.info(
            "serving the library's API, dashboard %s", os.environ.get("VECTRIXDB_DASHBOARD", "yes")
        )
        return api
    except Exception as exc:  # noqa: BLE001 - whatever it is, ingestion carries on
        log.exception("the API could not be built, so only ingestion is running")
        return _explaining(f"{type(exc).__name__}: {exc}")


def _lean() -> Any:
    """The ingest app's face: ``/health``, ``/health/wiring`` and this deployment's own routes, and none of the library's sixty.

    Queries are the query app's. An availability test still has ``/health``
    here, and ``/health/wiring`` still says what this instance was given.
    """
    from fastapi import FastAPI

    api = FastAPI(title="VectrixDB ingest app", docs_url=None, redoc_url=None)

    @api.middleware("http")
    async def the_key_is_asked_for(request: Any, call_next: Any) -> Any:
        """The library's key layer is the query app's, not this one's, so the
        routes _ours adds (a collection's delete among them) were open to
        anyone who found the address. The server's own key is asked for here,
        as the library asks for it: in api-key, or as a Bearer token."""
        import hashlib
        import hmac

        from fastapi.responses import JSONResponse
        from vectrixdb.api.signin import _hashed, get_api_key

        key, key_sha = get_api_key(), _hashed("VECTRIXDB_API_KEY_SHA256")
        if request.url.path in ("/health", "/health/wiring") or not (key or key_sha):
            return await call_next(request)
        given = request.headers.get("api-key") or ""
        bearer = request.headers.get("authorization") or ""
        if not given and bearer.lower().startswith("bearer "):
            given = bearer[7:].strip()
        if given and (
            (key and hmac.compare_digest(given.encode(), key.encode()))
            or (
                key_sha and hmac.compare_digest(hashlib.sha256(given.encode()).hexdigest(), key_sha)
            )
        ):
            return await call_next(request)
        return JSONResponse(
            {"ok": False, "message": "API key required", "data": None}, status_code=401
        )

    @api.get("/health", tags=["about"], summary="Is the ingest app up")
    async def health() -> Dict[str, Any]:
        return {"status": "ok", "role": "ingest"}

    _ours(api)
    log.info(
        "serving the ingest app's health and its own routes; the API and the dashboard are the query app's"
    )
    return api


def _ours(api: Any) -> None:
    """The routes that are this deployment's rather than the library's.

    THE JOBS THE LIBRARY DOES NOT START
    -----------------------------------

        POST /api/v1/golden/write?n=100                                      step four
        GET  /api/v1/golden/write
        POST /api/v1/chunking/run?golden=<blob url>&collection=financial     step five
        GET  /api/v1/chunking/run/status
        POST /api/v1/chunking/apply                                          step six: the cut in force, applied
        GET  /api/v1/chunking/apply/status
        POST /api/v1/evaluations/run?golden=<blob url>&collection=financial  step nine

    THE DASHBOARD'S OWN FLOWS

        POST /api/v1/collections/<name>/setup          make a collection: its record and policy first, its index
        POST /api/v1/collections/<name>/files          drop a file, or typed text, in raw/<name>/
        GET  /api/v1/collections/<name>/setup/status   where it is, as the spinner says it
        POST /api/v1/collections/<name>/delete         everything of it, no copy kept
        GET  /api/v1/ingest/state                      whether ingestion is paused because the extraction app is down, and until when
        GET  /api/v1/ingest/failed                     the files that would not read: the poison queue, in words, ten a page
        POST /api/v1/ingest/failed/<id>/retry          back on the ingest queue
        POST /api/v1/ingest/failed/<id>/drop           the file and everything of it gone
        GET  /api/v1/evaluations/run/status

    The library serves runs and their golden files but starts none, because
    starting one is a job and the library is a library. Each of these is
    queued and answers 202 at once, because the work takes longer than a
    request may, and the ``GET`` beside each says how it is going. None is
    ``GET /api/v1/evaluations/run`` or ``GET /api/v1/chunking/apply``: the
    library's ``/api/v1/evaluations/{run}`` and ``/api/v1/chunking/{run}``
    answer those, as a run of that name. The walkthrough calls them for you.

    THE PICKS, AND STEP TEN
    -----------------------

        GET  /api/v1/picks                                the cut and each collection's way of searching in force, and why
        POST /api/v1/collections/{name}/search/answer     step ten: {"question": "..."}

    ``search/answer`` is a search to the sign-in layer, so whoever may search
    a collection may ask it, which its record decides, as it does for every
    search of it. The rest are an admin's.
    """
    from fastapi import Body, HTTPException, Query, Request
    from fastapi.responses import JSONResponse
    from vectrixdb.api.replies import refusal
    from starlette.concurrency import run_in_threadpool

    from vectrixdb.evaluation import check_golden, plan_chunking

    def checked(golden: str) -> Any:
        """The golden file at ``golden`` held to the schema, or the reply that says it could not be read."""
        try:
            return check_golden(golden, fetcher=BlobFetcher(_blobs()))
        except Exception as exc:  # noqa: BLE001 - the address is the caller's, and the reason belongs in the reply
            return refusal(400, f"Could not read {golden}: {exc}")

    def refused(check: Any) -> Any:
        """A golden file with mistakes, refused with every one of them, so it is put right in one go and nothing is queued."""
        return refusal(
            400,
            f"{check.summary().splitlines()[0]}. Nothing was run.",
            data={
                "problems": [str(p) for p in check.errors],
                "notes": [str(p) for p in check.warnings],
            },
        )

    def misconfigured(exc: Exception) -> Any:
        """A pick's setting that names nothing: the server's to put right, not the caller's."""
        return refusal(500, f"INGEST_CHUNKING or RETRIEVAL_SETUP names no pick: {exc}")

    @api.get("/health/wiring", tags=["about"], summary="What this deployment is wired to")
    async def wiring() -> Dict[str, Any]:
        """What this deployment is wired to. Cheap: nothing opened, nothing called.

        Not ``/health``: the library serves that one, and a route registered
        second does not win, so this sat unreachable until somebody looked at
        what actually answered. That is worth knowing before adding any route
        here, because nothing warns you.

        The division of labour, once you have all three: ``/health`` says the
        app is up, this says what it was given, and ``/api/v1/info`` answers
        from the database and so is the only one that proves Azure AI Search
        was reached.
        """
        import platform

        def given(*names: str) -> bool:
            """Set to something, which is all a health check may say about a secret."""
            return all(os.environ.get(name, "").strip() for name in names)

        try:
            from vectrixdb import __version__ as version
        except ImportError:  # pragma: no cover - a wheel that installed has one
            version = "unknown"
        service = extraction_app_from_environment()
        return {
            "status": "ok",
            "role": role(),
            "vectrixdb": version,
            "python": platform.python_version(),
            "collections": named(),
            # Which collections this instance has opened: a cold start shows
            # as an empty list, a warm one does not.
            "warm": sorted(_open),
            "reads": what_reads_what(extractors_from_environment()),
            # Suffix by suffix, the path each file is sent to, when an
            # extraction app reads them: what to compare with its own routes.
            "routes": dict(sorted(service.routes.items())) if service is not None else None,
            # What steps six and ten were told to follow; GET /api/v1/picks says what that comes to.
            "picks": {
                "INGEST_CHUNKING": _setting("INGEST_CHUNKING"),
                "RETRIEVAL_SETUP": _setting("RETRIEVAL_SETUP"),
            },
            "wired": {
                "search": given("AZURE_SEARCH_ENDPOINT"),
                "blobs": given("INGEST_BLOB_ACCOUNT"),
                "queue": given("INGEST_QUEUE"),
                "extraction_app": given("VECTRIXDB_EXTRACTOR_URL", "VECTRIXDB_EXTRACTOR_KEY"),
                "documents": given("AZURE_DOCINTEL_ENDPOINT", "AZURE_DOCINTEL_KEY"),
                "speech": given("AZURE_SPEECH_ENDPOINT", "AZURE_SPEECH_KEY"),
                "pictures": given("AZURE_VISION_ENDPOINT", "AZURE_VISION_KEY"),
                "second_vector": given("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_EMBED_DEPLOYMENT"),
                "golden_writer": given("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_WRITER_DEPLOYMENT"),
                "shared_parents": given("VECTRIXDB_PARENT_STORE"),
                # What the collection pages count, from every instance, and
                # the Markdown the Documents page opens.
                "shared_chunks": given("VECTRIXDB_CHUNK_STORE"),
                "kept_markdown": given("VECTRIXDB_KEEP_SOURCE"),
                # Each collection's record, who may retrieve from it among the rest; sign-in; and each
                # person's groups for a policy that reads them from the store.
                "collection_rules": given("VECTRIXDB_COLLECTION_STORE"),
                "shared_signin": given("VECTRIXDB_SIGNIN_STORE"),
            },
        }

    @api.post(
        "/api/v1/golden/write",
        tags=["evaluations"],
        summary="Draft the golden dataset with a model",
    )
    async def ask_for_the_golden_dataset(
        n: int = Query(
            0,
            ge=0,
            le=MOST,
            description="how many questions; the hundred of step four when left out",
        ),
    ) -> Any:
        """Queue step four: questions drafted from every collection's kept Markdown into evals/golden_dataset/golden.jsonl.

        Queued, not written here, because it takes ten to twenty minutes and a
        request ends at 230 seconds. The answer is 202 with where the file
        will be, and ``GET`` on the same path says how it is going. Refused
        before anything is queued when no model is named, when a golden file
        is already there, and while another writing is under way.
        """
        folder = _golden_folder()
        if golden_writer() is None:
            return refusal(400, NO_WRITER)
        if folder.exists(GOLDEN):
            return refusal(409, THERE_ALREADY)
        if folder.busy():
            return refusal(
                409,
                "A golden dataset is being written already.",
                data={"status": folder.read_status()},
            )
        wanted = n or golden_questions()["n"]
        _queue().send_message(golden_message(wanted))
        folder.status("asked", wanted=wanted)
        log.info("the golden dataset was asked for: %s questions", wanted)
        return JSONResponse(
            {
                "message": f"Writing {wanted} questions into {folder.url(GOLDEN)}. It takes ten to twenty minutes.",
                "status": "GET /api/v1/golden/write",
            },
            202,
        )

    @api.get(
        "/api/v1/golden/write",
        tags=["evaluations"],
        summary="How the golden dataset's writing is going",
    )
    async def golden_dataset_status() -> Any:
        """What golden.status.json says: asked, writing so many of so many, done, refused or failed, and why."""
        return _golden_folder().read_status() or {
            "state": "not asked",
            "message": "POST /api/v1/golden/write asks for it.",
        }

    @api.post(
        "/api/v1/chunking/run",
        tags=["evaluations"],
        summary="Compare the ways of cutting the documents",
    )
    async def run_chunking(
        golden: str = Query(
            "",
            description="the blob address of the golden questions; evals/golden_dataset/golden.jsonl when left out",
        ),
        collection_name: str = Query(
            "",
            alias="collection",
            description="which collection's documents to cut; every one when left out",
        ),
    ) -> Any:
        """Queue step five: the kept Markdown cut every way, each handing the model the same characters, and one picked.

        Queued a build at a time, because forty builds take longer than a
        request may and longer than one message may. The answer is 202 at
        once, and ``GET /api/v1/chunking/run/status`` says how it is going.
        The file is checked first, so one with mistakes is refused with all
        of them and nothing is queued. With ``INGEST_CHUNKING=auto``, a run
        over checked questions and every collection moves the cut in force.
        """
        folder = _golden_folder()
        where = golden or os.environ.get("EVAL_GOLDEN_URL", "") or folder.url(GOLDEN)
        if collection_name and collection_name not in named():
            return refusal(400, f"{collection_name!r} is not one of {', '.join(named())}.")
        names = [collection_name] if collection_name else named()
        check = checked(where)
        if isinstance(check, JSONResponse):
            return check
        if not check.ok:
            return refused(check)
        if folder.busy(into=CHUNKING_STATUS):
            return refusal(
                409,
                "A chunking comparison is running already.",
                data={"status": folder.read_status(CHUNKING_STATUS)},
            )
        plan, skipped, _, _ = plan_chunking(chat=golden_writer())
        builds, budget, job = len(plan), chunking_rules()["budget"], chunking_job()
        _queue().send_message(chunking_message(where, names, job))
        folder.status(
            "asked", into=CHUNKING_STATUS, golden=where, collections=names, job=job, of=builds
        )
        judged = (
            "each answer written and judged by step four's model"
            if golden_writer() is not None
            else "no model is named, so it counts what was found in them"
        )
        log.info(
            "a chunking comparison was asked for: %s builds over %s questions, %s",
            builds,
            check.ready,
            ", ".join(names),
        )
        return JSONResponse(
            {
                "message": f"Cutting the documents of {', '.join(names)} {builds} ways, each handing the model {budget:,} characters for each of {check.ready} questions, {judged}. One build a message, so it takes a while.",
                "status": "GET /api/v1/chunking/run/status",
                "left_out": sorted(skipped.values()),
                "notes": [str(p) for p in check.warnings],
            },
            202,
        )

    @api.get(
        "/api/v1/chunking/run/status",
        tags=["evaluations"],
        summary="How the chunking comparison is going",
    )
    async def chunking_status() -> Any:
        """What chunking.status.json says: asked, running build so many of so many, done with what it did to the cut, refused or failed."""
        return _golden_folder().read_status(CHUNKING_STATUS) or {
            "state": "not asked",
            "message": "POST /api/v1/chunking/run asks for it.",
        }

    @api.post(
        "/api/v1/chunking/apply",
        tags=["evaluations"],
        summary="Cut every kept document the way in force",
    )
    async def apply_cut() -> Any:
        """Queue THE CUT APPLIED for the cut in force: after a new one is pinned with INGEST_CHUNKING, say.

        Step five queues it on its own when its pick moves. The answer is 202
        at once, and ``GET /api/v1/chunking/apply/status`` says how it is
        going. Refused while there is no cut in force, and while one is
        being applied.
        """
        folder = _golden_folder()
        try:
            cut = the_cut()
        except ValueError as exc:
            return misconfigured(exc)
        if cut["key"] is None:
            return refusal(409, cut["why"])
        if folder.busy(into=APPLY_STATUS):
            return refusal(
                409,
                "A cut is being applied already.",
                data={"status": folder.read_status(APPLY_STATUS)},
            )
        _queue().send_message(apply_message(cut["key"], named()))
        folder.status("asked", into=APPLY_STATUS, key=cut["key"], collections=named())
        log.info("the cut %s was asked to be applied", cut["key"])
        return JSONResponse(
            {
                "message": f"Cutting every kept document of {', '.join(named())} the way {cut['key']} says, from its Markdown, then asking step nine again.",
                "status": "GET /api/v1/chunking/apply/status",
            },
            202,
        )

    @api.get(
        "/api/v1/chunking/apply/status",
        tags=["evaluations"],
        summary="How applying the cut is going",
    )
    async def apply_status() -> Any:
        """What apply.status.json says: asked, running so many documents in, done, or failed and which."""
        return _golden_folder().read_status(APPLY_STATUS) or {
            "state": "not asked",
            "message": "POST /api/v1/chunking/apply asks for it.",
        }

    @api.post(
        "/api/v1/evaluations/run",
        tags=["evaluations"],
        summary="Run the golden questions every way",
    )
    async def run_evaluation(
        golden: str = Query(
            "",
            description="the blob address of the golden questions; evals/golden_dataset/golden.jsonl when left out",
        ),
        collection_name: str = Query(
            "", alias="collection", description="which collection to ask; every one when left out"
        ),
    ) -> Any:
        """Queue step nine: each collection asked its own golden questions every way, and three named.

        Queued, not run here, because the searching takes longer than a
        request may. The answer is 202 at once, and
        ``GET /api/v1/evaluations/run/status`` says how it is going. The file
        is checked first, so one with mistakes is refused with all of them
        and nothing is queued. Step ten follows what a run over checked
        questions names, unless ``RETRIEVAL_SETUP`` pins a way.
        """
        folder = _golden_folder()
        where = golden or os.environ.get("EVAL_GOLDEN_URL", "") or folder.url(GOLDEN)
        if collection_name and collection_name not in named():
            return refusal(400, f"{collection_name!r} is not one of {', '.join(named())}.")
        names = [collection_name] if collection_name else named()
        check = checked(where)
        if isinstance(check, JSONResponse):
            return check
        if not check.ok:
            return refused(check)
        if folder.busy(into=EVALUATION_STATUS):
            return refusal(
                409,
                "An evaluation is running already.",
                data={"status": folder.read_status(EVALUATION_STATUS)},
            )
        _queue().send_message(evaluation_message(where, names))
        folder.status("asked", into=EVALUATION_STATUS, golden=where, collections=names)
        log.info("an evaluation was asked for: %s questions, %s", check.ready, ", ".join(names))
        return JSONResponse(
            {
                "message": f"Asking {check.ready} questions of {', '.join(names)}, every way, each collection its own. It takes some minutes.",
                "status": "GET /api/v1/evaluations/run/status",
                "notes": [str(p) for p in check.warnings],
            },
            202,
        )

    @api.get(
        "/api/v1/evaluations/run/status",
        tags=["evaluations"],
        summary="How the evaluation is going",
    )
    async def evaluation_status() -> Any:
        """What evaluation.status.json says: asked, running a collection's setups, done with each run's picks, refused or failed, and why."""
        return _golden_folder().read_status(EVALUATION_STATUS) or {
            "state": "not asked",
            "message": "POST /api/v1/evaluations/run asks for it.",
        }

    @api.get(
        "/api/v1/picks",
        tags=["evaluations"],
        summary="The cut and the ways of searching in force, and why",
    )
    async def picks_in_force() -> Any:
        """What steps six and ten use, and why: each pick's setting, whether it follows the runs or is pinned, and the run that chose it.

        {"chunking": {"setting": "auto", "key": "markdown-1000-h", "by": "auto", "run": ..., "why": ...},
         "retrieval": {"financial": {"setting": "auto", "setup": ..., "by": "auto", "role": "best_for_balance",
                                     "run": ..., "search": {...}}, ...}}
        """
        picks = _golden_folder().read_picks()
        try:
            return {
                "chunking": {"setting": _setting("INGEST_CHUNKING"), **the_cut(picks)},
                "retrieval": {
                    name: {"setting": _setting("RETRIEVAL_SETUP"), **the_way(name, picks)}
                    for name in named()
                },
            }
        except ValueError as exc:
            return misconfigured(exc)

    @api.post(
        "/api/v1/collections/{name}/search/answer",
        tags=["search"],
        summary="Ask a question, searched the way in force and answered",
    )
    async def search_and_answer(
        name: str,
        request: Request,
        question: str = Body(
            ...,
            embed=True,
            min_length=1,
            max_length=2000,
            description="the question, as a person asks it",
        ),
    ) -> Any:
        """Step ten: the question searched the way in force for this collection, and answered from what was found, with its sources."""
        from vectrixdb.api.signin import principal_of

        if name not in named():
            raise HTTPException(status_code=404, detail=f"Collection '{name}' not found")
        # Settled at sign-in; None for a bare key, which a record carrying an entitlement refuses.
        principal = principal_of(request)
        try:
            return await run_in_threadpool(retrieve, name, question, principal)
        except NothingIsCut as exc:
            return refusal(
                409, f"{exc}. POST /api/v1/chunking/run picks one, or INGEST_CHUNKING pins one."
            )
        except ConfigurationError as exc:
            return refusal(500, str(exc))

    # ------------------------------------------------------------------
    # The dashboard's own flows. Each is an admin's: the library's role table
    # knows no action for these paths, and a path it does not know is an
    # admin's alone.
    # ------------------------------------------------------------------

    from pydantic import BaseModel

    class SetupRequest(BaseModel):
        policy: Optional[Dict[str, Any]] = None

    class TypedText(BaseModel):
        title: Optional[str] = None
        text: str

    def _who(request: Request) -> Optional[str]:
        from vectrixdb.api.signin import caller_of

        caller = caller_of(request)
        return caller.who if caller is not None else None

    def _log(request: Request, event: str, **line: Any) -> None:
        from vectrixdb.api.signin import caller_of, runtime_of

        runtime, caller = runtime_of(request), caller_of(request)
        if runtime is None:
            return
        runtime.access.record(
            event,
            who=caller.who if caller else None,
            role=caller.role if caller else None,
            method=caller.method if caller else None,
            **line,
        )

    @api.post(
        "/api/v1/collections/{name}/setup",
        tags=["collections"],
        summary="Make a collection the way the app makes one: its policy first",
    )
    async def setup_collection(name: str, request: Request, body: SetupRequest) -> Any:
        """A new collection: its record, with who may retrieve from it, then its index. Files come next, through /files."""
        from vectrixdb.collection_access import AccessPolicy
        from vectrixdb.collection_records import CollectionRecord

        records = _collection_store()
        if records is None:
            return refusal(
                500,
                "This app keeps no collection records: set VECTRIXDB_COLLECTION_STORE, then this can make a collection.",
            )
        try:
            name = collection_setup.check_name(name)
            if body.policy is None:
                raise ConfigurationError(
                    f"Who may retrieve from {name} is required. Without it, nobody can."
                )
            policy = AccessPolicy.from_dict(body.policy)
        except ConfigurationError as exc:
            return refusal(400, str(exc))
        if records.get(name) is not None:
            return refusal(409, f"There is a collection called {name} already.")
        record = CollectionRecord(
            name=name,
            generation=collection_setup.now(),
            path=f"raw/{name}/",
            policy=policy.to_dict(),
        )
        records.put(record, by=_who(request))
        _log(request, "policy_changed", collection=name, reason=policy.describe(), by=_who(request))
        # The index, so the collection is there to see before its first file is read.
        made = True
        try:
            await run_in_threadpool(collection, name)
        except Exception as exc:  # noqa: BLE001 - said in the reply; the first file makes it otherwise
            log.warning(
                "%s: the index could not be made now (%s); the first file makes it", name, exc
            )
            made = False
        _log(request, "write", action="collection.create", collection=name)
        return {
            "name": name,
            "path": record.path,
            "policy": record.policy,
            "index": "made" if made else "with the first file",
            "status": f"/api/v1/collections/{name}/setup/status",
        }

    @api.post(
        "/api/v1/collections/{name}/files",
        tags=["collections"],
        summary="Drop a file, or typed text, into raw/<name>/",
    )
    async def add_file(name: str, request: Request) -> Any:
        """One file a request, as its bytes with the name in X-Filename, or JSON {title, text} kept as Markdown. Event Grid does the rest."""
        records = _collection_store()
        if records is None or records.get(name) is None:
            return refusal(
                404,
                f"There is no collection called {name}. Make it first, with who may retrieve from it.",
            )
        try:
            if (request.headers.get("content-type") or "").split(";")[
                0
            ].strip().lower() == "application/json":
                typed = TypedText(**(await request.json()))
                filename = collection_setup.file_name(None, title=typed.title, typed=True)
                data = typed.text.encode("utf-8")
            else:
                filename = collection_setup.file_name(
                    unquote(request.headers.get("x-filename") or "")
                )
                data = await request.body()
            blob = await run_in_threadpool(
                collection_setup.put_file, _blobs(), name, filename, data
            )
        except ConfigurationError as exc:
            return refusal(400, str(exc))
        except (TypeError, ValueError) as exc:
            return refusal(400, f"Typed text is JSON with text, and a title if you like: {exc}")
        _log(request, "write", action="content.write", collection=name, item=filename)
        return {
            "collection": name,
            "file": filename,
            "blob": blob,
            "then": "Event Grid puts a message on the queue, and the function reads it",
        }

    @api.get(
        "/api/v1/collections/{name}/setup/status",
        tags=["collections"],
        summary="Where a new collection is, as the spinner says it",
    )
    async def setup_status(name: str) -> Any:
        """One step and one line, from what is there: the record, the files, their Markdown, the chunks, and the jobs."""
        from vectrixdb.api import chunk_source

        records = _collection_store()
        record = records.get(name) if records is not None else None
        blobs = _blobs()
        files = collection_setup.count_or_zero(lambda: len(collection_setup.files_of(blobs, name)))
        kept = written_to(blobs, name)["keep_source"]
        read = collection_setup.count_or_zero(lambda: len(kept.ids()))
        try:
            masking = collection_setup.masking_of(kept.entries().values())
        except Exception:  # noqa: BLE001 - a store that cannot be asked right now: the stage waits
            masking = None

        def chunks() -> int:
            if name not in _open:
                return 0
            return (
                chunk_source.count(_open[name]._collection)
                if chunk_source.shared(_open[name]._collection) is not None
                else _open[name]._count_all()
            )

        held = collection_setup.count_or_zero(chunks)
        try:
            cut = the_cut()["key"]
        except Exception:  # noqa: BLE001 - no pick yet, or a setting that names none
            cut = None
        folder = _golden_folder()
        jobs = {
            "golden": folder.read_status(),
            "chunking": folder.read_status(CHUNKING_STATUS),
            "apply": folder.read_status(APPLY_STATUS),
            "evaluation": folder.read_status(EVALUATION_STATUS),
        }
        return collection_setup.progress(
            name,
            record=record,
            files=files,
            read=read,
            chunks=held,
            cut=cut,
            jobs=jobs,
            masking=masking,
        )

    def _writer_only(request: Request) -> Any:
        """The files that would not read are the business of whoever may write: a viewer neither retries nor drops."""
        from vectrixdb.api.signin import caller_of

        caller = caller_of(request)
        if caller is not None and not caller.can("content.write"):
            return refusal(403, "Your role does not allow this")
        return None

    @api.get(
        "/api/v1/ingest/state",
        tags=["collections"],
        summary="Whether ingestion is paused because the extraction app is down",
    )
    async def ingest_state(request: Request) -> Any:
        """The breaker's state in words: paused since when, the next try, how many reads in a row could not reach the extraction app."""
        refused = _writer_only(request)
        if refused is not None:
            return refused
        return {
            "ingestion": await run_in_threadpool(_breaker().status),
            "queue": os.environ["INGEST_QUEUE"],
        }

    @api.get(
        "/api/v1/ingest/failed",
        tags=["collections"],
        summary="The files that would not read: the poison queue, in words",
    )
    async def failed_files(request: Request, limit: int = 10, offset: int = 0) -> Any:
        """Each message on the poison queue as one row: the file, what stopped it and why, how many tries, when; newest first, ten a page."""
        refused = _writer_only(request)
        if refused is not None:
            return refused
        rows = await run_in_threadpool(
            lambda: dead_letters.poison_rows(
                _poison_queue().peek_messages(max_messages=32), _blobs()
            )
        )
        rows.sort(key=lambda r: r.get("last_tried") or "", reverse=True)
        limit, offset = max(1, min(int(limit), 32)), max(0, int(offset))
        return {
            "files": rows[offset : offset + limit],
            "total": len(rows),
            "offset": offset,
            "queue": dead_letters.poison_queue_name(os.environ["INGEST_QUEUE"]),
        }

    @api.post(
        "/api/v1/ingest/failed/{message_id}/retry",
        tags=["collections"],
        summary="Put a file that would not read back on the ingest queue",
    )
    async def retry_failed(message_id: str, request: Request) -> Any:
        """The message goes back as it was; the worker starts from the Markdown when there is one. Logged under your name."""
        refused = _writer_only(request)
        if refused is not None:
            return refused
        message = await run_in_threadpool(dead_letters.take_message, _poison_queue(), message_id)
        if message is None:
            return refusal(404, "That file is no longer on the poison queue")
        row = dead_letters.poison_rows([message], _blobs())[0]
        await run_in_threadpool(dead_letters.retry, _poison_queue(), _queue(), message)
        _log(
            request,
            "write",
            action="content.write",
            collection=row["collection"],
            item=row["file"],
            reason="retry",
        )
        return {
            "retried": row["file"],
            "collection": row["collection"],
            "then": "the message is back on the ingest queue; the function reads it again, from the Markdown when there is one",
        }

    @api.post(
        "/api/v1/ingest/failed/{message_id}/drop",
        tags=["collections"],
        summary="Drop a file that would not read, and everything of it",
    )
    async def drop_failed(message_id: str, request: Request) -> Any:
        """The file, its Markdown, its chunks and its record are deleted, and the message with them. Nothing of it stays. Logged under your name."""
        refused = _writer_only(request)
        if refused is not None:
            return refused
        message = await run_in_threadpool(dead_letters.take_message, _poison_queue(), message_id)
        if message is None:
            return refusal(404, "That file is no longer on the poison queue")
        row = dead_letters.poison_rows([message], _blobs())[0]
        gone = await run_in_threadpool(
            dead_letters.drop, _blobs(), _poison_queue(), message, row["collection"], row["file"]
        )
        _log(
            request,
            "write",
            action="content.write",
            collection=row["collection"],
            item=row["file"],
            reason="drop",
        )
        return {"dropped": row["file"], "collection": row["collection"], "gone": gone}

    @api.post(
        "/api/v1/collections/{name}/delete",
        tags=["collections"],
        summary="Delete a collection and everything of it",
    )
    async def delete_everything(name: str, request: Request) -> Any:
        """Its record first, so nothing more lands as its; then every file of it; then the index and its records. No copy is kept."""
        from vectrixdb.api.server import get_db

        records = _collection_store()
        had_record = bool(records is not None and records.delete(name))
        gone = await run_in_threadpool(collection_setup.remove_everything, _blobs(), name)
        with _opening:
            opened = _open.pop(name, None)
            _workers.pop(name, None)
        if opened is not None:
            try:
                opened.close()
            except Exception:  # noqa: BLE001 - closing what is about to go
                pass
        try:
            had_index = bool(await run_in_threadpool(get_db().delete_collection, name))
        except Exception as exc:  # noqa: BLE001 - the API's database may not hold it
            log.warning("%s: the index could not be deleted through the API (%s)", name, exc)
            had_index = False
        from vectrixdb.api.signin import runtime_of

        runtime = runtime_of(request)
        if runtime is not None:
            runtime.forget(name)
        steps = collection_setup.steps_of_delete(gone, had_index, had_record)
        _log(
            request,
            "write",
            action="collection.delete",
            collection=name,
            reason=f"{gone.get('raw', 0)} files, index {'removed' if had_index else 'none'}",
        )
        return {
            "collection": name,
            "deleted": had_record or had_index or any(gone.values()),
            "removed": gone,
            "steps": steps,
        }


def _said(setup: Any) -> str:
    """One pick, in words rather than as a key."""
    found = setup.get("found")
    took = setup.get("seconds") or setup.get("took_ms")
    parts = [str(setup.get("label") or setup.get("key"))]
    if found is not None:
        parts.append(f"finds {found:.0%}" if isinstance(found, float) else f"finds {found}")
    if took is not None:
        parts.append(f"{took:.0f}ms" if setup.get("took_ms") else f"{took:.2f}s")
    return ", ".join(parts)


def _explaining(reason: str) -> Any:
    """A whole ASGI app that says why there is no API, and nothing else.

    Built when ``create_app`` raises. Without it a bad setting would be an
    exception at import, the worker would index no functions at all, and
    ingestion would stop for a reason that has nothing to do with ingestion.
    """

    async def application(scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = json.dumps(
            {"status": "no api", "reason": reason, "ingestion": "still running"}, indent=2
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return application


# ============================================================================
# MAIN SCRIPT
# ============================================================================


def collection(name: str) -> Vectrix:
    """One collection, opened once for the life of the process, with steps two, three, seven and eight.

    Opening loads the embedding model, so it happens on a cold start and not
    per file, and one at a time, because a question and a file can ask for
    it at once. A collection's policy comes from its record in
    ``access/collection_records``, read by the library as it opens: from then
    on every search of it is a decision, written to the audit trail, and a
    principal who is not entitled sees nothing rather than a refusal. The
    hosted API reads the same record, so it enforces the same policy without
    being told, and a policy changed in the record reaches both within thirty
    seconds, with nothing published again.
    """
    with _opening:
        if name not in _open:
            blobs = _blobs()
            reading = file_readers()
            log.info("opening %s, reading with %s", name, what_reads_what(reading["extractors"]))
            _open[name] = Vectrix(
                name,
                **reading,
                **blob_folders(blobs, name),
                **search_index(name, embedding_models()),
                collection_store=_collection_store(),
                on_retrieval=_audit_sink(),
            )
            rules = _open[name].policy
            log.info(
                "%s %s",
                name,
                f"carries the policy {rules.version or rules.fingerprint}"
                if rules is not None
                else "carries no policy",
            )
            log.info("%s keeps its parent sections in %s", name, _open[name].parents_are_at)
        return _open[name]


def worker(name: str, cut: Dict[str, Any]) -> IngestWorker:
    """The worker for one collection: steps two and three for each file, and six to eight when ``cut`` is a cut.

    ``cut`` is step six's, :func:`chunking`, read when the message arrived,
    so a new pick reaches the next file without a restart: the worker is
    made again when the cut changes. The rest comes with the collection.
    """
    made_with = json.dumps({k: v for k, v in cut.items() if not callable(v)}, sort_keys=True)
    held = _workers.get(name)
    if held is None or held[0] != made_with:
        db = collection(name)
        # "record" so the log names the file that failed and the batch goes
        # on; the raise in handle_events is still what sends the message round
        # again.
        held = (
            made_with,
            IngestWorker(
                db,
                BlobFetcher(_blobs()),
                on_error="record",
                # Every chunk carries the collection it is in, and nothing a
                # rule reads: who may retrieve is the record's, on the server.
                metadata_of=lambda event: metadata_for(event.uri),
                # The path inside the collection, not the whole blob address, so
                # an id survives the storage account being made again.
                doc_id_of=doc_id_of,
                # A pace on index writes, chunks a second, for a burst of files; unset, none.
                chunks_per_second=float(os.environ.get("INGEST_CHUNKS_PER_SECOND") or 0) or None,
                **cut,
                # A scan that reads badly is written anyway and says so on the
                # Ingest page, rather than being refused where nobody sees it.
                on_low_quality="warn",
            ),
        )
        _workers[name] = held
    return held[1]


#: The app the Functions host runs. Azure is the building and the library's
#: key layer is the door, so the Functions auth level is deliberately open:
#: see HOW A CALLER GETS IN, in _api.
app = func.AsgiFunctionApp(app=_api(), http_auth_level=func.AuthLevel.ANONYMOUS)


@app.queue_trigger(
    arg_name="message", queue_name="%INGEST_QUEUE%", connection="AzureWebJobsStorage"
)
def ingest_one(message: func.QueueMessage) -> None:
    """Each message on the ingest queue: one of the jobs asked for, or a file that arrived.

        {"vectrixdb": "golden", ...}     step four, write_golden_dataset
        {"vectrixdb": "chunking", ...}   step five, one build of compare_techniques
        {"vectrixdb": "apply", ...}      step six, apply_the_cut
        {"vectrixdb": "evaluate", ...}   step nine, evaluate_collections
        anything else                    a blob event, step one

    The queue is where this app's long work waits, because the queue trigger
    is the only function this file declares: one function that fails to load
    takes every other with it.
    """
    body = message.get_body()
    if role() == "query":
        # The query app never reads a file. Step 06 disables this trigger on it with AzureWebJobs.ingest_one.Disabled;
        # should a message reach it anyway, it goes back for the ingest app rather than being lost or tried here.
        _queue().send_message(
            body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body),
            visibility_timeout=60,
        )
        log.error(
            "message %s reached the query app: set AzureWebJobs.ingest_one.Disabled=true on it; the message is back on the queue",
            message.id,
        )
        return
    asked = golden_request(body)
    if asked is not None:
        write_golden_dataset(asked)
        return
    cutting = chunking_request(body)
    if cutting is not None:
        compare_techniques(cutting)
        return
    applying = apply_request(body)
    if applying is not None:
        apply_the_cut(applying)
        return
    run = evaluation_request(body)
    if run is not None:
        evaluate_collections(run)
        return
    handle_events(message)
