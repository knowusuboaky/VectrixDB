"""
Embedded Models for VectrixDB

ONNX-based models that run locally with zero network calls.
Core models bundled with pip install (~890MB).
Additional models auto-downloaded from GitHub on first use.

Bundled Dense Models (no download needed):
- DenseEmbedder(language="en"): intfloat/e5-small-v2 (~33MB INT8)
- DenseEmbedder(model="bge-small"): BAAI/bge-small-en-v1.5 (~127MB FP32) - higher quality
- DenseEmbedder(model="e5-small-fp32"): intfloat/e5-small-v2 (~127MB FP32)

Bundled Sparse Models (no download needed):
- SparseEmbedder(): BM25 vocabulary-based (~1MB)
- SparseEmbedder(model="splade"): SPLADE++ neural sparse (~508MB FP32) - ~29% better than BM25

Bundled Reranker Models (no download needed):
- RerankerEmbedder(model="L6"): ms-marco-MiniLM-L-6-v2 (~87MB FP32) - smaller/faster

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
import logging

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

from ..exceptions import ModelNotFoundError


__all__ = [
    "MODEL_CONFIG",
    "GITHUB_REPO",
    "GITHUB_RELEASE_BASE",
    "get_models_dir",
    "is_models_installed",
    "download_models",
    "SimpleTokenizer",
    "DenseEmbedder",
    "SparseEmbedder",
    "RerankerEmbedder",
    "LateInteractionEmbedder",
    "Triplet",
    "GraphExtractor",
    "REBELExtractor",
]


# ============================================================================
# SETTINGS: the model registry, and the release they download from
# ============================================================================
#
# Every model the library knows: its files, its directory, its dimension and
# its pooling, and the GitHub release the ones not bundled are fetched from.
# Model configuration

MODEL_CONFIG: Dict[str, Dict[str, Any]] = {
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
        "pooling": "mean",
        "description": "The English default before 2.2. Fetched once on first use so older collections keep working; not in the wheel since 2.2",
    },
    "bge_small_en": {
        "name": "bge-small-en-v1.5",
        "dimension": 384,
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 33,
        "huggingface_id": "BAAI/bge-small-en-v1.5",
        "github_release": "bge-small-en",
        "languages": "english",
        "quantization": "int8",
        "pooling": "cls",
        "description": "The English default from 2.2: SciFact nDCG@10 0.72 against 0.65 for e5-small-v2",
    },
    "bge_base_en": {
        "name": "bge-base-en-v1.5",
        "dimension": 768,
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 110,  # INT8 quantized
        "huggingface_id": "BAAI/bge-base-en-v1.5",
        "github_release": "v1.9.0",  # published with 1.9.0, beside the code
        "languages": "english",
        "quantization": "int8",
        "description": "Higher quality English embeddings (768 dim, +15-20% vs e5-small)",
    },
    "e5_small": {
        "name": "e5-small-v2",
        "dimension": 384,
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 127,
        "huggingface_id": "intfloat/e5-small-v2",
        "github_release": "e5-small",
        "languages": "english",
        "quantization": "fp32",
    },
    "sparse": {
        "name": "bm25",
        "vocab_file": "vocab.json",
        "idf_file": "idf.json",
        "config_file": "config.json",
        "size_mb": 1,
        "languages": "any",
    },
    "splade_pp_en": {
        "name": "Splade_PP_en_v1",
        "vocab_size": 30522,
        "max_length": 256,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 508,
        "huggingface_id": "prithivida/Splade_PP_en_v1",
        "github_release": "splade-en",
        "languages": "english",
        "quantization": "fp32",
        "description": "Neural sparse embeddings (SPLADE++), ~29% better than BM25",
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
    "reranker_en_l6": {
        "name": "ms-marco-MiniLM-L-6-v2",
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 87,
        "huggingface_id": "cross-encoder/ms-marco-MiniLM-L6-v2",
        "github_release": "reranker-en-l6",
        "languages": "english",
        "quantization": "fp32",
        "description": "Smaller/faster English reranker (L6 vs L12)",
    },
    "bge_reranker_base": {
        "name": "bge-reranker-base",
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 110,  # INT8 quantized
        "huggingface_id": "BAAI/bge-reranker-base",
        "github_release": "v1.9.0",  # published with 1.9.0, beside the code
        "languages": "english",
        "quantization": "int8",
        "description": "Higher quality English reranker (+10-15% vs L12)",
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
    "colbert_v2": {
        "name": "colbertv2.0",
        "dimension": 128,
        "max_length": 512,
        "onnx_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "config_file": "config.json",
        "size_mb": 110,  # INT8 quantized
        "huggingface_id": "colbert-ir/colbertv2.0",
        "github_release": "v1.9.0",  # published with 1.9.0, beside the code
        "languages": "english",
        "quantization": "int8",
        "description": "Higher quality ColBERT v2 late interaction (+5-10%)",
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


# ============================================================================
# WHERE THE MODELS ARE, AND WHETHER THEY ARE HERE
# ============================================================================
#
# INPUT   a model type
# OUTPUT  the models directory; the directory that holds this model; whether
#         it is installed; the models downloaded and converted, when asked
#
# Core models are bundled with pip install; the rest are fetched once, and
# only when asked.


def get_models_dir() -> Path:
    """Get the models directory path."""
    # Check environment variable first
    env_path = os.environ.get("VECTRIXDB_MODELS_DIR")
    if env_path:
        return Path(env_path)

    # Default: package directory
    return Path(__file__).parent / "data"


# Three model types do not install into a directory named after themselves:
# the multilingual late-interaction model is BGE-M3, and both the English
# ColBERT and the "colbert" alias share one directory. Everything else uses
# its own name. The downloader and this check read the same map.
MODEL_DIRS = {
    "late_interaction": "bge-m3",
    "late_interaction_en": "colbert",
    "colbert": "colbert",
}

#: Where a model may also live. The bundled cross-encoder ships as
#: ``reranker_en`` while the downloader writes to ``reranker``, so a reranker
#: is installed if either directory has it. Checking only the second answered
#: False on a correct install, and the twelve tests gated on that skipped
#: everywhere, CI included.
MODEL_DIR_ALTERNATIVES = {
    "reranker": ("reranker_en",),
    "dense": ("bge_small_en", "dense_en"),
}

#: The model directories the wheel carries. Everything else is a download;
#: pyproject.toml's wheel excludes are the other half of this list, and a
#: test holds the two together.
WHEEL_MODEL_DIRS = ("bge_small_en", "reranker_en", "colbert", "sparse")


def model_dir_name(model_type: str) -> str:
    """The directory under the models directory that holds this model."""
    return MODEL_DIRS.get(model_type, model_type)


def is_models_installed(model_type: str = "all", *, exact: bool = False) -> bool:
    """
    Check if models are installed.

    Args:
        model_type: "dense", "sparse", "reranker", "colbert", "rebel", or "all"
        exact: Count only the type's own directory. Left off, a bundled English
            model answers for its kind, so ``"reranker"`` is True on a fresh
            install: a reranker is here. On, ``"reranker"`` means the
            multilingual reranker itself, which is the question a download or
            a list of models asks.

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
        config = MODEL_CONFIG.get(mt, {})

        if mt == "sparse":
            wanted = config.get("vocab_file", "vocab.json")
        elif mt == "rebel":
            wanted = config.get("onnx_encoder_file", "encoder.onnx")
        else:
            wanted = config.get("onnx_file", "model.onnx")

        candidates: Tuple[str, ...] = (model_dir_name(mt),)
        if not exact:
            candidates += MODEL_DIR_ALTERNATIVES.get(mt, ())
        if not any((models_dir / name / wanted).exists() for name in candidates):
            return False

    return True


def download_models(model_type: str = "all", force: bool = False, progress: bool = True) -> None:
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

    # Only the type's own directory counts: the bundled English model is not
    # the multilingual one, and counting it meant neither ever downloaded.
    for mt in types_to_download:
        if force or not is_models_installed(mt, exact=True):
            downloader.download(mt)


# ============================================================================
# THE TOKENIZER: minimal, offline
# ============================================================================
#
# INPUT   tokenizer.json in Hugging Face's format
# OUTPUT  ids and attention masks for a batch of texts, with the model's own
#         truncation
#
# Enough of a tokenizer to run the bundled models, with no dependency on the
# tokenizers package.

logger = logging.getLogger(__name__)


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
            raise ModelNotFoundError(
                f"Tokenizer not found: {self.tokenizer_path}\nRun: vectrixdb download-models"
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
            self._special_tokens.get("<pad>")
            or self._special_tokens.get("[PAD]")
            or self._vocab.get("<pad>")
            or self._vocab.get("[PAD]", 1)
        )
        self.cls_token_id = (
            self._special_tokens.get("<s>")
            or self._special_tokens.get("[CLS]")
            or self._vocab.get("<s>")
            or self._vocab.get("[CLS]", 0)
        )
        self.sep_token_id = (
            self._special_tokens.get("</s>")
            or self._special_tokens.get("[SEP]")
            or self._vocab.get("</s>")
            or self._vocab.get("[SEP]", 2)
        )
        self.unk_token_id = (
            self._special_tokens.get("<unk>")
            or self._special_tokens.get("[UNK]")
            or self._vocab.get("<unk>")
            or self._vocab.get("[UNK]", 3)
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
            token_ids = token_ids[: max_length - 1] + [self.sep_token_id]

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
        pad_to_longest: bool = False,
        **kwargs,
    ) -> Dict[str, np.ndarray]:
        """Encode multiple texts as one padded batch."""
        max_length = max_length or self._max_length
        padding = kwargs.pop("padding", True)

        encoded = [
            self.encode(text, max_length=max_length, padding=False, **kwargs) for text in texts
        ]

        # Pad to max_length by default. The bundled dense models are INT8 with
        # dynamic quantisation, and their activation ranges were measured with
        # 512-token padding: padding to the longest text instead cut the dense
        # MRR on the fixture question set from 0.955 to 0.929 (0.945 at 128),
        # and made new queries drift from vectors users have already stored.
        # Every consumer masks padding, so ``pad_to_longest`` is correct for
        # a model that is not padding-sensitive, and a fp32 or statically
        # quantised re-export would let it become the default: 200 short
        # texts take 0.65 s that way and 34 s at 512.
        longest = max((int(e["input_ids"].shape[1]) for e in encoded), default=1)
        if not padding:
            width = longest
        elif pad_to_longest:
            width = min(max_length, -(-longest // _PAD_BUCKET) * _PAD_BUCKET)
        else:
            width = max_length

        n = len(encoded)
        input_ids = np.full((n, width), self.pad_token_id, dtype=np.int64)
        attention_mask = np.zeros((n, width), dtype=np.int64)
        token_type_ids = np.zeros((n, width), dtype=np.int64)
        for row, e in enumerate(encoded):
            ids = e["input_ids"][0][:width]
            input_ids[row, : len(ids)] = ids
            attention_mask[row, : len(ids)] = e["attention_mask"][0][: len(ids)]
            token_type_ids[row, : len(ids)] = e["token_type_ids"][0][: len(ids)]

        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
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
            [self.cls_token_id]
            + token_ids_a
            + [self.sep_token_id]
            + token_ids_b
            + [self.sep_token_id]
        )

        # Token type IDs: 0 for first segment, 1 for second
        token_type_ids = (
            [0] * (len(token_ids_a) + 2)  # [CLS] + tokens_a + [SEP]
            + [1] * (len(token_ids_b) + 1)  # tokens_b + [SEP]
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
        words = re.findall(r"\b\w+\b|[^\w\s]", text)

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

    # A word longer than this is not a word. The greedy longest-match scan
    # below is quadratic in the length of what it is given, and each step
    # slices a fresh substring, so one unbroken token of a few thousand
    # characters takes tens of seconds and one of fifty thousand never
    # finishes. Real corpora are full of them: base64 blobs, minified
    # JavaScript, long URLs, hashes, DNA. The reference WordPiece
    # implementation caps the same way, at the same length, so nothing that
    # is actually a word tokenizes differently.
    MAX_CHARS_PER_WORD = 100

    def _wordpiece_tokenize(self, word: str) -> List[str]:
        """Tokenize a single word into wordpieces."""
        if not word:
            return []

        if len(word) > self.MAX_CHARS_PER_WORD:
            return ["[UNK]"]

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


# ============================================================================
# THE DENSE EMBEDDER
# ============================================================================
#
# INPUT   texts, or a query with its prefix
# OUTPUT  one normalised vector a text, from ONNX: bge-small-en-v1.5 in
#         English, multilingual-e5-small otherwise
#
# Padded to the longest text in each batch, which is what embed() does by
# default.


class DenseEmbedder:
    """
    Dense embedding model using ONNX.

    Default model: intfloat/multilingual-e5-small (384 dim, 100+ languages)
    English model: intfloat/e5-small-v2 (384 dim, English-optimized)
    No network calls - uses bundled or custom ONNX model.

    Available models:
        - "multilingual" / "multi" / None: multilingual-e5-small (100+ languages, INT8)
        - "e5-small" / "e5": e5-small-v2 (English, INT8, default for language="en")
        - "bge-small" / "bge": bge-small-en-v1.5 (English, FP32, higher quality)
        - "e5-small-fp32": e5-small-v2 (English, FP32)

    Usage:
        # Multilingual (default)
        embedder = DenseEmbedder()
        vectors = embedder.embed(["hello world", "how are you"])

        # English-optimized (smaller, faster for English)
        embedder = DenseEmbedder(language="en")
        vectors = embedder.embed(["hello world"])

        # BGE model (higher quality English embeddings)
        embedder = DenseEmbedder(model="bge-small")
        vectors = embedder.embed(["hello world"])

        # Custom ONNX model
        embedder = DenseEmbedder(model_dir="/path/to/my/model", dimension=768)
        vectors = embedder.embed(["hello world"])
    """

    # Model name aliases
    MODEL_ALIASES = {
        # Multilingual
        "multilingual": "dense",
        "multi": "dense",
        "multilingual-e5-small": "dense",
        # English INT8 (default for language="en")
        "e5-small": "dense_en",
        "e5": "dense_en",
        "e5-small-v2": "dense_en",
        # BGE English FP32 (small)
        "bge-small": "bge_small_en",
        "bge-small-en": "bge_small_en",
        "bge-small-en-v1.5": "bge_small_en",
        # BGE English INT8 (base - higher quality)
        "bge": "bge_base_en",
        "bge-base": "bge_base_en",
        "bge-base-en": "bge_base_en",
        "bge-base-en-v1.5": "bge_base_en",
        # E5 English FP32
        "e5-small-fp32": "e5_small",
        "e5-fp32": "e5_small",
    }

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        device: str = "cpu",
        dimension: Optional[int] = None,
        max_length: Optional[int] = None,
        onnx_file: str = "model.onnx",
        tokenizer_file: str = "tokenizer.json",
        language: Optional[str] = None,
        model: Optional[str] = None,
        pooling: Optional[str] = None,
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
            model: Specific model to use - "bge-small", "e5-small", "e5-small-fp32",
                   "multilingual". Overrides language parameter if specified.
        """
        # Determine which model to use
        self.language = language
        self.model_name = model

        # Model selection priority: model > language > default
        if model:
            # Resolve model alias
            config_key = self.MODEL_ALIASES.get(model.lower(), model.lower())
            if config_key not in MODEL_CONFIG:
                raise ValueError(
                    f"Unknown model: {model}. Available models: {list(self.MODEL_ALIASES.keys())}"
                )
            default_dir = get_models_dir() / config_key
        elif language in ("en", "english"):
            config_key = "bge_small_en"
            default_dir = get_models_dir() / "bge_small_en"
        else:
            config_key = "dense"
            default_dir = get_models_dir() / "dense"

        self._config_key = config_key
        self.model_dir = Path(model_dir) if model_dir else default_dir
        self.device = device
        self.onnx_file = onnx_file
        self.tokenizer_file = tokenizer_file

        # Use provided values or defaults from config
        self.dimension = dimension or MODEL_CONFIG[config_key]["dimension"]
        self.max_length = max_length or MODEL_CONFIG[config_key]["max_length"]

        self._session: Any = None
        self._tokenizer: Any = None
        self._has_token_type_ids = True  # Default, will be detected
        # Pooling is a property of the model: mean for e5 and MiniLM, the
        # [CLS] vector for bge and arctic. The config knows; a caller may
        # override for a custom model directory.
        if pooling is None:
            pooling = str(MODEL_CONFIG.get(config_key, {}).get("pooling", "mean"))
        if pooling not in ("mean", "cls"):
            raise ValueError(f"pooling must be 'mean' or 'cls', got {pooling!r}")
        self.pooling = pooling

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

        # Models that should be available (bundled or cached from GitHub)
        BUNDLED_MODELS = {"bge_small_en", "e5_small", "bge_base_en"}

        if not model_path.exists():
            # e5-small-v2 left the wheel in 2.2. A collection written before
            # 2.2 was built with it, so it is fetched once on first use, or
            # the collection is re-embedded with the current default; either
            # is one command, and the message names both.
            if getattr(self, "_config_key", None) == "dense_en":
                from .._net import auto_download_allowed
                from ..exceptions import ModelDownloadError

                if not auto_download_allowed():
                    raise ModelDownloadError(
                        "The e5-small-v2 model is not on this machine. Collections written "
                        "before VectrixDB 2.2 were built with it, and since 2.2 it is not in "
                        "the wheel. Two ways forward, each one command:\n"
                        "  fetch it once:  vectrixdb download-models --type dense_en\n"
                        "  or move the collection to the current default:  "
                        'Vectrix(name, path=..., dense_model="bge-small").reembed()\n'
                        "Or set VECTRIXDB_AUTO_DOWNLOAD=1 to allow first-use downloads."
                    )
                logger.info("e5-small-v2 not found; downloading once, as allowed")
                download_models(model_type="dense_en", progress=True)
            # Check if this is a bundled model
            elif hasattr(self, "_config_key") and self._config_key in BUNDLED_MODELS:
                # Check if it's a GitHub-hosted model vs truly bundled
                github_models = {"bge_base_en"}
                if self._config_key in github_models:
                    raise ModelNotFoundError(
                        f"Dense model not found: {model_path}\n\n"
                        f"This model must be downloaded from GitHub releases.\n"
                        f"For Databricks, cache models to a Volume first:\n"
                        f"  MODELS_VOLUME_PATH = '/Volumes/catalog/schema/vectrixdb_models/'\n"
                        f"  os.environ['VECTRIXDB_MODELS_DIR'] = MODELS_VOLUME_PATH\n\n"
                        # /releases/latest rather than a pinned tag: this named
                        # v1.9.0 while the package shipped 2.2.0.
                        f"Download: https://github.com/knowusuboaky/VectrixDB/releases/latest\n"
                        f"Unpack it into: {model_path.parent}"
                    )
                else:
                    raise ModelNotFoundError(
                        f"Dense model not found: {model_path}\n"
                        f"If this model is bundled, reinstalling restores it:\n"
                        f"  pip install --force-reinstall vectrixdb\n"
                        f"If it is not, fetch it from the releases page and\n"
                        f"unpack it into: {model_path.parent}"
                    )
            # A first-use download, which happens only when asked for.
            else:
                from .._net import auto_download_allowed, refuse_implicit

                if not auto_download_allowed():
                    raise refuse_implicit(
                        "The multilingual dense model", "vectrixdb download-models --type dense"
                    )
                logger.info("Multilingual dense model not found; downloading, as allowed")
                try:
                    download_models(model_type="dense", progress=True)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to auto-download multilingual dense model.\n\n"
                        f"Options:\n"
                        f"  1. Manual download:  vectrixdb download-models --type dense\n"
                        f"  2. Use English model: DenseEmbedder(language='en')  [bundled, no download]\n"
                        f"  3. Use BGE model: DenseEmbedder(model='bge-small')  [bundled, no download]\n"
                        f"  4. Use custom model:  DenseEmbedder(model_dir='/path/to/model')\n\n"
                        f"Error: {e}\n\n"
                        f"Note: If downloads are blocked, use bundled English models."
                    ) from e

        # Set up ONNX Runtime session
        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4

        self._session = ort.InferenceSession(
            str(model_path), sess_options=sess_options, providers=providers
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
        pad_to_longest: bool = True,
    ) -> np.ndarray:
        """
        Generate embeddings for texts.

        Args:
            texts: Single text or list of texts
            normalize: L2 normalize embeddings
            batch_size: Batch size for processing
            pad_to_longest: Pad each batch to its longest text instead of
                to max_length. Roughly fifty times faster on short texts.
                On the INT8 models the vectors differ slightly between the
                two settings; measured on SciFact the difference is within
                noise (0.645 against 0.647 nDCG@10), so this is the default.
                Pass False to reproduce vectors from a 2.1 collection.

        Returns:
            Embeddings array, shape (n_texts, 384)
        """
        if isinstance(texts, str):
            texts = [texts]

        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            embeddings = self._embed_batch(batch, pad_to_longest=pad_to_longest)
            all_embeddings.append(embeddings)

        result = np.vstack(all_embeddings)

        if normalize:
            norms = np.linalg.norm(result, axis=1, keepdims=True)
            result = result / (norms + 1e-8)

        return result.astype(np.float32)

    def _embed_batch(self, texts: List[str], pad_to_longest: bool = False) -> np.ndarray:
        """Embed a batch of texts."""
        # Tokenize
        inputs = self.tokenizer.encode_batch(
            texts,
            max_length=self.max_length,
            padding=True,
            truncation=True,
            pad_to_longest=pad_to_longest,
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

        # Pooling: mean over unmasked tokens (MiniLM, e5) or the [CLS] vector
        # (bge, arctic-embed), whichever the model was trained with.
        token_embeddings = outputs[0]  # (batch, seq_len, hidden_dim)
        attention_mask = inputs["attention_mask"]
        if getattr(self, "pooling", "mean") == "cls":
            return token_embeddings[:, 0, :]

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


# ============================================================================
# THE SPARSE EMBEDDER: BM25 and SPLADE
# ============================================================================
#
# INPUT   texts
# OUTPUT  one sparse vector a text, term weights by BM25 or by SPLADE's
#         learned expansion
#
# The same shape either way, so the sparse index does not know which.


class SparseEmbedder:
    """
    Sparse embedding model supporting BM25 and SPLADE.

    Available models:
        - "bm25" / None: Pure algorithmic BM25 (default, vocabulary-based)
        - "splade": Neural SPLADE++ model (~29% better than BM25)

    No network calls - uses bundled vocabulary/ONNX model.

    Usage:
        # BM25 (default)
        embedder = SparseEmbedder()
        sparse_vectors = embedder.embed(["hello world"])

        # SPLADE (neural, higher quality)
        embedder = SparseEmbedder(model="splade")
        sparse_vectors = embedder.embed(["hello world"])
    """

    # Model aliases
    MODEL_ALIASES = {
        "bm25": "sparse",
        "splade": "splade_pp_en",
        "splade++": "splade_pp_en",
        "splade-pp": "splade_pp_en",
    }

    # BM25 parameters
    k1: float = 1.5
    b: float = 0.75

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        model: Optional[str] = None,
        device: str = "cpu",
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
            model: Model to use - "bm25" (default) or "splade" (neural)
            device: "cpu" or "cuda" (for SPLADE only)
            k1: BM25 k1 parameter (term frequency saturation)
            b: BM25 b parameter (length normalization)
            vocab_file: Name of vocabulary file (BM25 only)
            idf_file: Name of IDF weights file (BM25 only)
            config_file: Name of config file
        """
        self.model_name = model
        self.device = device

        # Determine model type
        if model:
            config_key = self.MODEL_ALIASES.get(model.lower(), model.lower())
        else:
            config_key = "sparse"  # Default to BM25

        self._config_key = config_key
        self._is_splade = config_key == "splade_pp_en"

        if self._is_splade:
            # SPLADE neural model
            self.model_dir = Path(model_dir) if model_dir else (get_models_dir() / "splade_pp_en")
            config = MODEL_CONFIG.get("splade_pp_en", {})
            self.max_length = config.get("max_length", 256)
            self.vocab_size = config.get("vocab_size", 30522)
            self._session: Any = None
            self._tokenizer: Any = None
            self._has_token_type_ids = True
        else:
            # BM25 vocabulary-based
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

    def _load_splade_model(self):
        """Load SPLADE ONNX model and tokenizer."""
        if self._session is not None:
            return

        import onnxruntime as ort

        model_path = self.model_dir / "model.onnx"
        tokenizer_path = self.model_dir / "tokenizer.json"

        if not model_path.exists():
            raise ModelNotFoundError(
                f"SPLADE model not found: {model_path}\n"
                f"This model should be bundled with the package.\n"
                f"Try reinstalling: pip install --force-reinstall vectrixdb"
            )

        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4

        self._session = ort.InferenceSession(
            str(model_path), sess_options=sess_options, providers=providers
        )

        input_names = [inp.name for inp in self._session.get_inputs()]
        self._has_token_type_ids = "token_type_ids" in input_names

        self._tokenizer = SimpleTokenizer(tokenizer_path)

    def _load_vocab(self):
        """Load vocabulary and IDF values for BM25."""
        if self._loaded:
            return

        vocab_path = self.model_dir / self.vocab_file
        idf_path = self.model_dir / self.idf_file
        config_path = self.model_dir / self.config_file

        if vocab_path.exists():
            with open(vocab_path, "r", encoding="utf-8") as f:
                self._vocab = json.load(f)
        else:
            self._vocab = {}

        if idf_path.exists():
            with open(idf_path, "r", encoding="utf-8") as f:
                self._idf = json.load(f)

        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                self._avg_doc_len = config.get("avg_doc_len", 50.0)
                self.k1 = config.get("k1", self.k1)
                self.b = config.get("b", self.b)

        self._loaded = True

    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization for BM25."""
        text = text.lower()
        tokens = re.findall(r"\b\w+\b", text)
        tokens = [t for t in tokens if len(t) > 1]
        return tokens

    def embed(
        self,
        texts: Union[str, List[str]],
        batch_size: int = 32,
    ) -> List[Dict[int, float]]:
        """
        Generate sparse embeddings.

        Args:
            texts: Single text or list of texts
            batch_size: Batch size for SPLADE processing

        Returns:
            List of sparse vectors (dict of term_id -> weight)
        """
        if isinstance(texts, str):
            texts = [texts]

        if self._is_splade:
            return self._embed_splade(texts, batch_size)
        else:
            return self._embed_bm25(texts)

    def _embed_bm25(self, texts: List[str]) -> List[Dict[int, float]]:
        """Generate BM25 sparse embeddings."""
        self._load_vocab()

        results = []
        for text in texts:
            tokens = self._tokenize(text)
            term_freqs = Counter(tokens)
            doc_len = len(tokens)

            sparse_vec = {}
            for term, tf in term_freqs.items():
                if term not in self._vocab:
                    self._vocab[term] = len(self._vocab)

                term_id = self._vocab[term]
                idf = self._idf.get(term, 1.0)
                tf_component = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_len)
                )
                weight = idf * tf_component
                sparse_vec[term_id] = weight

            results.append(sparse_vec)

        return results

    def _embed_splade(self, texts: List[str], batch_size: int) -> List[Dict[int, float]]:
        """Generate SPLADE sparse embeddings."""
        self._load_splade_model()

        all_results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]

            inputs = self._tokenizer.encode_batch(
                batch,
                max_length=self.max_length,
                padding=True,
                truncation=True,
            )

            # SPLADE uses different input names: input_mask, segment_ids
            onnx_inputs = {
                "input_ids": inputs["input_ids"],
                "input_mask": inputs["attention_mask"],
                "segment_ids": inputs["token_type_ids"],
            }

            outputs = self._session.run(None, onnx_inputs)
            logits = outputs[0]

            # SPLADE activation: log(1 + ReLU(x))
            relu_log = np.log1p(np.maximum(logits, 0))

            # Max pooling across sequence, masked
            attention_mask = inputs["attention_mask"]
            mask_expanded = np.expand_dims(attention_mask, axis=-1)
            relu_log_masked = relu_log * mask_expanded + (1 - mask_expanded) * (-1e9)
            sparse_vectors = np.max(relu_log_masked, axis=1)

            for vec in sparse_vectors:
                sparse_dict = {}
                nonzero_indices = np.where(vec > 0)[0]
                for idx in nonzero_indices:
                    sparse_dict[int(idx)] = float(vec[idx])
                all_results.append(sparse_dict)

        return all_results

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

    def get_terms(self, text: str, top_k: int = 20) -> List[Tuple[str, float]]:
        """Get terms with their weights."""
        if self._is_splade:
            self._load_splade_model()
            sparse_vec = self.embed(text)[0]
            sorted_items = sorted(sparse_vec.items(), key=lambda x: x[1], reverse=True)
            results = []
            for token_id, weight in sorted_items[:top_k]:
                term = self._tokenizer._vocab_inv.get(token_id, f"[{token_id}]")
                results.append((term, weight))
            return results
        else:
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

            return sorted(results, key=lambda x: x[1], reverse=True)[:top_k]

    def __call__(self, texts: Union[str, List[str]]) -> List[Dict[int, float]]:
        """Alias for embed()."""
        return self.embed(texts)


# ============================================================================
# THE RERANKER: a cross-encoder
# ============================================================================
#
# INPUT   a query and candidate texts
# OUTPUT  a score a pair, batched by padded width so a long text does not pad
#         every short one
#
# Bucketed batches: the sequences grouped by width before they are padded.

#: Sequences are padded to a multiple of this many tokens, so a document's
#: score depends only on its own length, never on the batch it shares.
_PAD_BUCKET = 32


def _bucketed_batches(lengths):
    """Group sequence indices by padded width. Yields (width, [indices])."""
    groups: Dict[int, List[int]] = {}
    for i, length in enumerate(lengths):
        width = max(_PAD_BUCKET, ((length + _PAD_BUCKET - 1) // _PAD_BUCKET) * _PAD_BUCKET)
        groups.setdefault(width, []).append(i)
    for width in sorted(groups):
        yield width, groups[width]


class RerankerEmbedder:
    """
    Cross-encoder reranker using ONNX.

    Default model: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (multilingual, 15+ languages)
    English models:
        - "L12": cross-encoder/ms-marco-MiniLM-L-12-v2 (English, default for language="en")
        - "L6": cross-encoder/ms-marco-MiniLM-L-6-v2 (English, smaller/faster)

    No network calls - uses bundled or custom ONNX model.

    Usage:
        # Multilingual (default)
        reranker = RerankerEmbedder()
        scores = reranker.score("query", ["doc1", "doc2", "doc3"])

        # English L12 (default English model)
        reranker = RerankerEmbedder(language="en")
        scores = reranker.score("query", ["doc1", "doc2"])

        # English L6 (smaller, faster)
        reranker = RerankerEmbedder(model="L6")
        scores = reranker.score("query", ["doc1", "doc2"])

        # Custom ONNX model
        reranker = RerankerEmbedder(model_dir="/path/to/my/reranker")
    """

    # Model aliases
    MODEL_ALIASES = {
        # Multilingual
        "multi": "reranker",
        "multilingual": "reranker",
        # English L12 (default for language="en")
        "l12": "reranker_en",
        "L12": "reranker_en",
        "en": "reranker_en",
        # English L6 (smaller/faster)
        "l6": "reranker_en_l6",
        "L6": "reranker_en_l6",
        "minilm-l6": "reranker_en_l6",
        # BGE Reranker Base (higher quality)
        "bge": "bge_reranker_base",
        "bge-base": "bge_reranker_base",
        "bge-reranker": "bge_reranker_base",
        "bge-reranker-base": "bge_reranker_base",
    }

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        device: str = "cpu",
        max_length: Optional[int] = None,
        onnx_file: str = "model.onnx",
        tokenizer_file: str = "tokenizer.json",
        language: Optional[str] = None,
        model: Optional[str] = None,
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
            model: Specific model to use - "L6" for smaller/faster English model,
                   "L12" for default English model. Overrides language parameter.
        """
        # Determine which model to use
        self.language = language
        self.model_name = model

        # Model selection priority: model > language > default
        if model:
            config_key = self.MODEL_ALIASES.get(model, model.lower())
            if config_key not in MODEL_CONFIG:
                raise ValueError(
                    f"Unknown model: {model}. Available models: {list(self.MODEL_ALIASES.keys())}"
                )
            default_dir = get_models_dir() / config_key
        elif language in ("en", "english"):
            config_key = "reranker_en"
            default_dir = get_models_dir() / "reranker_en"
        else:
            config_key = "reranker"
            default_dir = get_models_dir() / "reranker"

        self._config_key = config_key
        self.model_dir = Path(model_dir) if model_dir else default_dir
        self.device = device
        self.max_length = max_length or MODEL_CONFIG[config_key]["max_length"]
        self.onnx_file = onnx_file
        self.tokenizer_file = tokenizer_file

        self._session: Any = None
        self._tokenizer: Any = None
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
                logger.info("multilingual reranker not found; downloading")
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
                logger.info("English reranker not found; downloading from the releases page")
                try:
                    download_models(model_type="reranker_en", progress=True)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to download English reranker model.\n\n"
                        f"Error: {e}\n\n"
                        f"Try manual download: vectrixdb download-models --type reranker_en"
                    ) from e

            # Re-check model path after download
            if not model_path.exists():
                raise ModelNotFoundError(
                    f"Model file not found after download: {model_path}\n"
                    f"The download may have failed or extracted to wrong location.\n"
                    f"Try manual download: vectrixdb download-models --type reranker_en"
                )

        # Set up ONNX Runtime session
        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4

        self._session = ort.InferenceSession(
            str(model_path), sess_options=sess_options, providers=providers
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
        all_scores = self._score_batch(query, list(documents), batch_size=batch_size)

        return np.array(all_scores, dtype=np.float32)

    def _score_batch(self, query: str, documents: List[str], batch_size: int = 16) -> List[float]:
        """Score documents in bucketed batches, one session call per bucket.

        This ran the model once per document, each padded to max_length, so a
        pair cost about 150 ms and thirty candidates five seconds. The INT8
        export is padding-sensitive, so simply padding a batch to its longest
        member would make a document's score depend on its neighbours; padding
        to the document's own 32-token bucket keeps scores reproducible.
        """
        if not documents:
            return []
        encoded = [
            self.tokenizer.encode_pair(query, doc, max_length=self.max_length) for doc in documents
        ]
        lengths = [int(e["attention_mask"].sum()) for e in encoded]
        scores = [0.0] * len(documents)
        pad = self.tokenizer.pad_token_id

        # The INT8 model computes activation scales per batch, so a score
        # moves slightly with its batch-mates. Batches are therefore formed
        # from a canonical order (length, then text) rather than input order:
        # the same candidate set always scores the same, however it arrived.
        for width, indices in _bucketed_batches(lengths):
            width = min(width, self.max_length)
            indices = sorted(indices, key=lambda i: (lengths[i], documents[i]))
            for start in range(0, len(indices), max(1, batch_size)):
                chunk = indices[start : start + max(1, batch_size)]
                n = len(chunk)
                input_ids = np.full((n, width), pad, dtype=np.int64)
                attention_mask = np.zeros((n, width), dtype=np.int64)
                token_type_ids = np.zeros((n, width), dtype=np.int64)
                for row, i in enumerate(chunk):
                    length = min(lengths[i], width)
                    input_ids[row, :length] = encoded[i]["input_ids"][0, :length]
                    attention_mask[row, :length] = 1
                    token_type_ids[row, :length] = encoded[i]["token_type_ids"][0, :length]

                onnx_inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
                if self._has_token_type_ids:
                    onnx_inputs["token_type_ids"] = token_type_ids

                logits = self.session.run(None, onnx_inputs)[0]
                if logits.shape[-1] == 1:
                    probs = 1.0 / (1.0 + np.exp(-logits[:, 0]))
                else:
                    shifted = np.exp(logits - logits.max(axis=1, keepdims=True))
                    probs = shifted[:, 1] / shifted.sum(axis=1)
                for row, i in enumerate(chunk):
                    scores[i] = float(probs[row])
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


# ============================================================================
# LATE INTERACTION: ColBERT-style MaxSim
# ============================================================================
#
# INPUT   queries and documents
# OUTPUT  one vector a token, scored by the sum of each query token's best
#         match
#
# What ultimate mode searches with.


class LateInteractionEmbedder:
    """
    Late interaction embedder using ONNX (ColBERT-style MaxSim scoring).

    Default model: BAAI/bge-m3 (1024 dim, multilingual, 100+ languages)
    English models:
        - "colbert" / language="en": answerai-colbert-small-v1 (128 dim, default English)
        - "colbert-v2": colbertv2.0 (128 dim, higher quality +5-10%)

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

        # ColBERT v2 (higher quality English)
        embedder = LateInteractionEmbedder(model="colbert-v2")
        score = embedder.max_sim(query_emb, doc_emb)

        # Custom ONNX model
        embedder = LateInteractionEmbedder(model_dir="/path/to/my/model")
    """

    # Model aliases
    MODEL_ALIASES = {
        # Multilingual
        "multi": "late_interaction",
        "multilingual": "late_interaction",
        "bge-m3": "late_interaction",
        # English ColBERT small (default for language="en")
        "colbert": "late_interaction_en",
        "colbert-small": "late_interaction_en",
        "answerai-colbert": "late_interaction_en",
        "late_interaction_en": "late_interaction_en",  # Allow direct key
        # ColBERT v2 (higher quality)
        "colbert-v2": "colbert_v2",
        "colbertv2": "colbert_v2",
        "colbertv2.0": "colbert_v2",
        "colbert_v2": "colbert_v2",  # Allow direct key (from easy.py resolution)
    }

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        device: str = "cpu",
        dimension: Optional[int] = None,
        max_length: Optional[int] = None,
        onnx_file: str = "model.onnx",
        tokenizer_file: str = "tokenizer.json",
        language: Optional[str] = None,
        model: Optional[str] = None,
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
            model: Specific model to use - "colbert", "colbert-v2", "bge-m3".
                   Overrides language parameter if specified.
        """
        # Determine which model to use
        self.language = language
        self.model_name = model

        # Model selection priority: model > language > default
        if model:
            config_key = self.MODEL_ALIASES.get(model.lower(), model.lower())
            if config_key not in MODEL_CONFIG:
                raise ValueError(
                    f"Unknown model: {model}. Available models: {list(self.MODEL_ALIASES.keys())}"
                )
            # Determine directory name based on config_key
            if config_key == "late_interaction_en":
                default_dir = get_models_dir() / "colbert"
            elif config_key == "colbert_v2":
                default_dir = get_models_dir() / "colbert_v2"
            else:
                default_dir = get_models_dir() / "bge-m3"
        elif language in ("en", "english"):
            config_key = "late_interaction_en"
            default_dir = get_models_dir() / "colbert"
        else:
            config_key = "late_interaction"
            default_dir = get_models_dir() / "bge-m3"

        self._config_key = config_key
        self.model_dir = Path(model_dir) if model_dir else default_dir
        self.device = device
        self.onnx_file = onnx_file
        self.tokenizer_file = tokenizer_file

        # Use provided values or defaults from config
        self.dimension = dimension or MODEL_CONFIG[config_key]["dimension"]
        self.max_length = max_length or MODEL_CONFIG[config_key]["max_length"]

        self._session: Any = None
        self._tokenizer: Any = None
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
                logger.info("multilingual late-interaction model (BGE-M3) not found; downloading")
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
                raise ModelNotFoundError(
                    f"ColBERT model not found: {model_path}\n"
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
            str(model_path), sess_options=sess_options, providers=providers
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
        embeddings = token_embeddings[valid_tokens][:, : self.dimension]

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
        embeddings = token_embeddings[valid_tokens][:, : self.dimension]

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
        Encode multiple documents into token-level embeddings, batched.

        One session call per length bucket instead of one per document (which
        cost ~20 s for fifty candidates). Documents are padded to their own
        32-token bucket, so an embedding never depends on its batch-mates.

        Args:
            documents: List of document texts
            normalize: L2 normalize embeddings
            batch_size: Upper bound on documents per session call

        Returns:
            List of arrays, each of shape (num_tokens, dimension)
        """
        if not documents:
            return []
        encoded = [
            self.tokenizer.encode(doc, max_length=self.max_length, padding=True, truncation=True)
            for doc in documents
        ]
        lengths = [int(e["attention_mask"][0].sum()) for e in encoded]
        results: List[Optional[np.ndarray]] = [None] * len(documents)
        pad = self.tokenizer.pad_token_id

        for width, indices in _bucketed_batches(lengths):
            width = min(width, self.max_length)
            indices = sorted(indices, key=lambda i: (lengths[i], documents[i]))
            for start in range(0, len(indices), max(1, batch_size)):
                chunk = indices[start : start + max(1, batch_size)]
                n = len(chunk)
                input_ids = np.full((n, width), pad, dtype=np.int64)
                attention_mask = np.zeros((n, width), dtype=np.int64)
                token_type_ids = np.zeros((n, width), dtype=np.int64)
                for row, i in enumerate(chunk):
                    length = min(lengths[i], width)
                    input_ids[row, :length] = encoded[i]["input_ids"][0, :length]
                    attention_mask[row, :length] = 1
                    if "token_type_ids" in encoded[i]:
                        token_type_ids[row, :length] = encoded[i]["token_type_ids"][0, :length]
                inputs = {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                    "token_type_ids": token_type_ids,
                }
                outputs = self.session.run(None, self._build_onnx_inputs(inputs))[0]
                for row, i in enumerate(chunk):
                    length = min(lengths[i], width)
                    emb = outputs[row, :length, : self.dimension]
                    if normalize:
                        emb = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-8)
                    results[i] = emb.astype(np.float32)
        return [r for r in results if r is not None]

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
        doc_embs = self.encode_documents(documents)
        scores = [self.max_sim(query_emb, doc_emb) for doc_emb in doc_embs]
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


# ============================================================================
# THE GRAPH EXTRACTOR: mREBEL triplets
# ============================================================================
#
# INPUT   text
# OUTPUT  head, relation and tail triplets for GraphRAG, through a
#         SentencePiece tokenizer
#
# The multilingual REBEL model, wrapped so the extractor interface is one.


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

    def __call__(
        self,
        text: str,
        return_tensors: str = "np",
        max_length: int = 256,
        truncation: bool = True,
        padding: str = "max_length",
    ):
        """Tokenize text and return input_ids and attention_mask."""
        ids = self.sp.EncodeAsIds(text)

        # Truncate if needed
        if truncation and len(ids) > max_length - 2:  # Leave room for special tokens
            ids = ids[: max_length - 2]

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
            if skip_special_tokens and i in (
                self.pad_token_id,
                self.bos_token_id,
                self.eos_token_id,
            ):
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

        self._encoder_session: Any = None
        self._decoder_session: Any = None
        self._tokenizer: Any = None
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
            logger.info("GraphRAG extraction model (mREBEL) not found; downloading")
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
            str(encoder_path), sess_options=sess_options, providers=providers
        )

        self._decoder_session = ort.InferenceSession(
            str(decoder_path), sess_options=sess_options, providers=providers
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
            },
        )
        encoder_hidden_states = encoder_outputs[0]

        # Generate with decoder (greedy decoding)
        generated_ids = self._greedy_decode(
            encoder_hidden_states,
            attention_mask,
            max_length=256,
        )

        # Decode tokens to text
        if hasattr(self._tokenizer, "decode"):
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
                    },
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
                        },
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
        if hasattr(self._tokenizer, "_vocab_inv"):
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
        RELATION_MARKERS = r"__tn__|__zu__|__wo__|__xh__|__yo__"
        # Separator markers (between head and tail)
        SEPARATOR_MARKERS = r"__uk__|__tn__|__yo__"

        # Try Format 2 (ONNX mREBEL)
        if "__sv__" in text:
            # Split by __sv__ to find individual triplet blocks
            # Format: <type> __sv__ content __sv__ content ...
            sv_pattern = r"(?:<(\w+)>)?\s*__sv__\s*"
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
                    all_markers = list(
                        re.finditer(f"({RELATION_MARKERS})\\s*([^_<]+?)(?=__|<|$)", content)
                    )

                    if all_markers:
                        # Use the last marker as the relation
                        last_match = all_markers[-1]
                        relation = last_match.group(2).strip()
                        head_tail_part = content[: last_match.start()].strip()

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
                        head = re.sub(r"\s*__\w+__\s*", ", ", head).strip(", ")
                        tail = re.sub(r"\s*__\w+__\s*", ", ", tail).strip(", ")

                        if head and tail and relation:
                            triplets.append(
                                Triplet(
                                    head=head,
                                    head_type=entity_type.lower(),
                                    relation=relation,
                                    tail=tail,
                                    tail_type="entity",
                                )
                            )

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
                                triplets.append(
                                    Triplet(
                                        head=head,
                                        head_type="entity",
                                        relation=relation,
                                        tail=tail,
                                        tail_type="entity",
                                    )
                                )
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


# Backward-compatible alias for older GraphRAG imports.
REBELExtractor = GraphExtractor
