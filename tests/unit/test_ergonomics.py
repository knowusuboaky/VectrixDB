"""What a maintainer feels every day: closing, numpy in and out, frames, reprs.

None of this shows in a benchmark, and all of it decides whether the library
is pleasant to hold.
"""

import pickle

import numpy as np
import pytest

from vectrixdb import Vectrix
from vectrixdb.exceptions import ConfigurationError, DependencyError

TEXTS = ["alpha beta gamma", "delta epsilon", "zeta eta theta iota"]


@pytest.fixture
def db(tmp_path) -> Vectrix:
    db = Vectrix("ergo", path=str(tmp_path))
    db.add(TEXTS)
    return db


class TestClosing:
    def test_context_manager_flushes_and_reopens(self, tmp_path):
        with Vectrix("ctx", path=str(tmp_path)) as db:
            db.add(TEXTS)
        reopened = Vectrix("ctx", path=str(tmp_path))
        assert reopened.count() == 3
        assert reopened.search("alpha", limit=1).top.text == TEXTS[0]

    def test_close_is_idempotent(self, db):
        db.close()
        db.close()

    def test_use_after_close_raises_clearly(self, db):
        db.close()
        with pytest.raises(ConfigurationError, match="closed"):
            db.count()
        with pytest.raises(ConfigurationError, match="closed"):
            db.search("alpha")
        with pytest.raises(ConfigurationError, match="closed"):
            db.add("more")

    def test_repr_after_close(self, db):
        db.close()
        assert repr(db) == "Vectrix('ergo', closed)"

    def test_graph_pipeline_is_closed_too(self, tmp_path):
        db = Vectrix("g", path=str(tmp_path), tier="graph")
        db.add(["Marie Curie discovered radium in Paris."])
        assert db._graph_pipeline is not None
        db.close()
        assert db._graph_pipeline is None


class TestNumpy:
    def test_embed_returns_float32_matrix(self, db):
        vectors = db.embed(TEXTS)
        assert isinstance(vectors, np.ndarray)
        assert vectors.dtype == np.float32
        assert vectors.shape == (3, db.dimension)

    def test_embed_one_text_is_still_two_dimensional(self, db):
        assert db.embed("just one").shape == (1, db.dimension)

    def test_add_accepts_precomputed_vectors(self, tmp_path):
        source = Vectrix("src", path=str(tmp_path / "a"))
        vectors = source.embed(TEXTS)

        target = Vectrix("dst", path=str(tmp_path / "b"))
        target.add(TEXTS, vectors=vectors)
        assert target.count() == 3
        assert target.search("alpha", limit=1).top.text == TEXTS[0]

    def test_wrong_vector_count_is_refused(self, db):
        with pytest.raises(ValueError, match="texts"):
            db.add(["a", "b"], vectors=np.zeros((3, db.dimension), dtype=np.float32))

    def test_wrong_dimension_is_refused(self, db):
        with pytest.raises(ValueError, match="dimension"):
            db.add(["a"], vectors=np.zeros((1, db.dimension + 1), dtype=np.float32))


class TestSerialisation:
    def test_results_round_trip_through_dict(self, db):
        results = db.search("alpha", limit=2, token_budget=500)
        data = results.to_dict()
        back = type(results).from_dict(data)
        assert back.ids == results.ids
        assert back.scores == pytest.approx(results.scores)
        assert back.token_budget == 500 and back.mode == results.mode

    def test_results_pickle(self, db):
        results = db.search("alpha", limit=2)
        clone = pickle.loads(pickle.dumps(results))
        assert clone.ids == results.ids and clone.top.text == results.top.text

    def test_result_to_dict_is_plain(self, db):
        row = db.search("alpha", limit=1).top.to_dict()
        assert set(row) == {"id", "text", "score", "metadata"}


class TestFrames:
    def test_to_pandas(self, db):
        pd = pytest.importorskip("pandas")
        frame = db.search("alpha", limit=3).to_pandas()
        assert isinstance(frame, pd.DataFrame)
        assert list(frame.columns) == ["id", "score", "text", "metadata"]
        assert len(frame) == 3

    def test_to_polars_or_a_clear_dependency_error(self, db):
        results = db.search("alpha", limit=2)
        try:
            import polars  # noqa: F401
        except ImportError:
            with pytest.raises(DependencyError, match="polars"):
                results.to_polars()
        else:
            assert results.to_polars().shape == (2, 4)

    def test_empty_frame_keeps_its_columns(self, tmp_path):
        pd = pytest.importorskip("pandas")
        empty = Vectrix("empty", path=str(tmp_path)).search("anything")
        assert list(empty.to_pandas().columns) == ["id", "score", "text", "metadata"]
        assert isinstance(empty.to_pandas(), pd.DataFrame)


class TestReprs:
    def test_results_html_is_a_table_and_escapes(self, tmp_path):
        db = Vectrix("html", path=str(tmp_path))
        db.add(["<script>alert(1)</script> is not html"])
        html = db.search("script", limit=1)._repr_html_()
        assert "<table>" in html
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_result_html(self, db):
        assert "score=" in db.search("alpha", limit=1).top._repr_html_()

    def test_vectrix_html(self, db):
        html = db._repr_html_()
        assert "ergo" in html and "3" in html

    def test_truncation_shows_in_html(self, db):
        html = db.search("alpha", limit=3, token_budget=1)._repr_html_()
        assert "cut to fit" in html


class TestProgress:
    @pytest.mark.slow
    def test_progress_bar_does_not_change_the_result(self, tmp_path):
        pytest.importorskip("tqdm")
        texts = [f"note number {i} about topic {i % 7}" for i in range(40)]
        db = Vectrix("bar", path=str(tmp_path))
        db.add(texts, progress=True)
        assert db.count() == 40
        assert db.search("topic 3", limit=1)

    def test_progress_off_is_the_default_for_small_adds(self, db, monkeypatch):
        import vectrixdb.easy as easy

        calls = []
        original = easy.Vectrix._embed

        def counting(self, texts):
            calls.append(len(texts) if isinstance(texts, list) else 1)
            return original(self, texts)

        monkeypatch.setattr(easy.Vectrix, "_embed", counting)
        db.add([f"t{i}" for i in range(10)])
        assert calls == [10], "a small add embeds in one call"
