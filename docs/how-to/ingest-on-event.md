# Ingest when a file lands

A file arrives in a bucket, a blob container or a directory, and it should be in the index a moment later, cut up, embedded, stamped with its lineage and recorded. That is one worker, `vectrixdb.worker.IngestWorker`, and the cloud-specific part is a parser for the event and a fetcher for the bytes. The worker is the same on Azure, on AWS and on a laptop, and so is everything it applies: the policy's metadata contract, the quality gate, the audit sink and the build stamping, because they belong to the `Vectrix` it was given.

Who reads each file type, a built-in reader, a function or a service you run, and keeping what was read so an original is only ever read once, are covered in [Extract, keep, index](extract-keep-index.md). The worker uses whatever its collection was opened with.

## The worker

```python
from vectrixdb import Vectrix
from vectrixdb.worker import IngestEvent, IngestWorker, LocalFetcher

db = Vectrix("inbox")
worker = IngestWorker(db, LocalFetcher(), chunk="markdown")

with open("policy.md", "w", encoding="utf-8") as fh:
    fh.write("# Leave\n\nEmployees get twelve weeks of paid parental leave.\n")

first = worker.handle(IngestEvent(kind="created", uri="policy.md"))
again = worker.handle(IngestEvent(kind="created", uri="policy.md"))
first.action, again.action
```

A collection opened with a describer or an image embedder gets the pictures inside every document the worker reads, so a scanned chart arriving in a bucket is described exactly as one added from a file is. The second call is a no-op: `handle()` is idempotent on the document's bytes, which is what makes it safe behind a queue that delivers twice. A changed file replaces its chunks under the same document id, and a `deleted` event removes the document and mints a build, so lineage records that it was there and then was not:

```python
gone = worker.handle(IngestEvent(kind="deleted", uri="policy.md"))
gone.action, gone.chunks
```

The document id is the URI unless `doc_id_of=` says otherwise. `metadata_of=` stamps metadata on every chunk of a document, which is where a policy's fields come from when the object store does not carry them: on a policied collection an event whose metadata does not satisfy the contract is refused, exactly as a direct `add_document()` would be.

## On AWS: S3 to Lambda, or S3 to SQS to Lambda

`events_from_s3` reads an S3 notification whether Lambda received it directly or through an SQS queue, and `S3Fetcher` reads objects through a boto3 client the function already holds. Prefer the queue: it gives you retries and a dead-letter queue, and the worker's idempotency is what makes retries safe.

```python
import boto3

from vectrixdb import Vectrix
from vectrixdb.worker import IngestWorker, S3Fetcher, events_from_s3

db = Vectrix("inbox", path="/mnt/efs/your-index")
worker = IngestWorker(db, S3Fetcher(boto3.client("s3")))


def handler(event, context):
    outcomes = worker.handle_all(events_from_s3(event))
    return [o.action for o in outcomes]
```

Subscribe the bucket's `s3:ObjectCreated:*` and `s3:ObjectRemoved:*` events. The index lives on a mounted volume or a storage backend; a Lambda's own disk is not durable.

**Setting a bucket's notifications replaces all of them.** `put_bucket_notification_configuration` takes the whole configuration, so a script that sends only its own rule deletes every rule somebody else set, silently. Read what is there, add yours, and write the lot back. Make the rest of the stack in dependency order: a separate bucket for the kept Markdown, the queue and its dead letter queue, the role and the function.

**Put a queue in the middle.** S3 straight to Lambda retries twice and then drops the event. S3 to SQS to Lambda retries as many times as you say and then parks the file in a dead letter queue where a person can find it. Give the queue a visibility timeout about six times the function's, so a long OCR is not handed to a second worker while the first is still reading, and have the function report partial batch failures, so one bad file goes round again on its own and not with the nine good ones beside it.

### A burst

A bucket that receives a thousand files sends a thousand events, often more than one for the same object. `handle_all()` acts only on the last event for each object and answers the earlier ones `superseded`, so an object created and then deleted in one batch is never read. It also saves the vector index once, at the end of the batch, instead of after every document: `add()` rewrites the index file each time, which is what makes one write durable and a thousand slow. `db.deferred_saves()` is the same switch for your own loops. Each document is still its own ingestion, with its own record and build.

## On Azure: Event Grid to a Function

`events_from_event_grid` reads Blob Storage's `BlobCreated` and `BlobDeleted` events in either the Event Grid or the CloudEvents schema, and `BlobFetcher` reads blobs through a `BlobServiceClient` built with the function's managed identity.

```python
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from vectrixdb import Vectrix
from vectrixdb.worker import BlobFetcher, IngestWorker, events_from_event_grid

client = BlobServiceClient("https://your-account.blob.core.windows.net", credential=DefaultAzureCredential())
db = Vectrix("inbox", path="/mounts/your-index")
worker = IngestWorker(db, BlobFetcher(client))


def main(event: func.EventGridEvent) -> None:
    worker.handle_all(events_from_event_grid(event.get_json() | {"eventType": event.event_type}))
```

**Put a queue in the middle here too.** Event Grid straight to a function gives the function a few minutes and a handful of retries, and then the event is gone: a two-hundred-page scan or an hour of audio is stopped half way, nothing is written, and nobody is told. Make the subscription's endpoint a Storage Queue instead, and let a queue-triggered function take one message at a time with an hour to read it. A failed read raises, the message comes back, and after five tries the host moves it to `<queue>-poison`, which is then the list of files that would not read; put an alert on its length. Have the function read a message whether the host decoded it or not, and give no events, never an error, for a message that is not a blob event, since raising would send the subscription's handshake to the poison queue.

The same worker runs behind an Azure AI Search index: give it a `Vectrix` opened on the Azure backend and the chunks land in the service, promoted fields and all.

## Outside both: a directory

`LocalWatcher` polls a directory and hands the worker a created event for every new or changed file and a deleted event for every file that is gone. Call `poll()` from a scheduler, a systemd timer or a loop:

```python
import os

from vectrixdb.worker import LocalWatcher

os.makedirs("inbox", exist_ok=True)
with open("inbox/notes.md", "w", encoding="utf-8") as fh:
    fh.write("The offline switch makes no-download a hard rule.\n")

watcher = LocalWatcher("inbox", worker)
[o.action for o in watcher.poll()], [o.action for o in watcher.poll()]
```

## What the worker never does

It never authenticates. Every fetcher is built around a client the host constructed with the host's credentials, and the worker sees bytes, never a key. It never decides what a document is entitled to: that is `metadata_of=` and the policy's contract. And it never parses a document itself: the loaders in `add_document()` do, so a PDF needs `pypdf` on the worker's machine as it would anywhere else.
