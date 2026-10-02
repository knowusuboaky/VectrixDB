"""The Lambda behind the AWS ingestion stack: an S3 event, by way of SQS, becomes
an ingestion.

Reference code. It is deployed by ``aws_ingest_stack.py`` and is not run by
the test suite, because everything in it talks to AWS.

What it shows, in order:

* the collection is opened once per container, not once per event;
* a policy's fields come from the object's key, never from the file;
* scanned PDFs and images go to Textract, everything else is read here;
* the Markdown of what was indexed is kept in a bucket of its own;
* a record that fails is handed back to SQS on its own, so one bad file
  does not send the whole batch round again.
"""

from __future__ import annotations

import json
import os

import boto3

from vectrixdb import (
    DocumentStore,
    IngestWorker,
    S3Fetcher,
    S3Files,
    Vectrix,
    VectrixDB,
    events_from_s3,
)
from vectrixdb.extract.engines import Textract
from vectrixdb.models.bedrock import BedrockEmbedder
from vectrixdb.policy import Overlap, Policy

REGION = os.environ.get("AWS_REGION", "us-east-1")
S3 = boto3.client("s3")

_worker = None


def worker() -> IngestWorker:
    global _worker
    if _worker is None:
        store = VectrixDB.with_opensearch(
            os.environ["OPENSEARCH_ENDPOINT"],
            region=REGION,
            filter_fields={"client_id": "string"},
            knn_engine="faiss",
            embeddings="bedrock",
            embed_fn=BedrockEmbedder(
                boto3.client("bedrock-runtime", region_name=REGION), dimensions=1024
            ),
        )
        textract = Textract(boto3.client("textract", region_name=REGION))
        db = Vectrix(
            os.environ.get("COLLECTION", "docs"),
            storage_backend=store,
            path="/tmp/vectrixdb",
            mode="hybrid",
            policy=Policy([Overlap("client_id", "clients", scope=True)], require_pushdown=True),
            extractors={
                ".pdf": textract,
                ".png": textract,
                ".jpg": textract,
                ".jpeg": textract,
                ".tif": textract,
            },
            # A bucket of its own. Never the one the events come from.
            keep_source=DocumentStore(
                S3Files(S3, os.environ["EXTRACTED_BUCKET"], prefix="markdown")
            ),
        )
        _worker = IngestWorker(
            db,
            S3Fetcher(S3),
            # s3://originals/acme/contracts/msa.pdf -> client "acme"
            metadata_of=lambda event: {"client_id": event.uri.split("/", 4)[3]},
            chunk="markdown",
            chunk_size=900,
            embed_heading=True,
            on_error="record",
        )
    return _worker


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            outcomes = worker().handle_all(events_from_s3(json.loads(record["body"])))
            for outcome in outcomes:
                print(
                    json.dumps(
                        {
                            "action": outcome.action,
                            "uri": outcome.uri,
                            "chunks": outcome.chunks,
                            "error": outcome.error,
                        }
                    )
                )
                if outcome.action == "failed":
                    raise RuntimeError(outcome.error)
        except (
            Exception
        ) as exc:  # the record goes back to the queue, and in time to the dead letter queue
            print(json.dumps({"failed": record.get("messageId"), "why": str(exc)}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
