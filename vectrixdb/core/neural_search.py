"""
Neural Search Components for VectrixDB

Advanced search features to match/exceed Qdrant:
- ColBERT late interaction (MaxSim token-level matching) - bundled model
- SPLADE learned sparse encoders
- Optimized cross-encoder reranking
- Multi-vector search

Models (bundled, no network calls):
- ColBERT: answerdotai/answerai-colbert-small-v1 (~33MB, English)
- Reranker: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (~100MB, 15+ languages)

Author: VectrixDB Team
"""

from __future__ import annotations

import numpy as np
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
import time


@dataclass
class ColBERTResult:
    """Result from ColBERT search."""
    id: str
    score: float
    max_sim_scores: List[float] = field(default_factory=list)


@dataclass
class SPLADEVector:
    """SPLADE sparse vector representation."""
    indices: List[int]
    values: List[float]

    def to_dict(self) -> Dict[int, float]:
        return dict(zip(self.indices, self.values))

    @classmethod
    def from_dict(cls, d: Dict[int, float]) -> "SPLADEVector":
        indices = list(d.keys())
        values = list(d.values())
        return cls(indices=indices, values=values)


class ColBERTEncoder:
    """
    ColBERT-style late interaction encoder.

    Uses bundled ONNX model (answerdotai/answerai-colbert-small-v1).
    No network calls - fully offline after initial setup.

    Produces token-level embeddings for MaxSim scoring.
    This enables fine-grained matching between query and document tokens.
    """

    def __init__(self, model_name: str = "answerdotai/answerai-colbert-small-v1"):
        """
        Initialize ColBERT encoder.

        Args:
            model_name: Model identifier (uses bundled ONNX model)
        """
        self.model_name = model_name
        self._embedder = None
        self._dimension = 128  # ColBERT uses 128-dim token embeddings

    @property
    def dimension(self) -> int:
        return self._dimension

    def _load_model(self):
        """Lazy load the bundled ColBERT model."""
        if self._embedder is None:
            try:
                # Use bundled ONNX model - no network calls
                from ..models.embedded import ColBERTEmbedder
                self._embedder = ColBERTEmbedder()
                self._dimension = self._embedder.dimension
            except Exception as e:
                raise ImportError(
                    f"Could not load bundled ColBERT model: {e}\n"
                    f"Run: vectrixdb download-models --type colbert"
                )

    def encode_query(self, query: str) -> np.ndarray:
        """
        Encode query into token embeddings.

        Uses bundled ONNX model - no network calls.

        Returns:
            Array of shape (num_tokens, dimension)
        """
        self._load_model()
        return self._embedder.encode_query(query)

    def encode_document(self, text: str) -> np.ndarray:
        """
        Encode document into token embeddings.

        Uses bundled ONNX model - no network calls.

        Returns:
            Array of shape (num_tokens, dimension)
        """
        self._load_model()
        return self._embedder.encode_document(text)

    def max_sim(
        self,
        query_embeddings: np.ndarray,
        doc_embeddings: np.ndarray
    ) -> float:
        """
        Calculate MaxSim score between query and document.

        MaxSim = sum over query tokens of max similarity to any doc token

        Args:
            query_embeddings: (num_query_tokens, dim)
            doc_embeddings: (num_doc_tokens, dim)

        Returns:
            MaxSim score
        """
        self._load_model()
        return self._embedder.max_sim(query_embeddings, doc_embeddings)


class SPLADEEncoder:
    """
    SPLADE sparse encoder for learned sparse representations.

    SPLADE produces sparse vectors where each dimension corresponds to a
    vocabulary term, enabling both semantic matching and exact term matching.
    """

    def __init__(self, model_name: str = "naver/splade-cocondenser-ensembledistil"):
        """
        Initialize SPLADE encoder.

        Args:
            model_name: HuggingFace model name for SPLADE
        """
        self.model_name = model_name
        self._model = None
        self._tokenizer = None
        self._vocab_size = 30522  # BERT vocab size

    def _load_model(self):
        """Lazy load the SPLADE model."""
        if self._model is None:
            try:
                from transformers import AutoModelForMaskedLM, AutoTokenizer
                import torch

                self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
                self._model = AutoModelForMaskedLM.from_pretrained(self.model_name)
                self._model.eval()

                if torch.cuda.is_available():
                    self._model = self._model.cuda()

            except Exception as e:
                # Fall back to BM25-style sparse vectors
                self._use_fallback = True

    def encode(self, text: str, is_query: bool = True) -> SPLADEVector:
        """
        Encode text into SPLADE sparse vector.

        Args:
            text: Input text
            is_query: Whether this is a query (affects expansion)

        Returns:
            SPLADEVector with sparse representation
        """
        self._load_model()

        if hasattr(self, '_use_fallback') and self._use_fallback:
            # Fallback to TF-IDF style sparse vector
            return self._fallback_encode(text)

        import torch

        with torch.no_grad():
            inputs = self._tokenizer(
                text,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=256 if not is_query else 64
            )

            if torch.cuda.is_available():
                inputs = {k: v.cuda() for k, v in inputs.items()}

            outputs = self._model(**inputs)
            logits = outputs.logits

            # SPLADE aggregation: max over sequence, then ReLU + log1p
            attention_mask = inputs["attention_mask"].unsqueeze(-1)
            masked_logits = logits * attention_mask

            # Max pooling over sequence
            sparse_rep = torch.max(masked_logits, dim=1).values

            # Apply SPLADE activation: log(1 + ReLU(x))
            sparse_rep = torch.log1p(torch.relu(sparse_rep))

            # Get non-zero values
            sparse_rep = sparse_rep.squeeze(0).cpu().numpy()

            # Threshold small values
            threshold = 0.1
            nonzero_indices = np.where(sparse_rep > threshold)[0]
            nonzero_values = sparse_rep[nonzero_indices]

            return SPLADEVector(
                indices=nonzero_indices.tolist(),
                values=nonzero_values.tolist()
            )

    def _fallback_encode(self, text: str) -> SPLADEVector:
        """Fallback to simple term frequency encoding."""
        import re
        from collections import Counter

        # Simple tokenization
        tokens = re.findall(r'\b[a-z0-9]+\b', text.lower())
        token_counts = Counter(tokens)

        # Create pseudo sparse vector using hash of tokens
        indices = []
        values = []

        for token, count in token_counts.items():
            # Hash token to index (simple hash)
            idx = hash(token) % self._vocab_size
            if idx < 0:
                idx += self._vocab_size
            indices.append(idx)
            # Log-scaled TF
            values.append(np.log1p(count))

        return SPLADEVector(indices=indices, values=values)

    def similarity(self, vec1: SPLADEVector, vec2: SPLADEVector) -> float:
        """Calculate dot product similarity between sparse vectors."""
        dict1 = vec1.to_dict()
        dict2 = vec2.to_dict()

        score = 0.0
        for idx in dict1:
            if idx in dict2:
                score += dict1[idx] * dict2[idx]

        return score


class CrossEncoderReranker:
    """
    Optimized cross-encoder reranker for final-stage ranking.

    Uses bundled ONNX model (mmarco-mMiniLMv2-L12-H384-v1) - 15+ languages.
    No network calls - fully offline after initial setup.

    Cross-encoders jointly encode query-document pairs for maximum accuracy.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        batch_size: int = 32
    ):
        """
        Initialize cross-encoder.

        Args:
            model_name: Model identifier (uses bundled ONNX model)
            batch_size: Batch size for inference
        """
        self.model_name = model_name
        self.batch_size = batch_size
        self._reranker = None

    def _load_model(self):
        """Lazy load the bundled cross-encoder model."""
        if self._reranker is None:
            try:
                # Use bundled ONNX model - no network calls
                from ..models.embedded import CrossEncoderReranker as BundledReranker
                self._reranker = BundledReranker()
            except Exception as e:
                raise ImportError(
                    f"Could not load bundled reranker model: {e}\n"
                    f"Run: vectrixdb download-models --type reranker"
                )

    def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        limit: int = 10,
        text_field: str = "text"
    ) -> List[Dict[str, Any]]:
        """
        Rerank documents using cross-encoder.

        Uses bundled ONNX model - no network calls.

        Args:
            query: Search query
            documents: List of document dicts (must have text_field)
            limit: Number of results to return
            text_field: Field containing document text

        Returns:
            Reranked documents with cross-encoder scores
        """
        if not documents:
            return []

        self._load_model()

        # Extract texts from documents
        texts = []
        valid_docs = []

        for doc in documents:
            text = doc.get(text_field) or doc.get("metadata", {}).get(text_field, "")
            if text:
                texts.append(text)
                valid_docs.append(doc)

        if not texts:
            return documents[:limit]

        # Score using bundled reranker
        scores = self._reranker.score(query, texts)

        # Combine scores with documents
        results = []
        for doc, score in zip(valid_docs, scores):
            doc_copy = doc.copy()
            doc_copy["cross_encoder_score"] = float(score)
            doc_copy["original_score"] = doc.get("score", 0)
            doc_copy["score"] = float(score)
            results.append(doc_copy)

        # Sort by cross-encoder score
        results.sort(key=lambda x: x["score"], reverse=True)

        return results[:limit]


class NeuralHybridSearcher:
    """
    Advanced hybrid searcher combining multiple neural methods.

    All models are bundled - no network calls after initial setup.

    Combines:
    - Dense embeddings (multilingual-e5-small, 100+ languages)
    - Sparse/BM25 (lexical, language agnostic)
    - ColBERT (answerai-colbert-small-v1, English late interaction)
    - SPLADE (learned sparse, fallback to BM25)
    - Cross-encoder (mmarco-mMiniLMv2-L12-H384-v1, 15+ languages)

    Total bundle: ~247MB

    This is the "ultimate" search that should match/exceed Qdrant.
    """

    def __init__(
        self,
        use_colbert: bool = True,
        use_splade: bool = True,
        use_cross_encoder: bool = True,
        colbert_weight: float = 0.3,
        dense_weight: float = 0.35,
        sparse_weight: float = 0.35,
    ):
        """
        Initialize neural hybrid searcher.

        Args:
            use_colbert: Enable ColBERT late interaction
            use_splade: Enable SPLADE sparse encoding
            use_cross_encoder: Enable cross-encoder reranking
            colbert_weight: Weight for ColBERT scores in fusion
            dense_weight: Weight for dense scores
            sparse_weight: Weight for sparse/BM25 scores
        """
        self.use_colbert = use_colbert
        self.use_splade = use_splade
        self.use_cross_encoder = use_cross_encoder

        self.colbert_weight = colbert_weight
        self.dense_weight = dense_weight
        self.sparse_weight = sparse_weight

        # Lazy-loaded encoders
        self._colbert: Optional[ColBERTEncoder] = None
        self._splade: Optional[SPLADEEncoder] = None
        self._cross_encoder: Optional[CrossEncoderReranker] = None

    @property
    def colbert(self) -> ColBERTEncoder:
        if self._colbert is None:
            self._colbert = ColBERTEncoder()
        return self._colbert

    @property
    def splade(self) -> SPLADEEncoder:
        if self._splade is None:
            self._splade = SPLADEEncoder()
        return self._splade

    @property
    def cross_encoder(self) -> CrossEncoderReranker:
        if self._cross_encoder is None:
            self._cross_encoder = CrossEncoderReranker()
        return self._cross_encoder

    def search(
        self,
        query: str,
        query_vector: np.ndarray,
        dense_results: List[Dict],
        sparse_results: List[Tuple[str, float]],
        document_texts: Dict[str, str],
        limit: int = 10,
        prefetch_limit: int = 100,
    ) -> List[Dict]:
        """
        Perform neural hybrid search.

        Args:
            query: Query text
            query_vector: Dense query embedding
            dense_results: Results from dense search [(id, score, metadata), ...]
            sparse_results: Results from BM25 [(id, score), ...]
            document_texts: Mapping of doc_id to text
            limit: Final number of results
            prefetch_limit: Candidates for ColBERT/reranking

        Returns:
            Fused and reranked results
        """
        start_time = time.time()

        # Stage 1: RRF Fusion of dense and sparse
        rrf_k = 60
        scores: Dict[str, Dict] = {}

        # Add dense scores
        for i, result in enumerate(dense_results[:prefetch_limit]):
            doc_id = result["id"] if isinstance(result, dict) else result.id
            score = result["score"] if isinstance(result, dict) else result.score
            metadata = result.get("metadata", {}) if isinstance(result, dict) else getattr(result, "metadata", {})

            scores[doc_id] = {
                "dense_rrf": 1.0 / (rrf_k + i + 1),
                "sparse_rrf": 0,
                "colbert_score": 0,
                "dense_score": score,
                "metadata": metadata,
            }

        # Add sparse scores
        for i, (doc_id, score) in enumerate(sparse_results[:prefetch_limit]):
            if doc_id not in scores:
                scores[doc_id] = {
                    "dense_rrf": 0,
                    "sparse_rrf": 0,
                    "colbert_score": 0,
                    "dense_score": 0,
                    "metadata": {},
                }
            scores[doc_id]["sparse_rrf"] = 1.0 / (rrf_k + i + 1)
            scores[doc_id]["sparse_score"] = score

        # Stage 2: ColBERT scoring (if enabled)
        if self.use_colbert and document_texts:
            try:
                query_embeddings = self.colbert.encode_query(query)

                # Score top candidates with ColBERT
                colbert_candidates = sorted(
                    scores.keys(),
                    key=lambda x: scores[x]["dense_rrf"] + scores[x]["sparse_rrf"],
                    reverse=True
                )[:min(30, len(scores))]  # Top 30 for ColBERT

                for doc_id in colbert_candidates:
                    if doc_id in document_texts:
                        doc_embeddings = self.colbert.encode_document(document_texts[doc_id])
                        colbert_score = self.colbert.max_sim(query_embeddings, doc_embeddings)
                        scores[doc_id]["colbert_score"] = colbert_score

            except Exception as e:
                # ColBERT failed, continue without it
                pass

        # Stage 3: Compute final fusion scores
        # Normalize ColBERT scores if we have them
        colbert_scores = [s["colbert_score"] for s in scores.values() if s["colbert_score"] > 0]
        if colbert_scores:
            max_colbert = max(colbert_scores)
            if max_colbert > 0:
                for doc_id in scores:
                    scores[doc_id]["colbert_normalized"] = scores[doc_id]["colbert_score"] / max_colbert

        for doc_id in scores:
            dense_rrf = scores[doc_id]["dense_rrf"]
            sparse_rrf = scores[doc_id]["sparse_rrf"]
            colbert_norm = scores[doc_id].get("colbert_normalized", 0)

            # Weighted combination
            if colbert_norm > 0:
                # Use ColBERT in fusion
                combined = (
                    self.dense_weight * dense_rrf +
                    self.sparse_weight * sparse_rrf +
                    self.colbert_weight * colbert_norm
                )
            else:
                # Fall back to dense + sparse
                combined = (
                    (self.dense_weight + self.colbert_weight/2) * dense_rrf +
                    (self.sparse_weight + self.colbert_weight/2) * sparse_rrf
                )

            # Intersection boost
            if dense_rrf > 0 and sparse_rrf > 0:
                combined *= 1.15

            scores[doc_id]["combined"] = combined

        # Sort by combined score
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x]["combined"], reverse=True)

        # Build candidate list for reranking
        candidates = []
        for doc_id in sorted_ids[:prefetch_limit]:
            candidates.append({
                "id": doc_id,
                "score": scores[doc_id]["combined"],
                "metadata": scores[doc_id]["metadata"],
                "text": document_texts.get(doc_id, ""),
                "dense_score": scores[doc_id].get("dense_score", 0),
                "sparse_score": scores[doc_id].get("sparse_score", 0),
                "colbert_score": scores[doc_id].get("colbert_score", 0),
            })

        # Stage 4: Cross-encoder reranking (if enabled)
        if self.use_cross_encoder and candidates:
            try:
                reranked = self.cross_encoder.rerank(
                    query=query,
                    documents=candidates,
                    limit=limit,
                    text_field="text"
                )
                return reranked
            except Exception as e:
                # Cross-encoder failed, return fusion results
                pass

        return candidates[:limit]
