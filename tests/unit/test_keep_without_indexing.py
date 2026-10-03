"""A document kept and not yet cut: ``add_document(index=False)``, then ``rechunk()`` with the cut chosen later.

What is held to. ``index=False`` keeps the Markdown, with the metadata the
document came with, records no chunking, writes nothing to the index, and
needs a store to keep it in. Sent again unchanged, the Markdown is not
written again. A worker given ``index=False`` reads each file once and
keeps it. ``rechunk()`` then cuts a waiting document with the options it is
given and records them, a model's note and late embedding as flags too, and
a flag recorded is never handed back to ``add_document`` as an option.
"""

from __future__ import annotations

import numpy as np
import pytest

from vectrixdb.documents import DocumentStore, LocalFiles
from vectrixdb.exceptions import ConfigurationError

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


def keeping(tmp_path, **options):
    return open_db(
        tmp_path,
        keep_source=DocumentStore(LocalFiles(tmp_path / "markdown")),
        markdown_first=True,
        **options,
    )


class TestKeptNotCut:
    def test_the_markdown_is_kept_with_its_metadata_and_nothing_is_indexed(self, tmp_path):
        db = keeping(tmp_path)
        try:
            assert (
                db.add_document(
                    CONTRACT, doc_id="acme/msa.md", metadata={"client_id": "td"}, index=False
                )
                == 0
            )
            assert db.count() == 0 and list(db._collection._iter_documents_raw()) == []
            entry = db.documents.entry("acme/msa.md")
            assert entry["user_metadata"] == {"client_id": "td"}
            assert "chunking" not in entry, "no chunking recorded: it is still waiting to be cut"
        finally:
            db.close()

    def test_sent_again_unchanged_it_is_not_written_again(self, tmp_path):
        db = keeping(tmp_path)
        try:
            db.add_document(CONTRACT, doc_id="msa.md", index=False, source_version="v1")
            first = db.documents.entry("msa.md")["extracted_at"]
            put = []
            real = db.documents.put
            db.documents.put = lambda *a, **k: put.append(a) or real(*a, **k)
            db.add_document(CONTRACT, doc_id="msa.md", index=False, source_version="v1")
            assert put == [] and db.documents.entry("msa.md")["extracted_at"] == first
        finally:
            db.close()

    def test_without_a_store_there_is_nowhere_to_keep_it(self, tmp_path):
        db = open_db(tmp_path)
        try:
            with pytest.raises(ConfigurationError, match="keep_source"):
                db.add_document(CONTRACT, doc_id="msa.md", index=False)
        finally:
            db.close()

    def test_a_worker_given_it_keeps_every_file_and_indexes_none(self, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.worker import IngestEvent, IngestWorker, LocalFetcher

        read = []
        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"%PDF the bytes")
        db = Vectrix(
            "inbox",
            path=str(tmp_path / "db"),
            embed_fn=embed,
            dimension=8,
            extractors={
                ".pdf": lambda data, name: (
                    read.append(name) or "The covenant is tested every quarter."
                )
            },
            keep_source=DocumentStore(LocalFiles(tmp_path / "markdown")),
            markdown_first=True,
        )
        try:
            worker = IngestWorker(
                db, LocalFetcher(), doc_id_of=lambda uri: uri.rsplit("/", 1)[-1], index=False
            )
            event = IngestEvent("created", scan.as_uri(), version="0x1")
            assert worker.handle(event).chunks == 0
            assert worker.handle(event).chunks == 0
            assert read == ["scan.pdf"], "read once: the second message found it kept"
            assert db.count() == 0 and "chunking" not in db.documents.entry("scan.pdf")
        finally:
            db.close()


class TestCutLater:
    def test_rechunk_cuts_a_waiting_document_and_records_how(self, tmp_path):
        db = keeping(tmp_path)
        try:
            db.add_document(CONTRACT, doc_id="msa.md", metadata={"client_id": "td"}, index=False)
            written = db.rechunk(
                "msa.md", chunk="markdown", chunk_size=200, overlap=40, embed_heading=True
            )
            assert written > 0 and db.count() == written
            assert all(
                m.get("client_id") == "td" for _, _, m in db._collection._iter_documents_raw()
            ), "the metadata it came with"
            assert db.documents.entry("msa.md")["chunking"] == {
                "chunk": "markdown",
                "chunk_size": 200,
                "overlap": 40,
                "parent_size": None,
                "embed_heading": True,
            }
        finally:
            db.close()

    def test_a_model_note_and_late_embedding_are_recorded_as_flags(self, tmp_path):
        db = keeping(tmp_path)
        try:
            db.add_document(CONTRACT, doc_id="msa.md", index=False)
            db.rechunk(
                "msa.md",
                chunk="markdown",
                chunk_size=200,
                overlap=40,
                context_with=lambda text, doc, start, end: "A contract's terms.",
            )
            assert db.documents.entry("msa.md")["chunking"]["context"] is True
            # Cut again with nothing new asked: the flag is not handed back as an option.
            assert db.rechunk("msa.md") > 0
            assert "context" not in db.documents.entry("msa.md")["chunking"], (
                "no note this time, and none recorded"
            )
        finally:
            db.close()

    def test_what_the_reader_masked_is_kept_with_the_document(self, tmp_path):
        from vectrixdb.extract import LoadedDocument

        db = keeping(tmp_path)
        try:
            doc = LoadedDocument(
                text="# Notes\n\nCall [PHONE] about the account.",
                metadata={
                    "filename": "notes.md",
                    "kind": "markdown",
                    "masking": {
                        "counts": {"phone": 1},
                        "score": 0.5,
                        "engine": "language",
                        "language": "en",
                        "regex_only": False,
                    },
                },
            )
            db.add_document(doc, doc_id="notes.md", index=False)
            entry = db.documents.entry("notes.md")
            assert entry["masking"] == {
                "counts": {"phone": 1},
                "score": 0.5,
                "engine": "language",
                "language": "en",
                "regex_only": False,
            }, "counts and the score, in the front matter, for a status page to count"
            db.add_document(CONTRACT, doc_id="msa.md", index=False)
            assert "masking" not in db.documents.entry("msa.md"), (
                "a document nobody masked says nothing"
            )
        finally:
            db.close()

    def test_what_rechunk_still_refuses(self, tmp_path):
        db = keeping(tmp_path)
        try:
            db.add_document(CONTRACT, doc_id="msa.md", index=False)
            with pytest.raises(TypeError, match="does not take"):
                db.rechunk("msa.md", index=False)
        finally:
            db.close()
