"""One ingestion worker for every event source: idempotent on the bytes,
replaces a changed file under the same document id, and treats a delete as a
write that lineage records.

No cloud. The S3 and Blob fetchers take the client the host built, so the
tests hand them a fake with the two methods the worker calls, and the event
parsers are exercised on the shapes the services document.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vectrixdb.worker import (
    BlobFetcher,
    IngestEvent,
    IngestWorker,
    LocalFetcher,
    LocalWatcher,
    S3Fetcher,
    events_from_event_grid,
    events_from_s3,
)

TEXT_A = "Basalt forms when lava cools quickly.\n\nSourdough is leavened by wild yeast and a long ferment."
TEXT_B = "Basalt forms when lava cools quickly.\n\nGranite cools slowly, underground, and is coarse."


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        return {"Body": _Body(self.objects[(Bucket, Key)])}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeBlobService:
    def __init__(self, blobs):
        self.blobs = dict(blobs)

    def get_blob_client(self, container, blob):
        data = self.blobs[(container, blob)]

        class _Client:
            def download_blob(self_inner):
                class _Stream:
                    def readall(self_stream):
                        return data

                return _Stream()

        return _Client()


class TestEventParsing:
    def test_s3_records_direct_and_through_sqs(self):
        direct = {
            "Records": [
                {"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": "memos"}, "object": {"key": "2026/q3+notes.pdf", "eTag": "abc"}}},
                {"eventName": "ObjectRemoved:Delete", "s3": {"bucket": {"name": "memos"}, "object": {"key": "old.md"}}},
                {"eventName": "ObjectRestore:Completed", "s3": {"bucket": {"name": "memos"}, "object": {"key": "x"}}},
            ]
        }
        events = events_from_s3(direct)
        assert [(e.kind, e.uri, e.version) for e in events] == [
            ("created", "s3://memos/2026/q3 notes.pdf", "abc"),
            ("deleted", "s3://memos/old.md", None),
        ]
        via_sqs = {"Records": [{"body": json.dumps(direct)}, {"body": "not json"}, {"body": json.dumps({"other": 1})}]}
        assert [e.uri for e in events_from_s3(via_sqs)] == [e.uri for e in events]

    def test_event_grid_and_cloudevents_shapes(self):
        grid = [
            {"eventType": "Microsoft.Storage.BlobCreated", "data": {"url": "https://acct.blob.core.windows.net/memos/q3.pdf", "eTag": "0x1", "api": "PutBlob"}},
            {"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": "x"}},
            {"type": "Microsoft.Storage.BlobDeleted", "data": {"url": "https://acct.blob.core.windows.net/memos/old.md"}},
        ]
        events = events_from_event_grid(grid)
        assert [(e.kind, e.uri, e.version) for e in events] == [
            ("created", "https://acct.blob.core.windows.net/memos/q3.pdf", "0x1"),
            ("deleted", "https://acct.blob.core.windows.net/memos/old.md", None),
        ]
        assert events_from_event_grid(grid[0]) == events[:1]

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="created or deleted"):
            IngestEvent(kind="renamed", uri="x")


class TestFetchers:
    def test_s3_fetcher_reads_through_the_host_client(self):
        s3 = FakeS3({("memos", "2026/q3 notes.pdf"): b"hello"})
        assert S3Fetcher(s3).fetch("s3://memos/2026/q3%20notes.pdf") == b"hello"
        assert s3.calls == [("memos", "2026/q3 notes.pdf")]
        with pytest.raises(ValueError):
            S3Fetcher(s3).fetch("https://not-s3/x")

    def test_blob_fetcher_reads_container_and_name(self):
        svc = FakeBlobService({("memos", "2026/q3.pdf"): b"blob"})
        assert BlobFetcher(svc).fetch("https://acct.blob.core.windows.net/memos/2026/q3.pdf") == b"blob"
        with pytest.raises(ValueError):
            BlobFetcher(svc).fetch("https://acct.blob.core.windows.net/memos")

    def test_local_fetcher(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_bytes(b"local")
        assert LocalFetcher().fetch(str(p)) == b"local"
        assert LocalFetcher().fetch("file://" + str(p)) == b"local"
        spaced = tmp_path / "a b.txt"
        spaced.write_bytes(b"spaced")
        assert LocalFetcher().fetch(spaced.as_uri()) == b"spaced"


@pytest.fixture
def db(tmp_path):
    from vectrixdb import Vectrix

    db = Vectrix("inbox", path=str(tmp_path / "db"), mode="dense")
    yield db
    db.close()


def _chunks_of(db, doc_id):
    return [(i, m) for i, _, m in db._collection._iter_documents_raw() if m.get("_vx_doc") == doc_id]


class TestWorker:
    def test_create_is_idempotent_on_the_bytes(self, db):
        s3 = FakeS3({("memos", "notes.md"): TEXT_A.encode()})
        worker = IngestWorker(db, S3Fetcher(s3), chunk="sentence", chunk_size=60, overlap=0)
        event = IngestEvent(kind="created", uri="s3://memos/notes.md", version="v1")

        first = worker.handle(event)
        assert first.action == "created" and first.chunks >= 2 and first.build_id
        assert first.doc_id == "s3://memos/notes.md"
        chunks = _chunks_of(db, first.doc_id)
        assert chunks and all(m["source"] == "s3://memos/notes.md" and m["filename"] == "notes.md" for _, m in chunks)
        assert all(m["object_version"] == "v1" for _, m in chunks)
        assert chunks[0][1]["_vx_citation"].startswith("notes.md")

        again = worker.handle(event)
        assert again.action == "unchanged" and again.chunks == 0
        assert again.build_id == first.build_id, "a redelivered event mints nothing"
        assert len(_chunks_of(db, first.doc_id)) == len(chunks)

    def test_a_changed_file_replaces_its_chunks(self, db):
        s3 = FakeS3({("memos", "notes.md"): TEXT_A.encode()})
        worker = IngestWorker(db, S3Fetcher(s3), chunk="sentence", chunk_size=60, overlap=0)
        event = IngestEvent(kind="created", uri="s3://memos/notes.md")
        first = worker.handle(event)
        s3.objects[("memos", "notes.md")] = TEXT_B.encode()
        second = worker.handle(event)
        assert second.action == "updated" and second.build_id != first.build_id
        texts = [db._collection.get(i).text for i, _ in _chunks_of(db, event.uri)]
        assert any("Granite" in t for t in texts) and not any("Sourdough" in t for t in texts)

    def test_delete_removes_the_document_and_mints_a_build(self, db):
        s3 = FakeS3({("memos", "notes.md"): TEXT_A.encode()})
        worker = IngestWorker(db, S3Fetcher(s3))
        created = worker.handle(IngestEvent(kind="created", uri="s3://memos/notes.md"))
        gone = worker.handle(IngestEvent(kind="deleted", uri="s3://memos/notes.md"))
        assert gone.action == "deleted" and gone.chunks == created.chunks
        assert gone.build_id != created.build_id
        assert _chunks_of(db, "s3://memos/notes.md") == []
        absent = worker.handle(IngestEvent(kind="deleted", uri="s3://memos/notes.md"))
        assert absent.action == "absent" and absent.build_id is None

    def test_doc_id_and_metadata_hooks(self, db):
        s3 = FakeS3({("memos", "acme/2026/q3.md"): TEXT_A.encode()})
        worker = IngestWorker(
            db,
            S3Fetcher(s3),
            doc_id_of=lambda uri: uri.rsplit("/", 1)[-1],
            metadata_of=lambda e: {"client_id": e.uri.split("/")[3]},
        )
        out = worker.handle(IngestEvent(kind="created", uri="s3://memos/acme/2026/q3.md"))
        assert out.doc_id == "q3.md"
        assert all(m["client_id"] == "acme" for _, m in _chunks_of(db, "q3.md"))

    def test_the_policy_contract_still_gates_the_worker(self, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.exceptions import MetadataContractError
        from vectrixdb.policy import Overlap, Policy

        db = Vectrix("walled", path=str(tmp_path / "w"), mode="dense", policy=Policy([Overlap("client_id", "clients")]))
        try:
            s3 = FakeS3({("memos", "q3.md"): TEXT_A.encode()})
            bare = IngestWorker(db, S3Fetcher(s3))
            with pytest.raises(MetadataContractError):
                bare.handle(IngestEvent(kind="created", uri="s3://memos/q3.md"))
            stamped = IngestWorker(db, S3Fetcher(s3), metadata_of=lambda e: {"client_id": "acme"})
            assert stamped.handle(IngestEvent(kind="created", uri="s3://memos/q3.md")).action == "created"
        finally:
            db.close()

    def test_the_ingestion_record_carries_the_object(self, db, tmp_path):
        from vectrixdb.audit import DENY, MemorySink

        sink = MemorySink(query_key=b"k", on_failure=DENY)
        db.on_retrieval = sink
        s3 = FakeS3({("memos", "notes.md"): TEXT_A.encode()})
        IngestWorker(db, S3Fetcher(s3)).handle(IngestEvent(kind="created", uri="s3://memos/notes.md"))
        record = [r for r in sink.records if getattr(r, "ingestion_id", None)][-1]
        assert record.source == "s3://memos/notes.md" and record.document_id == "s3://memos/notes.md"
        assert record.documents_written >= 1


class TestLocalWatcher:
    def test_poll_reports_new_changed_and_deleted_files(self, db, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "a.md").write_text(TEXT_A, encoding="utf-8")
        watcher = LocalWatcher(inbox, IngestWorker(db, LocalFetcher()))

        first = watcher.poll()
        assert [o.action for o in first] == ["created"]
        assert watcher.poll() == []

        (inbox / "a.md").write_text(TEXT_B, encoding="utf-8")
        (inbox / "b.md").write_text("A second file about sourdough.", encoding="utf-8")
        second = watcher.poll()
        assert sorted(o.action for o in second) == ["created", "updated"]

        (inbox / "b.md").unlink()
        third = watcher.poll()
        assert [o.action for o in third] == ["deleted"]
        assert _chunks_of(db, str(inbox / "b.md")) == []
        assert Path(first[0].doc_id).name == "a.md"


class TestBursts:
    """A bucket that receives a thousand files sends a thousand events."""

    def _files(self, tmp_path, n):
        paths = []
        for i in range(n):
            path = tmp_path / f"memo-{i}.txt"
            path.write_text(f"Memo number {i} is about basalt and how lava cools into it.", encoding="utf-8")
            paths.append(str(path))
        return paths

    def test_the_index_is_saved_once_for_the_batch(self, db, tmp_path, monkeypatch):
        saves = []
        original = db._collection.save
        monkeypatch.setattr(db._collection, "save", lambda: (saves.append(1), original())[1])
        worker = IngestWorker(db, LocalFetcher())
        outcomes = worker.handle_all([IngestEvent("created", uri) for uri in self._files(tmp_path, 5)])
        assert [o.action for o in outcomes] == ["created"] * 5
        assert len(saves) == 1, "five documents, one write of the index file"
        assert len({o.build_id for o in outcomes}) == 5, "and still one ingestion each"

    def test_only_the_last_event_for_an_object_is_acted_on(self, db, tmp_path):
        a, b = self._files(tmp_path, 2)

        class Counting(LocalFetcher):
            fetched = []

            def fetch(self, uri):
                self.fetched.append(uri)
                return super().fetch(uri)

        worker = IngestWorker(db, Counting())
        outcomes = worker.handle_all(
            [IngestEvent("created", a), IngestEvent("created", a), IngestEvent("created", b), IngestEvent("deleted", b)]
        )
        assert [o.action for o in outcomes] == ["superseded", "created", "superseded", "absent"]
        assert Counting.fetched == [a], "b was created and then deleted: it was never read"

    def test_the_index_is_saved_when_the_block_ends_badly_too(self, db, monkeypatch):
        saves = []
        original = db._collection.save
        monkeypatch.setattr(db._collection, "save", lambda: (saves.append(1), original())[1])
        with pytest.raises(RuntimeError):
            with db.deferred_saves():
                db.add(["one written before the failure"], ids=["kept"])
                assert saves == []
                raise RuntimeError("something else went wrong")
        assert len(saves) == 1 and db.count() == 1
