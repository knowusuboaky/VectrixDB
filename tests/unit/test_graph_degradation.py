"""A caller who asked for the graph can tell a fallback from a hit.

mode="graph" falls back to vector results when the graph cannot answer. That
is right, but it used to be invisible: the result looked the same either way.
Results.degraded now carries the reason, and GraphUnavailable separates the
ordinary case (no graph to ask) from a real failure inside graph search.
"""

import logging

import pytest

from vectrixdb import Vectrix
from vectrixdb.exceptions import GraphUnavailable, SearchError, VectrixError

CORPUS = [
    "Marie Curie discovered radium in Paris.",
    "Pierre Curie worked with Marie Curie on radioactivity.",
    "Radioactivity research in Paris led to the Nobel Prize.",
]


@pytest.fixture
def db(tmp_path) -> Vectrix:
    db = Vectrix("kg", path=str(tmp_path), tier="graph")
    db.add(CORPUS)
    return db


def _unavailable(caplog, level):
    return [
        r
        for r in caplog.records
        if r.levelname == level and "Graph search unavailable" in r.getMessage()
    ]


class TestException:
    def test_it_is_a_search_error(self):
        assert issubclass(GraphUnavailable, SearchError)
        assert issubclass(GraphUnavailable, VectrixError)

    def test_it_is_exported(self):
        import vectrixdb

        assert vectrixdb.GraphUnavailable is GraphUnavailable


class TestPipelineRaisesIt:
    def test_when_not_built(self, db):
        pipeline = db.graph
        pipeline._is_built = False
        with pytest.raises(GraphUnavailable):
            pipeline.search("radium")

    def test_when_a_searcher_is_missing(self, db):
        pipeline = db.graph
        pipeline.hybrid_searcher = None
        with pytest.raises(GraphUnavailable, match="Hybrid searcher"):
            pipeline.search("radium")


class TestResultsDegraded:
    def test_a_working_graph_is_not_degraded(self, db):
        assert db.search("radioactivity", mode="graph").degraded is None

    def test_unavailable_is_recorded_quietly(self, db, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG, logger="vectrixdb")

        def unavailable(*args, **kwargs):
            raise GraphUnavailable("Graph not built")

        monkeypatch.setattr(type(db.graph), "search", unavailable)
        results = db.search("radioactivity", mode="graph")
        assert results, "the fallback still answers"
        assert results.degraded == "graph unavailable: Graph not built"
        assert not _unavailable(caplog, "WARNING"), "ordinary, so not a warning"
        assert _unavailable(caplog, "DEBUG")

    def test_a_real_failure_is_recorded_and_warned_once(self, db, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG, logger="vectrixdb")

        def explode(*args, **kwargs):
            raise RuntimeError("searcher exploded")

        monkeypatch.setattr(type(db.graph), "search", explode)
        for _ in range(3):
            results = db.search("radioactivity", mode="graph")
            assert results.degraded == "graph search failed: searcher exploded"
        assert len(_unavailable(caplog, "WARNING")) == 1

    def test_other_modes_never_set_it(self, db):
        assert db.search("radioactivity", mode="dense").degraded is None
