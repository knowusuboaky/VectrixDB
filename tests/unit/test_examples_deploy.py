"""The reference AWS stack cannot be run here, since every line of ``create``
talks to AWS. What can be tested is: the part that decides what a bucket's
notifications become, which is the part that loses other people's rules when
it is wrong; the plan; and that the handler only uses names the library has.
"""

from __future__ import annotations

import ast
import json
import importlib.util
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "examples" / "deploy"
if not DEPLOY.exists():
    pytest.skip("examples/ is kept on the machine that runs it, not in the repository", allow_module_level=True)



def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stack = load("aws_ingest_stack")
QUEUE = "arn:aws:sqs:us-east-1:111122223333:acme-ingest"


def test_a_rule_is_merged_into_what_is_already_there():
    existing = {
        "ResponseMetadata": {"RequestId": "x"},
        "TopicConfigurations": [{"Id": "audit", "TopicArn": "arn:aws:sns:us-east-1:1:audit", "Events": ["s3:ObjectRemoved:*"]}],
        "QueueConfigurations": [{"Id": "thumbnails", "QueueArn": "arn:aws:sqs:us-east-1:1:thumbs", "Events": ["s3:ObjectCreated:*"]}],
        "EventBridgeConfiguration": {},
    }
    merged = stack.merged_notification(existing, QUEUE, "acme-ingest", prefix="acme/")
    assert "ResponseMetadata" not in merged, "put refuses what get returns"
    assert merged["TopicConfigurations"] == existing["TopicConfigurations"] and merged["EventBridgeConfiguration"] == {}
    assert [r["Id"] for r in merged["QueueConfigurations"]] == ["thumbnails", "acme-ingest"]
    ours = merged["QueueConfigurations"][1]
    assert ours["QueueArn"] == QUEUE and ours["Events"] == ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]
    assert ours["Filter"] == {"Key": {"FilterRules": [{"Name": "prefix", "Value": "acme/"}]}}
    assert existing["QueueConfigurations"] == [{"Id": "thumbnails", "QueueArn": "arn:aws:sqs:us-east-1:1:thumbs", "Events": ["s3:ObjectCreated:*"]}], "and the input is left alone"


def test_running_create_twice_does_not_add_the_rule_twice():
    once = stack.merged_notification({}, QUEUE, "acme-ingest")
    twice = stack.merged_notification(once, QUEUE, "acme-ingest")
    assert len(twice["QueueConfigurations"]) == 1 and "Filter" not in twice["QueueConfigurations"][0]


def test_delete_takes_out_its_own_rule_and_nothing_else():
    merged = stack.merged_notification({"QueueConfigurations": [{"Id": "thumbnails", "QueueArn": "x", "Events": []}]}, QUEUE, "acme-ingest")
    assert [r["Id"] for r in stack.without_rule(merged, "acme-ingest")["QueueConfigurations"]] == ["thumbnails"]


def test_the_plan_needs_no_aws_and_names_the_separate_bucket():
    lines = stack.plan("acme")
    assert len(lines) == len(stack.STEPS) and lines[0].startswith("1. originals bucket: s3://acme-originals")
    assert "never the originals bucket" in lines[1] and "merged into what is already there" in lines[5]
    assert stack.main(["plan", "--name", "acme"]) == 0


def test_the_handler_uses_names_the_library_has():
    import vectrixdb

    tree = ast.parse((DEPLOY / "lambda_handler.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("vectrixdb"):
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name) or alias.name in getattr(vectrixdb, "__all__", []), f"{node.module}.{alias.name}"



def _uses_names_the_library_has(path):
    import vectrixdb

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("vectrixdb"):
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name) or alias.name in getattr(vectrixdb, "__all__", []), f"{node.module}.{alias.name}"
                found.append(alias.name)
    return tree, found


class TestTheEvaluationFunctions:
    """The Function App that runs every setup when the golden file changes,
    and its AWS twin. Neither runs here; what they call is the library's."""

    APP = DEPLOY / "azure_evaluate" / "function_app.py"

    def test_the_function_app_uses_names_the_library_has(self):
        _, found = _uses_names_the_library_has(self.APP)
        assert {"read_golden", "run_setup", "setups_of", "build_report", "report_store"} <= set(found)

    def test_every_step_of_the_flow_is_a_function(self):
        tree = ast.parse(self.APP.read_text(encoding="utf-8"))
        names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list}
        assert names == {"golden_changed", "evaluate_now", "evaluate_golden", "golden_sha", "list_setups", "run_one_setup", "save_report"}
        source = self.APP.read_text(encoding="utf-8")
        assert "create_timer" in source and "task_all" in source, "a debounce, then every setup at once"
        assert 'client.start_new("evaluate_golden"' in source and "get_status(instance)" in source, "one run per golden file"

    def test_the_host_and_the_requirements(self):
        host = json.loads((DEPLOY / "azure_evaluate" / "host.json").read_text(encoding="utf-8"))
        assert host["version"] == "2.0" and "durableTask" in host["extensions"]
        needs = (DEPLOY / "azure_evaluate" / "requirements.txt").read_text(encoding="utf-8").split()
        assert {"azure-functions", "azure-functions-durable", "vectrixdb[azure]"} <= set(needs)
        settings = json.loads((DEPLOY / "azure_evaluate" / "local.settings.example.json").read_text(encoding="utf-8"))["Values"]
        for name in ("EVAL_COLLECTION", "EVAL_GOLDEN_URL", "AZURE_SEARCH_ENDPOINT"):
            assert name in settings
        assert not any("key" in k.lower() and v for k, v in settings.items()), "no secret in an example"

    def test_the_aws_handler_uses_names_the_library_has(self):
        _, found = _uses_names_the_library_has(DEPLOY / "aws_evaluate_handler.py")
        assert {"evaluate", "read_golden", "report_store"} <= set(found)


class TestTheIngestQueue:
    """A queue between the storage event and the worker. The function cannot be
    imported without the Functions runtime; what reads a message, and decides
    whether a file goes round again, is a file of its own and is run here."""

    HERE = DEPLOY / "azure_ingest_queue"
    EVENT = {
        "eventType": "Microsoft.Storage.BlobCreated",
        "subject": "/blobServices/default/containers/reports/blobs/q3.pdf",
        "data": {"url": "https://acct.blob.core.windows.net/reports/q3.pdf", "eTag": "0x8D1", "api": "PutBlob"},
    }

    @classmethod
    def queue(cls):
        spec = importlib.util.spec_from_file_location("ingest_queue_example", cls.HERE / "ingest_queue.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_message_is_read_as_json_as_bytes_and_as_base64(self):
        import base64

        queue = self.queue()
        text = json.dumps(self.EVENT)
        for body in (text, text.encode("utf-8"), base64.b64encode(text.encode("utf-8")).decode("ascii"), self.EVENT, [self.EVENT]):
            events = queue.events_in(body)
            assert [(e.kind, e.uri, e.version) for e in events] == [("created", "https://acct.blob.core.windows.net/reports/q3.pdf", "0x8D1")]

    def test_what_is_not_a_blob_event_is_nothing_and_never_an_error(self):
        """Raising would send it round five times and then to the poison queue."""
        queue = self.queue()
        handshake = {"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": "x"}}
        for body in ("", "not json", "!!!!", "bm90IGpzb24=", json.dumps(handshake), json.dumps(7), None):
            assert queue.events_in(body) == []

    def test_only_a_failed_read_goes_round_again(self):
        from vectrixdb.worker import IngestOutcome

        queue = self.queue()
        again = {a: queue.should_go_round_again(IngestOutcome(a, "d", "u")) for a in ("created", "updated", "unchanged", "deleted", "absent", "failed", "ignored", "superseded")}
        assert [a for a, yes in again.items() if yes] == ["failed"]

    def test_the_log_line_names_the_file_and_why(self):
        from vectrixdb.worker import IngestOutcome

        queue = self.queue()
        assert queue.outcome_line(IngestOutcome("created", "d", "https://a/q3.pdf", chunks=12)) == "created | https://a/q3.pdf | 12 chunks"
        assert queue.outcome_line(IngestOutcome("failed", "d", "https://a/q3.pdf", error="the reader answered 503")).endswith("| the reader answered 503")

    def test_the_function_uses_names_the_library_has_and_takes_one_message_at_a_time(self):
        _, found = _uses_names_the_library_has(self.HERE / "function_app.py")
        assert {"IngestWorker", "BlobFetcher", "BlobFiles", "HttpExtractor", "ExtractionError"} <= set(found)
        _uses_names_the_library_has(self.HERE / "ingest_queue.py")
        source = (self.HERE / "function_app.py").read_text(encoding="utf-8")
        assert "queue_trigger" in source and "raise ExtractionError" in source, "a failed read raises, which is what asks for the retry"
        host = json.loads((self.HERE / "host.json").read_text(encoding="utf-8"))
        queues = host["extensions"]["queues"]
        assert queues["batchSize"] == 1 and queues["maxDequeueCount"] == 5 and host["functionTimeout"] == "01:00:00"
        settings = json.loads((self.HERE / "local.settings.example.json").read_text(encoding="utf-8"))["Values"]
        assert settings["INGEST_KEPT_CONTAINER"] not in settings["INGEST_QUEUE"] and settings["EXTRACTION_API_KEY"] == "", "no secret in an example"
