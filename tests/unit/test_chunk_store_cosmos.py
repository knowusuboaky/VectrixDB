"""The Cosmos DB chunk store, against a fake container that answers the store's own queries.

The fake answers each of the store's queries the way Cosmos would, and holds
the store to three things on every one: it runs inside one collection's
partition, it names every parameter it is given and no other, and it does not
group, because Python's Cosmos SDK does not run GROUP BY. An aggregate can
come back as one partial answer a page, the way a partition read in two pages
answers, and the store adds them up.

What is held to beyond that. An item a chunk, with an id Cosmos can hold
whatever the point id is, and plain JSON whatever the metadata held. A chunk
written again keeps the time it was first written. Writes go in batches
Cosmos takes, a hundred operations and under a megabyte, and one at a time on
an SDK too old to batch. A delete that races another process still removes the
rest. A metadata change made elsewhere between the read and the write is read
again. Opening names the address it wants, makes a missing container with the
partition the queries need, and refuses one partitioned any other way.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
import sys
import types
from datetime import datetime

import numpy as np
import pytest

from vectrixdb import chunk_store as cs
from vectrixdb.chunk_store import INDEXING, CosmosChunks
from vectrixdb.exceptions import ConfigurationError, DependencyError

PARAMETER = re.compile(r"@\w+")
WHEN = "2026-09-21T10:00:00+00:00"
LATER = "2026-09-22T10:00:00+00:00"


class Status(Exception):
    """An error with the status code the SDK's errors carry."""

    def __init__(self, code: int) -> None:
        super().__init__(f"status {code}")
        self.status_code = code


class FakeContainer:
    """One container's items, keyed by partition and id, answering the chunk store's queries."""

    def __init__(self) -> None:
        self.items: dict = {}
        self.clock = itertools.count(1_800_000_000)
        self.queries: list = []
        self.batches: list = []
        self.upserts = 0
        #: Aggregates come back as two partial answers, as a partition read in two pages does.
        self.split = False
        #: replace_item answers 412 this many times before it takes a write.
        self.conflicts = 0
        #: Ids another process deletes just before the next batch runs.
        self.raced: set = set()

    # ---------------------------------------------------------------- writes

    def upsert_item(self, body, **_):
        self.upserts += 1
        ts = next(self.clock)
        stored = {**copy.deepcopy(body), "_ts": ts, "_etag": f'"{ts}"'}
        self.items[(body["collection"], body["id"])] = stored
        return dict(stored)

    def replace_item(self, item, body, etag=None, match_condition=None, **_):
        key = (body["collection"], item)
        if key not in self.items:
            raise Status(404)
        if self.conflicts:
            self.conflicts -= 1
            raise Status(412)
        if etag is not None and self.items[key]["_etag"] != etag:
            raise Status(412)
        return self.upsert_item(body)

    def read_item(self, item, partition_key, **_):
        held = self.items.get((partition_key, item))
        if held is None:
            raise Status(404)
        return copy.deepcopy(held)

    def delete_item(self, item, partition_key, **_):
        if self.items.pop((partition_key, item), None) is None:
            raise Status(404)

    def execute_item_batch(self, batch_operations, partition_key, **_):
        operations = list(batch_operations)
        assert 0 < len(operations) <= 100, len(operations)
        assert len(json.dumps([args for _, args in operations], default=str)) < 1_200_000
        self.batches.append((partition_key, [kind for kind, _ in operations]))
        for key in list(self.raced):
            self.items.pop((partition_key, key), None)
        self.raced.clear()
        # All or nothing, as the service does it.
        for kind, args in operations:
            if kind == "delete" and (partition_key, args[0]) not in self.items:
                raise Status(404)
            if kind == "upsert":
                assert args[0]["collection"] == partition_key
        for kind, args in operations:
            if kind == "upsert":
                self.upsert_item(args[0])
            else:
                del self.items[(partition_key, args[0])]
        return []

    # ----------------------------------------------------------------- reads

    def query_items(self, query, parameters=None, partition_key=None, max_item_count=None, **_):
        assert partition_key is not None, "every query stays inside one collection's partition"
        assert "GROUP BY" not in query.upper(), "Python's Cosmos SDK does not run GROUP BY"
        bound = {p["name"]: p["value"] for p in parameters or []}
        assert set(PARAMETER.findall(query)) == set(bound), (query, bound)
        self.queries.append((query, dict(bound), partition_key))
        rows = [item for (partition, _), item in sorted(self.items.items()) if partition == partition_key]
        by_id = sorted(rows, key=lambda row: row["id"])
        if query == cs._COUNT:
            return [len(part) for part in self._pages(rows)] if rows else [0]
        if query == cs._CHANGED:
            return [max(row["_ts"] for row in part) for part in self._pages(rows)] if rows else []
        if query == cs._WRITTEN:
            return [{"written": r["written"], "build": r["build"], "quality": r["quality"]} for r in rows]
        if query == cs._SCORES:
            return [{"point": r["point"], "quality": r["quality"]} for r in rows]
        if query == cs._PAGE_OF_IDS:
            start = bound["@offset"]
            return [r["point"] for r in by_id][start : start + bound["@limit"]]
        if query == cs._EACH:
            return [{"point": r["point"], "metadata": r["metadata"]} for r in by_id]
        if query == cs._OF_DOCUMENT:
            return [r["point"] for r in rows if r["doc"] == bound["@doc"]]
        if query == cs._HELD:
            assert len(bound["@ids"]) <= 100
            return [{"id": r["id"], "written": r["written"]} for r in rows if r["id"] in bound["@ids"]]
        if query == cs._EVERY_ID:
            return [r["id"] for r in rows]
        raise AssertionError(f"a query the fake does not know: {query}")

    def _pages(self, rows):
        if self.split and len(rows) > 1:
            return [rows[: len(rows) // 2], rows[len(rows) // 2 :]]
        return [rows]


class UnbatchedContainer(FakeContainer):
    """An SDK from before transactional batches, 4.5.0 and 4.5.1."""

    execute_item_batch = None


@pytest.fixture
def container():
    return FakeContainer()


@pytest.fixture
def docs(container):
    return CosmosChunks(container).collection("docs")


def key(point_id):
    return hashlib.sha256(point_id.encode("utf-8")).hexdigest()


class TestItems:
    def test_one_item_a_chunk_in_its_collections_partition(self, container, docs):
        docs.put(["financial/td/q3.pdf:0"], ["Net income rose."], [{"_vx_doc": "financial/td/q3.pdf", "_vx_build": "build_1", "_vx_quality": 0.9}], WHEN)
        ((partition, item_id), item), = container.items.items()
        assert partition == "docs" and item_id == key("financial/td/q3.pdf:0")
        # An id Cosmos can hold: no slash, and 64 characters whatever the point id was.
        assert "/" not in item_id and len(item_id) == 64
        assert {k: item[k] for k in ("collection", "point", "doc", "build", "quality", "written", "text")} == {
            "collection": "docs",
            "point": "financial/td/q3.pdf:0",
            "doc": "financial/td/q3.pdf",
            "build": "build_1",
            "quality": 0.9,
            "written": WHEN,
            "text": "Net income rose.",
        }
        assert "vector" not in item and "_embedding" not in item

    def test_metadata_goes_in_as_plain_json_whatever_made_it(self, container, docs):
        docs.put(["a"], [None], [{"_vx_quality": np.float32(0.5), "pages": np.array([1, 2]), "n": np.int64(3)}], WHEN)
        (item,) = container.items.values()
        json.dumps(item)
        assert item["quality"] == 0.5 and item["metadata"]["pages"] == [1, 2] and item["metadata"]["n"] == 3
        assert item["text"] == ""

    def test_a_chunk_written_again_keeps_the_time_it_was_first_written(self, container, docs):
        docs.put(["a"], ["first"], [{"_vx_build": "build_1"}], WHEN)
        docs.put(["a", "b"], ["second", "new"], [{"_vx_build": "build_2"}, {}], LATER)
        a, b = container.items[("docs", key("a"))], container.items[("docs", key("b"))]
        assert (a["written"], a["text"], a["build"]) == (WHEN, "second", "build_2")
        assert b["written"] == LATER

    def test_the_last_of_two_under_one_id_is_kept(self, container, docs):
        docs.put(["a", "a"], ["old", "new"], [{}, {}], WHEN)
        assert [item["text"] for item in container.items.values()] == ["new"]

    def test_writes_go_in_batches_of_a_hundred_inside_one_partition(self, container, docs):
        docs.put([f"c{n}" for n in range(250)], ["text"] * 250, [{}] * 250, WHEN)
        assert [len(kinds) for _, kinds in container.batches] == [100, 100, 50]
        assert {partition for partition, _ in container.batches} == {"docs"}
        # And the lookup of which were there already is in hundreds too.
        assert [len(bound["@ids"]) for query, bound, _ in container.queries if query == cs._HELD] == [100, 100, 50]

    def test_a_batch_stays_under_a_megabyte(self, container, docs):
        docs.put([f"c{n}" for n in range(40)], ["x" * 60_000] * 40, [{}] * 40, WHEN)
        assert len(container.batches) > 1 and len(container.items) == 40

    def test_an_item_too_big_for_a_batch_goes_on_its_own(self, container, docs):
        docs.put(["big", "small"], ["x" * 1_100_000, "small"], [{}, {}], WHEN)
        assert container.upserts == 2 and [kinds for _, kinds in container.batches] == [["upsert"]]

    def test_an_sdk_too_old_to_batch_writes_one_at_a_time(self):
        old = UnbatchedContainer()
        docs = CosmosChunks(old).collection("docs")
        docs.put(["a", "b"], ["alpha", "beta"], [{}, {}], WHEN)
        assert old.upserts == 2 and old.batches == []
        assert docs.delete(["a", "missing"]) == 1
        assert list(old.items) == [("docs", key("b"))]


class TestDelete:
    def test_it_counts_what_was_there_and_leaves_other_collections(self, container, docs):
        notes = CosmosChunks(container).collection("notes")
        docs.put(["a", "b"], ["alpha", "beta"], [{}, {}], WHEN)
        notes.put(["a"], ["note"], [{}], WHEN)
        assert docs.delete(["a", "missing", "a"]) == 1
        assert docs.ids(10, 0) == ["b"] and notes.ids(10, 0) == ["a"]

    def test_a_delete_that_races_another_process_still_removes_the_rest(self, container, docs):
        docs.put(["a", "b", "c"], ["x", "y", "z"], [{}, {}, {}], WHEN)
        container.raced = {key("b")}
        assert docs.delete(["a", "b", "c"]) == 3
        assert container.items == {}

    def test_clear_takes_every_chunk_of_the_collection_and_no_other(self, container, docs):
        notes = CosmosChunks(container).collection("notes")
        docs.put([f"c{n}" for n in range(120)], ["t"] * 120, [{}] * 120, WHEN)
        notes.put(["n"], ["note"], [{}], WHEN)
        assert docs.clear() == 120
        assert docs.count() == 0 and notes.count() == 1


class TestUpdate:
    def test_merged_or_replaced_with_what_the_pages_read_worked_out_again(self, container, docs):
        docs.put(["a"], ["alpha"], [{"_vx_quality": 0.2, "k": 1}], WHEN)
        assert docs.update("a", {"_vx_quality": 0.8, "_vx_doc": "x.pdf"}) is True
        item = container.items[("docs", key("a"))]
        assert item["metadata"] == {"_vx_quality": 0.8, "k": 1, "_vx_doc": "x.pdf"}
        assert (item["quality"], item["doc"], item["written"], item["text"]) == (0.8, "x.pdf", WHEN, "alpha")
        assert docs.update("a", {"k": 2}, merge=False) is True
        item = container.items[("docs", key("a"))]
        assert item["metadata"] == {"k": 2} and item["quality"] is None and item["doc"] is None

    def test_a_chunk_that_is_not_there_is_false(self, docs):
        assert docs.update("missing", {"k": 1}) is False

    def test_changed_elsewhere_between_the_read_and_the_write_it_reads_again(self, container, docs):
        docs.put(["a"], ["alpha"], [{"k": 1}], WHEN)
        container.conflicts = 2
        assert docs.update("a", {"j": 2}) is True
        assert container.items[("docs", key("a"))]["metadata"] == {"k": 1, "j": 2}

    def test_changed_three_times_over_it_says_so(self, container, docs):
        docs.put(["a"], ["alpha"], [{}], WHEN)
        container.conflicts = 3
        with pytest.raises(RuntimeError, match="changed three times"):
            docs.update("a", {"j": 2})


class TestReads:
    @pytest.fixture
    def filled(self, container, docs):
        docs.put(
            ["r:0", "r:1", "r:2", "s:0"],
            ["alpha", "beta", "gamma", "delta"],
            [
                {"_vx_doc": "r.pdf", "_vx_build": "build_1", "_vx_quality": 0.9},
                {"_vx_doc": "r.pdf", "_vx_build": "build_1", "_vx_quality": 0.3},
                {"_vx_doc": "r.pdf", "_vx_build": "build_1"},
                {"_vx_doc": "s.pdf", "_vx_build": "build_2", "_vx_quality": 0.7},
            ],
            WHEN,
        )
        CosmosChunks(container).collection("notes").put(["n"], ["note"], [{"_vx_doc": "r.pdf"}], WHEN)
        return docs

    def test_count_adds_up_the_partial_answers(self, container, filled):
        assert filled.count() == 4
        container.split = True
        assert filled.count() == 4

    def test_changed_at_is_the_latest_of_the_partial_answers(self, container, filled):
        whole = filled.changed_at()
        container.split = True
        assert filled.changed_at() == whole and datetime.fromisoformat(whole).utcoffset().total_seconds() == 0
        assert CosmosChunks(container).collection("empty").changed_at() is None

    def test_written_and_scores_are_the_small_projection_the_pages_add_up(self, filled):
        assert sorted(filled.written(), key=repr) == sorted(
            [
                (WHEN, {"_vx_build": "build_1", "_vx_quality": 0.9}),
                (WHEN, {"_vx_build": "build_1", "_vx_quality": 0.3}),
                (WHEN, {"_vx_build": "build_1"}),
                (WHEN, {"_vx_build": "build_2", "_vx_quality": 0.7}),
            ],
            key=repr,
        )
        assert dict(filled.scores()) == {"r:0": 0.9, "r:1": 0.3, "r:2": None, "s:0": 0.7}

    def test_one_chunk_by_id(self, filled):
        point = filled.get("r:1")
        assert (point.id, point.text, point.metadata["_vx_quality"], point.vector) == ("r:1", "beta", 0.3, [])
        assert point.created_at == datetime.fromisoformat(WHEN) and point.updated_at is not None
        assert filled.get("missing") is None

    def test_pages_of_ids_cover_everything_once_in_one_order(self, filled):
        pages = [filled.ids(3, 0), filled.ids(3, 3), filled.ids(3, 6)]
        assert [len(page) for page in pages] == [3, 1, 0]
        everything = pages[0] + pages[1]
        assert sorted(everything) == ["r:0", "r:1", "r:2", "s:0"]
        assert [point for point, _ in filled.each()] == everything

    def test_one_documents_chunks_and_nothing_from_another_collection(self, filled):
        assert sorted(filled.of_document("r.pdf")) == ["r:0", "r:1", "r:2"]
        assert filled.of_document("nothing.pdf") == []

    def test_every_query_stays_inside_the_collections_partition(self, container, filled):
        container.queries.clear()
        filled.count(), filled.changed_at(), list(filled.written()), list(filled.scores()), filled.ids(2, 0), list(filled.each()), filled.of_document("r.pdf")
        assert len(container.queries) == 7 and {partition for _, _, partition in container.queries} == {"docs"}


class TestOpen:
    @pytest.fixture
    def sdk(self, monkeypatch):
        """Stand-in azure.cosmos and azure.identity modules, and what the store asked of them."""
        made: dict = {"container": {"partitionKey": {"paths": ["/collection"]}}, "refuse": False}

        class PartitionKey:
            def __init__(self, path):
                self.path = path

        class Container:
            def __init__(self, properties):
                self.properties = properties

            def read(self):
                if self.properties is None:
                    raise Status(404)
                return self.properties

        class Database:
            def get_container_client(self, name):
                made["container_name"] = name
                return Container(made["container"])

            def create_container_if_not_exists(self, id, partition_key, indexing_policy=None):
                if made["refuse"]:
                    raise Status(403)
                made.update(created=id, partition=partition_key.path, indexing=indexing_policy)
                return Container({"partitionKey": {"paths": [partition_key.path]}})

        class Client:
            def __init__(self, url, credential=None):
                made.update(url=url, credential=credential)

            def get_database_client(self, name):
                made["database"] = name
                return Database()

            def create_database_if_not_exists(self, name):
                made["created_database"] = name
                return Database()

        cosmos = types.ModuleType("azure.cosmos")
        cosmos.CosmosClient, cosmos.PartitionKey = Client, PartitionKey
        identity = types.ModuleType("azure.identity")
        identity.DefaultAzureCredential = lambda: "the managed identity"
        monkeypatch.setitem(sys.modules, "azure.cosmos", cosmos)
        monkeypatch.setitem(sys.modules, "azure.identity", identity)
        return made

    URL = "cosmos://acct.documents.azure.com/vectrixdb/chunks"

    def test_an_existing_container_with_the_key_or_the_managed_identity(self, sdk):
        store = CosmosChunks.open(self.URL, key="the-key")
        assert (sdk["url"], sdk["credential"], sdk["database"], sdk["container_name"]) == ("https://acct.documents.azure.com:443/", "the-key", "vectrixdb", "chunks")
        assert store.describe() == "Cosmos DB acct.documents.azure.com/vectrixdb/chunks"
        CosmosChunks.open(self.URL)
        assert sdk["credential"] == "the managed identity"

    def test_a_missing_container_is_made_partitioned_by_collection(self, sdk):
        sdk["container"] = None
        CosmosChunks.open(self.URL)
        assert (sdk["created"], sdk["partition"], sdk["indexing"]) == ("chunks", "/collection", INDEXING)
        # Only what the queries filter and sort by is indexed: not the text, not the metadata.
        assert {"path": "/text/?"} in INDEXING["excludedPaths"] and {"path": "/metadata/*"} in INDEXING["excludedPaths"]

    def test_one_it_may_not_make_says_how_to_make_it(self, sdk):
        sdk.update(container=None, refuse=True)
        with pytest.raises(ConfigurationError, match="/collection"):
            CosmosChunks.open(self.URL)

    def test_a_container_partitioned_any_other_way_is_refused(self, sdk):
        sdk["container"] = {"partitionKey": {"paths": ["/doc"]}}
        with pytest.raises(ConfigurationError, match="partitioned by /doc"):
            CosmosChunks.open(self.URL)

    def test_an_address_without_a_database_and_a_container_is_refused(self, sdk):
        for wrong in ("cosmos://acct.documents.azure.com/vectrixdb", "cosmos:///vectrixdb/chunks", "cosmos://acct.documents.azure.com/a/b/c"):
            with pytest.raises(ConfigurationError, match="<database>/<container>"):
                CosmosChunks.open(wrong)

    def test_without_the_sdk_it_names_the_extra(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "azure.cosmos", None)
        with pytest.raises(DependencyError):
            CosmosChunks.open(self.URL)
