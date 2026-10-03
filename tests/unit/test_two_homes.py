"""A collection kept on a store has two homes: the service, and the index beside this process.

Every write already reached both, under the same ids. ``homes=`` lets a search
say which to ask: the store and nothing else, the local index and nothing
else, or both fused by rank, with the local index answering alone, and saying
so, when the service does not answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("azure.search.documents")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_azure_search import FakeIndexClient  # noqa: E402

from vectrixdb.core.storage import StorageBackend, StorageConfig  # noqa: E402
from vectrixdb.core.storage_azure import AzureSearchStorage  # noqa: E402
from vectrixdb.exceptions import ConfigurationError  # noqa: E402

WORDS = {"alpha": 0, "beta": 1, "gamma": 2, "delta": 3}


def embed(texts):
    out = np.zeros((len(texts), 4), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().split():
            if word in WORDS:
                out[i, WORDS[word]] += 1.0
        if not out[i].any():
            out[i] = [0.5, 0.5, 0.5, 0.5]
    return out


TEXTS = {
    "a": "alpha alpha alpha",
    "b": "alpha alpha beta",
    "c": "alpha beta beta",
    "d": "alpha gamma gamma gamma",
}


def opened(monkeypatch, tmp_path, **options):
    from vectrixdb import Vectrix
    from vectrixdb.core.database import VectrixDB

    fake = FakeIndexClient()
    storage = AzureSearchStorage(
        StorageConfig(
            backend=StorageBackend.AZURE_SEARCH,
            azure_search_index_prefix="t",
            azure_search_filter_fields={"client_id": "string"},
        ),
        index_client=fake,
        client_factory=fake.get_search_client,
    )
    storage.connect()
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: storage)
    backend = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k")
    docs = Vectrix(
        "memos",
        storage_backend=backend,
        path=str(tmp_path),
        embed_fn=embed,
        dimension=4,
        **{"mode": "hybrid", **options},
    )
    return docs, fake, storage


@pytest.fixture
def homes(monkeypatch, tmp_path):
    docs, fake, storage = opened(monkeypatch, tmp_path)
    docs.add(list(TEXTS.values()), ids=list(TEXTS), metadata=[{"client_id": "acme"} for _ in TEXTS])
    yield docs, fake, storage
    docs.close()


def only_in_the_store(docs, storage, monkeypatch, doc_id):
    """Gone from the index here, still in the service: what a second writer's delete looks like from this process."""
    with monkeypatch.context() as patched:
        patched.setattr(storage, "delete_batch", lambda collection, ids: 0)
        docs.delete(doc_id)


def only_here(fake, doc_id):
    index = fake.get_index("t-memos")
    for key in [k for k, row in index.docs.items() if row["doc_id"] == doc_id]:
        del index.docs[key]


def unreachable(storage, monkeypatch):
    def down(*args, **kwargs):
        raise ConnectionError("the service did not answer")

    for name in ("vector_search", "text_search", "hybrid_search"):
        monkeypatch.setattr(storage, name, down)


class TestOneHome:
    def test_each_home_answers_from_what_it_holds(self, homes, monkeypatch):
        docs, fake, storage = homes
        only_in_the_store(docs, storage, monkeypatch, "a")
        only_here(fake, "b")
        assert [h.id for h in docs.search("alpha", limit=4, mode="dense", homes="store")] == [
            "a",
            "c",
            "d",
        ]
        assert [h.id for h in docs.search("alpha", limit=4, mode="dense", homes="local")] == [
            "b",
            "c",
            "d",
        ]

    def test_the_local_home_never_calls_the_service(self, homes, monkeypatch):
        docs, _, storage = homes
        unreachable(storage, monkeypatch)
        for mode in ("dense", "sparse", "hybrid"):
            found = docs.search("alpha", limit=3, mode=mode, homes="local", rerank=False)
            assert found.items and found.degraded is None, mode

    def test_the_store_asked_for_by_name_raises_when_it_does_not_answer(self, homes, monkeypatch):
        """No answer from somewhere else in its place: the caller said where to ask."""
        docs, _, storage = homes
        unreachable(storage, monkeypatch)
        for mode in ("dense", "sparse", "hybrid"):
            with pytest.raises(Exception, match="did not answer"):
                docs.search("alpha", limit=3, mode=mode, homes="store", rerank=False)

    def test_left_out_nothing_changes(self, homes):
        docs, _, _ = homes
        assert [h.id for h in docs.search("alpha", limit=4)] == [
            h.id for h in docs.search("alpha", limit=4, homes="local")
        ]


class TestBothHomes:
    def test_the_two_lists_are_fused_by_rank(self, homes, monkeypatch):
        docs, fake, storage = homes
        only_in_the_store(docs, storage, monkeypatch, "a")
        only_here(fake, "b")
        found = docs.search("alpha", limit=4, mode="dense", homes="both", explain=True)
        assert {h.id for h in found} == {"a", "b", "c", "d"}, "what either home holds"
        ranks = {h.id: h.explain["home_ranks"] for h in found}
        assert ranks["a"] == {"store": 1} and ranks["b"] == {"local": 1}
        assert set(ranks["c"]) == {"store", "local"} and "did not answer" not in (
            found.degraded or ""
        )
        by_id = {h.id: h.score for h in found}
        assert by_id["c"] > by_id["a"], "found by both, second in each, beats first in one"
        assert [h.score for h in found] == sorted((h.score for h in found), reverse=True)

    def test_a_result_keeps_its_text_metadata_and_relevance(self, homes):
        docs, _, _ = homes
        top = docs.search("alpha", limit=1, homes="both").items[0]
        assert (
            top.id == "a"
            and top.text == TEXTS["a"]
            and top.metadata["client_id"] == "acme"
            and top.relevance is not None
        )

    def test_when_the_service_does_not_answer_the_local_index_does_and_says_so(
        self, homes, monkeypatch
    ):
        docs, _, storage = homes
        unreachable(storage, monkeypatch)
        for mode in ("dense", "sparse", "hybrid"):
            found = docs.search("alpha", limit=3, mode=mode, homes="both", rerank=False)
            assert [h.id for h in found][:1] == ["a"], mode
            assert (
                "the store did not answer (ConnectionError)" in found.degraded
                and "local index alone" in found.degraded
            )

    def test_the_limit_the_filter_and_the_budget_still_apply(self, homes):
        docs, _, _ = homes
        assert len(docs.search("alpha", limit=2, homes="both")) == 2
        assert docs.search("alpha", limit=4, homes="both", filter={"client_id": "zeta"}).items == []
        cut = docs.search("alpha", limit=4, homes="both", token_budget=6)
        assert cut.truncated and len(cut) < 4

    def test_a_delete_reaches_both_so_neither_home_brings_it_back(self, homes):
        docs, _, _ = homes
        docs.delete("a")
        for where in ("store", "local", "both"):
            assert "a" not in [h.id for h in docs.search("alpha", limit=4, homes=where)], where


class TestWhatIsRefused:
    def test_a_collection_with_no_store_has_one_home(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("plain", path=str(tmp_path), embed_fn=embed, dimension=4)
        db.add(["alpha"], ids=["a"])
        with pytest.raises(ConfigurationError, match="not kept on a store"):
            db.search("alpha", homes="both")
        db.close()

    def test_a_word_that_is_not_a_home(self, homes):
        with pytest.raises(ConfigurationError, match="'store', 'local' or 'both'"):
            homes[0].search("alpha", homes="azure")

    def test_a_mistake_in_the_call_is_not_taken_for_the_service_being_away(
        self, monkeypatch, tmp_path
    ):
        dense, _, _ = opened(monkeypatch, tmp_path, mode="dense")
        dense.add(["alpha"], ids=["a"])
        with pytest.raises(ValueError, match="Cannot use 'hybrid' mode"):
            dense.search("alpha", mode="hybrid", homes="both")
        dense.close()

    def test_modes_that_live_in_one_home(self, homes):
        with pytest.raises(ConfigurationError, match="one home to ask"):
            homes[0].search("alpha", mode="ultimate", homes="both")

    def test_not_under_an_entitlement_policy(self, monkeypatch, tmp_path):
        from vectrixdb.policy import Overlap, Policy

        docs, _, _ = opened(
            monkeypatch, tmp_path, policy=Policy([Overlap("client_id", "clients", scope=True)])
        )
        docs.add(["alpha memo"], ids=["a"], metadata=[{"client_id": "acme"}])
        with pytest.raises(ConfigurationError, match="not offered under one"):
            docs.as_principal({"clients": ["acme"]}).search("alpha", homes="both")
        assert [h.id for h in docs.as_principal({"clients": ["acme"]}).search("alpha")] == ["a"], (
            "and the usual search is untouched"
        )
        docs.close()
