"""The same run on AWS: golden.jsonl lands in S3 and a Lambda evaluates every setup.

Reference code, not run by the test suite, because everything in it talks to
AWS. The Azure version in ``azure_evaluate/`` spreads the setups over
parallel activities; this one runs them in one invocation, which is enough
while a run fits in Lambda's fifteen minutes. Past that, a Step Functions
Map state over :func:`vectrixdb.evaluation.setups_of` does what Durable
Functions does there, one task per setup.

The bucket's notification sends ObjectCreated for keys ending in
``golden.jsonl`` to this function. The run is saved beside the file under
``retrieval/runs/<id>/``, where the server's Evaluate pages read it when
``VECTRIXDB_EVALUATIONS`` points there. Nothing is switched.

Settings: ``OPENSEARCH_ENDPOINT``, ``COLLECTION``, ``EVAL_REPORTS`` (optional).
"""

from __future__ import annotations

import json
import os
from urllib.parse import unquote_plus

import boto3

from vectrixdb import Vectrix, VectrixDB
from vectrixdb.evaluation import Target, evaluate, read_golden, report_store
from vectrixdb.models.bedrock import BedrockEmbedder

REGION = os.environ.get("AWS_REGION", "us-east-1")
COLLECTION = os.environ.get("COLLECTION", "docs")

_targets = None


def targets() -> list:
    """The indexes to compare, opened once per container. Edit this for your deployment."""
    global _targets
    if _targets is None:
        store = VectrixDB.with_opensearch(
            os.environ["OPENSEARCH_ENDPOINT"],
            region=REGION,
            knn_engine="faiss",
            embeddings="both",
            embed_fn=BedrockEmbedder(boto3.client("bedrock-runtime", region_name=REGION), dimensions=1024),
        )
        handle = Vectrix(COLLECTION, storage_backend=store, path="/tmp/vectrixdb", mode="hybrid")
        _targets = [Target(handle, name="OpenSearch", collection=COLLECTION)]
    return _targets


def handler(event, context):
    done = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        if not key.endswith("golden.jsonl"):
            continue
        url = f"s3://{bucket}/{key}"
        golden = read_golden(url)
        store = report_store(os.environ.get("EVAL_REPORTS") or url.rsplit("/", 1)[0])
        # The same bytes already have a run: a second event for one save starts nothing.
        if any((run.get("golden") or {}).get("sha256") == golden.sha256 for run in store.history(limit=5)):
            print(json.dumps({"golden": url, "skipped": "these bytes already have a run"}))
            continue
        report = evaluate(targets(), golden, save_to=store)
        print(json.dumps({"golden": url, "run": report["id"], "picks": report["picks"], "missing": report["missing"]}))
        done.append(report["id"])
    return {"runs": done}
