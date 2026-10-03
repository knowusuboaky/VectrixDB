"""Filters and entitlement policies run inside OpenSearch, over metadata paths
promoted to mapped fields. Before this, a policied search on OpenSearch took
the local path and ``require_pushdown`` refused to open.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_opensearch import FakeOpenSearch  # noqa: E402

from vectrixdb.core.storage import OpenSearchStorage, StorageBackend, StorageConfig  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402
from vectrixdb.policy import AtMost, Overlap, Policy  # noqa: E402

FIELDS = {
    "client_id": "string",
    "tags": "strings",
    "classification": "number",
    "active": "boolean",
    "deal.region": "string",
}


def storage(fields=FIELDS, engine="nmslib"):
    client = FakeOpenSearch()
    store = OpenSearchStorage(
        StorageConfig(
            backend=StorageBackend.OPENSEARCH,
            opensearch_index_prefix="t",
            opensearch_filter_fields=fields,
            opensearch_knn_engine=engine,
        ),
        client=client,
    )
    return store, client


def embed(texts):
    return np.asarray(
        [[1, 0, 0, 0] if "alpha" in t else [0, 1, 0, 0] for t in texts], dtype=np.float32
    )


class TestCompiler:
    @pytest.mark.parametrize(
        "filter_, expected",
        [
            ({"client_id": "acme"}, {"term": {"f_client_id": "acme"}}),
            ({"classification": {"$lte": 2}}, {"range": {"f_classification": {"lte": 2.0}}}),
            (
                {"client_id": {"$in": ["acme", "zeta"]}},
                {"terms": {"f_client_id": ["acme", "zeta"]}},
            ),
            ({"tags": {"$in": ["urgent"]}}, {"terms": {"f_tags": ["urgent"]}}),
            ({"active": True}, {"term": {"f_active": True}}),
            ({"deal.region": "emea"}, {"term": {"f_deal__region": "emea"}}),
            (
                {"client_id": {"$exists": False}},
                {"bool": {"must_not": [{"exists": {"field": "f_client_id"}}]}},
            ),
            (
                {"client_id": {"$ne": "zeta"}},
                {
                    "bool": {
                        "filter": [{"exists": {"field": "f_client_id"}}],
                        "must_not": [{"term": {"f_client_id": "zeta"}}],
                    }
                },
            ),
            (
                {"$or": [{"client_id": "acme"}, {"classification": {"$lt": 2}}]},
                {
                    "bool": {
                        "should": [
                            {"term": {"f_client_id": "acme"}},
                            {"range": {"f_classification": {"lt": 2.0}}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            ),
            (
                {"client_id": "acme", "classification": {"$gte": 1}},
                {
                    "bool": {
                        "filter": [
                            {"term": {"f_client_id": "acme"}},
                            {"range": {"f_classification": {"gte": 1.0}}},
                        ]
                    }
                },
            ),
        ],
    )
    def test_the_filter_grammar_as_a_query(self, filter_, expected):
        store, _ = storage()
        assert store.compile_filter("memos", filter_) == expected

    @pytest.mark.parametrize(
        "filter_",
        [
            {"owner": "sam"},
            {"classification": "high"},
            {"active": {"$gt": False}},
            {"tags": "urgent"},
            {"client_id": {"$in": []}},
            {},
        ],
    )
    def test_what_has_no_honest_translation_is_left_to_the_collection(self, filter_):
        store, _ = storage()
        assert store.compile_filter("memos", filter_) is None

    def test_a_bad_kind_is_refused(self):
        store, _ = storage(fields={"client_id": "text"})
        with pytest.raises(
            ConfigurationError, match="a kind is one of string, strings, number, boolean"
        ):
            store.filter_fields("memos")


class TestTheIndex:
    def test_promoted_fields_are_mapped_and_carried(self):
        store, client = storage()
        store.create_collection("memos", {"dimension": 4})
        mapping = (
            client.indices.created["t_memos"]["mappings"]["properties"]
            if hasattr(client.indices, "created")
            else None
        )
        store.insert(
            "memos",
            "a",
            {
                "text_content": "alpha",
                "_embedding": [1, 0, 0, 0],
                "client_id": "acme",
                "tags": ["urgent", 7],
                "classification": 2,
                "active": True,
                "deal": {"region": "emea"},
            },
        )
        (doc,) = client._docs("t_memos").values()
        assert (
            doc["f_client_id"] == "acme"
            and doc["f_tags"] == ["urgent", "7"]
            and doc["f_classification"] == 2.0
        )
        assert doc["f_active"] is True and doc["f_deal__region"] == "emea"
        assert doc["metadata"]["client_id"] == "acme", "and the metadata is still whole"
        if mapping is not None:
            assert mapping["f_client_id"] == {"type": "keyword"} and mapping[
                "f_classification"
            ] == {"type": "double"}

    def test_a_value_of_the_wrong_type_is_null_and_so_fails_closed(self):
        store, client = storage()
        store.create_collection("memos", {"dimension": 4})
        store.insert(
            "memos",
            "a",
            {
                "text_content": "alpha",
                "_embedding": [1, 0, 0, 0],
                "classification": "high",
                "active": "yes",
            },
        )
        (doc,) = client._docs("t_memos").values()
        assert doc["f_classification"] is None and doc["f_active"] is None
        assert (
            store.vector_search(
                "memos",
                [1, 0, 0, 0],
                5,
                filter=store.compile_filter("memos", {"classification": {"$lte": 9}}),
            )
            == []
        )

    @pytest.mark.parametrize("engine", ["nmslib", "lucene"])
    def test_a_filter_goes_where_the_engine_wants_it(self, engine):
        store, client = storage(engine=engine)
        store.create_collection("memos", {"dimension": 4})
        for i in range(30):
            store.insert(
                "memos",
                f"z{i}",
                {"text_content": "alpha", "_embedding": [1, 0, 0, 0], "client_id": "zeta"},
            )
        store.insert(
            "memos",
            "a",
            {"text_content": "alpha", "_embedding": [0.9, 0.1, 0, 0], "client_id": "acme"},
        )
        seen = []
        original = client.search
        client.search = lambda **kwargs: (seen.append(kwargs["body"]["query"]), original(**kwargs))[
            1
        ]
        hits = store.vector_search(
            "memos", [1, 0, 0, 0], 3, filter=store.compile_filter("memos", {"client_id": "acme"})
        )
        assert [h[0] for h in hits] == ["a"], "thirty nearer neighbours belong to somebody else"
        query = seen[-1]
        if engine == "lucene":
            assert query["knn"]["dense_embedding"]["filter"] == {"term": {"f_client_id": "acme"}}
        else:
            assert (
                query["bool"]["filter"] == [{"term": {"f_client_id": "acme"}}]
                and query["bool"]["must"][0]["knn"]["dense_embedding"]["k"] >= 100
            )


class TestThroughVectrix:
    @pytest.fixture
    def memos(self, monkeypatch, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.audit import DENY, MemorySink
        from vectrixdb.core.database import VectrixDB

        store, client = storage(fields={"client_id": "string", "classification": "number"})
        monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: store)
        backend = VectrixDB.with_opensearch(
            "https://x.us-east-1.aoss.amazonaws.com",
            filter_fields={"client_id": "string", "classification": "number"},
        )
        policy = Policy(
            [Overlap("client_id", "clients", scope=True), AtMost("classification", "clearance")],
            require_pushdown=True,
        )
        sink = MemorySink(query_key=b"k", on_failure=DENY)
        docs = Vectrix(
            "memos",
            storage_backend=backend,
            path=str(tmp_path),
            embed_fn=embed,
            dimension=4,
            mode="hybrid",
            policy=policy,
            on_retrieval=sink,
        )
        docs.add(
            ["alpha memo for acme", "alpha secret for acme", "alpha memo for zeta"],
            ids=["acme-1", "acme-secret", "zeta-1"],
            metadata=[
                {"client_id": "acme", "classification": 1},
                {"client_id": "acme", "classification": 3},
                {"client_id": "zeta", "classification": 1},
            ],
        )
        yield docs, client, sink
        docs.close()

    def test_require_pushdown_opens_and_the_scope_runs_in_the_service(self, memos):
        docs, client, sink = memos
        seen = []
        original = client.search
        client.search = lambda **kwargs: (seen.append(kwargs["body"]["query"]), original(**kwargs))[
            1
        ]
        hits = docs.as_principal({"clients": ["acme"], "clearance": 2}).search(
            "alpha memo", limit=5, rerank=False
        )
        assert [h.id for h in hits] == ["acme-1"]
        assert seen and all("f_client_id" in repr(q) for q in seen), (
            "every request carried the scope"
        )
        assert not any("f_classification" in repr(q) for q in seen), (
            "and the redaction rule stayed here, so it can be counted"
        )
        record = sink.records[-1]
        assert (
            record.pushdown_mode == "engine"
            and record.withheld_disclosable == 1
            and record.withheld_undisclosable == 0
        )

    def test_a_revocation_reaches_the_service(self, memos):
        docs, client, _ = memos
        acme = docs.as_principal({"clients": ["acme"], "clearance": 2})
        assert [h.id for h in acme.search("alpha memo", limit=5, rerank=False)] == ["acme-1"]
        docs._collection.update_metadata("acme-1", {"client_id": "quarantine"})
        stored = {d["id"]: d for d in client._docs("t_memos").values()}
        assert stored["acme-1"]["f_client_id"] == "quarantine"
        assert acme.search("alpha memo", limit=5, rerank=False).items == []

    def test_without_promoted_fields_require_pushdown_still_refuses(self, monkeypatch, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.core.database import VectrixDB
        from vectrixdb.exceptions import PushdownUnavailable

        store, _ = storage(fields=None)
        monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: store)
        backend = VectrixDB.with_opensearch("https://x.us-east-1.aoss.amazonaws.com")
        with pytest.raises(PushdownUnavailable):
            Vectrix(
                "memos",
                storage_backend=backend,
                path=str(tmp_path),
                embed_fn=embed,
                dimension=4,
                policy=Policy([Overlap("client_id", "clients", scope=True)], require_pushdown=True),
            )
