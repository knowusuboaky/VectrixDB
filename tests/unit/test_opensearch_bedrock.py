"""Bedrock beside OpenSearch: a Bedrock embedding model as the collection's
vectors, or a second vector next to the collection's own, and Bedrock rerank
as the rerank stage. Every Bedrock client here is a fake.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_opensearch import FakeOpenSearch  # noqa: E402

from vectrixdb.core.storage import OpenSearchStorage, StorageBackend, StorageConfig  # noqa: E402
from vectrixdb.exceptions import ConfigurationError, ModelError  # noqa: E402
from vectrixdb.models.bedrock import BedrockEmbedder, BedrockReranker  # noqa: E402

A = "Alpha memo about the covenant schedule."
B = "Beta memo about the repayment holiday."
C = "Gamma memo about both of them."
QUESTION = "which memo"

OURS = {A: [1, 0, 0, 0], B: [0, 0, 1, 0], C: [0.8, 0.6, 0, 0], QUESTION: [1, 0, 0, 0]}
TITAN = {
    A: [0, 0, 1, 0, 0, 0],
    B: [0, 1, 0, 0, 0, 0],
    C: [0, 0.8, 0.6, 0, 0, 0],
    QUESTION: [0, 1, 0, 0, 0, 0],
}


def ours(texts):
    return np.asarray([OURS.get(t, [0, 0, 0, 1]) for t in texts], dtype=np.float32)


class FakeBedrockRuntime:
    def __init__(self, table=TITAN, width=6):
        self.table, self.width, self.calls = table, width, []

    def invoke_model(self, modelId, body, accept, contentType):
        request = json.loads(body)
        self.calls.append((modelId, request))
        vector = self.table.get(request["inputText"], [0] * (self.width - 1) + [1])
        return {"body": io.BytesIO(json.dumps({"embedding": vector}).encode())}


class TestBedrockEmbedder:
    def test_one_call_a_text_with_the_width_asked_for(self):
        client = FakeBedrockRuntime()
        embed = BedrockEmbedder(client, dimensions=6, concurrency=2)
        vectors = embed([A, B])
        assert vectors.shape == (2, 6) and vectors.dtype == np.float32
        assert sorted(c[1]["inputText"] for c in client.calls) == sorted([A, B])
        assert client.calls[0][1]["dimensions"] == 6 and client.calls[0][1]["normalize"] is True
        assert embed.label == "bedrock:amazon.titan-embed-text-v2:0" and embed([]).shape == (0, 6)

    def test_a_failure_names_the_model(self):
        class Broken:
            def invoke_model(self, **kwargs):
                raise RuntimeError("AccessDeniedException")

        with pytest.raises(
            ModelError, match="could not embed with amazon.titan-embed-text-v2:0: AccessDenied"
        ):
            BedrockEmbedder(Broken())(["x"])


class TestBedrockReranker:
    def test_scores_come_back_as_an_order(self):
        seen = {}

        class Client:
            def rerank(self, queries, sources, rerankingConfiguration):
                seen.update(
                    query=queries[0]["textQuery"]["text"],
                    n=len(sources),
                    config=rerankingConfiguration,
                )
                return {
                    "results": [
                        {"index": 2, "relevanceScore": 0.91},
                        {"index": 0, "relevanceScore": 0.40},
                        {"index": 1, "relevanceScore": 0.05},
                    ]
                }

        ranked = BedrockReranker(Client(), region="ca-central-1").rerank(
            "covenant", [A, B, C], top=2
        )
        assert ranked == [(2, 0.91), (0, 0.40)]
        assert seen["query"] == "covenant" and seen["n"] == 3
        arn = seen["config"]["bedrockRerankingConfiguration"]["modelConfiguration"]["modelArn"]
        assert arn == "arn:aws:bedrock:ca-central-1::foundation-model/amazon.rerank-v1:0"

    def test_a_long_list_goes_up_in_slices(self):
        calls = []

        class Client:
            def rerank(self, queries, sources, rerankingConfiguration):
                calls.append(len(sources))
                return {
                    "results": [
                        {"index": i, "relevanceScore": 1.0 / (i + 1 + len(calls))}
                        for i in range(len(sources))
                    ]
                }

        ranked = BedrockReranker(Client()).rerank("q", [f"text {i}" for i in range(250)])
        assert calls == [100, 100, 50] and len(ranked) == 250 and len({i for i, _ in ranked}) == 250


def build(
    monkeypatch, tmp_path, embeddings, *, weights=None, embed_fn=ours, reranker=None, mode="dense"
):
    from vectrixdb import Vectrix
    from vectrixdb.core.database import VectrixDB

    runtime = FakeBedrockRuntime()
    titan = BedrockEmbedder(runtime, dimensions=6, concurrency=1)
    client = FakeOpenSearch()
    store = OpenSearchStorage(
        StorageConfig(
            backend=StorageBackend.OPENSEARCH,
            opensearch_index_prefix="t",
            opensearch_embeddings=embeddings,
            opensearch_embed_fn=titan if embeddings != "vectrixdb" else None,
            opensearch_vector_weights=weights,
        ),
        client=client,
    )
    monkeypatch.setattr("vectrixdb.core.database.create_storage", lambda config: store)
    backend = VectrixDB.with_opensearch("https://x.us-east-1.aoss.amazonaws.com")
    options = {"embed_fn": embed_fn, "dimension": 4} if embed_fn is not None else {}
    docs = Vectrix(
        "memos",
        storage_backend=backend,
        path=str(tmp_path),
        mode=mode,
        reranker=reranker,
        **options,
    )
    return docs, client, runtime


class TestEmbeddings:
    def test_what_is_refused(self):
        with pytest.raises(
            ConfigurationError, match="embeddings is one of vectrixdb, bedrock, both"
        ):
            OpenSearchStorage(
                StorageConfig(backend=StorageBackend.OPENSEARCH, opensearch_embeddings="titan"),
                client=FakeOpenSearch(),
            )
        with pytest.raises(ConfigurationError, match="needs embed_fn"):
            OpenSearchStorage(
                StorageConfig(backend=StorageBackend.OPENSEARCH, opensearch_embeddings="both"),
                client=FakeOpenSearch(),
            )
        with pytest.raises(ConfigurationError, match="needs the Bedrock model's dimensions"):
            OpenSearchStorage(
                StorageConfig(
                    backend=StorageBackend.OPENSEARCH,
                    opensearch_embeddings="both",
                    opensearch_embed_fn=ours,
                ),
                client=FakeOpenSearch(),
            )

    def test_both_one_index_three_answers(self, monkeypatch, tmp_path):
        docs, client, runtime = build(monkeypatch, tmp_path, "both")
        docs.add([A, B, C], ids=["a", "b", "c"])
        stored = {d["id"]: d for d in client._docs("t_memos").values()}
        assert stored["a"]["dense_embedding"] == [1.0, 0.0, 0.0, 0.0] and stored["a"][
            "dense_bedrock"
        ] == [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        assert "named_vectors" not in stored["a"]["metadata"]
        assert len(runtime.calls) == 3, "each chunk embedded by Titan once"

        assert docs.search(QUESTION, limit=1, vectors="vectrixdb").top.id == "a"
        assert docs.search(QUESTION, limit=1, vectors="bedrock").top.id == "b"
        assert runtime.calls[-1][1]["inputText"] == QUESTION, (
            "the question went to Titan for the Bedrock vector"
        )
        fused = [h.id for h in docs.search(QUESTION, limit=3)]
        assert set(fused[:2]) == {"a", "b"} and fused[2] == "c"
        docs.close()

    def test_weights_and_explain(self, monkeypatch, tmp_path):
        docs, _, _ = build(
            monkeypatch, tmp_path, "both", weights={"vectrixdb": 1.0, "bedrock": 5.0}, mode="hybrid"
        )
        docs.add([A, B, C], ids=["a", "b", "c"])
        hits = docs.search(QUESTION, limit=3, explain=True, rerank=False)
        assert hits.top.id == "b"
        assert hits.top.explain["vector_ranks"] == {"vectrixdb": 3, "bedrock": 1}
        docs.close()

    def test_bedrock_alone_is_the_collections_embedder(self, monkeypatch, tmp_path):
        docs, client, _ = build(monkeypatch, tmp_path, "bedrock", embed_fn=None)
        assert docs.dimension == 6 and docs.model_name == "bedrock:amazon.titan-embed-text-v2:0"
        docs.add([A, B], ids=["a", "b"])
        stored = {d["id"]: d for d in client._docs("t_memos").values()}
        assert (
            stored["b"]["dense_embedding"] == [0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
            and "dense_bedrock" not in stored["b"]
        )
        assert docs.search(QUESTION, limit=1).top.id == "b"
        docs.close()

    def test_asking_for_a_vector_the_index_does_not_hold(self, monkeypatch, tmp_path):
        docs, _, _ = build(monkeypatch, tmp_path, "vectrixdb")
        docs.add([A], ids=["a"])
        with pytest.raises(ConfigurationError, match="vectors='bedrock' is not in this index"):
            docs.search(QUESTION, vectors="bedrock")
        docs.close()


class TestRerankStage:
    def test_a_reranker_you_bring_takes_the_cross_encoders_place(self, monkeypatch, tmp_path):
        class Reranker:
            label = "bedrock-rerank"

            def __init__(self):
                self.calls = []

            def rerank(self, query, texts, top=None):
                self.calls.append((query, list(texts), top))
                order = sorted(range(len(texts)), key=lambda i: texts[i], reverse=True)
                return [(i, 1.0 - n / 10) for n, i in enumerate(order)][:top]

        reranker = Reranker()
        docs, _, _ = build(monkeypatch, tmp_path, "vectrixdb", reranker=reranker, mode="hybrid")
        docs.add([A, B, C], ids=["a", "b", "c"])
        hits = docs.search("memo about", limit=2, explain=True)
        assert [h.id for h in hits] == ["c", "b"], (
            "Gamma then Beta: the reranker's order, not the fused one"
        )
        assert reranker.calls and reranker.calls[0][0] == "memo about"
        assert (
            hits.top.explain["reranker"] == "bedrock-rerank" and hits.top.explain["rerank"] == 1.0
        )
        docs.close()

    def test_a_reranker_that_fails_keeps_the_fused_order(self, monkeypatch, tmp_path):
        class Broken:
            def rerank(self, query, texts, top=None):
                raise ModelError("throttled")

        docs, _, _ = build(monkeypatch, tmp_path, "vectrixdb", reranker=Broken(), mode="hybrid")
        docs.add([A, B, C], ids=["a", "b", "c"])
        assert len(docs.search("memo about", limit=3)) == 3
        docs.close()

    def test_what_a_reranker_is(self, tmp_path):
        from vectrixdb import Vectrix

        with pytest.raises(TypeError, match="rerank\\(query, texts, top=None\\)"):
            Vectrix("x", path=str(tmp_path), embed_fn=ours, dimension=4, reranker=object())
