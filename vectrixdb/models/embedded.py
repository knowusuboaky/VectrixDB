"""
Embedded Models for VectrixDB

ONNX-based models that run locally with zero network calls.
Core models bundled with pip install (~35MB).
Additional models auto-downloaded from GitHub on first use.

Bundled Models (no download needed):
- DenseEmbedder(language="en"): intfloat/e5-small-v2 (~33MB INT8)
- SparseEmbedder: BM25 vocabulary-based (~1MB)

English Models (auto-download from GitHub on first use):
- RerankerEmbedder(language="en"): cross-encoder/ms-marco-MiniLM-L-12-v2 (~22MB INT8)
- LateInteractionEmbedder(language="en"): answerdotai/answerai-colbert-small-v1 (~22MB INT8)

Multilingual Models (auto-download from GitHub on first use):
- DenseEmbedder(): intfloat/multilingual-e5-small (~113MB INT8) - 100+ languages
- RerankerEmbedder(): cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (~113MB INT8) - 15+ languages
- LateInteractionEmbedder(): BAAI/bge-m3 (~563MB INT8) - 100+ languages

GraphRAG (auto-download from GitHub on first use):
- GraphExtractor: Babelscape/mrebel-base (~718MB INT8) - 18 languages
"""

from __future__ import annotations

import os
import json
import math
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from collections import Counter
from dataclasses import dataclass
import numpy as np

# Model configuration
MODEL_CONFIG = {
    "dense": {
        "name": "multilingual-e5-small",
        "dimension": 384,
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 113,
        "huggingface_id": "intfloat/multilingual-e5-small",
        "github_release": "dense-multi",  # GitHub release tag for fallback
        "languages": "100+",
        "quantization": "int8",
    },
    "dense_en": {
        "name": "e5-small-v2",
        "dimension": 384,
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 32,
        "huggingface_id": "intfloat/e5-small-v2",
        "github_release": "dense-en",
        "languages": "english",
        "quantization": "int8",
    },
    "sparse": {
        "name": "bm25",
        "vocab_file": "vocab.json",
        "idf_file": "idf.json",
        "config_file": "config.json",
        "size_mb": 1,
        "languages": "any",
    },
    "reranker": {
        "name": "mmarco-mMiniLMv2-L12-H384-v1",
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 113,
        "huggingface_id": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        "github_release": "reranker-multi",
        "languages": "15+",
        "quantization": "int8",
    },
    "reranker_en": {
        "name": "ms-marco-MiniLM-L-12-v2",
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 32,
        "huggingface_id": "cross-encoder/ms-marco-MiniLM-L-12-v2",
        "github_release": "reranker-en",
        "languages": "english",
        "quantization": "int8",
    },
    "late_interaction": {
        "name": "bge-m3",
        "dimension": 1024,
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 563,
        "huggingface_id": "BAAI/bge-m3",
        "github_release": "bge-m3",
        "languages": "100+",
        "quantization": "int8",
    },
    "late_interaction_en": {
        "name": "answerai-colbert-small-v1",
        "dimension": 128,
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 32,
        "huggingface_id": "answerdotai/answerai-colbert-small-v1",
        "github_release": "colbert-en",
        "languages": "english",
        "quantization": "int8",
    },
    "rebel": {
        "name": "mrebel-base-int8",
        "max_length": 256,
        "onnx_encoder_file": "encoder.onnx",
        "onnx_decoder_file": "decoder.onnx",
        "tokenizer_file": "sentencepiece.bpe.model",
        "config_file": "config.json",
        "size_mb": 718,  # INT8 quantized (~4x smaller than float32)
        "huggingface_id": "Babelscape/mrebel-base",
        "github_release": "mrebel",
        "languages": "18",  # ar, ca, de, el, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, sv, vi, zh
        "quantization": "int8",
        "description": "Multilingual relation extraction (triplets: head, relation, tail)",
    },
}

# GitHub repository for model releases (fallback when HuggingFace is blocked)
GITHUB_REPO = "knowusuboaky/VectrixDB"
GITHUB_RELEASE_BASE = f"https://github.com/{GITHUB_REPO}/releases/download"


def get_models_dir() -> Path:
    """Get the models directory path."""
    # Check environment variable first
    env_path = os.environ.get("VECTRIXDB_MODELS_DIR")
    if env_path:
        return Path(env_path)

    # Default: package directory
    return Path(__file__).parent / "data"


def is_models_installed(model_type: str = "all") -> bool:
    """
    Check if models are installed.

    Args:
        model_type: "dense", "sparse", "reranker", "colbert", "rebel", or "all"

    Returns:
        True if models are installed
    """
    models_dir = get_models_dir()

    if model_type == "all":
        types_to_check = ["dense", "sparse", "reranker", "colbert"]
    elif model_type == "graphrag":
        # GraphRAG needs rebel model
        types_to_check = ["rebel"]
    else:
        types_to_check = [model_type]

    for mt in types_to_check:
        model_dir = models_dir / mt
        config = MODEL_CONFIG.get(mt, {})

        if mt == "sparse":
            # Check BM25 files
            if not (model_dir / config.get("vocab_file", "vocab.json")).exists():
                return False
        elif mt == "rebel":
            # Check REBEL encoder/decoder ONNX files
            if not (model_dir / config.get("onnx_encoder_file", "encoder.onnx")).exists():
                return False
        else:
            # Check ONNX model
            if not (model_dir / config.get("onnx_file", "model.onnx")).exists():
                return False

    return True


def download_models(
    model_type: str = "all",
    force: bool = False,
    progress: bool = True
) -> None:
    """
    Download models from HuggingFace and convert to ONNX.

    This is a ONE-TIME setup operation. After this, no network calls needed.

    Args:
        model_type: "dense", "sparse", "reranker", or "all"
        force: Re-download even if exists
        progress: Show progress bar
    """
    from .downloader import ModelDownloader

    downloader = ModelDownloader(progress=progress)

    if model_type == "all":
        types_to_download = ["dense", "sparse", "reranker"]
    else:
        types_to_download = [model_type]

    for mt in types_to_download:
        if force or not is_models_installed(mt):
            downloader.download(mt)


# =============================================================================
# Tokenizer (minimal implementation for offline use)
# =============================================================================

class SimpleTokenizer:
    """
    Simple tokenizer that loads from tokenizer.json (HuggingFace format).
    No network calls - uses bundled vocabulary.
    """

    def __init__(self, tokenizer_path: Path):
        """Load tokenizer from file."""
        self.tokenizer_path = tokenizer_path
        self._vocab: Dict[str, int] = {}
        self._vocab_inv: Dict[int, str] = {}
        self._special_tokens: Dict[str, int] = {}
        self._max_length = 512

        self._load_tokenizer()

    def _load_tokenizer(self):
        """Load tokenizer configuration."""
        if not self.tokenizer_path.exists():
            raise FileNotFoundError(
                f"Tokenizer not found: {self.tokenizer_path}\n"
                f"Run: vectrixdb download-models"
            )

        with open(self.tokenizer_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Load vocabulary - handle different formats
        if "model" in data and "vocab" in data["model"]:
            vocab_data = data["model"]["vocab"]
            # Check if it's SentencePiece format (list of [token, score] pairs)
            if isinstance(vocab_data, list):
                self._vocab = {token: i for i, (token, score) in enumerate(vocab_data)}
            else:
                self._vocab = vocab_data
        elif "vocab" in data:
            self._vocab = data["vocab"]
        else:
            self._vocab = {}

        self._vocab_inv = {v: k for k, v in self._vocab.items()}

        # Load special tokens
        if "added_tokens" in data:
            for token in data["added_tokens"]:
                self._special_tokens[token["content"]] = token["id"]

        # Get special token IDs - handle both BERT and XLM-RoBERTa style
        # XLM-RoBERTa uses <s>, </s>, <pad>, <unk>
        # BERT uses [CLS], [SEP], [PAD], [UNK]
        self.pad_token_id = (
            self._special_tokens.get("<pad>") or
            self._special_tokens.get("[PAD]") or
            self._vocab.get("<pad>") or
            self._vocab.get("[PAD]", 1)
        )
        self.cls_token_id = (
            self._special_tokens.get("<s>") or
            self._special_tokens.get("[CLS]") or
            self._vocab.get("<s>") or
            self._vocab.get("[CLS]", 0)
        )
        self.sep_token_id = (
            self._special_tokens.get("</s>") or
            self._special_tokens.get("[SEP]") or
            self._vocab.get("</s>") or
            self._vocab.get("[SEP]", 2)
        )
        self.unk_token_id = (
            self._special_tokens.get("<unk>") or
            self._special_tokens.get("[UNK]") or
            self._vocab.get("<unk>") or
            self._vocab.get("[UNK]", 3)
        )

        # Try to get max_length from config
        if "truncation" in data and data["truncation"] is not None:
            self._max_length = data["truncation"].get("max_length", 512)
        else:
            self._max_length = 512  # Default max length

    def encode(
        self,
        text: str,
        max_length: Optional[int] = None,
        add_special_tokens: bool = True,
        padding: bool = True,
        truncation: bool = True,
    ) -> Dict[str, np.ndarray]:
        """
        Encode text to token IDs.

        Args:
            text: Input text
            max_length: Maximum sequence length
            add_special_tokens: Add [CLS] and [SEP]
            padding: Pad to max_length
            truncation: Truncate to max_length

        Returns:
            Dict with input_ids, attention_mask, token_type_ids
        """
        max_length = max_length or self._max_length

        # Simple wordpiece tokenization
        tokens = self._tokenize(text.lower())
        token_ids = [self._vocab.get(t, self.unk_token_id) for t in tokens]

        # Add special tokens
        if add_special_tokens:
            token_ids = [self.cls_token_id] + token_ids + [self.sep_token_id]

        # Truncate
        if truncation and len(token_ids) > max_length:
            token_ids = token_ids[:max_length - 1] + [self.sep_token_id]

        # Create attention mask
        attention_mask = [1] * len(token_ids)

        # Pad
        if padding:
            pad_length = max_length - len(token_ids)
            if pad_length > 0:
                token_ids = token_ids + [self.pad_token_id] * pad_length
                attention_mask = attention_mask + [0] * pad_length

        return {
            "input_ids": np.array([token_ids], dtype=np.int64),
            "attention_mask": np.array([attention_mask], dtype=np.int64),
            "token_type_ids": np.zeros((1, len(token_ids)), dtype=np.int64),
        }

    def encode_batch(
        self,
        texts: List[str],
        max_length: Optional[int] = None,
        **kwargs
    ) -> Dict[str, np.ndarray]:
        """Encode multiple texts."""
        max_length = max_length or self._max_length

        all_input_ids = []
        all_attention_masks = []
        all_token_type_ids = []

        for text in texts:
            encoded = self.encode(text, max_length=max_length, **kwargs)
            all_input_ids.append(encoded["input_ids"][0])
            all_attention_masks.append(encoded["attention_mask"][0])
            all_token_type_ids.append(encoded["token_type_ids"][0])

        return {
            "input_ids": np.array(all_input_ids, dtype=np.int64),
            "attention_mask": np.array(all_attention_masks, dtype=np.int64),
            "token_type_ids": np.array(all_token_type_ids, dtype=np.int64),
        }

    def encode_pair(
        self,
        text_a: str,
        text_b: str,
        max_length: Optional[int] = None,
    ) -> Dict[str, np.ndarray]:
        """Encode a text pair (for cross-encoder)."""
        max_length = max_length or self._max_length

        tokens_a = self._tokenize(text_a.lower())
        tokens_b = self._tokenize(text_b.lower())

        # [CLS] tokens_a [SEP] tokens_b [SEP]
        total_tokens = len(tokens_a) + len(tokens_b) + 3

        # Truncate if needed (truncate text_b first)
        if total_tokens > max_length:
            excess = total_tokens - max_length
            if len(tokens_b) > excess:
                tokens_b = tokens_b[:-excess]
            else:
                tokens_b = tokens_b[:1]
                remaining = max_length - len(tokens_b) - 3
                tokens_a = tokens_a[:remaining]

        token_ids_a = [self._vocab.get(t, self.unk_token_id) for t in tokens_a]
        token_ids_b = [self._vocab.get(t, self.unk_token_id) for t in tokens_b]

        # Build sequence
        input_ids = (
            [self.cls_token_id] +
            token_ids_a +
            [self.sep_token_id] +
            token_ids_b +
            [self.sep_token_id]
        )

        # Token type IDs: 0 for first segment, 1 for second
        token_type_ids = (
            [0] * (len(token_ids_a) + 2) +  # [CLS] + tokens_a + [SEP]
            [1] * (len(token_ids_b) + 1)    # tokens_b + [SEP]
        )

        attention_mask = [1] * len(input_ids)

        # Pad
        pad_length = max_length - len(input_ids)
        if pad_length > 0:
            input_ids = input_ids + [self.pad_token_id] * pad_length
            attention_mask = attention_mask + [0] * pad_length
            token_type_ids = token_type_ids + [0] * pad_length

        return {
            "input_ids": np.array([input_ids], dtype=np.int64),
            "attention_mask": np.array([attention_mask], dtype=np.int64),
            "token_type_ids": np.array([token_type_ids], dtype=np.int64),
        }

    def _tokenize(self, text: str) -> List[str]:
        """Simple wordpiece tokenization."""
        # Basic preprocessing
        text = text.strip()

        # Split on whitespace and punctuation
        words = re.findall(r'\b\w+\b|[^\w\s]', text)

        tokens = []
        for word in words:
            # Try full word first
            if word in self._vocab:
                tokens.append(word)
            else:
                # Wordpiece: split into subwords
                word_tokens = self._wordpiece_tokenize(word)
                tokens.extend(word_tokens)

        return tokens

    def _wordpiece_tokenize(self, word: str) -> List[str]:
        """Tokenize a single word into wordpieces."""
        if not word:
            return []

        tokens = []
        start = 0

        while start < len(word):
            end = len(word)
            found = False

            while start < end:
                substr = word[start:end]
                if start > 0:
                    substr = "##" + substr

                if substr in self._vocab:
                    tokens.append(substr)
                    found = True
                    break

                end -= 1

            if not found:
                tokens.append("[UNK]")
                start += 1
            else:
                start = end

        return tokens


# =============================================================================
# Dense Embedder (all-MiniLM-L6-v2)
# =============================================================================

class DenseEmbedder:
    """
    Dense embedding model using ONNX.

    Default model: intfloat/multilingual-e5-small (384 dim, 100+ languages)
    English model: intfloat/e5-small-v2 (384 dim, English-optimized)
    No network calls - uses bundled or custom ONNX model.

    Usage:
        # Multilingual (default)
        embedder = DenseEmbedder()
        vectors = embedder.embed(["hello world", "how are you"])

        # English-optimized (smaller, faster for English)
        embedder = DenseEmbedder(language="en")
        vectors = embedder.embed(["hello world"])

        # Custom ONNX model
        embedder = DenseEmbedder(model_dir="/path/to/my/model", dimension=768)
        vectors = embedder.embed(["hello world"])
    """

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        device: str = "cpu",
        dimension: Optional[int] = None,
        max_length: Optional[int] = None,
        onnx_file: str = "model.onnx",
        tokenizer_file: str = "tokenizer.json",
        language: Optional[str] = None,
    ):
        """
        Initialize dense embedder.

        Args:
            model_dir: Path to model directory (default: bundled)
            device: "cpu" or "cuda" (ONNX Runtime provider)
            dimension: Override embedding dimension (auto-detected if None)
            max_length: Override max sequence length (default: 256)
            onnx_file: Name of ONNX model file (default: model.onnx)
            tokenizer_file: Name of tokenizer file (default: tokenizer.json)
            language: Language variant - None/"multi" for multilingual (default),
                      "en"/"english" for English-optimized model
        """
        # Determine which model to use based on language
        self.language = language
        if language in ("en", "english"):
            config_key = "dense_en"
            default_dir = get_models_dir() / "dense_en"
        else:
            config_key = "dense"
            default_dir = get_models_dir() / "dense"

        self.model_dir = Path(model_dir) if model_dir else default_dir
        self.device = device
        self.onnx_file = onnx_file
        self.tokenizer_file = tokenizer_file

        # Use provided values or defaults from config
        self.dimension = dimension or MODEL_CONFIG[config_key]["dimension"]
        self.max_length = max_length or MODEL_CONFIG[config_key]["max_length"]

        self._session = None
        self._tokenizer = None
        self._has_token_type_ids = True  # Default, will be detected

    @property
    def session(self):
        """Lazy load ONNX session."""
        if self._session is None:
            self._load_model()
        return self._session

    @property
    def tokenizer(self):
        """Lazy load tokenizer."""
        if self._tokenizer is None:
            self._load_model()
        return self._tokenizer

    def _load_model(self):
        """Load ONNX model and tokenizer. Auto-downloads multilingual models if needed."""
        import onnxruntime as ort

        model_path = self.model_dir / self.onnx_file
        tokenizer_path = self.model_dir / self.tokenizer_file

        if not model_path.exists():
            # Auto-download for multilingual models
            if self.language not in ("en", "english"):
                print(f"Multilingual dense model not found. Downloading...")
                try:
                    download_models(model_type="dense", progress=True)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to auto-download multilingual dense model.\n\n"
                        f"Options:\n"
                        f"  1. Manual download:  vectrixdb download-models --type dense\n"
                        f"  2. Use English model: DenseEmbedder(language='en')  [bundled, no download]\n"
                        f"  3. Use custom model:  DenseEmbedder(model_dir='/path/to/model')\n\n"
                        f"Error: {e}\n\n"
                        f"Note: If downloads are blocked, use language='en' for bundled English models."
                    ) from e
            else:
                raise FileNotFoundError(
                    f"Dense model not found: {model_path}\n"
                    f"English models should be bundled. Try reinstalling: pip install --force-reinstall vectrixdb"
                )

        # Set up ONNX Runtime session
        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=providers
        )

        # Detect if model uses token_type_ids by checking input names
        input_names = [inp.name for inp in self._session.get_inputs()]
        self._has_token_type_ids = "token_type_ids" in input_names

        self._tokenizer = SimpleTokenizer(tokenizer_path)

    def embed(
        self,
        texts: Union[str, List[str]],
        normalize: bool = True,
        batch_size: int = 32,
    ) -> np.ndarray:
        """
        Generate embeddings for texts.

        Args:
            texts: Single text or list of texts
            normalize: L2 normalize embeddings
            batch_size: Batch size for processing

        Returns:
            Embeddings array, shape (n_texts, 384)
        """
        if isinstance(texts, str):
            texts = [texts]

        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            embeddings = self._embed_batch(batch)
            all_embeddings.append(embeddings)

        result = np.vstack(all_embeddings)

        if normalize:
            norms = np.linalg.norm(result, axis=1, keepdims=True)
            result = result / (norms + 1e-8)

        return result.astype(np.float32)

    def _embed_batch(self, texts: List[str]) -> np.ndarray:
        """Embed a batch of texts."""
        # Tokenize
        inputs = self.tokenizer.encode_batch(
            texts,
            max_length=self.max_length,
            padding=True,
            truncation=True,
        )

        # Build ONNX input dict based on model requirements
        onnx_inputs = {
            "input_ids": inputs["input_ids"],
            "attention_mask": inputs["attention_mask"],
        }

        # Only include token_type_ids if model expects it
        if self._has_token_type_ids:
            onnx_inputs["token_type_ids"] = inputs["token_type_ids"]

        # Run ONNX inference
        outputs = self.session.run(None, onnx_inputs)

        # Mean pooling
        token_embeddings = outputs[0]  # (batch, seq_len, hidden_dim)
        attention_mask = inputs["attention_mask"]

        # Expand mask for broadcasting
        mask_expanded = np.expand_dims(attention_mask, axis=-1)

        # Sum embeddings where mask is 1
        sum_embeddings = np.sum(token_embeddings * mask_expanded, axis=1)
        sum_mask = np.clip(np.sum(mask_expanded, axis=1), a_min=1e-9, a_max=None)

        # Mean pooling
        embeddings = sum_embeddings / sum_mask

        return embeddings

    def __call__(self, texts: Union[str, List[str]]) -> np.ndarray:
        """Alias for embed()."""
        return self.embed(texts)


# =============================================================================
# Sparse Embedder (BM25)
# =============================================================================

class SparseEmbedder:
    """
    Sparse BM25-based embeddings.

    No neural network - pure algorithmic BM25 with vocabulary-based tokenization.
    No network calls - uses bundled or custom vocabulary.

    Usage:
        # Bundled vocabulary (default)
        embedder = SparseEmbedder()
        sparse_vectors = embedder.embed(["hello world"])

        # Custom vocabulary
        embedder = SparseEmbedder(model_dir="/path/to/my/vocab")
    """

    # BM25 parameters
    k1: float = 1.5
    b: float = 0.75

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        k1: float = 1.5,
        b: float = 0.75,
        vocab_file: str = "vocab.json",
        idf_file: str = "idf.json",
        config_file: str = "config.json",
    ):
        """
        Initialize sparse embedder.

        Args:
            model_dir: Path to model directory (default: bundled)
            k1: BM25 k1 parameter (term frequency saturation)
            b: BM25 b parameter (length normalization)
            vocab_file: Name of vocabulary file
            idf_file: Name of IDF weights file
            config_file: Name of config file
        """
        self.model_dir = Path(model_dir) if model_dir else (get_models_dir() / "sparse")
        self.k1 = k1
        self.b = b
        self.vocab_file = vocab_file
        self.idf_file = idf_file
        self.config_file = config_file

        self._vocab: Dict[str, int] = {}
        self._idf: Dict[str, float] = {}
        self._avg_doc_len: float = 50.0
        self._loaded = False

    def _load_vocab(self):
        """Load vocabulary and IDF values."""
        if self._loaded:
            return

        vocab_path = self.model_dir / self.vocab_file
        idf_path = self.model_dir / self.idf_file
        config_path = self.model_dir / self.config_file

        # Load vocabulary
        if vocab_path.exists():
            with open(vocab_path, "r", encoding="utf-8") as f:
                self._vocab = json.load(f)
        else:
            # Create default vocabulary (will be built from usage)
            self._vocab = {}

        # Load IDF values
        if idf_path.exists():
            with open(idf_path, "r", encoding="utf-8") as f:
                self._idf = json.load(f)

        # Load config
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                self._avg_doc_len = config.get("avg_doc_len", 50.0)
                self.k1 = config.get("k1", self.k1)
                self.b = config.get("b", self.b)

        self._loaded = True

    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization for BM25."""
        # Lowercase and split
        text = text.lower()
        # Remove punctuation and split
        tokens = re.findall(r'\b\w+\b', text)
        # Filter short tokens
        tokens = [t for t in tokens if len(t) > 1]
        return tokens

    def embed(
        self,
        texts: Union[str, List[str]],
    ) -> List[Dict[int, float]]:
        """
        Generate sparse BM25 embeddings.

        Args:
            texts: Single text or list of texts

        Returns:
            List of sparse vectors (dict of term_id -> weight)
        """
        self._load_vocab()

        if isinstance(texts, str):
            texts = [texts]

        results = []

        for text in texts:
            tokens = self._tokenize(text)
            term_freqs = Counter(tokens)
            doc_len = len(tokens)

            sparse_vec = {}

            for term, tf in term_freqs.items():
                # Get or create term ID
                if term not in self._vocab:
                    self._vocab[term] = len(self._vocab)

                term_id = self._vocab[term]

                # Get IDF (default to 1.0 if not in corpus)
                idf = self._idf.get(term, 1.0)

                # BM25 formula
                tf_component = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_len)
                )

                weight = idf * tf_component
                sparse_vec[term_id] = weight

            results.append(sparse_vec)

        return results

    def embed_dense(
        self,
        texts: Union[str, List[str]],
        vocab_size: int = 30522,
    ) -> np.ndarray:
        """
        Generate dense representation of sparse vectors.

        Args:
            texts: Input texts
            vocab_size: Size of vocabulary (for dense vector)

        Returns:
            Dense array of shape (n_texts, vocab_size)
        """
        sparse_vecs = self.embed(texts)

        dense = np.zeros((len(sparse_vecs), vocab_size), dtype=np.float32)

        for i, sparse_vec in enumerate(sparse_vecs):
            for term_id, weight in sparse_vec.items():
                if term_id < vocab_size:
                    dense[i, term_id] = weight

        return dense

    def get_terms(self, text: str) -> List[Tuple[str, float]]:
        """Get terms with their BM25 weights."""
        self._load_vocab()

        tokens = self._tokenize(text)
        term_freqs = Counter(tokens)
        doc_len = len(tokens)

        results = []

        for term, tf in term_freqs.items():
            idf = self._idf.get(term, 1.0)
            tf_component = (tf * (self.k1 + 1)) / (
                tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_len)
            )
            weight = idf * tf_component
            results.append((term, weight))

        return sorted(results, key=lambda x: x[1], reverse=True)

    def __call__(self, texts: Union[str, List[str]]) -> List[Dict[int, float]]:
        """Alias for embed()."""
        return self.embed(texts)


# =============================================================================
# Reranker Embedder (Cross-Encoder)
# =============================================================================

class RerankerEmbedder:
    """
    Cross-encoder reranker using ONNX.

    Default model: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (multilingual, 15+ languages)
    English model: cross-encoder/ms-marco-MiniLM-L-12-v2 (English-optimized)
    No network calls - uses bundled or custom ONNX model.

    Usage:
        # Multilingual (default)
        reranker = RerankerEmbedder()
        scores = reranker.score("query", ["doc1", "doc2", "doc3"])

        # English-optimized (smaller, faster)
        reranker = RerankerEmbedder(language="en")
        scores = reranker.score("query", ["doc1", "doc2"])

        # Custom ONNX model
        reranker = RerankerEmbedder(model_dir="/path/to/my/reranker")
    """

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        device: str = "cpu",
        max_length: Optional[int] = None,
        onnx_file: str = "model.onnx",
        tokenizer_file: str = "tokenizer.json",
        language: Optional[str] = None,
    ):
        """
        Initialize reranker.

        Args:
            model_dir: Path to model directory (default: bundled)
            device: "cpu" or "cuda"
            max_length: Override max sequence length (default: 512)
            onnx_file: Name of ONNX model file
            tokenizer_file: Name of tokenizer file
            language: Language variant - None/"multi" for multilingual (default),
                      "en"/"english" for English-optimized model
        """
        # Determine which model to use based on language
        self.language = language
        if language in ("en", "english"):
            config_key = "reranker_en"
            default_dir = get_models_dir() / "reranker_en"
        else:
            config_key = "reranker"
            default_dir = get_models_dir() / "reranker"

        self.model_dir = Path(model_dir) if model_dir else default_dir
        self.device = device
        self.max_length = max_length or MODEL_CONFIG[config_key]["max_length"]
        self.onnx_file = onnx_file
        self.tokenizer_file = tokenizer_file

        self._session = None
        self._tokenizer = None
        self._has_token_type_ids = True  # Default, will be detected

    @property
    def session(self):
        """Lazy load ONNX session."""
        if self._session is None:
            self._load_model()
        return self._session

    @property
    def tokenizer(self):
        """Lazy load tokenizer."""
        if self._tokenizer is None:
            self._load_model()
        return self._tokenizer

    def _load_model(self):
        """Load ONNX model and tokenizer. Auto-downloads multilingual models if needed."""
        import onnxruntime as ort

        model_path = self.model_dir / self.onnx_file
        tokenizer_path = self.model_dir / self.tokenizer_file

        if not model_path.exists():
            # Auto-download for multilingual models
            if self.language not in ("en", "english"):
                print(f"Multilingual reranker model not found. Downloading...")
                try:
                    download_models(model_type="reranker", progress=True)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to auto-download multilingual reranker model.\n\n"
                        f"Options:\n"
                        f"  1. Manual download:  vectrixdb download-models --type reranker\n"
                        f"  2. Use English model: RerankerEmbedder(language='en')  [bundled, no download]\n"
                        f"  3. Use custom model:  RerankerEmbedder(model_dir='/path/to/model')\n\n"
                        f"Error: {e}\n\n"
                        f"Note: If downloads are blocked, use language='en' for bundled English models."
                    ) from e
            else:
                # English reranker model - auto-download from GitHub
                print(f"English reranker model not found. Downloading from GitHub...")
                try:
                    download_models(model_type="reranker_en", progress=True)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to download English reranker model.\n\n"
                        f"Error: {e}\n\n"
                        f"Try manual download: vectrixdb download-models --type reranker_en"
                    ) from e

        # Set up ONNX Runtime session
        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=providers
        )

        # Detect if model uses token_type_ids
        input_names = [inp.name for inp in self._session.get_inputs()]
        self._has_token_type_ids = "token_type_ids" in input_names

        self._tokenizer = SimpleTokenizer(tokenizer_path)

    def score(
        self,
        query: str,
        documents: List[str],
        batch_size: int = 16,
    ) -> np.ndarray:
        """
        Score query-document pairs.

        Args:
            query: Query text
            documents: List of documents to score

        Returns:
            Scores array, shape (n_documents,)
        """
        all_scores = []

        for i in range(0, len(documents), batch_size):
            batch_docs = documents[i:i + batch_size]
            batch_scores = self._score_batch(query, batch_docs)
            all_scores.extend(batch_scores)

        return np.array(all_scores, dtype=np.float32)

    def _score_batch(self, query: str, documents: List[str]) -> List[float]:
        """Score a batch of documents."""
        scores = []

        for doc in documents:
            inputs = self.tokenizer.encode_pair(query, doc, max_length=self.max_length)

            # Build ONNX input dict based on model requirements
            onnx_inputs = {
                "input_ids": inputs["input_ids"],
                "attention_mask": inputs["attention_mask"],
            }
            if self._has_token_type_ids:
                onnx_inputs["token_type_ids"] = inputs["token_type_ids"]

            outputs = self.session.run(
                None,
                onnx_inputs
            )

            # Get logits (cross-encoder outputs a single score)
            logits = outputs[0]

            # Sigmoid for probability
            if logits.shape[-1] == 1:
                score = 1 / (1 + np.exp(-logits[0, 0]))
            else:
                # Softmax if multiple outputs
                score = np.exp(logits[0, 1]) / np.sum(np.exp(logits[0]))

            scores.append(float(score))

        return scores

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
    ) -> List[Tuple[int, str, float]]:
        """
        Rerank documents by relevance to query.

        Args:
            query: Query text
            documents: List of documents
            top_k: Return top k results (None for all)

        Returns:
            List of (original_index, document, score) sorted by score
        """
        scores = self.score(query, documents)

        # Create (index, doc, score) tuples
        results = [(i, doc, score) for i, (doc, score) in enumerate(zip(documents, scores))]

        # Sort by score descending
        results.sort(key=lambda x: x[2], reverse=True)

        if top_k is not None:
            results = results[:top_k]

        return results

    def __call__(self, query: str, documents: List[str]) -> np.ndarray:
        """Alias for score()."""
        return self.score(query, documents)


# =============================================================================
# Late Interaction Embedder (ColBERT-style MaxSim)
# =============================================================================

class LateInteractionEmbedder:
    """
    Late interaction embedder using ONNX (ColBERT-style MaxSim scoring).

    Default model: BAAI/bge-m3 (1024 dim, multilingual, 100+ languages)
    English model: answerdotai/answerai-colbert-small-v1 (128 dim, English-only)
    No network calls - uses bundled or custom ONNX model.

    Produces token-level embeddings for MaxSim late interaction scoring.

    Usage:
        # Multilingual (default) - BGE-M3
        embedder = LateInteractionEmbedder()
        query_emb = embedder.encode_query("what is machine learning?")
        doc_emb = embedder.encode_document("Machine learning is a subset of AI...")
        score = embedder.max_sim(query_emb, doc_emb)

        # English-optimized (smaller, faster)
        embedder = LateInteractionEmbedder(language="en")
        score = embedder.max_sim(query_emb, doc_emb)

        # Custom ONNX model
        embedder = LateInteractionEmbedder(model_dir="/path/to/my/model")
    """

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        device: str = "cpu",
        dimension: Optional[int] = None,
        max_length: Optional[int] = None,
        onnx_file: str = "model.onnx",
        tokenizer_file: str = "tokenizer.json",
        language: Optional[str] = None,
    ):
        """
        Initialize late interaction embedder.

        Args:
            model_dir: Path to model directory (default: bundled)
            device: "cpu" or "cuda" (ONNX Runtime provider)
            dimension: Override embedding dimension
            max_length: Override max sequence length (default: 512)
            onnx_file: Name of ONNX model file
            tokenizer_file: Name of tokenizer file
            language: Language variant - None/"multi" for multilingual BGE-M3 (default),
                      "en"/"english" for English-optimized ColBERT model
        """
        # Determine which model to use based on language
        self.language = language
        if language in ("en", "english"):
            config_key = "late_interaction_en"
            default_dir = get_models_dir() / "colbert"
        else:
            config_key = "late_interaction"
            default_dir = get_models_dir() / "bge-m3"

        self.model_dir = Path(model_dir) if model_dir else default_dir
        self.device = device
        self.onnx_file = onnx_file
        self.tokenizer_file = tokenizer_file

        # Use provided values or defaults from config
        self.dimension = dimension or MODEL_CONFIG[config_key]["dimension"]
        self.max_length = max_length or MODEL_CONFIG[config_key]["max_length"]

        self._session = None
        self._tokenizer = None
        self._has_token_type_ids = True  # Default, will be detected

    @property
    def session(self):
        """Lazy load ONNX session."""
        if self._session is None:
            self._load_model()
        return self._session

    @property
    def tokenizer(self):
        """Lazy load tokenizer."""
        if self._tokenizer is None:
            self._load_model()
        return self._tokenizer

    def _load_model(self):
        """Load ONNX model and tokenizer. Auto-downloads multilingual models if needed."""
        import onnxruntime as ort

        model_path = self.model_dir / self.onnx_file
        tokenizer_path = self.model_dir / self.tokenizer_file

        if not model_path.exists():
            # Auto-download for multilingual models (BGE-M3)
            if self.language not in ("en", "english"):
                print(f"Multilingual late interaction model (BGE-M3) not found. Downloading...")
                try:
                    download_models(model_type="late_interaction", progress=True)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to auto-download multilingual late interaction model (BGE-M3).\n\n"
                        f"Options:\n"
                        f"  1. Manual download:  vectrixdb download-models --type late_interaction\n"
                        f"  2. Use English model: LateInteractionEmbedder(language='en')  [bundled, no download]\n"
                        f"  3. Use custom model:  LateInteractionEmbedder(model_dir='/path/to/model')\n\n"
                        f"Error: {e}\n\n"
                        f"Note: If downloads are blocked, use language='en' for bundled English ColBERT model."
                    ) from e
            else:
                # English ColBERT model - auto-download from GitHub
                print(f"English ColBERT model not found. Downloading from GitHub...")
                try:
                    download_models(model_type="late_interaction_en", progress=True)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to download English ColBERT model.\n\n"
                        f"Error: {e}\n\n"
                        f"Try manual download: vectrixdb download-models --type late_interaction_en"
                    ) from e

        # Set up ONNX Runtime session
        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=providers
        )

        # Detect if model uses token_type_ids
        input_names = [inp.name for inp in self._session.get_inputs()]
        self._has_token_type_ids = "token_type_ids" in input_names

        self._tokenizer = SimpleTokenizer(tokenizer_path)

    def _build_onnx_inputs(self, inputs: dict) -> dict:
        """Build ONNX inputs based on model requirements."""
        onnx_inputs = {
            "input_ids": inputs["input_ids"],
            "attention_mask": inputs["attention_mask"],
        }
        if self._has_token_type_ids:
            onnx_inputs["token_type_ids"] = inputs["token_type_ids"]
        return onnx_inputs

    def encode_query(
        self,
        query: str,
        normalize: bool = True,
    ) -> np.ndarray:
        """
        Encode query into token-level embeddings.

        Args:
            query: Query text
            normalize: L2 normalize embeddings

        Returns:
            Array of shape (num_tokens, dimension)
        """
        # Tokenize with shorter max length for queries
        inputs = self.tokenizer.encode(
            query,
            max_length=min(32, self.max_length),
            padding=True,
            truncation=True,
        )

        # Run ONNX inference
        outputs = self.session.run(None, self._build_onnx_inputs(inputs))

        # Get token embeddings (batch, seq_len, hidden_dim)
        token_embeddings = outputs[0][0]  # Remove batch dimension

        # Get attention mask to filter padding
        attention_mask = inputs["attention_mask"][0]

        # Filter out padding tokens and reduce to ColBERT dimension
        valid_tokens = attention_mask == 1
        embeddings = token_embeddings[valid_tokens][:, :self.dimension]

        if normalize:
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / (norms + 1e-8)

        return embeddings.astype(np.float32)

    def encode_document(
        self,
        document: str,
        normalize: bool = True,
    ) -> np.ndarray:
        """
        Encode document into token-level embeddings.

        Args:
            document: Document text
            normalize: L2 normalize embeddings

        Returns:
            Array of shape (num_tokens, dimension)
        """
        # Tokenize with full max length for documents
        inputs = self.tokenizer.encode(
            document,
            max_length=self.max_length,
            padding=True,
            truncation=True,
        )

        # Run ONNX inference
        outputs = self.session.run(None, self._build_onnx_inputs(inputs))

        # Get token embeddings
        token_embeddings = outputs[0][0]

        # Get attention mask to filter padding
        attention_mask = inputs["attention_mask"][0]

        # Filter out padding tokens and reduce to ColBERT dimension
        valid_tokens = attention_mask == 1
        embeddings = token_embeddings[valid_tokens][:, :self.dimension]

        if normalize:
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / (norms + 1e-8)

        return embeddings.astype(np.float32)

    def encode_documents(
        self,
        documents: List[str],
        normalize: bool = True,
        batch_size: int = 8,
    ) -> List[np.ndarray]:
        """
        Encode multiple documents into token-level embeddings.

        Args:
            documents: List of document texts
            normalize: L2 normalize embeddings
            batch_size: Batch size for processing

        Returns:
            List of arrays, each of shape (num_tokens, dimension)
        """
        results = []
        for doc in documents:
            embeddings = self.encode_document(doc, normalize=normalize)
            results.append(embeddings)
        return results

    def max_sim(
        self,
        query_embeddings: np.ndarray,
        doc_embeddings: np.ndarray,
    ) -> float:
        """
        Calculate MaxSim score between query and document.

        MaxSim = sum over query tokens of max similarity to any doc token.
        This is the core late interaction scoring mechanism.

        Args:
            query_embeddings: (num_query_tokens, dim)
            doc_embeddings: (num_doc_tokens, dim)

        Returns:
            MaxSim score (higher = more relevant)
        """
        if len(query_embeddings) == 0 or len(doc_embeddings) == 0:
            return 0.0

        # Compute similarity matrix (query_tokens x doc_tokens)
        # Embeddings are already normalized, so dot product = cosine similarity
        similarity_matrix = np.dot(query_embeddings, doc_embeddings.T)

        # MaxSim: for each query token, take max similarity to any doc token
        max_sims = np.max(similarity_matrix, axis=1)

        # Sum of max similarities
        return float(np.sum(max_sims))

    def score(
        self,
        query: str,
        documents: List[str],
    ) -> np.ndarray:
        """
        Score documents against a query using MaxSim.

        Args:
            query: Query text
            documents: List of documents

        Returns:
            Scores array, shape (n_documents,)
        """
        query_emb = self.encode_query(query)
        scores = []

        for doc in documents:
            doc_emb = self.encode_document(doc)
            score = self.max_sim(query_emb, doc_emb)
            scores.append(score)

        return np.array(scores, dtype=np.float32)

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
    ) -> List[Tuple[int, str, float]]:
        """
        Rerank documents by MaxSim relevance to query.

        Args:
            query: Query text
            documents: List of documents
            top_k: Return top k results (None for all)

        Returns:
            List of (original_index, document, score) sorted by score
        """
        scores = self.score(query, documents)

        # Create (index, doc, score) tuples
        results = [(i, doc, float(score)) for i, (doc, score) in enumerate(zip(documents, scores))]

        # Sort by score descending
        results.sort(key=lambda x: x[2], reverse=True)

        if top_k is not None:
            results = results[:top_k]

        return results

    def __call__(self, query: str, documents: List[str]) -> np.ndarray:
        """Alias for score()."""
        return self.score(query, documents)


# =============================================================================
# Graph Extractor (mREBEL Triplet Extraction for GraphRAG)
# =============================================================================

@dataclass
class Triplet:
    """A single extracted triplet (head, relation, tail)."""
    head: str
    head_type: str
    relation: str
    tail: str
    tail_type: str

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "head": self.head,
            "head_type": self.head_type,
            "relation": self.relation,
            "tail": self.tail,
            "tail_type": self.tail_type,
        }


class _SentencePieceWrapper:
    """
    Wrapper around SentencePiece to provide a tokenizer interface
    compatible with the mREBEL extractor.
    """

    def __init__(self, spm_path: Path):
        import sentencepiece as spm
        self.sp = spm.SentencePieceProcessor()
        self.sp.Load(str(spm_path))
        self.pad_token_id = 0  # Standard pad token
        self.eos_token_id = 2  # Standard eos token
        self.bos_token_id = 1  # Standard bos token

    def __call__(self, text: str, return_tensors: str = "np", max_length: int = 256,
                 truncation: bool = True, padding: str = "max_length"):
        """Tokenize text and return input_ids and attention_mask."""
        ids = self.sp.EncodeAsIds(text)

        # Truncate if needed
        if truncation and len(ids) > max_length - 2:  # Leave room for special tokens
            ids = ids[:max_length - 2]

        # Add bos and eos tokens
        ids = [self.bos_token_id] + ids + [self.eos_token_id]

        # Create attention mask
        attention_mask = [1] * len(ids)

        # Pad if needed
        if padding == "max_length":
            pad_length = max_length - len(ids)
            if pad_length > 0:
                ids = ids + [self.pad_token_id] * pad_length
                attention_mask = attention_mask + [0] * pad_length

        if return_tensors == "np":
            return {
                "input_ids": np.array([ids], dtype=np.int64),
                "attention_mask": np.array([attention_mask], dtype=np.int64),
            }
        return {"input_ids": ids, "attention_mask": attention_mask}

    def decode(self, ids, skip_special_tokens: bool = False):
        """Decode token IDs back to text."""
        if isinstance(ids, np.ndarray):
            ids = ids.tolist()
        # Get vocabulary size to filter out-of-range tokens
        vocab_size = self.sp.GetPieceSize()
        # Filter out special tokens and out-of-range tokens
        valid_ids = []
        for i in ids:
            if skip_special_tokens and i in (self.pad_token_id, self.bos_token_id, self.eos_token_id):
                continue
            if 0 <= i < vocab_size:
                valid_ids.append(i)
        return self.sp.DecodeIds(valid_ids)


class GraphExtractor:
    """
    Graph extractor for GraphRAG (mREBEL triplet extraction).

    Extracts (head, relation, tail) triplets from text without LLM.
    Uses Babelscape/mrebel-large model converted to ONNX.

    Supports 18 languages: ar, ca, de, el, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, sv, vi, zh

    Usage:
        extractor = GraphExtractor()
        triplets = extractor.extract("Albert Einstein was born in Germany.")
        # Returns: [Triplet(head="Albert Einstein", relation="country of birth", tail="Germany")]
    """

    # Special tokens used by mREBEL
    TRIPLET_START = "<triplet>"
    SUBJECT_START = "<subj>"
    OBJECT_START = "<obj>"

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        device: str = "cpu",
        max_length: Optional[int] = None,
    ):
        """
        Initialize REBEL extractor.

        Args:
            model_dir: Path to model directory (default: bundled)
            device: "cpu" or "cuda"
            max_length: Override max sequence length
        """
        self.model_dir = Path(model_dir) if model_dir else (get_models_dir() / "rebel")
        self.device = device
        self.max_length = max_length or MODEL_CONFIG["rebel"]["max_length"]

        self._encoder_session = None
        self._decoder_session = None
        self._tokenizer = None
        self._config = None

    def _load_model(self):
        """Load ONNX encoder/decoder and tokenizer."""
        if self._encoder_session is not None:
            return

        import onnxruntime as ort

        config = MODEL_CONFIG["rebel"]
        encoder_path = self.model_dir / config["onnx_encoder_file"]
        decoder_path = self.model_dir / config["onnx_decoder_file"]
        tokenizer_path = self.model_dir / config["tokenizer_file"]
        config_path = self.model_dir / config["config_file"]

        if not encoder_path.exists():
            print(f"GraphRAG extraction model (mREBEL) not found. Downloading...")
            try:
                download_models(model_type="rebel", progress=True)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to auto-download GraphRAG extraction model (mREBEL).\n\n"
                    f"Options:\n"
                    f"  1. Manual download:  vectrixdb download-models --type rebel\n"
                    f"  2. Use custom model:  GraphExtractor(model_dir='/path/to/model')\n\n"
                    f"Error: {e}\n\n"
                    f"Note: The mREBEL model (~700MB) supports 18 languages for triplet extraction.\n"
                    f"If downloads are blocked, you can use OpenAI or other LLM-based extraction instead."
                ) from e

        # Set up ONNX Runtime sessions
        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4

        self._encoder_session = ort.InferenceSession(
            str(encoder_path),
            sess_options=sess_options,
            providers=providers
        )

        self._decoder_session = ort.InferenceSession(
            str(decoder_path),
            sess_options=sess_options,
            providers=providers
        )

        # Load tokenizer - try transformers first, then fall back to direct sentencepiece
        try:
            from transformers import AutoTokenizer
            self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        except Exception as e:
            # AutoTokenizer failed, try direct SentencePiece
            try:
                import sentencepiece as spm
                self._tokenizer = _SentencePieceWrapper(tokenizer_path)
            except ImportError:
                raise ImportError(
                    "mREBEL model requires either 'transformers' or 'sentencepiece' library.\n"
                    f"AutoTokenizer error: {e}\n"
                    "Install with: pip install sentencepiece"
                )

        # Load config if exists
        if config_path.exists():
            with open(config_path, "r") as f:
                self._config = json.load(f)

    def extract(
        self,
        text: str,
        max_triplets: int = 20,
    ) -> List[Triplet]:
        """
        Extract triplets from text.

        Args:
            text: Input text
            max_triplets: Maximum number of triplets to extract

        Returns:
            List of Triplet objects
        """
        self._load_model()

        # Tokenize input - both transformers and our SentencePiece wrapper use __call__
        inputs = self._tokenizer(
            text,
            return_tensors="np",
            max_length=self.max_length,
            truncation=True,
            padding="max_length",
        )
        input_ids = inputs["input_ids"]
        attention_mask = inputs["attention_mask"]

        # Run encoder
        encoder_outputs = self._encoder_session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }
        )
        encoder_hidden_states = encoder_outputs[0]

        # Generate with decoder (greedy decoding)
        generated_ids = self._greedy_decode(
            encoder_hidden_states,
            attention_mask,
            max_length=256,
        )

        # Decode tokens to text
        if hasattr(self._tokenizer, 'decode'):
            output_text = self._tokenizer.decode(generated_ids[0], skip_special_tokens=False)
        else:
            output_text = self._simple_decode(generated_ids[0])

        # Parse triplets from output
        triplets = self._parse_triplets(output_text)

        return triplets[:max_triplets]

    def _greedy_decode(
        self,
        encoder_hidden_states: np.ndarray,
        encoder_attention_mask: np.ndarray,
        max_length: int = 256,
    ) -> np.ndarray:
        """Greedy decoding for seq2seq generation."""
        # Start with BOS token (for mBART it's usually 0 or 2)
        bos_token_id = 0
        eos_token_id = 2

        if self._config:
            bos_token_id = self._config.get("decoder_start_token_id", 0)
            eos_token_id = self._config.get("eos_token_id", 2)

        batch_size = encoder_hidden_states.shape[0]
        generated = np.array([[bos_token_id]] * batch_size, dtype=np.int64)

        for _ in range(max_length):
            # Run decoder - mREBEL only needs input_ids, encoder_hidden_states, encoder_attention_mask
            try:
                outputs = self._decoder_session.run(
                    None,
                    {
                        "input_ids": generated,
                        "encoder_hidden_states": encoder_hidden_states,
                        "encoder_attention_mask": encoder_attention_mask,
                    }
                )
            except Exception:
                # Some models have different input names
                try:
                    outputs = self._decoder_session.run(
                        None,
                        {
                            "decoder_input_ids": generated,
                            "encoder_hidden_states": encoder_hidden_states,
                            "encoder_attention_mask": encoder_attention_mask,
                        }
                    )
                except Exception:
                    break

            # Get logits for last token
            logits = outputs[0][:, -1, :]

            # Greedy: pick highest probability token
            next_token = np.argmax(logits, axis=-1, keepdims=True)

            # Append to generated sequence
            generated = np.concatenate([generated, next_token], axis=1)

            # Check for EOS
            if np.all(next_token == eos_token_id):
                break

        return generated

    def _simple_decode(self, token_ids: np.ndarray) -> str:
        """Simple decoding using vocabulary."""
        if hasattr(self._tokenizer, '_vocab_inv'):
            tokens = [self._tokenizer._vocab_inv.get(int(t), "") for t in token_ids]
            return " ".join(tokens)
        return ""

    def _parse_triplets(self, text: str) -> List[Triplet]:
        """
        Parse mREBEL output format into triplets.

        mREBEL output formats:
        Format 1 (original): <triplet> head <subj> relation <obj> tail
        Format 2 (ONNX): Various patterns with markers:
          - __sv__ = subject value (head entity)
          - __uk__ = object marker
          - __vi__ = via (intermediate entity)
          - __tn__, __zu__, __wo__, __xh__ = relation type markers
        """
        triplets = []
        import re

        # Clean up text
        text = text.replace("<s>", "").replace("</s>", "").replace("<pad>", "").strip()

        # Relation type markers (all end a triplet)
        RELATION_MARKERS = r'__tn__|__zu__|__wo__|__xh__|__yo__'
        # Separator markers (between head and tail)
        SEPARATOR_MARKERS = r'__uk__|__tn__|__yo__'

        # Try Format 2 (ONNX mREBEL)
        if "__sv__" in text:
            # Split by __sv__ to find individual triplet blocks
            # Format: <type> __sv__ content __sv__ content ...
            sv_pattern = r'(?:<(\w+)>)?\s*__sv__\s*'
            parts = re.split(sv_pattern, text)

            # parts: ['', type1, content1, type2, content2, ...]
            i = 1
            while i < len(parts) - 1:
                entity_type = parts[i] if parts[i] else "entity"
                content = parts[i + 1] if i + 1 < len(parts) else ""

                if content:
                    # Extract triplet from content block
                    # Find the LAST relation marker that has text after it
                    # Pattern: HEAD [...] TAIL RELATION_MARKER RELATION

                    # Find all relation markers and their positions
                    all_markers = list(re.finditer(f'({RELATION_MARKERS})\\s*([^_<]+?)(?=__|<|$)', content))

                    if all_markers:
                        # Use the last marker as the relation
                        last_match = all_markers[-1]
                        relation = last_match.group(2).strip()
                        head_tail_part = content[:last_match.start()].strip()

                        # Parse head and tail from head_tail_part
                        # Priority: __uk__ > __yo__ > __tn__ (as separator)

                        if "__uk__" in head_tail_part:
                            # Split by first __uk__ to get head and tail
                            uk_parts = head_tail_part.split("__uk__", 1)
                            head = uk_parts[0].strip()
                            tail = uk_parts[1].strip() if len(uk_parts) > 1 else ""
                        elif "__yo__" in head_tail_part:
                            # Split by __yo__ for head/tail separation
                            yo_parts = head_tail_part.split("__yo__", 1)
                            head = yo_parts[0].strip()
                            tail = yo_parts[1].strip() if len(yo_parts) > 1 else ""
                        elif "__tn__" in head_tail_part:
                            # Split by first __tn__ for head/tail separation
                            tn_parts = head_tail_part.split("__tn__", 1)
                            head = tn_parts[0].strip()
                            tail = tn_parts[1].strip() if len(tn_parts) > 1 else ""
                        else:
                            # No separator - the whole thing is head, no tail
                            head = head_tail_part
                            tail = ""

                        # Clean up intermediate markers (__vi__, __uk__, etc.) from head/tail
                        head = re.sub(r'\s*__\w+__\s*', ', ', head).strip(', ')
                        tail = re.sub(r'\s*__\w+__\s*', ', ', tail).strip(', ')

                        if head and tail and relation:
                            triplets.append(Triplet(
                                head=head,
                                head_type=entity_type.lower(),
                                relation=relation,
                                tail=tail,
                                tail_type="entity",
                            ))

                i += 2

            if triplets:
                return triplets

        # Try Format 1 (original mREBEL): <triplet> head <subj> relation <obj> tail
        parts = text.split(self.TRIPLET_START)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            try:
                # Parse: head <subj> relation <obj> tail
                if self.SUBJECT_START in part and self.OBJECT_START in part:
                    # Split by <subj>
                    head_rest = part.split(self.SUBJECT_START)
                    if len(head_rest) >= 2:
                        head = head_rest[0].strip()
                        rest = head_rest[1]

                        # Split by <obj>
                        rel_tail = rest.split(self.OBJECT_START)
                        if len(rel_tail) >= 2:
                            relation = rel_tail[0].strip()
                            tail = rel_tail[1].strip()

                            # Remove any trailing markers
                            tail = tail.split("<")[0].strip()

                            if head and relation and tail:
                                triplets.append(Triplet(
                                    head=head,
                                    head_type="entity",
                                    relation=relation,
                                    tail=tail,
                                    tail_type="entity",
                                ))
            except Exception:
                continue

        return triplets

    def extract_batch(
        self,
        texts: List[str],
        max_triplets_per_text: int = 20,
    ) -> List[List[Triplet]]:
        """
        Extract triplets from multiple texts.

        Args:
            texts: List of input texts
            max_triplets_per_text: Maximum triplets per text

        Returns:
            List of triplet lists
        """
        results = []
        for text in texts:
            triplets = self.extract(text, max_triplets=max_triplets_per_text)
            results.append(triplets)
        return results

    def __call__(self, text: str) -> List[Triplet]:
        """Alias for extract()."""
        return self.extract(text)
