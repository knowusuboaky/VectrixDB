"""Graph quality: entity schemas, relationship supersession, incremental
community detection, and the path and explain queries."""

from __future__ import annotations

import pytest

from vectrixdb.core.graphrag.config import EntitySchema, GraphRAGConfig, default_entity_schemas
from vectrixdb.core.graphrag.extractor.base import Entity, Relationship
from vectrixdb.core.graphrag.graph.community import CommunityHierarchy, detect_communities
from vectrixdb.core.graphrag.graph.incremental import affected_nodes, update_hierarchy
from vectrixdb.core.graphrag.graph.knowledge_graph import KnowledgeGraph
from vectrixdb.core.graphrag.graph.storage import GraphStorage
from vectrixdb.core.graphrag.queries import explain, path


def _entity(name, etype="PERSON"):
    return (
        Entity.create(name, etype)
        if hasattr(Entity, "create")
        else Entity(id=name, name=name, type=etype)
    )


def _rel(graph, a, b, rtype="RELATED_TO", strength=0.8):
    rel = Relationship.create(a, b, rtype, strength=strength)
    return graph.add_relationship(rel)


class TestEntitySchemas:
    def test_same_name_different_types_stay_apart(self):
        graph = KnowledgeGraph(schemas=default_entity_schemas())
        org = graph.add_entity(_entity("Apple", "ORGANIZATION"))
        fruit = graph.add_entity(_entity("Apple", "CONCEPT"))
        assert org != fruit and graph.entity_count == 2
        # Both are reachable by name; the plain name resolves to the first.
        assert graph.get_entity_by_name("Apple").id == org

    def test_person_initials_merge_and_concepts_do_not_fold(self):
        graph = KnowledgeGraph(schemas=default_entity_schemas())
        marie = graph.add_entity(_entity("Marie Curie", "PERSON"))
        assert graph.add_entity(_entity("M. Curie", "PERSON")) == marie
        ml = graph.add_entity(_entity("Machine Learning", "CONCEPT"))
        learning = graph.add_entity(_entity("Learning", "CONCEPT"))
        assert learning != ml, "CONCEPT has the subset rule off"
        # The same two names as PERSON would fold, because PERSON allows subsets.
        people = KnowledgeGraph(schemas=default_entity_schemas())
        full = people.add_entity(_entity("Machine Learning", "PERSON"))
        assert people.add_entity(_entity("Learning", "PERSON")) == full

    def test_cross_type_merge_when_a_schema_allows_it(self):
        schemas = default_entity_schemas()
        schemas["ALIAS"] = EntitySchema("ALIAS", 0.9, True, merge_across_types=True)
        graph = KnowledgeGraph(schemas=schemas)
        person = graph.add_entity(_entity("Marie Curie", "PERSON"))
        assert graph.add_entity(_entity("Marie Curie", "ALIAS")) == person

    def test_without_schemas_the_old_behaviour_holds(self):
        graph = KnowledgeGraph()
        a = graph.add_entity(_entity("Apple", "ORGANIZATION"))
        assert graph.add_entity(_entity("Apple", "CONCEPT")) == a

    def test_config_carries_schemas(self):
        config = GraphRAGConfig(enabled=True)
        assert config.entity_schemas["PERSON"].allow_subset is True
        assert config.entity_schemas["CONCEPT"].allow_subset is False
        assert config.incremental_communities is True


class TestSupersession:
    @pytest.fixture
    def graph(self):
        g = KnowledgeGraph()
        for name, t in (("Ada", "PERSON"), ("Acme", "ORGANIZATION"), ("Globex", "ORGANIZATION")):
            g.add_entity(_entity(name, t))
        return g

    def _ids(self, g):
        return {e.name: e.id for e in g.get_all_entities()}

    def test_new_fact_replaces_old_and_old_stays_for_the_record(self, graph):
        ids = self._ids(graph)
        old = _rel(graph, ids["Ada"], ids["Acme"], "WORKS_FOR")
        new = graph.add_relationship(
            Relationship.create(ids["Ada"], ids["Globex"], "WORKS_FOR", strength=0.9),
            supersedes=old,
        )
        live = graph.get_relationships_for_entity(ids["Ada"])
        assert [r.id for r in live] == [new]
        assert [r.id for r in graph.get_all_relationships()] == [new]
        everything = graph.get_relationships_for_entity(ids["Ada"], include_superseded=True)
        assert {r.id for r in everything} == {old, new}
        stale = graph.get_relationship(old)
        assert stale.superseded_by == new and stale.valid_to
        assert graph.get_relationship(new).valid_from == stale.valid_to
        assert graph.get_outgoing_relationships(ids["Ada"])[0].id == new

    def test_unknown_ids_are_refused(self, graph):
        ids = self._ids(graph)
        old = _rel(graph, ids["Ada"], ids["Acme"])
        assert graph.supersede_relationship(old, "nope") is False
        assert graph.supersede_relationship(old, old) is False

    def test_round_trips_through_dict_and_sqlite(self, graph, tmp_path):
        ids = self._ids(graph)
        old = _rel(graph, ids["Ada"], ids["Acme"], "WORKS_FOR")
        new = graph.add_relationship(
            Relationship.create(ids["Ada"], ids["Globex"], "WORKS_FOR"), supersedes=old
        )
        again = KnowledgeGraph.from_dict(graph.to_dict())
        assert again.get_relationship(old).superseded_by == new
        assert [r.id for r in again.get_all_relationships()] == [new]

        storage = GraphStorage(str(tmp_path / "graph.db"))
        storage.save_graph(graph)
        loaded = storage.load_graph()
        assert loaded.get_relationship(old).superseded_by == new
        assert loaded.get_relationship(new).valid_from
        assert [r.id for r in loaded.get_all_relationships()] == [new]

    def test_old_databases_gain_the_columns(self, tmp_path):
        import sqlite3

        db = tmp_path / "old.db"
        storage = GraphStorage(str(db))
        conn = sqlite3.connect(str(db))
        for column in ("valid_from", "valid_to", "superseded_by"):
            conn.execute(f"ALTER TABLE relationships DROP COLUMN {column}")
        conn.commit()
        conn.close()
        graph = KnowledgeGraph()
        a = graph.add_entity(_entity("A"))
        b = graph.add_entity(_entity("B"))
        _rel(graph, a, b)
        storage.save_graph(graph)  # must not fail on the pre-2.2 schema
        assert len(storage.load_graph().get_all_relationships()) == 1


def _two_clusters():
    """Two disconnected components, four nodes each."""
    g = KnowledgeGraph()
    names = {}
    for cluster in ("a", "b"):
        for i in range(4):
            names[f"{cluster}{i}"] = g.add_entity(_entity(f"{cluster.upper()}-{i}"))
        for i in range(4):
            for j in range(i + 1, 4):
                _rel(g, names[f"{cluster}{i}"], names[f"{cluster}{j}"])
    return g, names


class TestIncrementalCommunities:
    def test_only_the_touched_component_is_recomputed(self):
        graph, names = _two_clusters()
        hierarchy = detect_communities(graph, max_levels=1, min_community_size=2)
        for c in hierarchy.get_all_communities():
            c.summary = f"summary of {c.id}"
        before_nodes, before_edges = frozenset(graph.nodes), frozenset(graph.edges)
        a_ids = {c.id for c in hierarchy.get_all_communities() if names["a0"] in c.entity_ids}

        # Add a node to cluster B only.
        new = graph.add_entity(_entity("B-new"))
        _rel(graph, names["b0"], new)
        affected = affected_nodes(graph, before_nodes, before_edges)
        assert new in affected and names["b0"] in affected and names["a0"] not in affected

        result = update_hierarchy(
            graph, hierarchy, before_nodes, before_edges, max_levels=1, generation=1
        )
        assert not result.full
        kept = {c.id: c for c in result.hierarchy.get_all_communities()}
        assert a_ids <= set(kept), "cluster A communities are kept by id"
        assert all(kept[i].summary == f"summary of {i}" for i in a_ids), "and their summaries"
        assert all(c.id.endswith("_g1") for c in result.added), "new ids carry the generation"
        assert new in result.hierarchy.entity_to_community
        assert not any(i in kept for i in result.removed)

    def test_first_batch_and_total_change_fall_back_to_full(self):
        graph, names = _two_clusters()
        empty = CommunityHierarchy()
        result = update_hierarchy(graph, empty, frozenset(), frozenset(), max_levels=1)
        assert result.full and result.hierarchy.total_communities >= 2

    def test_no_change_means_no_work(self):
        graph, _ = _two_clusters()
        hierarchy = detect_communities(graph, max_levels=1)
        result = update_hierarchy(graph, hierarchy, frozenset(graph.nodes), frozenset(graph.edges))
        assert result.hierarchy is hierarchy and not result.added and not result.removed

    def test_pipeline_keeps_untouched_summaries(self, tmp_path, monkeypatch):
        from vectrixdb import Vectrix
        from vectrixdb.core.graphrag.config import ExtractorType

        # Small documents: two-entity communities must count, and the bundled
        # rule-based extractor keeps the test offline.
        config = GraphRAGConfig(enabled=True, extractor=ExtractorType.NLP, min_community_size=2)
        db = Vectrix("inc", path=str(tmp_path), mode="graph", graphrag_config=config)
        db.add(
            [
                "Marie Curie discovered radium in Paris. Pierre Curie worked with Marie Curie in Paris."
            ]
        )
        pipeline = db.graph
        first = {c.id: c.summary for c in pipeline.hierarchy.get_all_communities()}
        assert first
        calls = []
        import vectrixdb.core.graphrag.graph.incremental as inc

        original = inc.detect_communities

        def counting(graph, **kw):
            calls.append(graph.entity_count)
            return original(graph, **kw)

        monkeypatch.setattr(inc, "detect_communities", counting)
        db.add(
            ["Sourdough Bakery in Toronto sells rye bread. Rye Bread is baked by Sourdough Bakery."]
        )
        after = {c.id: c.summary for c in pipeline.hierarchy.get_all_communities()}
        for cid, summary in first.items():
            assert after.get(cid) == summary, "the Curie community was not touched"
        assert calls and max(calls) < pipeline.graph.entity_count, "detection ran on a subgraph"
        assert pipeline.hierarchy.total_communities >= len(first)
        db.close()


class TestPathAndExplain:
    @pytest.fixture
    def curie(self):
        g = KnowledgeGraph()
        ids = {}
        for name, t in (
            ("Marie Curie", "PERSON"),
            ("Pierre Curie", "PERSON"),
            ("Nobel Prize", "EVENT"),
            ("Sorbonne", "ORGANIZATION"),
            ("Warsaw", "LOCATION"),
        ):
            ids[name] = g.add_entity(_entity(name, t))
        _rel(g, ids["Marie Curie"], ids["Pierre Curie"], "MARRIED_TO", 0.9)
        _rel(g, ids["Pierre Curie"], ids["Nobel Prize"], "WON", 0.7)
        _rel(g, ids["Marie Curie"], ids["Sorbonne"], "WORKS_FOR", 0.6)
        _rel(g, ids["Marie Curie"], ids["Warsaw"], "BORN_IN", 0.5)
        return g, ids

    def test_path_finds_the_shortest_chain(self, curie):
        g, ids = curie
        result = path(g, "Marie Curie", "Nobel Prize")
        assert result["found"] and result["depth"] == 2
        assert result["entities"] == ["Marie Curie", "Pierre Curie", "Nobel Prize"]
        assert [r["type"] for r in result["relationships"]] == ["MARRIED_TO", "WON"]

    def test_path_resolves_names_and_reports_missing(self, curie):
        g, _ = curie
        assert path(g, "M. Curie", "Sorbonne")["found"]
        missing = path(g, "Nobody", "Sorbonne")
        assert not missing["found"] and missing["missing"] == ["Nobody"]
        assert path(g, "Warsaw", "Warsaw")["depth"] == 0

    def test_no_path_and_depth_limit(self, curie):
        g, ids = curie
        island = g.add_entity(_entity("Island"))
        assert not path(g, "Marie Curie", "Island")["found"]
        assert not path(g, "Warsaw", "Nobel Prize", max_depth=2)["found"]
        assert path(g, "Warsaw", "Nobel Prize", max_depth=3)["found"]

    def test_explain_one_and_two(self, curie):
        g, ids = curie
        hierarchy = detect_communities(g, max_levels=1, min_community_size=2)
        one = explain(g, "Marie Curie", hierarchy=hierarchy)
        assert one["found"] and one["entity"]["type"] == "PERSON"
        assert [r["type"] for r in one["relationships"]][0] == "MARRIED_TO", "strongest first"
        assert one["communities"] and one["communities"][0]["level"] == 0

        two = explain(g, "Marie Curie", "Nobel Prize", hierarchy=hierarchy)
        assert two["direct"] == [] and two["shared_neighbours"] == ["Pierre Curie"]
        assert two["path"]["depth"] == 2
        mine = {c["id"] for c in two["communities"]}
        theirs = {hierarchy.entity_to_community.get(ids["Nobel Prize"], {}).get(0)}
        assert two["same_community"] is bool(mine & theirs)

    def test_explain_shows_superseded_facts(self, curie):
        g, ids = curie
        old = [
            r for r in g.get_relationships_for_entity(ids["Marie Curie"]) if r.type == "WORKS_FOR"
        ][0].id
        new_org = g.add_entity(_entity("Radium Institute", "ORGANIZATION"))
        g.add_relationship(
            Relationship.create(ids["Marie Curie"], new_org, "WORKS_FOR"), supersedes=old
        )
        out = explain(g, "Marie Curie", "Sorbonne")
        assert out["direct"][0]["superseded_by"]
        assert "Sorbonne" not in [r["target"] for r in out["relationships"]]
        assert out["superseded"][0]["target"] == "Sorbonne"

    def test_vectrix_delegates(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("q", path=str(tmp_path), mode="graph")
        db.add(["Marie Curie married Pierre Curie in Paris. Pierre Curie won the Nobel Prize."])
        assert db.graph_explain("Marie Curie")["found"]
        result = db.graph_path("Marie Curie", "Nobel Prize")
        assert result["found"] or result.get("missing") == []
        db.close()
