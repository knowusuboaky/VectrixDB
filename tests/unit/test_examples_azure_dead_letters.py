"""The files that would not read: the record the worker keeps, the words the page says, and Retry and Drop.

What is being held to. A failed outcome leaves one record a file beside the
poison queue with the error, the tries and the time, and a read clears it.
The error becomes one line a person acts on and a reason under it, and the
raw error stays on the record. A poisoned message joins with its record
into one row; a message with no record still names its file and its tries.
Retry sends the message back as it was and takes it off the poison queue;
Drop takes the file, its Markdown, its chunks, its record and the message,
and counts each. No Azure: the blobs and the queue are stand-ins.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

MAIN_FUNCTION_APP = Path(__file__).resolve().parents[2] / "examples" / "azure" / "main_function_app"
if not MAIN_FUNCTION_APP.exists():
    pytest.skip(
        "examples/ is kept on the machine that runs it, not in the repository",
        allow_module_level=True,
    )

ACCOUNT = "https://acct.blob.core.windows.net"


@pytest.fixture
def dl(monkeypatch):
    monkeypatch.setenv("INGEST_COLLECTIONS", "financial,media")
    monkeypatch.syspath_prepend(str(MAIN_FUNCTION_APP))
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    sys.modules.pop("dead_letters", None)
    import dead_letters

    from test_documents import FakeBlobService

    return types.SimpleNamespace(module=dead_letters, blobs=FakeBlobService())


def event(collection, file):
    return {
        "eventType": "Microsoft.Storage.BlobCreated",
        "subject": f"/blobServices/default/containers/ingestion/blobs/raw/{collection}/{file}",
        "data": {
            "url": f"{ACCOUNT}/ingestion/raw/{collection}/{file}",
            "eTag": "0x1",
            "contentLength": 12,
        },
    }


def message(message_id, collection, file, tries=5):
    return types.SimpleNamespace(
        id=message_id,
        content=json.dumps(event(collection, file)),
        dequeue_count=tries,
        inserted_on=None,
    )


class Poison:
    def __init__(self, messages):
        self.messages = list(messages)
        self.deleted = []

    def receive_messages(self, messages_per_page=32, visibility_timeout=30):
        return list(self.messages[:messages_per_page])

    def delete_message(self, m):
        self.messages = [x for x in self.messages if x.id != m.id]
        self.deleted.append(m.id)


class Ingest:
    def __init__(self):
        self.sent = []

    def send_message(self, content):
        self.sent.append(content)


class TestTheWords:
    @pytest.mark.parametrize(
        "error, file, said",
        [
            ("the extraction app timed out after 3600s", "town-hall.mp4", "Ran out of time"),
            ("no reader for .pptx", "board-pack.pptx", "No reader for .pptx"),
            (
                "Document Intelligence read 0 of 12 pages",
                "scan.pdf",
                "Nothing could be read from it",
            ),
            ("429 Too Many Requests", "a.pdf", "The reader was busy every time"),
            ("HTTPConnectionError: connection refused", "a.pdf", "The reader could not be reached"),
            ("something odd\nwith a second line", "a.pdf", "something odd"),
            ("", "a.pdf", "Reading failed"),
        ],
    )
    def test_each_error_becomes_a_line_a_person_acts_on(self, dl, error, file, said):
        line, why = dl.module.why_in_words(error, file)
        assert line == said and why


class TestTheRecord:
    def test_a_failure_is_recorded_beside_the_queue_and_a_read_clears_it(self, dl):
        outcome = types.SimpleNamespace(
            uri=f"{ACCOUNT}/ingestion/raw/financial/td/scan.pdf",
            action="failed",
            error="no text on any page",
        )
        record = dl.module.record_failure(dl.blobs, outcome, 3, now=86400)
        assert (
            record["collection"] == "financial"
            and record["file"] == "td/scan.pdf"
            and record["tries"] == 3
            and record["last_tried"] == "1970-01-02T00:00:00Z"
        )
        assert (
            record["said"] == "Nothing could be read from it"
            and record["error"] == "no text on any page"
        )
        assert ("ingestion", "failed/financial/td/scan.pdf.json") in dl.blobs.blobs
        assert (
            dl.module.clear_failure(dl.blobs, outcome.uri) is True
            and ("ingestion", "failed/financial/td/scan.pdf.json") not in dl.blobs.blobs
        )
        assert dl.module.clear_failure(dl.blobs, outcome.uri) is False

    def test_a_blob_in_no_collection_keeps_no_record(self, dl):
        assert (
            dl.module.record_failure(
                dl.blobs,
                types.SimpleNamespace(uri=f"{ACCOUNT}/ingestion/raw/nowhere/a.pdf", error="x"),
                1,
            )
            is None
        )
        assert dl.blobs.blobs == {}


class TestTheRows:
    def test_a_message_joins_its_record_and_one_without_still_says_what_the_queue_knows(self, dl):
        dl.module.record_failure(
            dl.blobs,
            types.SimpleNamespace(
                uri=f"{ACCOUNT}/ingestion/raw/financial/a.pdf", error="timed out"
            ),
            5,
            now=0,
        )
        rows = dl.module.poison_rows(
            [message("m1", "financial", "a.pdf"), message("m2", "media", "b.wav", tries=4)],
            dl.blobs,
        )
        assert (
            rows[0]["file"] == "a.pdf"
            and rows[0]["said"] == "Ran out of time"
            and rows[0]["tries"] == 5
            and rows[0]["last_tried"] == "1970-01-01T00:00:00Z"
        )
        assert rows[1] == {
            "id": "m2",
            "file": "b.wav",
            "collection": "media",
            "uri": f"{ACCOUNT}/ingestion/raw/media/b.wav",
            "error": "read failed",
            "said": "read failed",
            "why": "retry, and if it fails again the file needs a look",
            "tries": 4,
            "last_tried": None,
        }

    def test_a_message_that_is_not_a_blob_event_is_still_a_row(self, dl):
        rows = dl.module.poison_rows(
            [types.SimpleNamespace(id="m9", content="not json", dequeue_count=5, inserted_on=None)],
            dl.blobs,
        )
        assert rows[0]["file"] == "?" and rows[0]["collection"] is None


class TestRetryAndDrop:
    def test_retry_sends_the_message_back_as_it_was(self, dl):
        poison, ingest = Poison([message("m1", "financial", "a.pdf")]), Ingest()
        taken = dl.module.take_message(poison, "m1")
        assert taken is not None and dl.module.take_message(poison, "nope") is None
        dl.module.retry(poison, ingest, taken)
        assert ingest.sent == [taken.content] and poison.deleted == ["m1"] and poison.messages == []

    def test_drop_takes_the_file_and_everything_of_it(self, dl):
        for folder, rel in (
            ("raw", "a.pdf"),
            ("markdown", "a.pdf.md"),
            ("chunks", "a.pdf.jsonl"),
            ("raw", "keep.pdf"),
        ):
            dl.blobs.get_blob_client("ingestion", f"{folder}/financial/{rel}").upload_blob(
                b"x", overwrite=True
            )
        dl.module.record_failure(
            dl.blobs,
            types.SimpleNamespace(uri=f"{ACCOUNT}/ingestion/raw/financial/a.pdf", error="x"),
            5,
        )
        poison = Poison([message("m1", "financial", "a.pdf")])
        gone = dl.module.drop(dl.blobs, poison, poison.messages[0], "financial", "a.pdf")
        assert gone == {"raw": 1, "markdown": 1, "chunks": 1, "failed": 1} and poison.deleted == [
            "m1"
        ]
        assert [b for c, b in dl.blobs.blobs] == ["raw/financial/keep.pdf"], (
            "nothing else is touched"
        )
        assert dl.module.drop(
            dl.blobs,
            Poison([message("m2", "financial", "a.pdf")]),
            message("m2", "financial", "a.pdf"),
            "financial",
            "a.pdf",
        ) == {"raw": 0, "markdown": 0, "chunks": 0, "failed": 0}
