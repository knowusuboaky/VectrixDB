"""search(explain=True), fusion control, query expansion, HyDE and the
bundled reranker in dense mode."""

import pytest

from vectrixdb import Vectrix

CORPUS = [
    "Python is a programming language with significant whitespace.",
    "Rust guarantees memory safety without a garbage collector.",
    "Go compiles quickly and ships goroutines for cheap concurrency.",
    "Sourdough starter is flour and water kept alive by wild yeast.",
    "Basalt forms when lava cools quickly at the surface.",
    "The Nobel Prize was awarded for research on radioactivity.",
]


@pytest.fixture(scope="module")
def dense(tmp_path_factory):
    db = Vectrix("dense", path=str(tmp_path_factory.mktemp("d")))
    db.add(CORPUS)
    return db


@pytest.fixture(scope="module")
def hybrid(tmp_path_factory):
    db = Vectrix("hybrid", path=str(tmp_path_factory.mktemp("h")), mode="hybrid")
    db.add(CORPUS)
    return db


class TestExplain:
    def test_off_by_default(self, dense):
        assert dense.search("memory safety", limit=1).top.explain is None

    def test_dense_explains_its_similarity(self, dense):
        top = dense.search("memory safety", limit=1, explain=True).top
        assert top.explain == {"dense": pytest.approx(top.score)}

    def test_sparse_explains_bm25(self, dense):
        top = dense.search("garbage collector", limit=1, mode="sparse", explain=True).top
        assert set(top.explain) == {"bm25"} and top.explain["bm25"] > 0

    def test_hybrid_explains_every_stage(self, hybrid):
        top = hybrid.search("memory safety without garbage collection", limit=2, explain=True).top
        keys = set(top.explain)
        assert {"dense", "bm25", "rrf_dense", "rrf_sparse", "fusion", "alpha", "fused"} <= keys
        assert "rerank" in keys, "the cross-encoder ran and recorded its score"
        assert top.explain["fusion"] == "rrf"

    def test_explain_survives_to_dict(self, dense):
        row = dense.search("lava", limit=1, explain=True).top.to_dict()
        assert "explain" in row


class TestFusion:
    def test_weighted_records_normalised_parts(self, hybrid):
        top = hybrid.search("wild yeast", limit=1, explain=True, fusion="weighted", alpha=0.3).top
        assert top.explain["fusion"] == "weighted" and top.explain["alpha"] == 0.3
        assert 0.0 <= top.explain["norm_bm25"] <= 1.0

    def test_alpha_one_is_dense_only_ranking(self, hybrid):
        by_dense = hybrid.search("cheap concurrency", limit=3, fusion="rrf", alpha=1.0)
        assert by_dense.top.explain is None
        assert "Go" in by_dense.top.text

    def test_invalid_values_are_refused(self, hybrid):
        with pytest.raises(ValueError):
            hybrid.search("x", fusion="magic")
        with pytest.raises(ValueError):
            hybrid.search("x", alpha=1.5)


class TestExpansion:
    def test_expand_fuses_the_variants(self, dense):
        calls = []

        def expand(q):
            calls.append(q)
            return ["volcanic rock cooling", "igneous stone"]

        results = dense.search("basalt", limit=3, expand=expand, explain=True)
        assert calls == ["basalt"]
        assert "lava" in results.top.text.lower()
        assert results.top.explain["query_variants"] >= 1

    def test_hyde_embeds_the_hypothetical_answer(self, dense):
        seen = []

        def hyde(q):
            seen.append(q)
            return "Sourdough is made with a starter of flour and water and wild yeast."

        top = dense.search("how is that bread leavened", limit=1, hyde=hyde).top
        assert seen == ["how is that bread leavened"]
        assert "sourdough" in top.text.lower()


class TestBundledReranker:
    def test_dense_mode_can_rerank_offline(self, dense):
        results = dense.search(
            "language with whitespace", limit=3, rerank="cross-encoder", explain=True
        )
        assert results
        assert all("rerank" in r.explain for r in results)
        assert "Python" in results.top.text

    def test_unknown_rerank_method_is_refused(self, dense):
        with pytest.raises(ValueError):
            dense.search("x", rerank="magic")


class TestAddMany:
    @pytest.mark.slow
    def test_streams_from_a_generator(self, tmp_path):
        db = Vectrix("many", path=str(tmp_path))
        # Three batches, the last one partial. The count is kept small
        # because every text is embedded for real; 530 took 80 s.
        n = db.add_many((f"note {i} about topic {i % 4}" for i in range(45)), batch_size=20)
        assert n == 45 and db.count() == 45

    def test_metadata_and_ids_stay_aligned(self, tmp_path):
        db = Vectrix("aligned", path=str(tmp_path))
        db.add_many(
            (f"t{i}" for i in range(5)),
            metadata=({"i": i} for i in range(5)),
            ids=(f"id{i}" for i in range(5)),
            batch_size=2,
        )
        assert db.get("id3")[0].metadata["i"] == 3
