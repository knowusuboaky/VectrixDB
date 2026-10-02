"""The graph store: one JSON object a collection, wherever the evaluation runs go, with the build it was read from.

What is being held to. A folder address opens a store that keeps
``<collection>/graph.json``; a graph put is read back the same and deleted
for good; a collection with none reads as None. The object built off the
pipeline's storage carries the entities, relationships and communities in
plain dicts with the time, build and model. A graph is stale only when the
collection has a build now and it is not the graph's; what was written since
is counted by time, builds by their stamp.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from vectrixdb.graph_store import GraphStore, graph_json, graph_store, is_stale, nodes_and_edges, written_since


class TestTheStore:
    def test_a_folder_keeps_one_object_a_collection(self, tmp_path):
        store = graph_store(str(tmp_path / "graphs"))
        assert isinstance(store, GraphStore) and store.describe() == str(tmp_path / "graphs")
        assert store.get("memos") is None
        store.put("memos", {"collection": "memos", "entities": [{"id": "e1", "name": "Ada"}], "relationships": [], "communities": {}})
        assert (tmp_path / "graphs" / "memos" / "graph.json").exists()
        assert store.get("memos")["entities"][0]["name"] == "Ada"
        store.put("memos", {"collection": "memos", "entities": [], "relationships": [], "communities": {}})
        assert store.get("memos")["entities"] == [], "a new extraction replaces the last"
        assert store.delete("memos") is True and store.get("memos") is None and store.delete("memos") is False

    def test_a_store_is_passed_through_and_a_files_object_of_your_own_is_wrapped(self, tmp_path):
        store = graph_store(str(tmp_path))
        assert graph_store(store) is store

        class Files:
            def __init__(self):
                self.kept = {}

            def exists(self, rel):
                return rel in self.kept

            def read(self, rel):
                return self.kept[rel]

            def write(self, rel, data):
                self.kept[rel] = data

            def delete(self, rel):
                del self.kept[rel]

        own = graph_store(Files())
        own.put("g", {"entities": []})
        assert own.get("g") == {"entities": []} and own.describe() == "Files"


class _Storage:
    def load_all_entities(self):
        return [SimpleNamespace(id="e1", name="Meridian", type="Organization", description="a company", importance=0.8, aliases={"MH"})]

    def load_all_relationships(self):
        return [SimpleNamespace(id="r1", source_id="e1", target_id="e2", type="underwent", description="", strength=0.7, confidence=SimpleNamespace(value="extracted"), bidirectional=False, valid_from=None, valid_to=None, superseded_by=None)]

    def load_hierarchy(self):
        return SimpleNamespace(entity_to_community={"e1": {0: 3, 1: 1}})


class TestTheObject:
    def test_it_is_read_off_the_pipelines_storage_as_plain_dicts(self):
        graph = graph_json("memos", _Storage(), build="b1", model="spaCy")
        assert graph["collection"] == "memos" and graph["build"] == "b1" and graph["model"] == "spaCy"
        assert datetime.fromisoformat(graph["extracted_at"]).tzinfo is not None
        assert graph["entities"] == [{"id": "e1", "name": "Meridian", "type": "Organization", "description": "a company", "importance": 0.8}], "aliases and embeddings stay out"
        assert graph["relationships"][0]["confidence"] == "extracted" and "superseded_by" not in graph["relationships"][0]
        assert graph["communities"] == {"e1": 3}, "the level-0 community, for colouring"

    def test_the_page_gets_nodes_and_edges_in_the_shape_it_draws(self):
        graph = graph_json("memos", _Storage(), build="b1", model="spaCy")
        nodes, edges = nodes_and_edges(graph, limit=10)
        assert nodes[0]["data"] == {"id": "e1", "label": "Meridian", "type": "organization", "description": "a company", "importance": 0.8, "community": 3}
        assert edges[0]["data"]["source"] == "e1" and edges[0]["data"]["label"] == "underwent" and edges[0]["data"]["superseded"] is False
        assert nodes_and_edges({"entities": [{"id": str(n)} for n in range(30)], "relationships": []}, limit=10)[0].__len__() == 10

    def test_stale_means_the_collection_has_moved_to_another_build(self):
        assert is_stale({"build": "b1"}, "b2") is True
        assert is_stale({"build": "b1"}, "b1") is False
        assert is_stale({"build": "b1"}, None) is False, "a collection with no build yet has not moved on"
        assert is_stale({"build": None}, "b1") is True, "a graph read before builds were stamped is older than any build"

    def test_what_was_written_since_is_counted_by_time_and_builds_by_their_stamp(self):
        read = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
        graph = {"extracted_at": read.isoformat()}
        rows = [
            ((read - timedelta(days=1)).isoformat(), {"_vx_build": "old"}),
            ((read + timedelta(hours=1)).isoformat(), {"_vx_build": "b2"}),
            ((read + timedelta(hours=2)).isoformat().replace("+00:00", "Z"), {"_vx_build": "b2"}),
            ((read + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"), {"_vx_build": "b3"}),
            ("not a time", {"_vx_build": "b9"}),
        ]
        assert written_since(graph, iter(rows)) == {"builds": 2, "chunks": 3}, "a naive time reads as UTC, Z as +00:00, and a bad one is skipped"
        assert written_since({"extracted_at": None}, iter(rows)) == {"builds": 0, "chunks": 0}
