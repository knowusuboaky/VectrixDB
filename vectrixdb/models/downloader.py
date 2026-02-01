"""
Model Downloader for VectrixDB

Downloads and converts models to ONNX format.
This is a ONE-TIME setup operation. After download, no network calls needed.

English Bundle (Bundled with pip install, ~100MB):
- Dense EN: intfloat/e5-small-v2 (~32MB INT8) - English only
- Reranker EN: cross-encoder/ms-marco-MiniLM-L-12-v2 (~32MB INT8) - English only
- ColBERT: answerdotai/answerai-colbert-small-v1 (~32MB INT8) - English late interaction
- Sparse: BM25 vocabulary-based (~1MB) - language agnostic

Multilingual Bundle (Downloaded on demand, ~750MB additional):
- Dense: intfloat/multilingual-e5-small (~113MB INT8) - 100+ languages
- Reranker: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (~113MB INT8) - 15+ languages
- BGE-M3: BAAI/bge-m3 (~563MB INT8) - 100+ languages late interaction

GraphRAG (Optional, ~718MB additional):
- mREBEL: Babelscape/mrebel-base INT8 (~718MB) - 18 languages (triplet extraction)

Usage:
    # CLI
    vectrixdb download-models                    # Download all multilingual models
    vectrixdb download-models --type dense       # Download specific model
    vectrixdb download-models --type late_interaction  # Download BGE-M3

    # Python
    from vectrixdb.models import download_models
    download_models()                            # Download all
    download_models(model_type="dense")          # Download specific
"""

from __future__ import annotations

import os
import json
import shutil
import zipfile
import tempfile
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from .embedded import get_models_dir, MODEL_CONFIG, GITHUB_REPO, GITHUB_RELEASE_BASE


class ModelDownloader:
    """Download and convert models to ONNX format."""

    def __init__(self, progress: bool = True):
        """
        Initialize downloader.

        Args:
            progress: Show progress during download
        """
        self.progress = progress
        self.models_dir = get_models_dir()

    def _download_from_github(self, model_type: str, model_dir: Path, config: dict) -> bool:
        """
        Download pre-converted ONNX model from GitHub releases (fallback).

        Args:
            model_type: Type of model (dense, reranker, etc.)
            model_dir: Directory to save the model
            config: Model configuration

        Returns:
            True if successful, False otherwise
        """
        github_release = config.get("github_release")
        if not github_release:
            return False

        # GitHub release URL: https://github.com/REPO/releases/download/TAG/MODEL.zip
        zip_url = f"{GITHUB_RELEASE_BASE}/{github_release}/{model_type}.zip"

        print(f"  Trying GitHub fallback: {zip_url}")

        try:
            # Download with progress
            req = Request(zip_url, headers={"User-Agent": "VectrixDB-Downloader/1.0"})

            with urlopen(req, timeout=60) as response:
                total_size = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 8192

                # Download to temp file
                with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp_file:
                    tmp_path = tmp_file.name
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        tmp_file.write(chunk)
                        downloaded += len(chunk)

                        if self.progress and total_size > 0:
                            pct = (downloaded / total_size) * 100
                            print(f"\r  Downloading: {pct:.1f}% ({downloaded // 1024 // 1024}MB)", end="", flush=True)

                    if self.progress:
                        print()  # New line after progress

            # Extract zip to model directory
            print(f"  Extracting to: {model_dir}")
            model_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(tmp_path, "r") as zip_ref:
                zip_ref.extractall(model_dir)

            # Clean up temp file
            os.unlink(tmp_path)

            print(f"  Successfully downloaded from GitHub!")
            return True

        except HTTPError as e:
            if e.code == 404:
                print(f"  GitHub release not found (404). Model may not be uploaded yet.")
            else:
                print(f"  GitHub download failed: HTTP {e.code}")
            return False
        except URLError as e:
            print(f"  GitHub download failed: {e.reason}")
            return False
        except Exception as e:
            print(f"  GitHub download failed: {e}")
            return False

    def download(self, model_type: str) -> Path:
        """
        Download a specific model type.

        Args:
            model_type: "dense", "sparse", "reranker", "colbert", "late_interaction", or "rebel"

        Returns:
            Path to model directory
        """
        if model_type == "dense":
            return self._download_dense()
        elif model_type == "sparse":
            return self._create_sparse()
        elif model_type == "reranker":
            return self._download_reranker()
        elif model_type == "colbert":
            return self._download_colbert()
        elif model_type == "late_interaction":
            return self._download_late_interaction()
        elif model_type == "rebel":
            return self._download_rebel()
        else:
            raise ValueError(f"Unknown model type: {model_type}")

    def download_all(self, include_graphrag: bool = True, include_multilingual: bool = True) -> None:
        """Download all models."""
        print("Downloading VectrixDB models (one-time setup)...")
        size = "~250MB"
        if include_multilingual:
            size = "~1GB"
        if include_graphrag:
            size = "~1.7GB" if include_multilingual else "~950MB"
        print(f"Total bundle size: {size}")
        print("After this, no network calls will be needed.\n")

        self._download_dense()
        self._create_sparse()
        self._download_reranker()
        self._download_colbert()

        if include_multilingual:
            self._download_late_interaction()

        if include_graphrag:
            self._download_rebel()

        print("\nAll models downloaded successfully!")
        print(f"Location: {self.models_dir}")
        print("\nModels installed:")
        print("  - Dense: multilingual-e5-small (100+ languages)")
        print("  - Sparse: BM25 (language agnostic)")
        print("  - Reranker: mmarco-mMiniLMv2-L12-H384-v1 (15+ languages)")
        print("  - ColBERT: answerai-colbert-small-v1 (English late interaction)")
        if include_multilingual:
            print("  - BGE-M3: bge-m3 (100+ languages, multilingual late interaction)")
        if include_graphrag:
            print("  - mREBEL: mrebel-large (18 languages, GraphRAG triplet extraction)")

    def _download_dense(self) -> Path:
        """Download and convert dense model to ONNX."""
        config = MODEL_CONFIG["dense"]
        model_dir = self.models_dir / "dense"
        model_dir.mkdir(parents=True, exist_ok=True)

        print(f"Downloading dense model: {config['name']}...")

        # Try GitHub first (pre-converted ONNX models)
        if self._download_from_github("dense", model_dir, config):
            return model_dir

        # Fallback to HuggingFace if GitHub failed
        print("  GitHub download failed, falling back to HuggingFace...")
        hf_success = False
        try:
            from optimum.onnxruntime import ORTModelForFeatureExtraction
            from transformers import AutoTokenizer

            hf_id = config["huggingface_id"]
            print(f"  Trying HuggingFace: {hf_id}")

            model = ORTModelForFeatureExtraction.from_pretrained(
                hf_id,
                export=True,
            )
            model.save_pretrained(model_dir)

            tokenizer = AutoTokenizer.from_pretrained(hf_id)
            tokenizer.save_pretrained(model_dir)

            onnx_files = list(model_dir.glob("*.onnx"))
            if onnx_files:
                target = model_dir / config["onnx_file"]
                if not target.exists():
                    shutil.move(onnx_files[0], target)

            print(f"  Dense model saved to: {model_dir}")
            hf_success = True

        except ImportError:
            print("  optimum not installed, trying manual export...")
            try:
                self._manual_dense_export(model_dir, config)
                hf_success = True
            except Exception as e:
                print(f"  Manual export failed: {e}")

        except Exception as e:
            print(f"  HuggingFace download failed: {e}")

        if not hf_success:
            raise RuntimeError(
                f"Failed to download dense model from both GitHub and HuggingFace.\n"
                f"Please check your internet connection or try again later."
            )

        return model_dir

    def _manual_dense_export(self, model_dir: Path, config: dict) -> None:
        """Manual ONNX export without optimum."""
        import torch
        from transformers import AutoModel, AutoTokenizer

        hf_id = config["huggingface_id"]

        print(f"  Loading model from HuggingFace: {hf_id}")
        model = AutoModel.from_pretrained(hf_id)
        tokenizer = AutoTokenizer.from_pretrained(hf_id)

        model.eval()

        # Create dummy input
        dummy_input = tokenizer(
            "This is a test sentence.",
            return_tensors="pt",
            padding="max_length",
            max_length=config["max_length"],
            truncation=True,
        )

        # Export to ONNX
        onnx_path = model_dir / config["onnx_file"]

        print(f"  Exporting to ONNX: {onnx_path}")

        # Check if model uses token_type_ids
        has_token_type_ids = "token_type_ids" in dummy_input

        if has_token_type_ids:
            torch.onnx.export(
                model,
                (
                    dummy_input["input_ids"],
                    dummy_input["attention_mask"],
                    dummy_input["token_type_ids"],
                ),
                onnx_path,
                input_names=["input_ids", "attention_mask", "token_type_ids"],
                output_names=["last_hidden_state"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "sequence"},
                    "attention_mask": {0: "batch_size", 1: "sequence"},
                    "token_type_ids": {0: "batch_size", 1: "sequence"},
                    "last_hidden_state": {0: "batch_size", 1: "sequence"},
                },
                opset_version=14,
            )
        else:
            # Models like multilingual-e5-small don't use token_type_ids
            torch.onnx.export(
                model,
                (
                    dummy_input["input_ids"],
                    dummy_input["attention_mask"],
                ),
                onnx_path,
                input_names=["input_ids", "attention_mask"],
                output_names=["last_hidden_state"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "sequence"},
                    "attention_mask": {0: "batch_size", 1: "sequence"},
                    "last_hidden_state": {0: "batch_size", 1: "sequence"},
                },
                opset_version=14,
            )

        # Save tokenizer
        tokenizer.save_pretrained(model_dir)

        # Save config indicating whether token_type_ids is used
        model_config = {
            "has_token_type_ids": has_token_type_ids,
            "dimension": config.get("dimension", 384),
            "max_length": config.get("max_length", 512),
        }
        config_path = model_dir / "vectrix_config.json"
        with open(config_path, "w") as f:
            json.dump(model_config, f, indent=2)

        print(f"  Dense model exported to: {model_dir}")

    def _create_sparse(self) -> Path:
        """Create BM25 vocabulary files."""
        config = MODEL_CONFIG["sparse"]
        model_dir = self.models_dir / "sparse"
        model_dir.mkdir(parents=True, exist_ok=True)

        print(f"Creating sparse (BM25) model...")

        # Create default vocabulary (common English words)
        # This will be expanded as the database is used
        default_vocab = self._get_default_vocab()

        vocab_path = model_dir / config["vocab_file"]
        with open(vocab_path, "w", encoding="utf-8") as f:
            json.dump(default_vocab, f)

        # Create default IDF values
        # These are approximate IDFs for common words
        default_idf = {word: 1.0 for word in default_vocab.keys()}
        # Lower IDF for very common words
        common_words = ["the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
                       "have", "has", "had", "do", "does", "did", "will", "would", "could",
                       "should", "may", "might", "must", "shall", "can", "to", "of", "in",
                       "for", "on", "with", "at", "by", "from", "as", "into", "through"]
        for word in common_words:
            if word in default_idf:
                default_idf[word] = 0.1

        idf_path = model_dir / config["idf_file"]
        with open(idf_path, "w", encoding="utf-8") as f:
            json.dump(default_idf, f)

        # Create config
        bm25_config = {
            "k1": 1.5,
            "b": 0.75,
            "avg_doc_len": 50.0,
            "vocab_size": len(default_vocab),
        }

        config_path = model_dir / config["config_file"]
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(bm25_config, f, indent=2)

        print(f"  Sparse model saved to: {model_dir}")
        print(f"  Vocabulary size: {len(default_vocab)}")

        return model_dir

    def _get_default_vocab(self) -> dict:
        """Get default vocabulary for BM25."""
        # Basic English vocabulary
        words = [
            # Articles, prepositions, conjunctions
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "as", "into", "through", "during", "before",
            "after", "above", "below", "between", "under", "over",
            # Pronouns
            "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
            "them", "my", "your", "his", "its", "our", "their", "this", "that",
            "these", "those", "who", "whom", "which", "what", "whose",
            # Verbs
            "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
            "do", "does", "did", "will", "would", "could", "should", "may", "might",
            "must", "shall", "can", "need", "get", "got", "make", "made", "take",
            "took", "come", "came", "go", "went", "see", "saw", "know", "knew",
            "think", "thought", "want", "use", "find", "found", "give", "gave",
            "tell", "told", "work", "call", "try", "ask", "seem", "feel", "leave",
            "put", "mean", "keep", "let", "begin", "show", "hear", "play", "run",
            "move", "live", "believe", "hold", "bring", "happen", "write", "provide",
            "sit", "stand", "lose", "pay", "meet", "include", "continue", "set",
            "learn", "change", "lead", "understand", "watch", "follow", "stop",
            "create", "speak", "read", "allow", "add", "spend", "grow", "open",
            "walk", "win", "offer", "remember", "love", "consider", "appear", "buy",
            "wait", "serve", "die", "send", "expect", "build", "stay", "fall",
            "cut", "reach", "kill", "remain",
            # Nouns
            "time", "year", "people", "way", "day", "man", "thing", "woman", "life",
            "child", "world", "school", "state", "family", "student", "group", "country",
            "problem", "hand", "part", "place", "case", "week", "company", "system",
            "program", "question", "work", "government", "number", "night", "point",
            "home", "water", "room", "mother", "area", "money", "story", "fact",
            "month", "lot", "right", "study", "book", "eye", "job", "word", "business",
            "issue", "side", "kind", "head", "house", "service", "friend", "father",
            "power", "hour", "game", "line", "end", "member", "law", "car", "city",
            "community", "name", "president", "team", "minute", "idea", "kid", "body",
            "information", "back", "parent", "face", "others", "level", "office",
            "door", "health", "person", "art", "war", "history", "party", "result",
            "change", "morning", "reason", "research", "girl", "guy", "moment",
            "air", "teacher", "force", "education",
            # Adjectives
            "good", "new", "first", "last", "long", "great", "little", "own", "other",
            "old", "right", "big", "high", "different", "small", "large", "next",
            "early", "young", "important", "few", "public", "bad", "same", "able",
            "human", "local", "sure", "free", "better", "true", "whole", "real",
            "best", "hard", "possible", "special", "clear", "recent", "certain",
            "personal", "open", "red", "difficult", "available", "likely", "short",
            "single", "medical", "current", "wrong", "private", "past", "foreign",
            "fine", "common", "poor", "natural", "significant", "similar", "hot",
            "dead", "central", "happy", "serious", "ready", "simple", "left",
            "physical", "general", "environmental", "financial", "blue", "democratic",
            "dark", "various", "entire", "close", "legal", "religious", "cold",
            "final", "main", "green", "nice", "huge", "popular", "traditional",
            "cultural",
            # Adverbs
            "not", "also", "very", "often", "however", "too", "usually", "really",
            "early", "never", "always", "sometimes", "together", "likely", "simply",
            "generally", "instead", "actually", "already", "enough", "both", "well",
            "much", "even", "again", "still", "almost", "ever", "why", "here",
            "there", "where", "when", "how", "now", "then", "today", "just", "only",
            # Tech terms
            "data", "computer", "software", "system", "network", "internet", "web",
            "database", "server", "code", "program", "application", "app", "user",
            "file", "document", "search", "query", "vector", "embedding", "model",
            "machine", "learning", "ai", "artificial", "intelligence", "algorithm",
            "api", "cloud", "service", "platform", "development", "developer",
            "python", "javascript", "java", "programming", "language", "function",
            "class", "method", "variable", "string", "number", "array", "list",
            "object", "json", "xml", "html", "css", "framework", "library",
            "package", "module", "import", "export", "install", "run", "build",
            "test", "debug", "error", "exception", "log", "config", "setting",
            "option", "parameter", "argument", "value", "key", "index", "table",
            "row", "column", "field", "record", "schema", "query", "select",
            "insert", "update", "delete", "create", "drop", "join", "where",
            "order", "group", "limit", "offset",
        ]

        return {word: i for i, word in enumerate(words)}

    def _download_reranker(self) -> Path:
        """Download and convert reranker model to ONNX."""
        config = MODEL_CONFIG["reranker"]
        model_dir = self.models_dir / "reranker"
        model_dir.mkdir(parents=True, exist_ok=True)

        print(f"Downloading reranker model: {config['name']}...")

        # Try GitHub first (pre-converted ONNX models)
        if self._download_from_github("reranker", model_dir, config):
            return model_dir

        # Fallback to HuggingFace if GitHub failed
        print("  GitHub download failed, falling back to HuggingFace...")
        hf_success = False
        try:
            from optimum.onnxruntime import ORTModelForSequenceClassification
            from transformers import AutoTokenizer

            hf_id = config["huggingface_id"]
            print(f"  Trying HuggingFace: {hf_id}")

            model = ORTModelForSequenceClassification.from_pretrained(
                hf_id,
                export=True,
            )
            model.save_pretrained(model_dir)

            tokenizer = AutoTokenizer.from_pretrained(hf_id)
            tokenizer.save_pretrained(model_dir)

            onnx_files = list(model_dir.glob("*.onnx"))
            if onnx_files:
                target = model_dir / config["onnx_file"]
                if not target.exists():
                    shutil.move(onnx_files[0], target)

            print(f"  Reranker model saved to: {model_dir}")
            hf_success = True

        except ImportError:
            print("  optimum not installed, trying manual export...")
            try:
                self._manual_reranker_export(model_dir, config)
                hf_success = True
            except Exception as e:
                print(f"  Manual export failed: {e}")

        except Exception as e:
            print(f"  HuggingFace download failed: {e}")

        if not hf_success:
            raise RuntimeError(
                f"Failed to download reranker model from both GitHub and HuggingFace.\n"
                f"Please check your internet connection or try again later."
            )

        return model_dir

    def _manual_reranker_export(self, model_dir: Path, config: dict) -> None:
        """Manual ONNX export for reranker."""
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        hf_id = config["huggingface_id"]

        print(f"  Loading model from HuggingFace: {hf_id}")
        model = AutoModelForSequenceClassification.from_pretrained(hf_id)
        tokenizer = AutoTokenizer.from_pretrained(hf_id)

        model.eval()

        # Create dummy input (query-document pair)
        dummy_input = tokenizer(
            "What is machine learning?",
            "Machine learning is a subset of artificial intelligence.",
            return_tensors="pt",
            padding="max_length",
            max_length=config["max_length"],
            truncation=True,
        )

        # Export to ONNX
        onnx_path = model_dir / config["onnx_file"]

        print(f"  Exporting to ONNX: {onnx_path}")

        # Check if model uses token_type_ids
        has_token_type_ids = "token_type_ids" in dummy_input

        if has_token_type_ids:
            torch.onnx.export(
                model,
                (
                    dummy_input["input_ids"],
                    dummy_input["attention_mask"],
                    dummy_input["token_type_ids"],
                ),
                onnx_path,
                input_names=["input_ids", "attention_mask", "token_type_ids"],
                output_names=["logits"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "sequence"},
                    "attention_mask": {0: "batch_size", 1: "sequence"},
                    "token_type_ids": {0: "batch_size", 1: "sequence"},
                    "logits": {0: "batch_size"},
                },
                opset_version=14,
            )
        else:
            torch.onnx.export(
                model,
                (
                    dummy_input["input_ids"],
                    dummy_input["attention_mask"],
                ),
                onnx_path,
                input_names=["input_ids", "attention_mask"],
                output_names=["logits"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "sequence"},
                    "attention_mask": {0: "batch_size", 1: "sequence"},
                    "logits": {0: "batch_size"},
                },
                opset_version=14,
            )

        # Save tokenizer
        tokenizer.save_pretrained(model_dir)

        # Save config
        model_config = {
            "has_token_type_ids": has_token_type_ids,
            "max_length": config.get("max_length", 512),
        }
        config_path = model_dir / "vectrix_config.json"
        with open(config_path, "w") as f:
            json.dump(model_config, f, indent=2)

        print(f"  Reranker model exported to: {model_dir}")

    def _download_colbert(self) -> Path:
        """Download and convert ColBERT model to ONNX."""
        config = MODEL_CONFIG["colbert"]
        model_dir = self.models_dir / "colbert"
        model_dir.mkdir(parents=True, exist_ok=True)

        print(f"Downloading ColBERT model: {config['name']}...")

        # Try GitHub first (pre-converted ONNX models)
        if self._download_from_github("colbert", model_dir, config):
            return model_dir

        # Fallback to HuggingFace if GitHub failed
        print("  GitHub download failed, falling back to HuggingFace...")
        hf_success = False
        try:
            from optimum.onnxruntime import ORTModelForFeatureExtraction
            from transformers import AutoTokenizer

            hf_id = config["huggingface_id"]
            print(f"  Trying HuggingFace: {hf_id}")

            model = ORTModelForFeatureExtraction.from_pretrained(
                hf_id,
                export=True,
            )
            model.save_pretrained(model_dir)

            tokenizer = AutoTokenizer.from_pretrained(hf_id)
            tokenizer.save_pretrained(model_dir)

            onnx_files = list(model_dir.glob("*.onnx"))
            if onnx_files:
                target = model_dir / config["onnx_file"]
                if not target.exists():
                    shutil.move(onnx_files[0], target)

            print(f"  ColBERT model saved to: {model_dir}")
            hf_success = True

        except ImportError:
            print("  optimum not installed, trying manual export...")
            try:
                self._manual_colbert_export(model_dir, config)
                hf_success = True
            except Exception as e:
                print(f"  Manual export failed: {e}")

        except Exception as e:
            print(f"  HuggingFace download failed: {e}")

        if not hf_success:
            raise RuntimeError(
                f"Failed to download ColBERT model from both GitHub and HuggingFace.\n"
                f"Please check your internet connection or try again later."
            )

        return model_dir

    def _manual_colbert_export(self, model_dir: Path, config: dict) -> None:
        """Manual ONNX export for ColBERT."""
        import torch
        from transformers import AutoModel, AutoTokenizer

        hf_id = config["huggingface_id"]

        print(f"  Loading model from HuggingFace: {hf_id}")
        model = AutoModel.from_pretrained(hf_id)
        tokenizer = AutoTokenizer.from_pretrained(hf_id)

        model.eval()

        # Create dummy input
        dummy_input = tokenizer(
            "What is machine learning?",
            return_tensors="pt",
            padding="max_length",
            max_length=config["max_length"],
            truncation=True,
        )

        # Export to ONNX
        onnx_path = model_dir / config["onnx_file"]

        print(f"  Exporting to ONNX: {onnx_path}")

        # Check if model uses token_type_ids
        has_token_type_ids = "token_type_ids" in dummy_input

        if has_token_type_ids:
            torch.onnx.export(
                model,
                (
                    dummy_input["input_ids"],
                    dummy_input["attention_mask"],
                    dummy_input["token_type_ids"],
                ),
                onnx_path,
                input_names=["input_ids", "attention_mask", "token_type_ids"],
                output_names=["last_hidden_state"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "sequence"},
                    "attention_mask": {0: "batch_size", 1: "sequence"},
                    "token_type_ids": {0: "batch_size", 1: "sequence"},
                    "last_hidden_state": {0: "batch_size", 1: "sequence"},
                },
                opset_version=14,
            )
        else:
            torch.onnx.export(
                model,
                (
                    dummy_input["input_ids"],
                    dummy_input["attention_mask"],
                ),
                onnx_path,
                input_names=["input_ids", "attention_mask"],
                output_names=["last_hidden_state"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "sequence"},
                    "attention_mask": {0: "batch_size", 1: "sequence"},
                    "last_hidden_state": {0: "batch_size", 1: "sequence"},
                },
                opset_version=14,
            )

        # Save tokenizer
        tokenizer.save_pretrained(model_dir)

        # Save config
        model_config = {
            "has_token_type_ids": has_token_type_ids,
            "dimension": config.get("dimension", 128),
            "max_length": config.get("max_length", 512),
        }
        config_path = model_dir / "vectrix_config.json"
        with open(config_path, "w") as f:
            json.dump(model_config, f, indent=2)

        print(f"  ColBERT model exported to: {model_dir}")

    def _download_late_interaction(self) -> Path:
        """Download and convert BGE-M3 late interaction model to ONNX."""
        config = MODEL_CONFIG["late_interaction"]
        model_dir = self.models_dir / "bge-m3"
        model_dir.mkdir(parents=True, exist_ok=True)

        print(f"Downloading late interaction model: {config['name']}...")
        print("  This model is ~563MB and enables multilingual MaxSim scoring.")

        # Try GitHub first (pre-converted ONNX models)
        if self._download_from_github("late_interaction", model_dir, config):
            return model_dir

        # Fallback to HuggingFace if GitHub failed
        print("  GitHub download failed, falling back to HuggingFace...")
        hf_success = False
        try:
            from optimum.onnxruntime import ORTModelForFeatureExtraction
            from transformers import AutoTokenizer

            hf_id = config["huggingface_id"]
            print(f"  Trying HuggingFace: {hf_id}")

            model = ORTModelForFeatureExtraction.from_pretrained(
                hf_id,
                export=True,
            )
            model.save_pretrained(model_dir)

            tokenizer = AutoTokenizer.from_pretrained(hf_id)
            tokenizer.save_pretrained(model_dir)

            onnx_files = list(model_dir.glob("*.onnx"))
            if onnx_files:
                target = model_dir / config["onnx_file"]
                if not target.exists():
                    shutil.move(onnx_files[0], target)

            print(f"  BGE-M3 model saved to: {model_dir}")
            hf_success = True

        except ImportError:
            print("  optimum not installed, trying manual export...")
            try:
                self._manual_late_interaction_export(model_dir, config)
                hf_success = True
            except Exception as e:
                print(f"  Manual export failed: {e}")

        except Exception as e:
            print(f"  HuggingFace download failed: {e}")

        if not hf_success:
            raise RuntimeError(
                f"Failed to download BGE-M3 model from both GitHub and HuggingFace.\n"
                f"Please check your internet connection or try again later."
            )

        return model_dir

    def _manual_late_interaction_export(self, model_dir: Path, config: dict) -> None:
        """Manual ONNX export for BGE-M3."""
        import torch
        from transformers import AutoModel, AutoTokenizer

        hf_id = config["huggingface_id"]

        print(f"  Loading model from HuggingFace: {hf_id}")
        model = AutoModel.from_pretrained(hf_id)
        tokenizer = AutoTokenizer.from_pretrained(hf_id)

        model.eval()

        # Create dummy input
        dummy_input = tokenizer(
            "What is machine learning?",
            return_tensors="pt",
            padding="max_length",
            max_length=config["max_length"],
            truncation=True,
        )

        # Export to ONNX
        onnx_path = model_dir / config["onnx_file"]

        print(f"  Exporting to ONNX: {onnx_path}")

        # Check if model uses token_type_ids
        has_token_type_ids = "token_type_ids" in dummy_input

        if has_token_type_ids:
            torch.onnx.export(
                model,
                (
                    dummy_input["input_ids"],
                    dummy_input["attention_mask"],
                    dummy_input["token_type_ids"],
                ),
                onnx_path,
                input_names=["input_ids", "attention_mask", "token_type_ids"],
                output_names=["last_hidden_state"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "sequence"},
                    "attention_mask": {0: "batch_size", 1: "sequence"},
                    "token_type_ids": {0: "batch_size", 1: "sequence"},
                    "last_hidden_state": {0: "batch_size", 1: "sequence"},
                },
                opset_version=14,
            )
        else:
            torch.onnx.export(
                model,
                (
                    dummy_input["input_ids"],
                    dummy_input["attention_mask"],
                ),
                onnx_path,
                input_names=["input_ids", "attention_mask"],
                output_names=["last_hidden_state"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "sequence"},
                    "attention_mask": {0: "batch_size", 1: "sequence"},
                    "last_hidden_state": {0: "batch_size", 1: "sequence"},
                },
                opset_version=14,
            )

        # Save tokenizer
        tokenizer.save_pretrained(model_dir)

        # Save config
        model_config = {
            "has_token_type_ids": has_token_type_ids,
            "dimension": config.get("dimension", 1024),
            "max_length": config.get("max_length", 512),
        }
        config_path = model_dir / "vectrix_config.json"
        with open(config_path, "w") as f:
            json.dump(model_config, f, indent=2)

        print(f"  BGE-M3 model exported to: {model_dir}")

    def _download_rebel(self) -> Path:
        """Download and convert mREBEL model to ONNX."""
        config = MODEL_CONFIG["rebel"]
        model_dir = self.models_dir / "rebel"
        model_dir.mkdir(parents=True, exist_ok=True)

        print(f"Downloading mREBEL model: {config['name']}...")
        print("  This model is ~500MB and enables LLM-free GraphRAG triplet extraction.")

        # Try GitHub first (pre-converted ONNX models)
        if self._download_from_github("rebel", model_dir, config):
            return model_dir

        # Fallback to HuggingFace if GitHub failed
        print("  GitHub download failed, falling back to HuggingFace...")
        hf_success = False
        try:
            from optimum.onnxruntime import ORTModelForSeq2SeqLM
            from transformers import AutoTokenizer

            hf_id = config["huggingface_id"]
            print(f"  Trying HuggingFace: {hf_id}")

            model = ORTModelForSeq2SeqLM.from_pretrained(
                hf_id,
                export=True,
            )
            model.save_pretrained(model_dir)

            tokenizer = AutoTokenizer.from_pretrained(hf_id)
            tokenizer.save_pretrained(model_dir)

            encoder_files = list(model_dir.glob("*encoder*.onnx"))
            decoder_files = list(model_dir.glob("*decoder*.onnx"))

            if encoder_files:
                target = model_dir / config["onnx_encoder_file"]
                if not target.exists():
                    shutil.move(encoder_files[0], target)

            if decoder_files:
                for df in decoder_files:
                    if "with_past" not in df.name:
                        target = model_dir / config["onnx_decoder_file"]
                        if not target.exists():
                            shutil.move(df, target)
                        break

            print(f"  mREBEL model saved to: {model_dir}")
            hf_success = True

        except ImportError:
            print("  optimum not installed, trying manual export...")
            try:
                self._manual_rebel_export(model_dir, config)
                hf_success = True
            except Exception as e:
                print(f"  Manual export failed: {e}")

        except Exception as e:
            print(f"  HuggingFace download failed: {e}")

        if not hf_success:
            raise RuntimeError(
                f"Failed to download mREBEL model from both GitHub and HuggingFace.\n"
                f"Please check your internet connection or try again later."
            )

        return model_dir

    def _manual_rebel_export(self, model_dir: Path, config: dict) -> None:
        """Manual ONNX export for mREBEL (encoder-decoder model)."""
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        hf_id = config["huggingface_id"]

        print(f"  Loading model from HuggingFace: {hf_id}")
        model = AutoModelForSeq2SeqLM.from_pretrained(hf_id)
        tokenizer = AutoTokenizer.from_pretrained(hf_id)

        model.eval()

        # Create dummy input
        dummy_text = "Albert Einstein was a German-born theoretical physicist."
        dummy_input = tokenizer(
            dummy_text,
            return_tensors="pt",
            padding="max_length",
            max_length=config["max_length"],
            truncation=True,
        )

        # Export encoder
        encoder_path = model_dir / config["onnx_encoder_file"]
        print(f"  Exporting encoder to ONNX: {encoder_path}")

        # Get encoder
        encoder = model.get_encoder()

        torch.onnx.export(
            encoder,
            (
                dummy_input["input_ids"],
                dummy_input["attention_mask"],
            ),
            encoder_path,
            input_names=["input_ids", "attention_mask"],
            output_names=["last_hidden_state"],
            dynamic_axes={
                "input_ids": {0: "batch_size", 1: "sequence"},
                "attention_mask": {0: "batch_size", 1: "sequence"},
                "last_hidden_state": {0: "batch_size", 1: "sequence"},
            },
            opset_version=14,
        )

        # Export decoder
        decoder_path = model_dir / config["onnx_decoder_file"]
        print(f"  Exporting decoder to ONNX: {decoder_path}")

        # Create decoder inputs
        decoder_input_ids = torch.tensor([[tokenizer.lang_code_to_id["en_XX"]]], dtype=torch.long)
        encoder_outputs = encoder(
            dummy_input["input_ids"],
            attention_mask=dummy_input["attention_mask"],
        )

        # Export the full model for decoder (simpler approach)
        class DecoderWrapper(torch.nn.Module):
            def __init__(self, model):
                super().__init__()
                self.model = model

            def forward(self, input_ids, attention_mask, encoder_hidden_states, encoder_attention_mask):
                outputs = self.model(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    encoder_hidden_states=encoder_hidden_states,
                    encoder_attention_mask=encoder_attention_mask,
                    use_cache=False,
                )
                return outputs.logits

        decoder_wrapper = DecoderWrapper(model.model.decoder)
        decoder_attention_mask = torch.ones_like(decoder_input_ids)

        torch.onnx.export(
            decoder_wrapper,
            (
                decoder_input_ids,
                decoder_attention_mask,
                encoder_outputs.last_hidden_state,
                dummy_input["attention_mask"],
            ),
            decoder_path,
            input_names=["input_ids", "attention_mask", "encoder_hidden_states", "encoder_attention_mask"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch_size", 1: "sequence"},
                "attention_mask": {0: "batch_size", 1: "sequence"},
                "encoder_hidden_states": {0: "batch_size", 1: "encoder_sequence"},
                "encoder_attention_mask": {0: "batch_size", 1: "encoder_sequence"},
                "logits": {0: "batch_size", 1: "sequence"},
            },
            opset_version=14,
        )

        # Save tokenizer
        tokenizer.save_pretrained(model_dir)

        # Save config
        model_config = {
            "max_length": config.get("max_length", 256),
            "decoder_start_token_id": model.config.decoder_start_token_id,
            "eos_token_id": model.config.eos_token_id,
            "pad_token_id": model.config.pad_token_id,
        }
        config_path = model_dir / "vectrix_config.json"
        with open(config_path, "w") as f:
            json.dump(model_config, f, indent=2)

        print(f"  mREBEL model exported to: {model_dir}")


def download_models_cli():
    """CLI entry point for downloading models."""
    import argparse

    parser = argparse.ArgumentParser(description="Download VectrixDB models")
    parser.add_argument(
        "--type",
        choices=["all", "dense", "sparse", "reranker", "colbert", "late_interaction", "rebel", "graphrag"],
        default="all",
        help="Model type to download (late_interaction = BGE-M3, graphrag = rebel for triplet extraction)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if models exist",
    )
    parser.add_argument(
        "--no-graphrag",
        action="store_true",
        help="Skip GraphRAG model (mREBEL) to save ~500MB",
    )

    args = parser.parse_args()

    downloader = ModelDownloader()

    if args.type == "all":
        downloader.download_all(include_graphrag=not args.no_graphrag)
    elif args.type == "graphrag":
        downloader.download("rebel")
    else:
        downloader.download(args.type)


if __name__ == "__main__":
    download_models_cli()
