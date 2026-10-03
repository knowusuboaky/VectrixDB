"""Embeddings from any OpenAI-compatible endpoint.

    from vectrixdb import Vectrix
    from vectrixdb.models import OpenAIEmbedder

    embedder = OpenAIEmbedder("text-embedding-3-small")                    # api.openai.com
    embedder = OpenAIEmbedder("nomic-embed-text", base_url="http://localhost:11434/v1")  # Ollama
    embedder = OpenAIEmbedder("BAAI/bge-m3", base_url="http://localhost:8000/v1")        # vLLM, TEI

    db = Vectrix("docs", embed_fn=embedder, dimension=embedder.dimension)

Or in one keyword: ``Vectrix("docs", dense_model="openai:text-embedding-3-small")``.

The endpoint is called through the ``openai`` package with the key from
``OPENAI_API_KEY`` (or ``api_key=``), so Ollama, vLLM, LM Studio, Text
Embeddings Inference and the hosted service all work with one class. The
dimension is asked of the endpoint once, on the first call, unless given.
It and the Bedrock embedder are the only ones in the package that need a network.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Sequence, Union

import numpy as np

from ..exceptions import ConfigurationError, DependencyError

__all__ = ["OpenAIEmbedder"]


# ============================================================================
# THE EMBEDDER
# ============================================================================
#
# INPUT   texts, and an endpoint that speaks OpenAI's embeddings API
# OUTPUT  a callable texts -> ndarray for Vectrix(embed_fn=)
#
# Any compatible endpoint: OpenAI's own, Azure OpenAI, or a local server.


class OpenAIEmbedder:
    """Callable ``texts -> np.ndarray`` for ``Vectrix(embed_fn=...)``."""

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        dimension: Optional[int] = None,
        batch_size: int = 256,
        timeout: float = 60.0,
        client: Any = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.batch_size = max(1, batch_size)
        self._dimension = dimension
        self._client = client
        if client is None:
            # An explicitly passed key, empty string included, means "use
            # this one": never fall back to the environment. Reading it for
            # truthiness meant api_key="" against a local base_url handed
            # that third-party endpoint the caller's real OPENAI_API_KEY.
            if api_key is not None:
                key = api_key or ("local" if base_url else None)
            else:
                key = os.environ.get("OPENAI_API_KEY") or ("local" if base_url else None)
            if not key:
                raise ConfigurationError(
                    "OpenAIEmbedder needs an API key: pass api_key= or set OPENAI_API_KEY "
                    "(any value will do for a local server given base_url=)."
                )
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise DependencyError("openai") from exc
            self._client = OpenAI(api_key=key, base_url=base_url, timeout=timeout)

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self._dimension = int(self.embed(["dimension probe"]).shape[1])
        return self._dimension

    def embed(self, texts: Union[str, Sequence[str]], **_: Any) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        texts = [t if t.strip() else " " for t in texts]  # the API rejects empty input
        out: List[List[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            response = self._client.embeddings.create(model=self.model, input=batch)
            rows = sorted(response.data, key=lambda d: d.index)
            out.extend(list(r.embedding) for r in rows)
        array = np.asarray(out, dtype=np.float32)
        if self._dimension is None and array.ndim == 2:
            self._dimension = int(array.shape[1])
        return array

    def __call__(self, texts: Union[str, Sequence[str]]) -> np.ndarray:
        return self.embed(texts)

    def __repr__(self) -> str:
        where = self.base_url or "api.openai.com"
        return f"OpenAIEmbedder({self.model!r}, {where})"
