"""One unbroken token must not hang the embedder.

The WordPiece scan tries the longest match first and shortens by one on each
miss, slicing a fresh substring every time, so its cost grows with the square
of the word it is handed. Ordinary prose never reaches that: the longest word
in English is about thirty characters. Real corpora are nonetheless full of
tokens that are not words, and one of them is enough to stop an ingest:

* base64 blobs and data URIs pasted into documents,
* minified JavaScript and CSS on one line,
* long URLs with signed query strings,
* hashes, DNA sequences, and CSV rows with no spaces.

Measured before the guard: 4,000 characters took 7 seconds, 8,000 took 40,
and 50,000 did not finish. The reference WordPiece implementation caps word
length at 100 characters for exactly this reason, so the guard changes
nothing that is actually a word.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("onnxruntime", reason="the bundled model needs onnxruntime")


@pytest.fixture(scope="module")
def embedder():
    from vectrixdb.models import DenseEmbedder

    return DenseEmbedder(language="en")


def test_one_enormous_token_embeds_promptly(embedder):
    started = time.perf_counter()
    vectors = embedder.embed(["x" * 50_000])
    elapsed = time.perf_counter() - started

    assert vectors.shape[0] == 1
    assert elapsed < 5.0, f"a 50,000 character token took {elapsed:.1f}s"


def test_cost_does_not_explode_with_token_length(embedder):
    """Ten times the characters must not cost ten times the time, which is
    what a quadratic scan would do."""
    embedder.embed(["warmup"])

    def timed(length: int) -> float:
        started = time.perf_counter()
        embedder.embed(["y" * length])
        return time.perf_counter() - started

    small = timed(1_000)
    large = timed(10_000)
    assert large < max(small * 4, 2.0), f"1,000 chars {small:.2f}s, 10,000 chars {large:.2f}s"


def test_a_long_token_still_produces_a_usable_vector(embedder):
    import numpy as np

    vectors = embedder.embed(["z" * 5_000, "a document about volcanic rock formation"])
    assert vectors.shape == (2, vectors.shape[1])
    assert np.isfinite(vectors).all(), "the long token produced a non-finite vector"
    assert float(np.linalg.norm(vectors[0])) > 0, "the long token produced a zero vector"


def test_ordinary_words_are_unaffected(embedder):
    """The guard fires at 100 characters. Nothing that is a word reaches it,
    so normal text must tokenize as it always did."""
    from vectrixdb.models.embedded import SimpleTokenizer

    assert SimpleTokenizer.MAX_CHARS_PER_WORD >= 100

    vectors = embedder.embed(
        [
            "antidisestablishmentarianism is a very long English word",
            "the mitochondrion is the powerhouse of the cell",
        ]
    )
    import numpy as np

    similarity = float(
        vectors[0] @ vectors[1] / (np.linalg.norm(vectors[0]) * np.linalg.norm(vectors[1]))
    )
    assert -1.0 <= similarity <= 1.0


def test_a_document_of_one_long_token_can_be_added_and_found(tmp_path):
    """The end to end path a user hits: a pasted blob in a corpus."""
    from vectrixdb import Vectrix

    db = Vectrix("blobs", path=str(tmp_path / "db"))
    try:
        started = time.perf_counter()
        db.add(
            [
                "A" * 30_000,
                "Basalt forms when lava cools quickly at the surface.",
            ]
        )
        elapsed = time.perf_counter() - started
        assert elapsed < 15.0, f"adding a blob and a sentence took {elapsed:.1f}s"
        assert db.count() == 2
        assert "Basalt" in db.search("volcanic rock", limit=1).top.text
    finally:
        db.close()
