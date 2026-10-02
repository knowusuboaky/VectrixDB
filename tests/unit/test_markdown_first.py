"""Markdown first, then chunks: each step's output kept where it can be read, and a retry that starts from it.

``markdown_first`` keeps a document's Markdown before it is cut and cuts what
was kept; ``keep_chunks`` writes the chunks, a JSON line each, before anything
embeds them. A later step that fails leaves the Markdown, and the next try
starts from it instead of reading the original again. A deleted original takes
its chunks with it, and with ``keep_deleted=False`` its Markdown too.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from vectrixdb.documents import ChunkStore, DocumentStore, LocalFiles, chunks_name
from vectrixdb.exceptions import ConfigurationError, DocumentNotFoundError

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


def open_db(tmp_path, **options):
    from vectrixdb import Vectrix

    return Vectrix("docs", path=str(tmp_path / "db"), embed_fn=embed, dimension=8, **options)


def rows(db):
    return sorted(db._collection._iter_documents_raw(), key=lambda r: r[2]["_vx_chunk"])


def staged(tmp_path, keep_deleted=True, **options):
    """A collection that keeps its Markdown first and its chunks, each in a folder of its own."""
    return open_db(
        tmp_path,
        keep_source=DocumentStore(LocalFiles(tmp_path / "markdown"), keep_deleted=keep_deleted),
        keep_chunks=tmp_path / "chunks",
        markdown_first=True,
        **options,
    )


# ============================================================ the chunk store ===


class TestTheChunkStore:
    def test_a_documents_chunks_sit_where_its_markdown_does(self):
        assert chunks_name("inbox/acme/scan.pdf") == "inbox/acme/scan.pdf.jsonl"
        assert chunks_name("s3://inbox/acme/scan.pdf") == "inbox/acme/scan.pdf.jsonl"

    def test_a_line_a_chunk_read_back_in_order(self, tmp_path):
        store = ChunkStore(LocalFiles(tmp_path))
        store.put("td/report.pdf", ["a", "b"], ["first", "second"], [{"page": 1}, {"page": np.int64(2)}])
        lines = (tmp_path / "td" / "report.pdf.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["id"] for line in lines] == ["a", "b"]
        assert store.get("td/report.pdf") == [
            {"id": "a", "text": "first", "metadata": {"page": 1}},
            {"id": "b", "text": "second", "metadata": {"page": 2}},
        ]

    def test_written_again_replaces_and_deleted_is_gone(self, tmp_path):
        store = ChunkStore(LocalFiles(tmp_path))
        store.put("x.pdf", ["a", "b"], ["one", "two"], [{}, {}])
        store.put("x.pdf", ["c"], ["three"], [{}])
        assert [c["text"] for c in store.get("x.pdf")] == ["three"] and "x.pdf" in store
        assert store.delete("x.pdf") and "x.pdf" not in store and not store.delete("x.pdf")
        with pytest.raises(DocumentNotFoundError):
            store.get("x.pdf")


# ================================================================ keep_chunks ===


class TestKeepChunks:
    def test_the_chunks_written_are_the_chunks_indexed(self, tmp_path):
        db = open_db(tmp_path, keep_chunks=True)
        try:
            db.add_document(CONTRACT, doc_id="a.pdf", chunk="markdown", chunk_size=120, overlap=0)
            kept = db.kept_chunks.get("a.pdf")
            assert [(c["id"], c["text"]) for c in kept] == [(i, t) for i, t, _m in rows(db)]
            assert all(c["metadata"]["_vx_doc"] == "a.pdf" for c in kept)
            assert (tmp_path / "db" / "docs.chunks" / "a.pdf.jsonl").is_file(), "True keeps them beside the collection"
        finally:
            db.close()

    def test_cut_again_they_are_written_again(self, tmp_path):
        db = open_db(tmp_path, keep_source=True, keep_chunks=True)
        try:
            db.add_document(CONTRACT, doc_id="a.pdf", chunk="recursive", chunk_size=400, overlap=0)
            before = len(db.kept_chunks.get("a.pdf"))
            db.rechunk("a.pdf", chunk_size=80)
            after = db.kept_chunks.get("a.pdf")
            assert len(after) > before and [(c["id"], c["text"]) for c in after] == [(i, t) for i, t, _m in rows(db)]
        finally:
            db.close()

    def test_a_deleted_document_takes_its_chunks_with_it(self, tmp_path):
        db = open_db(tmp_path, keep_chunks=True)
        try:
            db.add_document(CONTRACT, doc_id="a.pdf")
            db.delete_document("a.pdf")
            assert "a.pdf" not in db.kept_chunks
        finally:
            db.close()


# ============================================================= markdown_first ===


class TestMarkdownFirst:
    def test_it_needs_somewhere_to_keep_the_markdown(self, tmp_path):
        with pytest.raises(ConfigurationError, match="keep_source"):
            open_db(tmp_path, markdown_first=True)

    def test_the_markdown_is_kept_even_when_a_later_step_fails(self, tmp_path, monkeypatch):
        db = staged(tmp_path)
        try:
            monkeypatch.setattr(db, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the index is down")))
            with pytest.raises(RuntimeError, match="the index is down"):
                db.add_document(CONTRACT, doc_id="a.pdf", source_version="v1")
            assert db.documents.entry("a.pdf")["source_version"] == "v1", "reading ended when the Markdown was written"
            assert "a.pdf" in db.kept_chunks, "and the chunks were written before anything embedded them"
            assert rows(db) == []
        finally:
            db.close()

    def test_without_it_a_failed_write_keeps_nothing_as_before(self, tmp_path, monkeypatch):
        db = open_db(tmp_path, keep_source=True)
        try:
            monkeypatch.setattr(db, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the index is down")))
            with pytest.raises(RuntimeError):
                db.add_document(CONTRACT, doc_id="a.pdf")
            assert db.documents.entry("a.pdf") is None, "the store never holds a document the index refused"
        finally:
            db.close()

    def test_what_is_cut_is_what_was_kept(self, tmp_path, monkeypatch):
        """The kept Markdown is read back and cut: a word only the read-back
        has is in the index and in the chunk file, and the word it replaced is not."""
        from dataclasses import replace

        db = staged(tmp_path)
        try:
            real = db.documents.get
            monkeypatch.setattr(
                db.documents, "get",
                lambda doc_id, images=False: replace(real(doc_id, images=images), text=real(doc_id).text.replace("thirty", "ninety")),
            )
            db.add_document(CONTRACT, doc_id="a.pdf")
            indexed = " ".join(t for _i, t, _m in rows(db))
            kept = " ".join(c["text"] for c in db.kept_chunks.get("a.pdf"))
            assert "ninety" in indexed and "thirty" not in indexed
            assert "ninety" in kept and "thirty" not in kept
        finally:
            db.close()

    def test_a_document_kept_as_it_is_is_not_written_again(self, tmp_path, monkeypatch):
        """So a second try after a later step failed still says when the document was really read."""
        db = staged(tmp_path)
        try:
            state = {"down": True}
            real_add = db.add

            def add(*args, **kwargs):
                if state["down"]:
                    raise RuntimeError("the index is down")
                return real_add(*args, **kwargs)

            monkeypatch.setattr(db, "add", add)
            with pytest.raises(RuntimeError):
                db.add_document(CONTRACT, doc_id="a.pdf", source_version="v1")
            puts = []
            real_put = db.documents.put
            monkeypatch.setattr(db.documents, "put", lambda *a, **k: puts.append(a[0]) or real_put(*a, **k))
            state["down"] = False
            assert db.add_document(CONTRACT, doc_id="a.pdf", source_version="v1") > 0
            assert puts == []
        finally:
            db.close()


# ================================================================ the worker ===


class TestTheWorkerStartsFromTheMarkdown:
    def worker(self, tmp_path, reader, **options):
        from vectrixdb import Vectrix
        from vectrixdb.worker import IngestWorker, LocalFetcher

        db = Vectrix(
            "inbox", path=str(tmp_path / "db"), embed_fn=embed, dimension=8, extractors={".pdf": reader}, **options
        )
        return db, IngestWorker(db, LocalFetcher(), doc_id_of=lambda uri: uri.rsplit("/", 1)[-1])

    def scan(self, tmp_path):
        path = tmp_path / "scan.pdf"
        path.write_bytes(b"%PDF the bytes")
        return path

    @pytest.mark.parametrize("etag", [None, "0x8DC1"], ids=["hashed", "with an etag"])
    def test_a_retry_after_the_index_failed_does_not_read_the_original_again(self, tmp_path, monkeypatch, etag):
        """With an ETag, as a blob event carries, the original is not even fetched again."""
        from vectrixdb.worker import IngestEvent

        read = []
        db, worker = self.worker(
            tmp_path,
            lambda data, name: read.append(name) or "OCR says the covenant is tested quarterly.",
            keep_source=DocumentStore(LocalFiles(tmp_path / "markdown")),
            keep_chunks=tmp_path / "chunks",
            markdown_first=True,
        )
        try:
            state = {"down": True}
            real_add = db.add
            monkeypatch.setattr(db, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")) if state["down"] else real_add(*a, **k))
            event = IngestEvent("created", self.scan(tmp_path).as_uri(), version=etag)
            with pytest.raises(RuntimeError):
                worker.handle(event)
            fetched = []
            real_fetch = worker.fetcher.fetch
            monkeypatch.setattr(worker.fetcher, "fetch", lambda uri: fetched.append(uri) or real_fetch(uri))
            state["down"] = False
            outcome = worker.handle(event)
            assert outcome.action == "created" and outcome.chunks == 1
            assert read == ["scan.pdf"], "read once: the second try started from the Markdown"
            assert fetched == ([] if etag else [event.uri])
            assert "covenant" in db.search("covenant", limit=1).top.text
        finally:
            db.close()

    def test_without_markdown_first_it_reads_it_again_as_before(self, tmp_path, monkeypatch):
        from vectrixdb.worker import IngestEvent

        read = []
        db, worker = self.worker(tmp_path, lambda data, name: read.append(name) or "The covenant is tested quarterly.", keep_source=True)
        try:
            state = {"down": True}
            real_add = db.add
            monkeypatch.setattr(db, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")) if state["down"] else real_add(*a, **k))
            event = IngestEvent("created", self.scan(tmp_path).as_uri())
            with pytest.raises(RuntimeError):
                worker.handle(event)
            state["down"] = False
            worker.handle(event)
            assert read == ["scan.pdf", "scan.pdf"]
        finally:
            db.close()

    def test_a_deleted_original_takes_its_markdown_and_its_chunks_with_it(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker = self.worker(
            tmp_path,
            lambda data, name: "The covenant is tested quarterly.",
            keep_source=DocumentStore(LocalFiles(tmp_path / "markdown"), keep_deleted=False),
            keep_chunks=tmp_path / "chunks",
            markdown_first=True,
        )
        try:
            scan = self.scan(tmp_path)
            worker.handle(IngestEvent("created", scan.as_uri()))
            assert (tmp_path / "markdown" / "scan.pdf.md").is_file() and (tmp_path / "chunks" / "scan.pdf.jsonl").is_file()
            outcome = worker.handle(IngestEvent("deleted", scan.as_uri()))
            assert outcome.action == "deleted" and rows(db) == []
            assert not (tmp_path / "markdown" / "scan.pdf.md").exists(), "its Markdown is gone"
            assert not list((tmp_path / "markdown").rglob("*.md")), "and no copy is kept"
            assert not (tmp_path / "chunks" / "scan.pdf.jsonl").exists(), "and so are its chunks"
        finally:
            db.close()

    def test_by_default_a_deleted_originals_markdown_is_moved_aside(self, tmp_path):
        """The library's own default is unchanged: what was indexed stays on the record."""
        from vectrixdb.worker import IngestEvent

        db, worker = self.worker(
            tmp_path, lambda data, name: "The covenant is tested quarterly.",
            keep_source=DocumentStore(LocalFiles(tmp_path / "markdown")), keep_chunks=tmp_path / "chunks",
        )
        try:
            scan = self.scan(tmp_path)
            worker.handle(IngestEvent("created", scan.as_uri()))
            worker.handle(IngestEvent("deleted", scan.as_uri()))
            assert list((tmp_path / "markdown" / "_deleted").rglob("scan.pdf.md"))
            assert not (tmp_path / "chunks" / "scan.pdf.jsonl").exists()
        finally:
            db.close()

    def test_the_chunk_stores_own_files_are_never_ingested(self, tmp_path):
        from vectrixdb.worker import IngestEvent

        db, worker = self.worker(
            tmp_path, lambda data, name: "text", keep_source=True, keep_chunks=tmp_path / "chunks"
        )
        try:
            inside = tmp_path / "chunks" / "scan.pdf.jsonl"
            inside.parent.mkdir(parents=True)
            inside.write_text("{}\n", encoding="utf-8")
            outcome = worker.handle(IngestEvent("created", inside.as_uri()))
            assert outcome.action == "ignored" and "chunk store" in outcome.error
        finally:
            db.close()
