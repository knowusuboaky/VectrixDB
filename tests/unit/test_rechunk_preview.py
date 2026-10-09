"""rechunk_preview(): what rechunk() would do, counted, with nothing written."""

from __future__ import annotations

import numpy as np
import pytest

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
            out[i, sum(map(ord, word)) % 8] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


def open_db(tmp_path, name="docs", **options):
    from vectrixdb import Vectrix

    return Vectrix(name, path=str(tmp_path / "db"), embed_fn=embed, dimension=8, **options)


def rows(db):
    return sorted(db._collection._iter_documents_raw(), key=lambda r: (r[2]["_vx_doc"], r[0]))


@pytest.fixture
def db(tmp_path):
    db = open_db(tmp_path, keep_source=True)
    db.add_document(CONTRACT, doc_id="a.pdf", chunk="markdown", metadata={"client_id": "acme"})
    db.add_document("# Other\n\nA note about basalt and how it forms.", doc_id="b.pdf")
    try:
        yield db
    finally:
        db.close()


def test_the_counts_match_what_rechunk_then_writes(db):
    before = rows(db)
    build = db.index_build_id
    planned = db.rechunk_preview("a.pdf", chunk="sentence", chunk_size=70, overlap=0)
    assert rows(db) == before, "a preview writes nothing"
    assert db.index_build_id == build
    assert db.documents.entry("a.pdf")["chunking"]["chunk"] == "markdown"

    (doc,) = planned.documents
    assert doc.doc_id == "a.pdf"
    assert doc.chunks_now == 2
    assert doc.chunking_now["chunk"] == "markdown"
    assert doc.chunking_after["chunk"] == "sentence"
    assert doc.chunking_after["chunk_size"] == 70
    assert doc.changed
    assert db.rechunk("a.pdf", chunk="sentence", chunk_size=70, overlap=0) == doc.chunks_after


def test_the_same_settings_change_nothing(db):
    planned = db.rechunk_preview()
    assert {d.doc_id for d in planned.documents} == {"a.pdf", "b.pdf"}
    assert planned.changed == []
    assert planned.chunks_now == planned.chunks_after == len(rows(db))
    summary = planned.to_dict()
    assert summary["changed"] == 0
    assert summary["chunks_after"] == planned.chunks_after
    assert "2 documents, 0 would change" in str(planned)


def test_where_picks_documents_and_unknown_options_are_refused(db):
    planned = db.rechunk_preview(where=lambda e: e["doc_id"] == "b.pdf", chunk_size=20)
    assert [d.doc_id for d in planned.documents] == ["b.pdf"]
    with pytest.raises(TypeError, match="rechunk_preview\\(\\) does not take"):
        db.rechunk_preview(chunk_sizes=10)


def test_a_document_still_waiting_to_be_cut_counts_zero_now(tmp_path):
    db = open_db(tmp_path, keep_source=True)
    try:
        db.add_document(CONTRACT, doc_id="waiting", index=False)
        (doc,) = db.rechunk_preview(chunk="markdown").documents
        assert doc.chunks_now == 0
        assert doc.chunks_after == 2
        assert doc.changed
    finally:
        db.close()
