"""tier="graph" must actually build a graph.

The mode documented itself as "Ultimate + Knowledge Graph" while `easy.py`
contained no reference to `GraphRAGPipeline` at all: it configured three models,
ran ultimate search, and returned dense similarity scores over a graph that was
never built. `count()` reported rows the whole time, so nothing looked wrong.

These cover the path end to end — add, extract, persist, reopen, search — because
that is the part that was missing, and a component test of the resolver would not
have caught any of it.
"""

import pytest

from vectrixdb import Vectrix
from vectrixdb.exceptions import ConfigurationError

CORPUS = [
    "Marie Curie discovered radium in Paris.",
    "Pierre Curie worked with Marie Curie on radioactivity.",
    "Radium is a radioactive element used in early medicine.",
    "The Nobel Prize was awarded for research on radioactivity.",
]


@pytest.fixture
def graph_db(tmp_path) -> Vectrix:
    db = Vectrix("kg", path=str(tmp_path), tier="graph")
    db.add(CORPUS)
    return db


class TestGraphIsBuilt:
    def test_add_extracts_entities(self, graph_db):
        assert len(graph_db.graph.graph.nodes) > 0, "tier='graph' did not build a graph"

    def test_vectors_are_stored_too(self, graph_db):
        """The graph is in addition to retrieval, not instead of it."""
        assert graph_db.count() == len(CORPUS)

    def test_expected_entities_are_present(self, graph_db):
        names = {e.name.lower() for e in graph_db.graph.graph.nodes.values()}
        assert any("curie" in n for n in names)
        assert any("paris" in n for n in names)

    def test_two_people_sharing_a_surname_are_not_fused(self, graph_db):
        """Resolution has to survive contact with the real extraction path."""
        names = {e.name.lower() for e in graph_db.graph.graph.nodes.values()}
        assert any("marie" in n for n in names)
        assert any("pierre" in n for n in names)


class TestGraphPersists:
    def test_graph_survives_reopen(self, tmp_path):
        first = Vectrix("kg", path=str(tmp_path), tier="graph")
        first.add(CORPUS)
        built = len(first.graph.graph.nodes)
        assert built > 0

        reopened = Vectrix("kg", path=str(tmp_path), tier="graph")
        assert len(reopened.graph.graph.nodes) == built

    def test_vectors_and_graph_reopen_together(self, tmp_path):
        Vectrix("kg", path=str(tmp_path), tier="graph").add(CORPUS)
        reopened = Vectrix("kg", path=str(tmp_path), tier="graph")
        assert reopened.count() == len(CORPUS)
        assert len(reopened.graph.graph.nodes) > 0


class TestGraphSearch:
    def test_graph_mode_returns_results(self, graph_db):
        results = list(graph_db.search("who worked on radioactivity", mode="graph", limit=3))
        assert results
        assert all(r.text for r in results)

    def test_graph_mode_ranks_by_score(self, graph_db):
        scores = [r.score for r in graph_db.search("radioactivity", mode="graph", limit=4)]
        assert scores == sorted(scores, reverse=True)

    def test_graph_mode_finds_the_relevant_document(self, graph_db):
        top = graph_db.search("radioactive element", mode="graph").top
        assert "radium" in top.text.lower() or "radioactiv" in top.text.lower()

    def test_an_empty_graph_does_not_break_search(self, tmp_path):
        """Graph search degrades to retrieval rather than failing."""
        db = Vectrix("empty", path=str(tmp_path), tier="graph")
        db.add(["A single document with nothing much in it."])
        assert list(db.search("document", mode="graph", limit=1))


class TestGraphAccess:
    def test_entities_are_queryable_by_name(self, graph_db):
        assert graph_db.graph.get_entity("Marie Curie") is not None

    def test_neighbours_are_reachable(self, graph_db):
        assert isinstance(graph_db.graph.get_neighbors("Marie Curie"), dict)

    def test_graph_is_refused_outside_graph_mode(self, tmp_path):
        """Asking for a graph in dense mode should say so, not return an empty one."""
        db = Vectrix("plain", path=str(tmp_path), tier="dense")
        with pytest.raises(ConfigurationError, match="graph mode"):
            _ = db.graph

    def test_the_pipeline_is_built_once(self, graph_db):
        assert graph_db.graph is graph_db.graph


class TestFailureDoesNotLoseData:
    def test_vectors_survive_a_broken_extractor(self, tmp_path, monkeypatch):
        """Extraction is the expensive, fragile half.

        A failure there must not discard the vectors that were already written,
        so add() reports the problem and keeps going.
        """
        db = Vectrix("kg", path=str(tmp_path), tier="graph")
        db.add(CORPUS[:1])

        def explode(*args, **kwargs):
            raise RuntimeError("extractor unavailable")

        monkeypatch.setattr(type(db.graph), "add_documents", explode)
        db.add(["A second document added while extraction is broken."])

        assert db.count() == 2, "a graph failure lost the vectors"
        assert list(db.search("second document", limit=1))
