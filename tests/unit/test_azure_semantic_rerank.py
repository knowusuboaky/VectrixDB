"""On the Azure backend with the semantic option, rerank is the service's
ranker: the score is its score, the explanation names it, and the local
cross-encoder stays out of it. A policied hybrid search goes through the
service once the policy's fields are promoted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("azure.search.documents")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_azure_search import FakeIndexClient, FakeSearchClient  # noqa: E402

from vectrixdb.core.storage import StorageBackend, StorageConfig  # noqa: E402
from vectrixdb.core.storage_azure import AzureSearchStorage  # noqa: E402
from vectrixdb.policy import AtMost, Overlap, Policy  # noqa: E402

FIELDS = {"client_id": "string", "classification": "number"}


def fake_storage(semantic=True, fields=None, fake=None) -> AzureSearchStorage:
    """A store on a fake service; pass ``fake`` to open a second handle on the same indexes."""
    fake = fake if fake is not None else FakeIndexClient()
    storage = AzureSearchStorage(
        StorageConfig(
            backend=StorageBackend.AZURE_SEARCH,
            azure_search_index_prefix="t",
            azure_search_semantic=semantic,
            azure_search_filter_fields=fields,
        ),
        index_client=fake,
        client_factory=fake.get_search_client,
    )
    storage.connect()
    return storage


@pytest.fixture
def azure(monkeypatch, request):
    from vectrixdb.core.database import VectrixDB

    semantic, fields = getattr(request, "param", (True, None))
    storage = fake_storage(semantic=semantic, fields=fields)
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: storage)
    db = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k", semantic=semantic, filter_fields=fields)
    yield db, storage
    db.close()


TEXTS = [
    ("Sourdough is leavened by wild yeast and a long ferment.", {"client_id": "acme", "classification": 1}),
    ("Basalt forms when lava cools quickly at the surface.", {"client_id": "acme", "classification": 3}),
    ("Zeta's private memo about sourdough starters.", {"client_id": "zeta", "classification": 1}),
]


class TestBackend:
    def test_the_backend_says_it_reranks_only_with_the_option(self):
        assert fake_storage(semantic=True).reranks is True
        assert fake_storage(semantic=False).reranks is False

    def test_hybrid_returns_the_ranker_score_and_carries_it_on_the_hit(self):
        storage = fake_storage(semantic=True)
        storage.create_collection("c", {"dimension": 3})
        storage.insert_batch("c", [("a", {"text_content": "sourdough bread", "dense_embedding": [1.0, 0.0, 0.0]})])
        (hit,) = storage.hybrid_search("c", [1.0, 0.0, 0.0], "sourdough", limit=1)
        doc_id, data, score = hit
        assert doc_id == "a" and data["_semantic_score"] == score and 0 < score <= 4.0
        plain = fake_storage(semantic=False)
        plain.create_collection("c", {"dimension": 3})
        plain.insert_batch("c", [("a", {"text_content": "sourdough bread", "dense_embedding": [1.0, 0.0, 0.0]})])
        (_, data2, _) = plain.hybrid_search("c", [1.0, 0.0, 0.0], "sourdough", limit=1)[0]
        assert "_semantic_score" not in data2


class TestThroughVectrix:
    def test_rerank_is_the_service_and_the_cross_encoder_stays_out(self, azure, tmp_path, monkeypatch):
        from vectrixdb import Vectrix

        db, _ = azure
        calls = []
        original = Vectrix._cross_encode

        def spy(self, query, candidates, limit, explain=False, enabled=True):
            calls.append(enabled)
            return original(self, query, candidates, limit, explain, enabled)

        monkeypatch.setattr(Vectrix, "_cross_encode", spy)
        docs = Vectrix("docs", storage_backend=db, path=str(tmp_path), mode="hybrid")
        docs.add([t for t, _ in TEXTS], metadata=[m for _, m in TEXTS])
        hits = docs.search("sourdough starter", limit=2, explain=True)
        assert hits.top.text.startswith(("Sourdough", "Zeta"))
        assert calls == [False], "the service reranked, so the local stage was told to stand down"
        assert "semantic" in (hits.top.explain or {})

    @pytest.mark.parametrize("azure", [(False, None)], indirect=True)
    def test_without_the_option_the_cross_encoder_runs(self, azure, tmp_path, monkeypatch):
        from vectrixdb import Vectrix

        db, _ = azure
        calls = []
        original = Vectrix._cross_encode
        monkeypatch.setattr(
            Vectrix, "_cross_encode", lambda self, q, c, l, explain=False, enabled=True: (calls.append(enabled), original(self, q, c, l, explain, enabled))[1]
        )
        docs = Vectrix("docs", storage_backend=db, path=str(tmp_path), mode="hybrid")
        docs.add([t for t, _ in TEXTS], metadata=[m for _, m in TEXTS])
        docs.search("sourdough", limit=2)
        assert calls == [True]

    @pytest.mark.parametrize("azure", [(True, FIELDS)], indirect=True)
    def test_a_policied_hybrid_search_goes_through_the_service_when_it_can(self, azure, tmp_path, monkeypatch):
        from vectrixdb import Vectrix
        from vectrixdb.audit import DENY, MemorySink

        db, _ = azure
        seen = []
        original = FakeSearchClient.search
        monkeypatch.setattr(
            FakeSearchClient, "search", lambda self, *a, **k: (seen.append(k.get("filter")), original(self, *a, **k))[1]
        )
        policy = Policy([Overlap("client_id", "clients", scope=True), AtMost("classification", "clearance")], require_pushdown=True)
        sink = MemorySink(query_key=b"k", on_failure=DENY)
        memos = Vectrix("memos", storage_backend=db, path=str(tmp_path), mode="hybrid", policy=policy, on_retrieval=sink)
        memos.add([t for t, _ in TEXTS], metadata=[m for _, m in TEXTS])

        hits = memos.as_principal({"clients": ["acme"], "clearance": 2}).search("sourdough", limit=5)
        assert [h.metadata["client_id"] for h in hits] == ["acme"]
        assert all(h.metadata["classification"] <= 2 for h in hits)
        pushed = [f for f in seen if f]
        assert pushed and "f_client_id" in pushed[-1] and "f_classification" not in pushed[-1]
        record = sink.records[-1]
        assert record.pushdown_mode == "engine"
        assert record.withheld_disclosable == 1 and record.withheld_undisclosable == 0

    def test_a_policy_the_service_cannot_enforce_takes_the_local_path(self, azure, tmp_path, monkeypatch):
        from vectrixdb import Vectrix

        db, storage = azure
        called = []
        original = storage.hybrid_search
        monkeypatch.setattr(storage, "hybrid_search", lambda *a, **k: (called.append(1), original(*a, **k))[1])
        policy = Policy([Overlap("client_id", "clients", scope=True)])
        memos = Vectrix("memos", storage_backend=db, path=str(tmp_path), mode="hybrid", policy=policy)
        memos.add([t for t, _ in TEXTS], metadata=[m for _, m in TEXTS])
        hits = memos.as_principal({"clients": ["acme"]}).search("sourdough", limit=5)
        assert {h.metadata["client_id"] for h in hits} == {"acme"}
        assert called == [], "no promoted fields, so the store is not the engine and the local path runs"


def configurations(storage, collection):
    """The semantic configurations the service holds for a collection's index, by name."""
    semantic = storage._index_client.get_index(storage._index_name(collection)).semantic_search
    return [c.name for c in (getattr(semantic, "configurations", None) or [])]


def one_row(storage):
    storage.create_collection("c", {"dimension": 3})
    storage.insert_batch("c", [("a", {"text_content": "sourdough bread", "dense_embedding": [1.0, 0.0, 0.0]})])


class TestAnIndexKeepsItsSemanticConfiguration:
    """Opening an index brings its definition up to date, and an update replaces
    the whole definition. The semantic configuration a production search
    depends on is kept whichever way a handle opens the index."""

    def test_a_handle_opened_with_the_ranker_off_keeps_it_and_can_still_ask_for_it(self):
        from vectrixdb.core.storage_azure import SEMANTIC_CONFIG

        on = fake_storage(semantic=True)
        one_row(on)
        off = fake_storage(semantic=False, fake=on._index_client)
        off.create_collection("c", {"dimension": 3})
        assert configurations(off, "c") == [SEMANTIC_CONFIG]
        (_, data, score) = off.hybrid_search("c", [1.0, 0.0, 0.0], "sourdough", limit=1, semantic=True)[0]
        assert data["_semantic_score"] == score, "the ranker runs when a search asks for it"
        (_, plain, _) = off.hybrid_search("c", [1.0, 0.0, 0.0], "sourdough", limit=1)[0]
        assert "_semantic_score" not in plain, "and only then: this handle was opened with it off"

    def test_a_handle_that_never_opened_the_index_learns_it_has_one(self):
        from vectrixdb.core.storage_azure import SEMANTIC_CONFIG

        on = fake_storage(semantic=True)
        one_row(on)
        reader = fake_storage(semantic=False, fake=on._index_client)
        assert reader.semantic_name("c") == SEMANTIC_CONFIG
        assert reader.text_search("c", "sourdough", semantic=True)[0][0] == "a"

    def test_a_configuration_made_in_the_portal_is_kept_and_used(self):
        from azure.search.documents.indexes.models import (
            SemanticConfiguration,
            SemanticField,
            SemanticPrioritizedFields,
            SemanticSearch,
        )

        off = fake_storage(semantic=False)
        one_row(off)
        off._index_client.get_index(off._index_name("c")).semantic_search = SemanticSearch(
            configurations=[
                SemanticConfiguration(
                    name="portal",
                    prioritized_fields=SemanticPrioritizedFields(content_fields=[SemanticField(field_name="text_content")]),
                )
            ]
        )
        on = fake_storage(semantic=True, fake=off._index_client)
        on.create_collection("c", {"dimension": 3})
        assert configurations(on, "c") == ["portal"], "not replaced, and nothing added beside it"
        assert on.semantic_name("c") == "portal"
        # The service refuses a configuration the index does not have, so a
        # ranked result is proof the portal's was the one named.
        (_, data, score) = on.hybrid_search("c", [1.0, 0.0, 0.0], "sourdough", limit=1)[0]
        assert data["_semantic_score"] == score

    def test_the_ranker_on_an_index_without_one_is_an_error_not_a_quiet_search(self):
        from vectrixdb.exceptions import ConfigurationError

        off = fake_storage(semantic=False)
        one_row(off)
        assert off.semantic_name("c") is None
        with pytest.raises(ConfigurationError, match="no semantic configuration"):
            off.text_search("c", "sourdough", semantic=True)
        with pytest.raises(ConfigurationError, match="no semantic configuration"):
            off.hybrid_search("c", [1.0, 0.0, 0.0], "sourdough", limit=1, semantic=True)


def spy_on_searches(monkeypatch):
    """What each search asked the service for, and whether the cross-encoder was let run."""
    from vectrixdb import Vectrix

    asked, crossed = [], []
    search = FakeSearchClient.search
    monkeypatch.setattr(FakeSearchClient, "search", lambda self, *a, **k: (asked.append(k.get("query_type")), search(self, *a, **k))[1])
    cross = Vectrix._cross_encode
    monkeypatch.setattr(
        Vectrix, "_cross_encode", lambda self, q, c, l, explain=False, enabled=True: (crossed.append(enabled), cross(self, q, c, l, explain, enabled))[1]
    )
    return asked, crossed


class TestTheRankerIsChosenPerSearch:
    """One index, searched with Azure's semantic ranker, with the cross-encoder
    instead, and with neither. The search chooses, and how the store was
    opened is only the default, which is what lets one index give an
    evaluation every setup."""

    def test_hybrid_with_the_service_ranker_the_cross_encoder_or_neither(self, azure, tmp_path, monkeypatch):
        from vectrixdb import Vectrix

        db, _ = azure
        docs = Vectrix("docs", storage_backend=db, path=str(tmp_path), mode="hybrid")
        docs.add([t for t, _ in TEXTS], metadata=[m for _, m in TEXTS])
        docs._collection._cache = None
        asked, crossed = spy_on_searches(monkeypatch)
        seen = {}
        for rerank in (None, "semantic", False, "cross-encoder"):
            asked.clear()
            crossed.clear()
            docs.search("sourdough starter", limit=2, rerank=rerank)
            seen[rerank] = ("semantic" in asked, list(crossed))
        assert seen == {
            None: (True, [False]),  # the store was opened with the ranker, so that is the default
            "semantic": (True, [False]),
            False: (False, [False]),
            "cross-encoder": (False, [True]),
        }

    def test_keyword_search_on_a_fresh_handle_is_the_services_own(self, azure, tmp_path, monkeypatch):
        from vectrixdb import Vectrix
        from vectrixdb.core.database import VectrixDB

        db, _ = azure
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        writer = Vectrix("docs", storage_backend=db, path=str(tmp_path / "a"), mode="hybrid")
        writer.add([t for t, _ in TEXTS], metadata=[m for _, m in TEXTS])
        again = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k", semantic=True)
        reader = Vectrix("docs", storage_backend=again, path=str(tmp_path / "b"), mode="hybrid")
        writer._collection._cache = None
        reader._collection._cache = None
        try:
            asked, _ = spy_on_searches(monkeypatch)
            hits = reader.search("sourdough", limit=3, mode="sparse")
            assert reader.count() == 0, "nothing here: the words index on this machine is empty"
            assert hits and all("sourdough" in h.text.lower() for h in hits)
            assert asked and "semantic" not in asked, "sent to the service, and not reranked: a keyword search asks for that"
            asked.clear()
            here = writer.search("sourdough", limit=3, mode="sparse")
            assert asked == [] and {h.id for h in here} == {h.id for h in hits}, "the words index here answers the same"
            ranked = reader.search("sourdough", limit=3, mode="sparse", rerank="semantic")
            assert ranked and "semantic" in asked
        finally:
            reader.close()
            writer.close()

    def test_the_ranker_is_asked_for_only_where_it_can_run(self, azure, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.exceptions import ConfigurationError

        db, _ = azure
        (tmp_path / "azure").mkdir()
        docs = Vectrix("docs", storage_backend=db, path=str(tmp_path / "azure"), mode="hybrid")
        docs.add([t for t, _ in TEXTS], metadata=[m for _, m in TEXTS])
        local = Vectrix("docs", path=str(tmp_path / "local"), mode="hybrid")
        local.add([t for t, _ in TEXTS])
        try:
            with pytest.raises(ConfigurationError, match="reads the words"):
                docs.search("sourdough", mode="dense", rerank="semantic")
            with pytest.raises(ConfigurationError, match="not stored on Azure AI Search"):
                local.search("sourdough", mode="hybrid", rerank="semantic")
        finally:
            local.close()
            docs.close()

    @pytest.mark.parametrize("azure", [(False, None)], indirect=True)
    def test_an_index_with_no_configuration_says_how_to_add_one(self, azure, tmp_path):
        from vectrixdb import Vectrix
        from vectrixdb.exceptions import ConfigurationError

        db, _ = azure
        docs = Vectrix("docs", storage_backend=db, path=str(tmp_path), mode="hybrid")
        docs.add([t for t, _ in TEXTS], metadata=[m for _, m in TEXTS])
        with pytest.raises(ConfigurationError, match="semantic=True"):
            docs.search("sourdough", mode="hybrid", rerank="semantic")
