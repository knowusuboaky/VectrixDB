# VectrixDB

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python Versions](https://img.shields.io/pypi/pyversions/vectrixdb.svg)](https://pypi.org/project/vectrixdb/)
[![VectrixDB Version](https://img.shields.io/pypi/v/vectrixdb.svg)](https://pypi.org/project/vectrixdb/)
[![Downloads](https://pepy.tech/badge/vectrixdb)](https://pepy.tech/project/vectrixdb)
[![Issues](https://img.shields.io/github/issues/knowusuboaky/VectrixDB)](https://github.com/knowusuboaky/VectrixDB/issues)
[![Contact](https://img.shields.io/badge/Email-Contact-green.svg)](mailto:kwadwo.owusuboakye@outlook.com)

**Where vectors come alive.**

A lightweight, visual-first vector database with embedded ML models - no API keys required.

## Why VectrixDB?

| Feature | VectrixDB | Qdrant | Chroma | Pinecone |
|---------|-----------|--------|--------|----------|
| Beautiful Dashboard | Yes | Basic | No | No |
| Embedded ML Models | Yes | No | No | No |
| 4 Search Modes | Yes | No | No | No |
| Model Selection | Yes | No | No | No |
| GraphRAG Built-in | Yes | No | No | No |
| Zero Config | Yes | No | Yes | Yes |
| No API Keys Needed | Yes | Yes | No | No |
| Open Source | Yes | Yes | Yes | No |

## Quick Start

\`\`\`bash
pip install vectrixdb
\`\`\`

\`\`\`python
from vectrixdb import Vectrix

# Basic usage (bundled models, offline)
db = Vectrix("my_docs")
db.add(["Python is great", "JavaScript powers the web", "Rust is fast"])
results = db.search("programming")
print(results.top.text)

# With model selection
db = Vectrix(
    "my_docs",
    mode="hybrid",
    dense_model="bge-small",
    sparse_model="splade",
    reranker_model="L6",
)
results = db.search("programming languages")
\`\`\`

## 4-Tier System

| Mode | Components | Use Case |
|------|------------|----------|
| **dense** | Vector similarity | Fast semantic search |
| **hybrid** | Dense + Sparse + Reranker | Better keyword matching |
| **ultimate** | Dense + Sparse + Reranker + ColBERT | Maximum accuracy |
| **graph** | Ultimate + Knowledge Graph | Complex reasoning (GraphRAG) |

\`\`\`python
# Dense mode (fastest)
db = Vectrix("docs", mode="dense")

# Hybrid mode (balanced)
db = Vectrix("docs", mode="hybrid")

# Ultimate mode (best quality)
db = Vectrix("docs", mode="ultimate")

# Graph mode (GraphRAG)
db = Vectrix("docs", mode="graph")
\`\`\`

## Model Selection

Choose your models for each component:

\`\`\`python
from vectrixdb import Vectrix

# Bundled models (offline, no download)
db = Vectrix(
    "docs",
    mode="ultimate",
    dense_model="bge-small",        # or "e5-small", "multilingual"
    sparse_model="splade",          # or "bm25"
    reranker_model="L6",            # or "L12"
    late_interaction_model="colbert",
)

# HuggingFace models (download on first use)
db = Vectrix(
    "docs",
    mode="hybrid",
    dense_model="BAAI/bge-large-en-v1.5",
    sparse_model="naver/splade-cocondenser-ensembledistil",
    reranker_model="cross-encoder/ms-marco-MiniLM-L-12-v2",
)

# GitHub releases (download on first use)
db = Vectrix(
    "docs",
    mode="ultimate",
    late_interaction_model="github:bge-m3",      # 100+ languages
    reranker_model="github:reranker-multi",      # 15+ languages
)
\`\`\`

### Available Models

#### Bundled (Offline)

| Component | Aliases | Model | Size |
|-----------|---------|-------|------|
| Dense | \`"bge-small"\` | BAAI/bge-small-en-v1.5 | 127MB |
| Dense | \`"e5-small"\` | intfloat/e5-small-v2 | 32MB |
| Sparse | \`"splade"\` | SPLADE++ (neural) | 508MB |
| Sparse | \`"bm25"\` | BM25 vocabulary | 1MB |
| Reranker | \`"L6"\` | ms-marco-MiniLM-L6-v2 | 87MB |
| Reranker | \`"L12"\` | ms-marco-MiniLM-L12-v2 | 32MB |
| Late Interaction | \`"colbert"\` | answerai-colbert-small-v1 | 33MB |

#### GitHub Releases

| Release Tag | Model | Type | Size |
|-------------|-------|------|------|
| \`github:bge-m3\` | BGE-M3 | Late Interaction | 563MB |
| \`github:reranker-multi\` | mMiniLMv2-L12 | Reranker | 113MB |
| \`github:dense-multi\` | multilingual-e5-small | Dense | 113MB |

#### HuggingFace

\`\`\`python
# Dense models
dense_model="BAAI/bge-large-en-v1.5"
dense_model="intfloat/e5-large-v2"
dense_model="sentence-transformers/all-mpnet-base-v2"

# Sparse models
sparse_model="naver/splade-cocondenser-ensembledistil"

# Reranker models
reranker_model="cross-encoder/ms-marco-MiniLM-L-12-v2"
reranker_model="BAAI/bge-reranker-base"

# Late interaction models
late_interaction_model="jinaai/jina-colbert-v2"
late_interaction_model="colbert-ir/colbertv2.0"
\`\`\`

## With Metadata

\`\`\`python
db.add(
    texts=["iPhone 15", "Galaxy S24", "Pixel 8"],
    metadata=[
        {"brand": "Apple", "price": 999},
        {"brand": "Samsung", "price": 899},
        {"brand": "Google", "price": 699}
    ]
)

# Filter by metadata
results = db.search("smartphone", filter={"brand": "Apple"})
\`\`\`

## REST API & Dashboard

\`\`\`bash
# Start server
VECTRIXDB_API_KEY=your_key vectrixdb serve --port 7337

# Open dashboard
# http://localhost:7337/dashboard
\`\`\`

\`\`\`bash
# Create collection
curl -X POST http://localhost:7337/api/v1/collections \
  -H "api-key: your_key" \
  -d '{"name": "docs", "dimension": 384}'

# Add with auto-embedding
curl -X POST http://localhost:7337/api/v1/collections/docs/text-upsert \
  -H "api-key: your_key" \
  -d '{"points": [{"id": "1", "text": "Hello world"}]}'

# Search
curl -X POST http://localhost:7337/api/v1/collections/docs/text-search \
  -H "api-key: your_key" \
  -d '{"query_text": "greeting", "limit": 10}'
\`\`\`

## Project Structure

\`\`\`
VectrixDB/
├── vectrixdb/
│   ├── core/           # Vector index, storage, search
│   │   ├── graphrag/   # Knowledge graph
│   │   └── search/     # Search algorithms
│   ├── api/            # FastAPI server
│   ├── models/         # Embedded ONNX models
│   │   └── data/       # Bundled models
│   ├── dashboard/      # Web UI
│   └── cli.py          # Command line
├── tests/              # Jupyter notebooks
└── requirements.txt
\`\`\`

## Installation from Source

\`\`\`bash
git clone https://github.com/knowusuboaky/VectrixDB.git
cd VectrixDB
pip install -e .
\`\`\`

## Requirements

- Python 3.9+
- No external API keys
- Models bundled or auto-downloaded

## License

Apache 2.0

## Author

**Kwadwo Daddy Nyame Owusu - Boakye**

GitHub: [@knowusuboaky](https://github.com/knowusuboaky)

---

*Where vectors come alive.*
