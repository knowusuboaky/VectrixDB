"""Embeddings and reranking from Amazon Bedrock, through a client the host built.

    import boto3
    from vectrixdb.models.bedrock import BedrockEmbedder, BedrockReranker

    embed = BedrockEmbedder(boto3.client("bedrock-runtime"), dimensions=1024)
    rerank = BedrockReranker(boto3.client("bedrock-agent-runtime"), region="us-east-1")

Both take the client rather than credentials, so keys, roles and regions
stay the host's business and a test hands in a fake. ``BedrockEmbedder`` is
callable ``texts -> np.ndarray``, which is what ``Vectrix(embed_fn=...)`` and
``with_opensearch(embed_fn=...)`` want.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from ..exceptions import ModelError

__all__ = ["BedrockEmbedder", "BedrockReranker"]


# ============================================================================
# THE EMBEDDER AND THE RERANKER
# ============================================================================
#
# INPUT   texts, or a question and some texts, through a boto3 client the host
#         built
# OUTPUT  Titan Text Embeddings, or any Bedrock model that answers the same
#         way; the texts' order back from the rerank API
#
# The client is the host's, so credentials and the region are never the
# library's business.


class BedrockEmbedder:
    """Titan Text Embeddings, or any Bedrock model that answers the same way.

    Titan takes one text a call, so a batch is a call per text, a few at a
    time. ``dimensions`` is what the model is asked for, 1024, 512 or 256 for
    Titan v2, and the index field has to be made for the same number: a
    mismatch is refused where the vector is written, not discovered later.
    """

    def __init__(
        self,
        client: Any,
        model_id: str = "amazon.titan-embed-text-v2:0",
        dimensions: Optional[int] = 1024,
        normalize: bool = True,
        concurrency: int = 4,
    ) -> None:
        self.client = client
        self.model_id = model_id
        self.dimensions = dimensions
        self.normalize = normalize
        self.concurrency = max(int(concurrency), 1)
        self.label = "bedrock:" + model_id

    def _one(self, text: str) -> List[float]:
        body: dict = {"inputText": text}
        if self.dimensions and "titan-embed-text-v2" in self.model_id:
            body["dimensions"] = int(self.dimensions)
            body["normalize"] = bool(self.normalize)
        try:
            reply = self.client.invoke_model(
                modelId=self.model_id,
                body=json.dumps(body),
                accept="application/json",
                contentType="application/json",
            )
            payload = reply["body"]
            parsed = json.loads(payload.read() if hasattr(payload, "read") else payload)
        except Exception as exc:
            raise ModelError(f"Bedrock could not embed with {self.model_id}: {exc}") from exc
        vector = parsed.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ModelError(f"Bedrock's reply for {self.model_id} carried no embedding")
        return [float(x) for x in vector]

    def __call__(self, texts: Sequence[str]) -> np.ndarray:
        items = [str(t) for t in texts]
        if not items:
            return np.zeros((0, int(self.dimensions or 0)), dtype=np.float32)
        if len(items) == 1 or self.concurrency == 1:
            vectors = [self._one(t) for t in items]
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                vectors = list(pool.map(self._one, items))
        return np.asarray(vectors, dtype=np.float32)


class BedrockReranker:
    """The Bedrock rerank API: a question, some texts, and their order back.

    ``client`` is a ``bedrock-agent-runtime`` client. ``model_arn`` names the
    reranking model; left out, it is Amazon Rerank in ``region``. The service
    takes a limited number of texts a call, so a long list goes up in slices
    and the scores, which are comparable across calls, are merged.
    """

    MAX_SOURCES = 100

    def __init__(
        self, client: Any, region: str = "us-east-1", model_arn: Optional[str] = None
    ) -> None:
        self.client = client
        self.model_arn = (
            model_arn or f"arn:aws:bedrock:{region}::foundation-model/amazon.rerank-v1:0"
        )
        self.label = "bedrock-rerank"

    def rerank(
        self, query: str, texts: Sequence[str], top: Optional[int] = None
    ) -> List[Tuple[int, float]]:
        """``(index into texts, relevance)`` pairs, best first."""
        scored: List[Tuple[int, float]] = []
        for start in range(0, len(texts), self.MAX_SOURCES):
            batch = list(texts[start : start + self.MAX_SOURCES])
            try:
                reply = self.client.rerank(
                    queries=[{"type": "TEXT", "textQuery": {"text": query}}],
                    sources=[
                        {
                            "type": "INLINE",
                            "inlineDocumentSource": {
                                "type": "TEXT",
                                "textDocument": {"text": t or " "},
                            },
                        }
                        for t in batch
                    ],
                    rerankingConfiguration={
                        "type": "BEDROCK_RERANKING_MODEL",
                        "bedrockRerankingConfiguration": {
                            "modelConfiguration": {"modelArn": self.model_arn},
                            "numberOfResults": len(batch),
                        },
                    },
                )
            except Exception as exc:
                raise ModelError(f"Bedrock could not rerank: {exc}") from exc
            for result in reply.get("results") or []:
                scored.append((start + int(result["index"]), float(result["relevanceScore"])))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top] if top else scored
