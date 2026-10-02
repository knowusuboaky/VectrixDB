"""A queue between the storage event and the worker, so a long read is never cut off.

Reference code for an Azure Function App on the Flex Consumption or Premium
plan. It is not run by the test suite, because every line of it talks to
Azure; what decides whether a file is lost, ``ingest_queue.py`` beside it,
is tested there.

Why a queue. Event Grid straight to a function gives the function a few
minutes and a handful of retries over a day, and then the event is gone. A
two-hundred-page scan or an hour of audio takes longer than that to read, so
the function is stopped half way, nothing is written, and nobody is told.
With a queue in the middle:

1. A blob is saved or deleted. The Event Grid subscription's endpoint is a
   **Storage Queue**, not a function, so Event Grid's only job is to write a
   message, which takes milliseconds and does not fail because OCR is slow.
2. :func:`ingest_one` takes one message at a time (``batchSize`` 1 in
   ``host.json``), and has the function's whole timeout, an hour here, for
   one file. The message stays hidden while it is being worked on.
3. A read that fails raises, the message comes back after
   ``visibilityTimeout``, and it is tried again. The worker is idempotent on
   a document's bytes, so a message delivered twice writes once.
4. After ``maxDequeueCount`` tries the host moves the message to
   ``<queue>-poison``. That queue is the list of files that would not read.
   Put an alert on its length; a file never vanishes.

Make the subscription with the queue as its endpoint::

    az eventgrid event-subscription create --name vectrixdb-ingest \\
      --source-resource-id <the storage account's id> \\
      --endpoint-type storagequeue \\
      --endpoint <the storage account's id>/queueServices/default/queues/vectrixdb-ingest \\
      --included-event-types Microsoft.Storage.BlobCreated Microsoft.Storage.BlobDeleted \\
      --subject-begins-with /blobServices/default/containers/reports/

The filter on the container matters: the kept Markdown goes to a different
container, ``INGEST_KEPT_CONTAINER``, and a subscription that covered it too
would hand the library its own output. The worker ignores such an event, but
it is better never sent.

Settings, in the Function App's configuration; the secrets as Key Vault
references, and storage and search reached with the app's managed identity:

``INGEST_QUEUE``            the queue Event Grid writes to
``INGEST_COLLECTION``       the collection's name
``INGEST_BLOB_ACCOUNT``     https://youraccount.blob.core.windows.net
``INGEST_KEPT_CONTAINER``   where the Markdown as read is kept; never the originals' container
``AZURE_SEARCH_ENDPOINT``   the Azure AI Search service
``AZURE_SEARCH_KEY``        optional; left out, the managed identity is used
``EXTRACTION_URL``          optional: an extraction server, see examples/extraction_server
``EXTRACTION_API_KEY``      the key that server asks for
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from ingest_queue import events_in, outcome_line, should_go_round_again
from vectrixdb import Vectrix, VectrixDB
from vectrixdb.documents import BlobFiles
from vectrixdb.exceptions import ExtractionError
from vectrixdb.extract import HttpExtractor
from vectrixdb.worker import BlobFetcher, IngestWorker

app = func.FunctionApp()
log = logging.getLogger("vectrixdb.ingest")
_worker: Optional[IngestWorker] = None


def worker() -> IngestWorker:
    """Opened once for the life of the process: the models load once, not once a file."""
    global _worker
    if _worker is None:
        blobs = BlobServiceClient(
            os.environ["INGEST_BLOB_ACCOUNT"], credential=DefaultAzureCredential()
        )
        reader: Any = None
        if os.environ.get("EXTRACTION_URL"):
            reader = HttpExtractor(
                os.environ["EXTRACTION_URL"],
                {
                    ".pdf": "/ocr/pdf",
                    ".png": "/ocr/image",
                    ".jpg": "/ocr/image",
                    ".wav": "/asr/audio",
                    ".mp3": "/asr/audio",
                },
                body="raw",
                headers={"api-key": os.environ.get("EXTRACTION_API_KEY", "")},
                # The queue gives the function an hour; the reader gets most of it.
                timeout=3000.0,
            )
        store = VectrixDB.with_azure_search(
            os.environ["AZURE_SEARCH_ENDPOINT"], key=os.environ.get("AZURE_SEARCH_KEY")
        )
        db = Vectrix(
            os.environ["INGEST_COLLECTION"],
            storage_backend=store,
            path="/tmp/vectrixdb",
            mode="hybrid",
            extractors=reader,
            keep_source=BlobFiles(blobs, os.environ["INGEST_KEPT_CONTAINER"]),
        )
        # "record" so the log names the file that failed; the raise below is
        # still what sends the message round again.
        _worker = IngestWorker(db, BlobFetcher(blobs), on_error="record")
    return _worker


@app.queue_trigger(
    arg_name="message", queue_name="%INGEST_QUEUE%", connection="AzureWebJobsStorage"
)
def ingest_one(message: func.QueueMessage) -> None:
    events = events_in(message.get_body())
    if not events:
        log.info("nothing to read in message %s", message.id)
        return
    outcomes = worker().handle_all(events)
    for outcome in outcomes:
        log.info("%s (try %s)", outcome_line(outcome), message.dequeue_count)
    failed = [o for o in outcomes if should_go_round_again(o)]
    if failed:
        raise ExtractionError(
            f"{len(failed)} of {len(outcomes)} did not read: {outcome_line(failed[0])}"
        )
