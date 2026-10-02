"""A reference AWS stack for ingest-on-event: create, status, delete.

    python aws_ingest_stack.py plan                       # what would be made, in order; needs no AWS
    python aws_ingest_stack.py create --name acme-docs --opensearch https://....aoss.amazonaws.com
    python aws_ingest_stack.py status --name acme-docs
    python aws_ingest_stack.py delete --name acme-docs    # asks first; --keep-buckets keeps the data

Reference code, not a product. It is not run by the test suite, since every
line of ``create`` talks to AWS; what is tested is the part that can be,
``merged_notification``. Read it, change it, and put the real thing in
whatever you deploy with.

What it makes, in dependency order, and ``delete`` unmakes in reverse:

1. a bucket for originals and a separate bucket for the kept Markdown. They
   are separate because a store inside the watched bucket would send an event
   for every file the ingestion writes;
2. a dead letter queue, then the queue, which retries a failed file a few
   times and then parks it rather than losing it;
3. the bucket's notification to the queue, MERGED into what is already
   there. ``put_bucket_notification_configuration`` replaces the whole
   configuration, so a script that sets only its own rule silently deletes
   everybody else's;
4. a role for the function, with the least it needs;
5. the function, from ``lambda_handler.py``, with the queue as its source and
   partial batch failures switched on.

The OpenSearch collection is yours to make: its encryption, network and data
access policies are decisions this script should not take for you.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List

STEPS = [
    ("originals bucket", "s3://{name}-originals, where files land"),
    ("extracted bucket", "s3://{name}-extracted, the kept Markdown; never the originals bucket"),
    ("dead letter queue", "{name}-ingest-dlq, where a file goes after 5 failed tries"),
    ("queue", "{name}-ingest, visibility timeout 6x the function's, so a slow OCR is not retried mid-run"),
    ("queue policy", "lets the originals bucket, and only it, send to the queue"),
    ("bucket notification", "ObjectCreated and ObjectRemoved to the queue, merged into what is already there"),
    ("function role", "{name}-ingest-role: logs, the queue, both buckets, Textract, Bedrock, OpenSearch"),
    ("function", "{name}-ingest, from lambda_handler.py, 15 minute timeout, 2048 MB"),
    ("event source", "the queue, batches of 10, ReportBatchItemFailures"),
]


def merged_notification(existing: Dict[str, Any], queue_arn: str, rule_id: str, prefix: str = "") -> Dict[str, Any]:
    """The bucket's notification configuration with this queue's rule in it.

    Everything already there is kept: other queues, topics, functions and
    EventBridge. A rule with the same id is replaced, so running ``create``
    twice does not add it twice. The response metadata AWS returns with a
    ``get`` is dropped, because ``put`` refuses it.
    """
    config = {k: copy.deepcopy(v) for k, v in existing.items() if k != "ResponseMetadata"}
    rule: Dict[str, Any] = {
        "Id": rule_id,
        "QueueArn": queue_arn,
        "Events": ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"],
    }
    if prefix:
        rule["Filter"] = {"Key": {"FilterRules": [{"Name": "prefix", "Value": prefix}]}}
    others = [r for r in config.get("QueueConfigurations", []) if r.get("Id") != rule_id]
    config["QueueConfigurations"] = others + [rule]
    return config


def without_rule(existing: Dict[str, Any], rule_id: str) -> Dict[str, Any]:
    config = {k: copy.deepcopy(v) for k, v in existing.items() if k != "ResponseMetadata"}
    config["QueueConfigurations"] = [r for r in config.get("QueueConfigurations", []) if r.get("Id") != rule_id]
    return config


def plan(name: str) -> List[str]:
    return [f"{i}. {title}: {detail.format(name=name)}" for i, (title, detail) in enumerate(STEPS, start=1)]


def _zip_handler() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(Path(__file__).with_name("lambda_handler.py"), "lambda_handler.py")
    return buffer.getvalue()


def _role_policy(name: str, account: str, region: str) -> Dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
             "Resource": f"arn:aws:sqs:{region}:{account}:{name}-ingest"},
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{name}-originals/*"},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
             "Resource": [f"arn:aws:s3:::{name}-extracted", f"arn:aws:s3:::{name}-extracted/*"]},
            {"Effect": "Allow", "Action": ["textract:DetectDocumentText", "textract:StartDocumentTextDetection",
                                           "textract:GetDocumentTextDetection"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": f"arn:aws:bedrock:{region}::foundation-model/*"},
            {"Effect": "Allow", "Action": ["aoss:APIAccessAll"], "Resource": f"arn:aws:aoss:{region}:{account}:collection/*"},
        ],
    }


def create(name: str, region: str, opensearch: str, layer: str) -> None:
    import boto3

    s3, sqs, iam, lam = (boto3.client(s, region_name=region) for s in ("s3", "sqs", "iam", "lambda"))
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]

    for bucket in (f"{name}-originals", f"{name}-extracted"):
        kwargs = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        try:
            s3.create_bucket(Bucket=bucket, **kwargs)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        print(f"bucket {bucket}")

    dlq = sqs.create_queue(QueueName=f"{name}-ingest-dlq", Attributes={"MessageRetentionPeriod": "1209600"})["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    queue = sqs.create_queue(QueueName=f"{name}-ingest", Attributes={
        "VisibilityTimeout": "5400",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}),
    })["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sqs.set_queue_attributes(QueueUrl=queue, Attributes={"Policy": json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "s3.amazonaws.com"}, "Action": "sqs:SendMessage",
                       "Resource": queue_arn,
                       "Condition": {"ArnEquals": {"aws:SourceArn": f"arn:aws:s3:::{name}-originals"},
                                     "StringEquals": {"aws:SourceAccount": account}}}],
    })})
    print(f"queue {queue_arn}, dead letters to {dlq_arn}")

    existing = s3.get_bucket_notification_configuration(Bucket=f"{name}-originals")
    s3.put_bucket_notification_configuration(
        Bucket=f"{name}-originals",
        NotificationConfiguration=merged_notification(existing, queue_arn, f"{name}-ingest"),
    )
    print("bucket notification merged")

    role_name = f"{name}-ingest-role"
    try:
        role_arn = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
        }))["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=role_name, PolicyName="ingest", PolicyDocument=json.dumps(_role_policy(name, account, region)))
    time.sleep(10)  # a new role is not assumable the instant it exists

    settings = {
        "Runtime": "python3.12", "Role": role_arn, "Handler": "lambda_handler.handler", "Timeout": 900, "MemorySize": 2048,
        "Layers": [layer], "EphemeralStorage": {"Size": 2048},
        "Environment": {"Variables": {"OPENSEARCH_ENDPOINT": opensearch, "EXTRACTED_BUCKET": f"{name}-extracted",
                                      "COLLECTION": "docs", "VECTRIXDB_OFFLINE": "1"}},
    }
    try:
        lam.create_function(FunctionName=f"{name}-ingest", Code={"ZipFile": _zip_handler()}, **settings)
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=f"{name}-ingest", ZipFile=_zip_handler())
    if not lam.list_event_source_mappings(FunctionName=f"{name}-ingest", EventSourceArn=queue_arn)["EventSourceMappings"]:
        lam.create_event_source_mapping(FunctionName=f"{name}-ingest", EventSourceArn=queue_arn, BatchSize=10,
                                        FunctionResponseTypes=["ReportBatchItemFailures"])
    print(f"function {name}-ingest, fed by the queue")


def status(name: str, region: str) -> None:
    import boto3

    sqs, lam = boto3.client("sqs", region_name=region), boto3.client("lambda", region_name=region)
    for queue in (f"{name}-ingest", f"{name}-ingest-dlq"):
        try:
            url = sqs.get_queue_url(QueueName=queue)["QueueUrl"]
            waiting = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
            print(f"{queue}: {waiting['Attributes']['ApproximateNumberOfMessages']} waiting")
        except Exception as exc:
            print(f"{queue}: {exc}")
    try:
        print(f"{name}-ingest: {lam.get_function(FunctionName=f'{name}-ingest')['Configuration']['State']}")
    except Exception as exc:
        print(f"{name}-ingest: {exc}")


def delete(name: str, region: str, keep_buckets: bool, force: bool) -> None:
    import boto3

    if not force and input(f"Delete the {name} ingestion stack? Type its name: ") != name:
        print("nothing deleted")
        return
    s3, sqs, iam, lam = (boto3.client(s, region_name=region) for s in ("s3", "sqs", "iam", "lambda"))
    steps = []
    for mapping in lam.list_event_source_mappings(FunctionName=f"{name}-ingest").get("EventSourceMappings", []):
        steps.append(lambda m=mapping: lam.delete_event_source_mapping(UUID=m["UUID"]))
    steps.append(lambda: lam.delete_function(FunctionName=f"{name}-ingest"))
    steps.append(lambda: iam.delete_role_policy(RoleName=f"{name}-ingest-role", PolicyName="ingest"))
    steps.append(lambda: iam.delete_role(RoleName=f"{name}-ingest-role"))
    steps.append(lambda: s3.put_bucket_notification_configuration(
        Bucket=f"{name}-originals",
        NotificationConfiguration=without_rule(s3.get_bucket_notification_configuration(Bucket=f"{name}-originals"), f"{name}-ingest")))
    for queue in (f"{name}-ingest", f"{name}-ingest-dlq"):
        steps.append(lambda q=queue: sqs.delete_queue(QueueUrl=sqs.get_queue_url(QueueName=q)["QueueUrl"]))
    if not keep_buckets:
        for bucket in (f"{name}-originals", f"{name}-extracted"):
            steps.append(lambda b=bucket: boto3.resource("s3", region_name=region).Bucket(b).objects.all().delete())
            steps.append(lambda b=bucket: s3.delete_bucket(Bucket=b))
    for step in steps:  # each on its own: a stack half made must still be removable
        try:
            step()
        except Exception as exc:
            print(f"skipped: {exc}")
    print("deleted" + (", buckets kept" if keep_buckets else ""))


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["plan", "create", "status", "delete"])
    parser.add_argument("--name", default="vectrixdb")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--opensearch", help="the OpenSearch endpoint the function writes to")
    parser.add_argument("--layer", help="ARN of a Lambda layer holding vectrixdb[aws,documents]")
    parser.add_argument("--keep-buckets", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "plan":
        print("\n".join(plan(args.name)))
    elif args.command == "create":
        if not args.opensearch or not args.layer:
            parser.error("create needs --opensearch and --layer")
        create(args.name, args.region, args.opensearch, args.layer)
    elif args.command == "status":
        status(args.name, args.region)
    else:
        delete(args.name, args.region, args.keep_buckets, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
