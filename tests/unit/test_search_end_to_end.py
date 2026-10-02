"""Search has to actually find things, and keep finding them after a restart.

Three defects made the product's central feature unusable, and none of them were
visible from the existing suite because nothing measured a real query:

1. The ANN index was never persisted. ``Collection.save()`` existed but was only
   reachable through ``VectrixDB.close()``, which the one-line API never
   exposes, so a fresh process loaded an empty index. ``count()`` still reported
   the rows, so the failure was silent.
2. ``SearchResult.score`` was a raw usearch *distance* on the default backend and
   a similarity on hnswlib. Callers ranking by score got the order inverted, and
   ``score_threshold`` discarded the closest matches.
3. Result text lived only in an in-memory cache, so every result from a reopened
   database came back with ``text=''``.

These tests exercise the whole path: embed, store, reopen, query, read the text.
"""

import numpy as np
import pytest

from vectrixdb import Vectrix

pytest.importorskip("usearch", reason="the default index backend")

CORPUS = [
    "Python is a programming language",
    "The Pacific is the largest ocean",
    "Rust is a systems programming language",
    "Mitochondria produce energy in the cell",
]


def _seeded(path) -> Vectrix:
    db = Vectrix("corpus", path=str(path))
    db.add(CORPUS)
    return db


def _reopened(path) -> Vectrix:
    return Vectrix("corpus", path=str(path))


class TestSearchWorks:
    def test_finds_the_obviously_relevant_document(self, tmp_path):
        db = _seeded(tmp_path)
        assert "programming language" in db.search("programming").top.text

    def test_ranking_matches_exact_cosine_similarity(self, tmp_path):
        """The approximate index must agree with the arithmetic it approximates."""
        db = _seeded(tmp_path)
        query = "programming language"

        results = list(db.search(query, limit=len(CORPUS), mode="dense"))
        assert len(results) == len(CORPUS)

        q = np.asarray(db._embed([query])[0], dtype=np.float32)
        exact = {}
        for text in CORPUS:
            v = np.asarray(db._embed([text])[0], dtype=np.float32)
            exact[text] = float(q @ v)

        expected = [t for t, _ in sorted(exact.items(), key=lambda kv: -kv[1])]
        assert [r.text for r in results] == expected

        # The index stores f32 and searches approximately, so a couple of
        # thousandths of drift is expected. The tolerance is still far tighter
        # than the ~0.8 error a distance-versus-similarity mix-up produces.
        for r in results:
            assert r.score == pytest.approx(exact[r.text], abs=5e-3)


class TestScoreIsASimilarity:
    def test_scores_descend(self, tmp_path):
        db = _seeded(tmp_path)
        scores = [r.score for r in db.search("programming", limit=4, mode="dense")]
        assert scores == sorted(scores, reverse=True), f"not ranked best first: {scores}"

    def test_scores_are_in_similarity_range(self, tmp_path):
        """A cosine similarity, not a distance. The distinction broke thresholds."""
        db = _seeded(tmp_path)
        for r in db.search("programming", limit=4, mode="dense"):
            assert -1.0 <= r.score <= 1.0

    def test_a_close_match_scores_higher_than_an_unrelated_one(self, tmp_path):
        db = _seeded(tmp_path)
        ranked = {r.text: r.score for r in db.search("programming", limit=4, mode="dense")}
        best = max(ranked, key=ranked.get)
        worst = min(ranked, key=ranked.get)
        assert "programming" in best
        assert ranked[best] > ranked[worst]


class TestSurvivesRestart:
    def test_index_is_persisted_by_add(self, tmp_path):
        _seeded(tmp_path)
        assert len(_reopened(tmp_path)._collection._index) == len(CORPUS)

    def test_search_still_works_in_a_new_instance(self, tmp_path):
        _seeded(tmp_path)
        results = list(_reopened(tmp_path).search("programming", limit=2))
        assert results, "search returned nothing after reopening the database"
        assert "programming language" in results[0].text

    def test_count_and_index_agree_after_reopen(self, tmp_path):
        """The silent failure was count() and the index disagreeing."""
        _seeded(tmp_path)
        db = _reopened(tmp_path)
        assert db.count() == len(CORPUS)
        assert len(db._collection._index) == db.count()


class TestResultText:
    def test_text_is_present_in_the_writing_process(self, tmp_path):
        db = _seeded(tmp_path)
        assert all(r.text for r in db.search("programming", limit=4))

    def test_text_is_recovered_from_storage_after_reopen(self, tmp_path):
        _seeded(tmp_path)
        results = list(_reopened(tmp_path).search("programming", limit=4))
        assert all(r.text for r in results), "text was lost with the in-memory cache"
        assert {r.text for r in results} <= set(CORPUS)

    def test_top_carries_its_text(self, tmp_path):
        _seeded(tmp_path)
        assert _reopened(tmp_path).search("ocean").top.text


class TestQueryAndDocumentShareAnEmbeddingSpace:
    """The query embedder must be the model that wrote the vectors.

    The REST API called ``DenseEmbedder()`` with no argument, which defaults to
    the multilingual model, while the library writes with the English one.
    Embedding the same sentence with both scored 0.0148 against itself, so every
    ranking the dashboard produced was noise. Nothing caught it because no test
    compared the two sides.
    """

    def test_api_query_embedder_matches_the_write_side(self):
        server = pytest.importorskip(
            "vectrixdb.api.server", reason="the API extra is not installed"
        )
        from vectrixdb.easy import _EMBEDDED_MODEL_KEYS, Vectrix

        assert server.DEFAULT_QUERY_MODEL == _EMBEDDED_MODEL_KEYS[Vectrix._default_model], (
            "the API would embed queries in a different space from stored documents"
        )

    def test_the_same_text_embeds_identically_on_both_sides(self, tmp_path):
        server = pytest.importorskip(
            "vectrixdb.api.server", reason="the API extra is not installed"
        )
        text = "Ctrl+C stops the server."

        db = Vectrix("space_check", path=str(tmp_path))
        written = np.asarray(db._embed([text])[0], dtype=np.float32)
        queried = np.asarray(server.get_text_embedder().embed(text)[0], dtype=np.float32)

        cosine = float(written @ queried / (np.linalg.norm(written) * np.linalg.norm(queried)))
        assert cosine > 0.99, (
            f"query and document embedders disagree (cosine {cosine:.4f}); "
            "search rankings would be meaningless"
        )


class TestSearchAfterDelete:
    """A deleted vector must not hide the documents that remain.

    usearch keeps deleted keys in the graph as tombstones, so the index holds
    more keys than the collection has live documents. search() asked for
    exactly the live count, so when the nearest neighbours were all deleted
    the result was empty while the survivors sat in the index, reachable one
    position further down. Replay: tests/replays/old_tombstone_clamp.py.
    """

    def test_the_survivor_is_found_when_the_nearest_match_was_deleted(self, tmp_path):
        db = Vectrix("tombstone", path=str(tmp_path))
        db.add(["Customer wants PDF", "Customer wants DOCX"])
        ids = [p.id for p in db._collection.scroll(limit=10)[0]]
        db.delete([ids[0]])
        assert db.count() == 1
        assert [r.text for r in db.search("format", limit=5)] == ["Customer wants DOCX"]
        db.close()

    def test_many_deletions_still_return_every_survivor(self, tmp_path):
        db = Vectrix("tombstones", path=str(tmp_path))
        db.add([f"note number {i} about storage and retrieval" for i in range(20)])
        ids = [p.id for p in db._collection.scroll(limit=50)[0]]
        db.delete(ids[:15])
        found = db.search("storage retrieval note", limit=10)
        assert len(found) == 5, "every surviving document is reachable"
        assert len({r.id for r in found}) == 5
        db.close()


class TestDuplicateDocuments:
    """Re-adding the same text must be an upsert, not a crash.

    Document ids are derived from content, so the same text always produces the
    same key. usearch rejects duplicate keys with a bare
    ``RuntimeError: Duplicate keys not allowed in high-level wrappers``, which
    meant indexing any corpus containing a repeated sentence failed outright.
    """

    def test_same_text_twice_in_one_call(self, tmp_path):
        db = Vectrix("dupes", path=str(tmp_path))
        db.add(["identical sentence", "identical sentence", "a different one"])
        assert db.count() == 2

    def test_same_text_across_calls_is_idempotent(self, tmp_path):
        db = Vectrix("dupes", path=str(tmp_path))
        db.add(["identical sentence"])
        db.add(["identical sentence"])
        assert db.count() == 1

    def test_duplicates_do_not_break_search(self, tmp_path):
        db = Vectrix("dupes", path=str(tmp_path))
        db.add(["identical sentence", "identical sentence", "the Pacific ocean"])
        results = list(db.search("identical", limit=3))
        assert results
        assert len({r.id for r in results}) == len(results), "a duplicate was indexed twice"
