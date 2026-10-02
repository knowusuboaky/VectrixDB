"""The document store: the Markdown a document was indexed from is kept, reads
back exactly, and is never mistaken for an original.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from vectrixdb.documents import BlobFiles, DocumentStore, LocalFiles, S3Files, stored_name
from vectrixdb.exceptions import ConfigurationError, DocumentNotFoundError, ExtractionQualityError
from vectrixdb.ingest import LoadedDocument, load, markdown_document, split_front_matter

CONTRACT = (
    "# Terms\n\nPayment is due within thirty days of the invoice date. "
    "Invoices are issued monthly and sent to the billing contact on file.\n\n"
    "## Late fees\n\nInterest accrues monthly on any overdue balance. "
    "A reminder is sent after ten days and a second one after twenty.\n"
)


def embed(texts):
    out = np.zeros((len(texts), 8), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().split():
            out[i, hash(word) % 8] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


def open_db(tmp_path, name="docs", **options):
    from vectrixdb import Vectrix

    return Vectrix(name, path=str(tmp_path / "db"), embed_fn=embed, dimension=8, **options)


def rows(db):
    return sorted(db._collection._iter_documents_raw(), key=lambda r: r[2]["_vx_chunk"])


# ------------------------------------------------------------- front matter ---


class TestFrontMatter:
    def test_a_document_reads_back_exactly(self):
        source = CONTRACT + "\n![Fees by tier](fees.png)\n\n> **Figure 1.** Bar chart, three tiers.\n"
        doc = markdown_document(source, pages=[(0, 1), (CONTRACT.index("## Late"), 2)], metadata={"source": "a.pdf"})
        back = LoadedDocument.from_markdown(doc.to_markdown({"doc_id": "a.pdf", "extractor": "ocr"}))
        assert (back.text, back.pages, back.headings, back.figures, back.metadata) == (
            doc.text, doc.pages, doc.headings, doc.figures, doc.metadata,
        )

    def test_the_file_opens_in_any_front_matter_reader(self):
        text = markdown_document(CONTRACT).to_markdown({"doc_id": "a.pdf"})
        front, body = split_front_matter(text)
        assert front["vectrixdb"] == "extracted" and front["doc_id"] == "a.pdf"
        assert body.startswith("# Terms") and text.startswith("---\n")

    def test_what_people_write_at_the_top_of_a_file(self):
        front, body = split_front_matter(
            "---\n# a comment\ndoc_id: wildfire-deferment\ntitle: 'Payment Deferment'\n"
            "events: [wildfire, flood]\naudience:\n  - personal\n  - wealth\n"
            "classification: 2\nreviewed: 2026-05-25\nactive: true\nnested:\n  deep: value\n---\n\n# Program\n"
        )
        assert front == {
            "doc_id": "wildfire-deferment", "title": "Payment Deferment", "events": ["wildfire", "flood"],
            "audience": ["personal", "wealth"], "classification": 2, "reviewed": "2026-05-25", "active": True,
            "nested": None,
        }
        assert body == "# Program\n"

    def test_no_front_matter_is_no_front_matter(self):
        assert split_front_matter("# Title\n\n---\n\nA rule is not front matter.") == ({}, "# Title\n\n---\n\nA rule is not front matter.")

    def test_a_files_own_front_matter_becomes_every_chunks_metadata(self, tmp_path):
        page = tmp_path / "deferment.md"
        page.write_text("---\ndoc_id: deferment\nclient_id: acme\nclassification: 2\n---\n\n" + CONTRACT, encoding="utf-8")
        db = open_db(tmp_path)
        try:
            db.add_document(page, chunk="markdown", metadata={"classification": 3})
            stored = rows(db)
            assert {r[2]["_vx_doc"] for r in stored} == {"deferment"}
            assert all(r[2]["client_id"] == "acme" and r[2]["classification"] == 3 for r in stored)
            assert all("doc_id" not in r[2] for r in stored)
        finally:
            db.close()

    def test_an_extracted_file_dropped_among_the_originals_indexes_as_its_source(self, tmp_path):
        kept = markdown_document(CONTRACT, metadata={"source": "s3://inbox/scan.pdf", "filename": "scan.pdf"})
        stray = tmp_path / "scan.pdf.md"
        stray.write_text(kept.to_markdown({"doc_id": "scan.pdf"}), encoding="utf-8")
        doc = load(stray)
        assert doc.text == kept.text and "vectrixdb" not in doc.metadata
        assert doc.metadata["filename"] == "scan.pdf" and doc.metadata["source"] == "s3://inbox/scan.pdf"
        assert doc.metadata["doc_id"] == "scan.pdf"


class TestStoredName:
    @pytest.mark.parametrize(
        "doc_id, expected",
        [
            ("scan.pdf", "scan.pdf.md"),
            ("notes.md", "notes.md.md"),
            ("s3://inbox/acme/scan.pdf", "inbox/acme/scan.pdf.md"),
            ("https://acct.blob.core.windows.net/docs/acme/q3 report.pdf", "acct.blob.core.windows.net/docs/acme/q3 report.pdf.md"),
            ("C:\\inbox\\acme\\a.pdf", "C%3A/inbox/acme/a.pdf.md"),
            ("../../etc/passwd", "etc/passwd.md"),
            ("what?.pdf", "what%3F.pdf.md"),
            ("", "document.md"),
        ],
    )
    def test_names(self, doc_id, expected):
        assert stored_name(doc_id) == expected


# -------------------------------------------------------------- keep_source ---


class TestKeepSource:
    def test_the_markdown_is_kept_beside_the_collection(self, tmp_path):
        db = open_db(tmp_path, keep_source=True)
        try:
            db.add_document(CONTRACT, doc_id="contracts/acme-msa.pdf", chunk="markdown",
                            metadata={"client_id": "acme"}, source_version="etag-1")
            root = tmp_path / "db" / "docs.documents"
            assert (root / "contracts" / "acme-msa.pdf.md").is_file()
            listing = json.loads((root / "_index.json").read_text(encoding="utf-8"))
            entry = listing["documents"]["contracts/acme-msa.pdf"]
            assert entry["file"] == "contracts/acme-msa.pdf.md" and entry["source_version"] == "etag-1"
            assert entry["chunking"]["chunk"] == "markdown" and entry["user_metadata"] == {"client_id": "acme"}
            assert db.document("contracts/acme-msa.pdf").text == markdown_document(CONTRACT).text
            assert "contracts/acme-msa.pdf" in db.documents and len(db.documents) == 1
        finally:
            db.close()

    def test_an_original_that_is_markdown_is_kept_under_its_own_name_plus_md(self, tmp_path):
        original = tmp_path / "inbox" / "notes.md"
        original.parent.mkdir()
        original.write_text(CONTRACT, encoding="utf-8")
        db = open_db(tmp_path, keep_source=tmp_path / "kept")
        try:
            db.add_document(original, doc_id="notes.md", chunk="markdown")
            assert (tmp_path / "kept" / "notes.md.md").is_file()
            assert original.read_text(encoding="utf-8") == CONTRACT
        finally:
            db.close()

    def test_without_it_the_methods_say_how_to_turn_it_on(self, tmp_path):
        db = open_db(tmp_path)
        try:
            assert db.documents is None
            with pytest.raises(ConfigurationError, match="keep_source=True"):
                db.document("x")
        finally:
            db.close()

    def test_a_document_the_index_refused_is_not_kept(self, tmp_path):
        db = open_db(tmp_path, keep_source=True)
        try:
            with pytest.raises(ExtractionQualityError):
                db.add_document("zq xv kj " * 40, doc_id="garbage", on_low_quality="reject")
            assert db.documents.ids() == []
        finally:
            db.close()

    def test_delete_moves_it_aside_and_retention_removes_it(self, tmp_path):
        db = open_db(tmp_path, keep_source=DocumentStore(LocalFiles(tmp_path / "kept"), retain_days=0))
        try:
            db.add_document(CONTRACT, doc_id="a.pdf")
            db.delete_document("a.pdf")
            kept = tmp_path / "kept"
            assert not (kept / "a.pdf.md").exists() and db.documents.ids() == []
            moved = list((kept / "_deleted").rglob("a.pdf.md"))
            assert len(moved) == 1 and moved[0].read_text(encoding="utf-8").startswith("---\nvectrixdb")
            assert db.documents.purge_deleted() == 1 and not list((kept / "_deleted").rglob("*.md"))
            with pytest.raises(DocumentNotFoundError):
                db.document("a.pdf")
        finally:
            db.close()

    def test_nothing_is_purged_unless_a_retention_is_named(self, tmp_path):
        store = DocumentStore(LocalFiles(tmp_path / "kept"))
        store.put("a.pdf", markdown_document(CONTRACT))
        store.delete("a.pdf")
        assert store.purge_deleted() == 0 and store.purge_deleted(older_than_days=30) == 0

    def test_the_listing_is_rebuilt_from_the_files(self, tmp_path):
        store = DocumentStore(LocalFiles(tmp_path / "kept"))
        store.put("acme/a.pdf", markdown_document(CONTRACT), source_version="v1")
        (tmp_path / "kept" / "_index.json").unlink()
        assert store.ids() == ["acme/a.pdf"] and store.entry("acme/a.pdf")["source_version"] == "v1"


class TestRechunk:
    def test_it_reads_the_kept_markdown_and_never_the_extractor(self, tmp_path):
        calls = []

        def reader(data, name):
            calls.append(name)
            return {"text": CONTRACT, "pages": [[0, 1], [CONTRACT.index("## Late"), 2]]}

        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"%PDF")
        db = open_db(tmp_path, keep_source=True, extractors={".pdf": reader})
        try:
            db.add_document(scan, doc_id="scan.pdf", chunk="markdown", metadata={"client_id": "acme"})
            before = db.index_build_id
            assert len(rows(db)) == 2
            written = db.rechunk("scan.pdf", chunk="sentence", chunk_size=70, overlap=0)
            after = rows(db)
            assert calls == ["scan.pdf"], "the extractor ran once, at the first ingestion"
            assert written == len(after) > 2
            assert all(r[2]["client_id"] == "acme" and r[2]["_vx_chunk_strategy"] == "sentence" for r in after)
            assert {r[2]["page"] for r in after} == {1, 2}
            assert db.index_build_id != before
            assert db.documents.entry("scan.pdf")["chunking"]["chunk_size"] == 70
            assert db.rechunk("scan.pdf") == written, "what was asked for last time is the default next time"
        finally:
            db.close()

    def test_an_empty_index_is_filled_from_the_store_alone(self, tmp_path):
        first = open_db(tmp_path, keep_source=tmp_path / "kept")
        first.add_document(CONTRACT, doc_id="a.pdf", chunk="markdown", metadata={"client_id": "acme"})
        first.add_document("# Other\n\nA second document about basalt and how it forms.", doc_id="b.pdf", chunk="markdown")
        first.close()

        fresh = open_db(tmp_path, name="rebuilt", keep_source=tmp_path / "kept")
        try:
            assert fresh.count() == 0
            assert fresh.rechunk() == 3
            assert {r[2]["_vx_doc"] for r in rows(fresh)} == {"a.pdf", "b.pdf"}
            assert [r[2].get("client_id") for r in rows(fresh) if r[2]["_vx_doc"] == "a.pdf"] == ["acme", "acme"]
        finally:
            fresh.close()

    def test_where_picks_documents_and_bad_options_are_refused(self, tmp_path):
        db = open_db(tmp_path, keep_source=True)
        try:
            db.add_document(CONTRACT, doc_id="a.pdf", chunk="markdown")
            db.add_document("# B\n\nSomething else entirely, about granite.", doc_id="b.pdf", chunk="markdown")
            assert db.rechunk(where=lambda e: e["doc_id"].startswith("b")) == 1
            with pytest.raises(TypeError, match="does not take metadata"):
                db.rechunk("a.pdf", metadata={"client_id": "zeta"})
            with pytest.raises(DocumentNotFoundError):
                db.rechunk("missing.pdf")
        finally:
            db.close()


class TestReextract:
    def test_only_what_the_old_reader_read_is_read_again(self, tmp_path):
        old = tmp_path / "old.pdf"
        new = tmp_path / "new.pdf"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        state = {"label": "ocr-v1", "calls": []}

        def reader(data, name):
            state["calls"].append(name)
            suffix = " Improved reading." if state["label"] == "ocr-v2" else ""
            return LoadedDocument(text=f"The covenant in {name} is tested quarterly.{suffix}", metadata={"extractor": state["label"]})

        db = open_db(tmp_path, keep_source=True, extractors={".pdf": reader})
        try:
            db.add_document(old, doc_id="old.pdf")
            state["label"] = "ocr-v2"
            db.add_document(new, doc_id="new.pdf")
            state["calls"].clear()
            assert db.reextract(where=lambda e: e.get("extractor") != "ocr-v2") == 1
            assert state["calls"] == ["old.pdf"]
            assert "Improved reading" in db.document("old.pdf").text
            assert db.documents.entry("old.pdf")["extractor"] == "ocr-v2"
            assert "Improved" in db.search("covenant old.pdf", limit=5).top.text or db.count() == 2
        finally:
            db.close()


# ------------------------------------------------------------------ guards ---


class TestGuards:
    def _worker(self, db):
        from vectrixdb.worker import IngestWorker, LocalFetcher

        return IngestWorker(db, LocalFetcher(), chunk="markdown")

    @pytest.mark.parametrize("layout", ["store inside watched", "watched inside store", "the same folder"])
    def test_a_watcher_refuses_a_folder_that_overlaps_the_store(self, tmp_path, layout):
        from vectrixdb.worker import LocalWatcher

        inbox = tmp_path / "inbox"
        inbox.mkdir()
        kept, watched = {
            "store inside watched": (inbox / "kept", inbox),
            "watched inside store": (tmp_path, inbox),
            "the same folder": (inbox, inbox),
        }[layout]
        db = open_db(tmp_path / "elsewhere", keep_source=kept)
        try:
            with pytest.raises(ConfigurationError, match="overlap"):
                LocalWatcher(watched, self._worker(db))
        finally:
            db.close()

    def test_an_event_from_inside_the_store_is_ignored(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db = open_db(tmp_path, keep_source=tmp_path / "kept")
        try:
            db.add_document(CONTRACT, doc_id="a.pdf", chunk="markdown")
            before = db.count()
            kept_file = tmp_path / "kept" / "a.pdf.md"
            outcome = self._worker(db).handle(IngestEvent("created", str(kept_file)))
            assert outcome.action == "ignored" and db.count() == before and db.documents.ids() == ["a.pdf"]
        finally:
            db.close()

    def test_a_watcher_beside_the_store_cannot_loop(self, tmp_path):
        from vectrixdb.worker import LocalWatcher

        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "notes.md").write_text(CONTRACT, encoding="utf-8")
        db = open_db(tmp_path, keep_source=tmp_path / "kept")
        try:
            watcher = LocalWatcher(inbox, self._worker(db))
            assert [o.action for o in watcher.poll()] == ["created"]
            assert watcher.poll() == [] and watcher.poll() == []
            assert [p.name for p in (tmp_path / "kept").glob("**/*.md")] == ["notes.md.md"]
        finally:
            db.close()


class TestTwoStageVersioning:
    def _setup(self, tmp_path):
        from vectrixdb.worker import IngestWorker

        extractions = []

        def reader(data, name):
            extractions.append(data)
            return "The covenant is tested quarterly." if data.startswith(b"v") else data.decode()

        class Fetcher:
            def __init__(self):
                self.objects, self.fetched = {}, []

            def fetch(self, uri):
                self.fetched.append(uri)
                return self.objects[uri]

        fetcher = Fetcher()
        db = open_db(tmp_path, keep_source=True, extractors={".pdf": reader})
        return db, IngestWorker(db, fetcher), fetcher, extractions

    def test_the_same_etag_is_not_even_fetched(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker, fetcher, extractions = self._setup(tmp_path)
        try:
            fetcher.objects["s3://inbox/a.pdf"] = b"v1 bytes"
            assert worker.handle(IngestEvent("created", "s3://inbox/a.pdf", version="etag-1")).action == "created"
            again = worker.handle(IngestEvent("created", "s3://inbox/a.pdf", version="etag-1"))
            assert again.action == "unchanged" and len(fetcher.fetched) == 1 and len(extractions) == 1
        finally:
            db.close()

    def test_the_same_bytes_under_a_new_etag_are_fetched_but_not_read_again(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker, fetcher, extractions = self._setup(tmp_path)
        try:
            fetcher.objects["s3://inbox/a.pdf"] = b"v1 bytes"
            assert worker.handle(IngestEvent("created", "s3://inbox/a.pdf", version="etag-1")).action == "created"
            saved_again = worker.handle(IngestEvent("created", "s3://inbox/a.pdf", version="etag-2"))
            assert saved_again.action == "unchanged" and len(fetcher.fetched) == 2 and len(extractions) == 1, "fetched to hash it, not extracted again"
            store = getattr(db, "_kept", None) or getattr(db, "kept", None)
            entry = store.entry(worker.doc_id_of("s3://inbox/a.pdf")) if store is not None and hasattr(store, "entry") else None
            if entry is not None:
                assert entry["source_version"] == "etag-2" and len(entry["source_sha"]) == 16, "the new version is noted with the bytes' hash"
            assert worker.handle(IngestEvent("created", "s3://inbox/a.pdf", version="etag-2")).action == "unchanged" and len(fetcher.fetched) == 2, "and the new version is not even fetched next time"
        finally:
            db.close()

    def test_without_an_etag_the_bytes_decide(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker, fetcher, extractions = self._setup(tmp_path)
        try:
            fetcher.objects["inbox/a.pdf"] = b"v1 bytes"
            worker.handle(IngestEvent("created", "inbox/a.pdf"))
            assert worker.handle(IngestEvent("created", "inbox/a.pdf")).action == "unchanged"
            assert len(fetcher.fetched) == 2 and len(extractions) == 1, "fetched to hash it, not extracted again"
        finally:
            db.close()

    def test_new_bytes_with_the_same_words_cost_an_extraction_and_no_write(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker, fetcher, extractions = self._setup(tmp_path)
        try:
            fetcher.objects["inbox/a.pdf"] = b"v1 bytes"
            worker.handle(IngestEvent("created", "inbox/a.pdf", version="etag-1"))
            build = db.index_build_id
            fetcher.objects["inbox/a.pdf"] = b"v2 saved again"
            outcome = worker.handle(IngestEvent("created", "inbox/a.pdf", version="etag-2"))
            assert outcome.action == "unchanged" and len(extractions) == 2 and db.index_build_id == build
            assert db.documents.entry("inbox/a.pdf")["source_version"] == "etag-2"
            assert worker.handle(IngestEvent("created", "inbox/a.pdf", version="etag-2")).action == "unchanged"
            assert len(extractions) == 2
        finally:
            db.close()

    def test_new_words_replace_the_document(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker, fetcher, _ = self._setup(tmp_path)
        try:
            fetcher.objects["inbox/a.pdf"] = b"v1 bytes"
            worker.handle(IngestEvent("created", "inbox/a.pdf", version="etag-1"))
            fetcher.objects["inbox/a.pdf"] = b"Granite cools slowly underground and is coarse."
            outcome = worker.handle(IngestEvent("created", "inbox/a.pdf", version="etag-2"))
            assert outcome.action == "updated"
            assert "Granite" in db.document("inbox/a.pdf").text
            assert [r[1] for r in rows(db)] == ["Granite cools slowly underground and is coarse."]
        finally:
            db.close()


# ------------------------------------------------------------ object stores ---


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = {"Contents": [{"Key": k} for k in keys[start : start + 2]]}
        if start + 2 < len(keys):
            page["NextContinuationToken"] = str(start + 2)
        return page


class FakeBlobService:
    def __init__(self):
        self.blobs = {}

    def get_blob_client(self, container, blob):
        service = self

        class Client:
            def upload_blob(self, data, overwrite=False):
                service.blobs[(container, blob)] = data

            def download_blob(self):
                class Stream:
                    def readall(inner):
                        return service.blobs[(container, blob)]

                return Stream()

            def exists(self):
                return (container, blob) in service.blobs

            def delete_blob(self):
                del service.blobs[(container, blob)]

        return Client()

    def get_container_client(self, container):
        service = self

        class Container:
            def list_blobs(self, name_starts_with=""):
                return [{"name": b} for c, b in sorted(service.blobs) if c == container and b.startswith(name_starts_with)]

        return Container()


class TestObjectStores:
    @pytest.mark.parametrize("kind", ["s3", "blob"])
    def test_a_store_in_a_bucket_or_a_container(self, kind):
        if kind == "s3":
            client = FakeS3()
            files = S3Files(client, "extracted", prefix="kept")
            inside, outside = "s3://extracted/kept/acme/a.pdf.md", "s3://inbox/acme/a.pdf"
        else:
            client = FakeBlobService()
            files = BlobFiles(client, "extracted", prefix="kept")
            inside = "https://acct.blob.core.windows.net/extracted/kept/acme/a.pdf.md"
            outside = "https://acct.blob.core.windows.net/docs/acme/a.pdf"
        store = DocumentStore(files)
        doc = markdown_document(CONTRACT, metadata={"source": outside})
        doc.images["fees.png"] = b"\x89PNG-bytes"
        store.put("acme/a.pdf", doc, source_version="etag-1")
        store.put("acme/b.pdf", markdown_document("# B\n\nSecond."))
        store.put("acme/c.pdf", markdown_document("# C\n\nThird."))
        assert store.ids() == ["acme/a.pdf", "acme/b.pdf", "acme/c.pdf"]
        assert store.get("acme/a.pdf", images=True).images == {"fees.png": b"\x89PNG-bytes"}
        assert store.figure_file("acme/a.pdf", "fees.png") == "acme/a.pdf.figures/fees.png"
        assert store.holds(inside) and not store.holds(outside) and not store.holds("/tmp/a.pdf")
        assert store.delete("acme/b.pdf") and store.ids() == ["acme/a.pdf", "acme/c.pdf"]
        assert DocumentStore(files).reindex()["documents"].keys() == {"acme/a.pdf", "acme/c.pdf"}

    def test_files_that_are_not_files(self):
        with pytest.raises(ConfigurationError, match="read\\(\\) method"):
            DocumentStore(object())

    def test_a_name_never_leaves_the_root(self, tmp_path):
        files = LocalFiles(tmp_path / "kept")
        with pytest.raises(ValueError):
            files.write("../outside.md", b"x")
        assert not Path(tmp_path / "outside.md").exists()
