"""Whose dense vectors an Azure AI Search index holds, and which of them answer
a search: the collection's own, an Azure OpenAI deployment's, or both in one
index, fused by the service.

Both embedders here are lookup tables, built so the two disagree about the
same three documents. That is the point of having two: if they always agreed
there would be nothing to test, and nothing to gain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("azure.search.documents")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_azure_search import FakeIndexClient, FakeSearchClient  # noqa: E402

from vectrixdb.core.storage import StorageBackend, StorageConfig  # noqa: E402
from vectrixdb.core.storage_azure import F_VECTOR, F_VECTOR_AZURE, AzureSearchStorage  # noqa: E402
from vectrixdb.exceptions import ConfigurationError, DependencyError  # noqa: E402

A = "Alpha memo about the covenant schedule."
B = "Beta memo about the repayment holiday."
C = "Gamma memo about both of them."
QUESTION = "which memo"

OURS = {A: [1, 0, 0, 0], B: [0, 0, 1, 0], C: [0.8, 0.6, 0, 0], QUESTION: [1, 0, 0, 0]}
THEIRS = {
    A: [0, 0, 1, 0, 0, 0],
    B: [0, 1, 0, 0, 0, 0],
    C: [0, 0.8, 0.6, 0, 0, 0],
    QUESTION: [0, 1, 0, 0, 0, 0],
}
SPEC = {
    "endpoint": "https://aoai.example.test",
    "deployment": "text-embedding-3-large",
    "dimensions": 6,
}


def ours(texts):
    return np.asarray([OURS.get(t, [0, 0, 0, 1]) for t in texts], dtype=np.float32)


class Theirs:
    """The deployment, counted."""

    def __init__(self):
        self.calls = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        return [THEIRS.get(t, [0, 0, 0, 0, 0, 1]) for t in texts]


def build(
    monkeypatch,
    tmp_path,
    embeddings,
    *,
    mode="dense",
    weights=None,
    vectorizer=True,
    fields=None,
    policy=None,
    embed_fn=ours,
):
    from vectrixdb import Vectrix
    from vectrixdb.core.database import VectrixDB

    theirs = Theirs()
    fake = FakeIndexClient(
        vectorize=(lambda text: THEIRS.get(text, [0, 0, 0, 0, 0, 1])) if vectorizer else None
    )
    spec = SPEC if vectorizer else {"dimensions": 6}
    storage = AzureSearchStorage(
        StorageConfig(
            backend=StorageBackend.AZURE_SEARCH,
            azure_search_index_prefix="t",
            azure_search_embeddings=embeddings,
            azure_search_vectorizer=spec if embeddings != "vectrixdb" else None,
            azure_search_embed_fn=theirs if embeddings != "vectrixdb" else None,
            azure_search_vector_weights=weights,
            azure_search_filter_fields=fields,
        ),
        index_client=fake,
        client_factory=fake.get_search_client,
    )
    storage.connect()
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: storage)
    backend = VectrixDB.with_azure_search("https://svc.search.windows.net", key="k")
    options = {"embed_fn": embed_fn, "dimension": 4} if embed_fn is not None else {}
    docs = Vectrix(
        "memos", storage_backend=backend, path=str(tmp_path), mode=mode, policy=policy, **options
    )
    return docs, storage, fake, theirs


def config(**overrides):
    base = {"backend": StorageBackend.AZURE_SEARCH, "azure_search_index_prefix": "t"}
    return StorageConfig(**{**base, **overrides})


class TestConfiguration:
    @pytest.mark.parametrize(
        "overrides, message",
        [
            ({"azure_search_embeddings": "openai"}, "embeddings is one of vectrixdb, azure, both"),
            (
                {"azure_search_embeddings": "both"},
                "needs azure_embedding with the deployment's dimensions",
            ),
            (
                {"azure_search_embeddings": "both", "azure_search_vectorizer": {"dimensions": 6}},
                "endpoint and deployment, or an embed_fn",
            ),
            ({"azure_search_vector_weights": {"azure": 0}}, "positive numbers"),
            ({"azure_search_vector_weights": {"cohere": 1.0}}, "positive numbers"),
        ],
    )
    def test_what_is_refused_at_construction(self, overrides, message):
        with pytest.raises(ConfigurationError, match=message):
            AzureSearchStorage(config(**overrides), index_client=FakeIndexClient())

    @pytest.mark.parametrize("embeddings", ["azure", "both"])
    def test_a_deployment_without_the_openai_package_is_refused_when_it_opens(
        self, monkeypatch, embeddings
    ):
        """Not at the first write, which comes after every document has been read and cut."""
        monkeypatch.setitem(sys.modules, "openai", None)
        with pytest.raises(DependencyError, match=r"pip install vectrixdb\[azure\]"):
            AzureSearchStorage(
                config(azure_search_embeddings=embeddings, azure_search_vectorizer=SPEC),
                index_client=FakeIndexClient(),
            )

    def test_an_embedder_of_its_own_or_none_needs_no_openai_package(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "openai", None)
        AzureSearchStorage(
            config(
                azure_search_embeddings="both",
                azure_search_vectorizer=SPEC,
                azure_search_embed_fn=Theirs(),
            ),
            index_client=FakeIndexClient(),
        )
        AzureSearchStorage(config(), index_client=FakeIndexClient())

    def test_the_index_holds_what_was_asked_for(self, monkeypatch, tmp_path):
        for embeddings, expected in (
            ("vectrixdb", [F_VECTOR]),
            ("azure", [F_VECTOR]),
            ("both", [F_VECTOR, F_VECTOR_AZURE]),
        ):
            (tmp_path / embeddings).mkdir()
            docs, storage, fake, _ = build(
                monkeypatch,
                tmp_path / embeddings,
                embeddings,
                embed_fn=ours if embeddings != "azure" else None,
            )
            docs.add([A])
            index = fake.get_index("t-memos")
            vectors = {f.name: f for f in index.fields if f.name.startswith("dense_")}
            assert (
                sorted(vectors) == sorted(expected)
                and storage.vector_names()
                == {
                    "vectrixdb": ("vectrixdb",),
                    "azure": ("azure",),
                    "both": ("vectrixdb", "azure"),
                }[embeddings]
            )
            assert bool(index.vectorizers) is (embeddings != "vectrixdb")
            if embeddings == "both":
                assert (
                    vectors[F_VECTOR].vector_search_dimensions == 4
                    and vectors[F_VECTOR_AZURE].vector_search_dimensions == 6
                )
            docs.close()


class TestBoth:
    def test_every_chunk_carries_both_vectors_and_the_deployment_is_asked_once(
        self, monkeypatch, tmp_path
    ):
        docs, _, fake, theirs = build(monkeypatch, tmp_path, "both")
        docs.add([A, B, C], ids=["a", "b", "c"])
        rows = {d["doc_id"]: d for d in fake.get_index("t-memos").docs.values()}
        assert rows["a"][F_VECTOR] == [1.0, 0.0, 0.0, 0.0] and rows["a"][F_VECTOR_AZURE] == [
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
        ]
        assert theirs.calls == [[A, B, C]]
        docs.close()

    def test_one_index_answers_three_ways(self, monkeypatch, tmp_path):
        docs, _, fake, theirs = build(monkeypatch, tmp_path, "both")
        docs.add([A, B, C], ids=["a", "b", "c"])
        theirs.calls.clear()
        assert docs.search(QUESTION, limit=1, vectors="vectrixdb").top.id == "a"
        assert fake.vectorized == [], "the deployment's vector was not asked"
        assert docs.search(QUESTION, limit=1, vectors="azure").top.id == "b"
        assert fake.vectorized == [QUESTION], (
            "the question went up as words, for the service to embed"
        )
        assert theirs.calls == [], "and was not embedded here"
        fused = [h.id for h in docs.search(QUESTION, limit=3)]
        # By rank fusion, first for one and last for the other narrowly beats
        # second for both: 1/61 + 1/63 against 2/62. So c is last, and a and b
        # tie ahead of it.
        assert set(fused[:2]) == {"a", "b"} and fused[2] == "c"
        assert [h.id for h in docs.search(QUESTION, limit=3, vectors="both")] == fused
        docs.close()

    def test_weights_move_the_fusion(self, monkeypatch, tmp_path):
        docs, _, _, _ = build(
            monkeypatch, tmp_path, "both", weights={"vectrixdb": 1.0, "azure": 5.0}
        )
        docs.add([A, B, C], ids=["a", "b", "c"])
        assert docs.search(QUESTION, limit=1).top.id == "b"
        docs.close()

    def test_without_a_vectorizer_the_question_is_embedded_here(self, monkeypatch, tmp_path):
        docs, _, fake, theirs = build(monkeypatch, tmp_path, "both", vectorizer=False)
        docs.add([A, B, C], ids=["a", "b", "c"])
        theirs.calls.clear()
        assert docs.search(QUESTION, limit=1, vectors="azure").top.id == "b"
        assert theirs.calls == [[QUESTION]] and fake.vectorized == []
        docs.close()

    def test_explain_says_which_vector_ranked_what(self, monkeypatch, tmp_path):
        docs, _, _, _ = build(monkeypatch, tmp_path, "both", mode="hybrid")
        docs.add([A, B, C], ids=["a", "b", "c"])
        hits = docs.search(QUESTION + " memo", limit=3, explain=True, rerank=False)
        ranks = {h.id: h.explain["vector_ranks"] for h in hits}
        assert ranks["c"] == {"vectrixdb": 2, "azure": 2} or set(ranks["c"]) == {
            "vectrixdb",
            "azure",
        }
        assert set(ranks["a"]) == {"vectrixdb", "azure"}
        docs.close()

    def test_the_second_vector_goes_through_the_embedding_cache(self, monkeypatch, tmp_path):
        docs, _, _, theirs = build(monkeypatch, tmp_path, "both")

        class Cache:
            def __init__(self):
                self.store, self.labels = {}, set()

            def get_many(self, label, texts):
                self.labels.add(label)
                return [self.store.get((label, t)) for t in texts]

            def put_many(self, label, texts, vectors):
                for t, v in zip(texts, vectors):
                    self.store[(label, t)] = np.asarray(v, dtype=np.float32)

            def close(self):
                pass

        docs._embed_cache = Cache()
        docs.add([A, B], ids=["a", "b"])
        docs.add([A, C], ids=["a2", "c"])
        assert [sorted(c) for c in theirs.calls] == [sorted([A, B]), [C]], "A was embedded once"
        assert "azure-openai:text-embedding-3-large" in docs._embed_cache.labels
        docs.close()

    def test_a_deployment_of_the_wrong_width_is_refused(self, monkeypatch, tmp_path):
        docs, storage, _, _ = build(monkeypatch, tmp_path, "both")
        storage._azure_embedder = lambda texts: [[0.0] * 5 for _ in texts]
        with pytest.raises(
            Exception, match="returned 5 dimensions and the index field was made for 6"
        ):
            docs.add([A])
        docs.close()

    def test_a_write_that_did_not_come_through_vectrix_is_embedded_at_insert(
        self, monkeypatch, tmp_path
    ):
        _, storage, fake, theirs = build(monkeypatch, tmp_path, "both")
        storage.insert_batch(
            "memos", [("direct", {"text_content": B, "dense_embedding": [0, 0, 1, 0]})]
        )
        assert theirs.calls == [[B]]
        assert fake.get_index("t-memos").docs and any(
            d.get(F_VECTOR_AZURE) == [0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
            for d in fake.get_index("t-memos").docs.values()
        )

    def test_a_policy_still_goes_with_every_vector(self, monkeypatch, tmp_path):
        from vectrixdb.policy import Overlap, Policy

        seen = []
        original = FakeSearchClient.search
        monkeypatch.setattr(
            FakeSearchClient,
            "search",
            lambda self, *a, **k: (
                seen.append((k.get("filter"), len(k.get("vector_queries") or []))),
                original(self, *a, **k),
            )[1],
        )
        policy = Policy([Overlap("client_id", "clients", scope=True)], require_pushdown=True)
        docs, _, _, _ = build(
            monkeypatch, tmp_path, "both", fields={"client_id": "string"}, policy=policy
        )
        docs.add(
            [A, B, C],
            ids=["a", "b", "c"],
            metadata=[{"client_id": "acme"}, {"client_id": "zeta"}, {"client_id": "acme"}],
        )
        hits = docs.as_principal({"clients": ["acme"]}).search(QUESTION, limit=3)
        assert {h.id for h in hits} == {"a", "c"}
        filtered = [s for s in seen if s[1] == 2]
        assert filtered and all("f_client_id" in (f or "") for f, _ in filtered), (
            "one request, two vectors, one filter"
        )
        docs.close()


class TestAzureOnly:
    def test_the_collections_embedder_is_the_deployment(self, monkeypatch, tmp_path):
        docs, _, fake, theirs = build(monkeypatch, tmp_path, "azure", embed_fn=None)
        assert docs.dimension == 6 and docs.model_name == "azure-openai:text-embedding-3-large"
        docs.add([A, B, C], ids=["a", "b", "c"])
        rows = {d["doc_id"]: d for d in fake.get_index("t-memos").docs.values()}
        assert (
            rows["b"][F_VECTOR] == [0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
            and F_VECTOR_AZURE not in rows["b"]
        )
        assert docs.search(QUESTION, limit=1).top.id == "b"
        assert fake.vectorized == [QUESTION]
        docs.close()


class TestVectrixdbOnly:
    def test_asking_for_a_vector_the_index_does_not_hold(self, monkeypatch, tmp_path):
        docs, _, _, _ = build(monkeypatch, tmp_path, "vectrixdb")
        docs.add([A, B], ids=["a", "b"])
        assert docs.search(QUESTION, limit=1).top.id == "a"
        assert docs.search(QUESTION, limit=1, vectors="vectrixdb").top.id == "a"
        with pytest.raises(ConfigurationError, match="vectors='azure' is not in this index"):
            docs.search(QUESTION, vectors="azure")
        with pytest.raises(ConfigurationError, match="needs an index built with embeddings='both'"):
            docs.search(QUESTION, vectors="both")
        docs.close()

    def test_a_local_collection_has_the_one_vector(self, tmp_path):
        from vectrixdb import Vectrix

        docs = Vectrix("plain", path=str(tmp_path), embed_fn=ours, dimension=4)
        try:
            docs.add([A])
            with pytest.raises(ConfigurationError, match="this collection has the one"):
                docs.search(QUESTION, vectors="azure")
        finally:
            docs.close()
