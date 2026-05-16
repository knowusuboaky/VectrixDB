# VectrixDB

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python Versions](https://img.shields.io/pypi/pyversions/vectrixdb.svg)](https://pypi.org/project/vectrixdb/)
[![VectrixDB Version](https://img.shields.io/pypi/v/vectrixdb.svg)](https://pypi.org/project/vectrixdb/)
[![Downloads](https://pepy.tech/badge/vectrixdb)](https://pepy.tech/project/vectrixdb)
[![Issues](https://img.shields.io/github/issues/knowusuboaky/VectrixDB)](https://github.com/knowusuboaky/VectrixDB/issues)
[![Contact](https://img.shields.io/badge/Email-Contact-green.svg)](mailto:kwadwo.owusuboakye@outlook.com)

**Where vectors come alive.**

A lightweight vector database with embedded ML models, beautiful dashboard, and GraphRAG - no API keys required.

---

## Features

- **4 Search Modes** - Dense, Hybrid, Ultimate, and Graph (GraphRAG)
- **Embedded Models** - Works offline with bundled ONNX models
- **Model Selection** - Choose from bundled, HuggingFace, or GitHub release models
- **Visual Dashboard** - Built-in web UI for managing collections
- **Zero Config** - Just `pip install` and start using

## Installation

```bash
pip install vectrixdb
```

## Quick Start

```python
from vectrixdb import Vectrix

db = Vectrix("my_docs")
db.add(["Python is great", "JavaScript powers the web", "Rust is fast"])

results = db.search("programming")
print(results.top.text)
```

## Search Modes

VectrixDB offers 4 search modes, each building on the previous:

| Mode | Components | Best For |
|------|------------|----------|
| `dense` | Vector similarity | Fast semantic search |
| `hybrid` | Dense + Sparse + Reranker | Keyword + semantic matching |
| `ultimate` | Hybrid + ColBERT | Maximum accuracy |
| `graph` | Ultimate + Knowledge Graph | Complex reasoning (GraphRAG) |

```python
# Choose your mode
db = Vectrix("docs", mode="dense")     # Fastest
db = Vectrix("docs", mode="hybrid")    # Balanced
db = Vectrix("docs", mode="ultimate")  # Best quality
db = Vectrix("docs", mode="graph")     # GraphRAG
```

## Model Selection

Customize models for each component. Models load from 3 sources:

### 1. Bundled Models (Offline)

Pre-packaged ONNX models that work without internet:

```python
db = Vectrix(
    "docs",
    mode="ultimate",
    dense_model="bge-small",
    sparse_model="splade",
    reranker_model="L6",
    late_interaction_model="colbert",
)
```

| Component | Alias | Model | Size |
|-----------|-------|-------|------|
| Dense | `bge-small` | BAAI/bge-small-en-v1.5 | 127MB |
| Dense | `e5-small` | intfloat/e5-small-v2 | 32MB |
| Sparse | `splade` | SPLADE++ | 508MB |
| Sparse | `bm25` | BM25 vocabulary | 1MB |
| Reranker | `L6` | ms-marco-MiniLM-L6-v2 | 87MB |
| Reranker | `L12` | ms-marco-MiniLM-L12-v2 | 32MB |
| ColBERT | `colbert` | answerai-colbert-small-v1 | 33MB |

### 2. HuggingFace Models

Use any compatible model from HuggingFace (downloads on first use):

```python
db = Vectrix(
    "docs",
    mode="hybrid",
    dense_model="BAAI/bge-large-en-v1.5",
    sparse_model="naver/splade-cocondenser-ensembledistil",
    reranker_model="cross-encoder/ms-marco-MiniLM-L-12-v2",
)
```

**Compatible models:**
- Dense: `BAAI/bge-large-en-v1.5`, `intfloat/e5-large-v2`, `sentence-transformers/all-mpnet-base-v2`
- Sparse: `naver/splade-cocondenser-ensembledistil`
- Reranker: `cross-encoder/ms-marco-MiniLM-L-12-v2`, `BAAI/bge-reranker-base`
- ColBERT: `jinaai/jina-colbert-v2`, `colbert-ir/colbertv2.0`

### 3. GitHub Release Models

Multilingual models hosted on GitHub releases:

```python
db = Vectrix(
    "docs",
    mode="ultimate",
    late_interaction_model="github:bge-m3",
    reranker_model="github:reranker-multi",
)
```

| Tag | Model | Type | Languages | Size |
|-----|-------|------|-----------|------|
| `github:bge-m3` | BGE-M3 | ColBERT | 100+ | 563MB |
| `github:reranker-multi` | mMiniLMv2-L12 | Reranker | 15+ | 113MB |
| `github:dense-multi` | multilingual-e5-small | Dense | 100+ | 113MB |

## Metadata & Filtering

```python
db.add(
    texts=["iPhone 15", "Galaxy S24", "Pixel 8"],
    metadata=[
        {"brand": "Apple", "price": 999},
        {"brand": "Samsung", "price": 899},
        {"brand": "Google", "price": 699}
    ]
)

results = db.search("smartphone", filter={"brand": "Apple"})
```

## Storage Backends

Use external storage backends (Lakebase, DeltaLake, CosmosDB) with full search mode support:

```python
from vectrixdb import Vectrix, VectrixDB

# Connect to Lakebase (PostgreSQL + pgvector)
lakebase = VectrixDB.with_lakebase(
    host="your-lakebase-host",
    database="vectrixdb",
    user="your-user",
    password="your-password",
)

# Use Vectrix with storage backend + ultimate mode
db = Vectrix(
    "products",
    mode="ultimate",
    dense_model="bge-small",
    sparse_model="splade",
    reranker_model="L6",
    late_interaction_model="colbert",
    storage_backend=lakebase,
)

db.add(texts=["Product A", "Product B"])
results = db.search("query")  # Full ultimate search from Lakebase
```

### Adaptive Schema

Schema adapts based on selected mode:

| Mode | Columns Created |
|------|-----------------|
| `dense` | `dense_embedding` |
| `hybrid` | `dense_embedding` + `sparse_embedding` |
| `ultimate` | `dense_embedding` + `sparse_embedding` + `late_interaction_embedding` |
| `graph` | Same as ultimate + graph tables |

All modes store `text_content` for reranker (computed at query time).

## REST API

Start the server:

```bash
VECTRIXDB_API_KEY=your_secret vectrixdb serve --port 7337
```

Open the dashboard at `http://localhost:7337/dashboard`

### API Examples

```bash
# Create collection
curl -X POST http://localhost:7337/api/v1/collections \
  -H "Content-Type: application/json" \
  -H "api-key: your_secret" \
  -d '{"name": "docs", "dimension": 384}'

# Add documents (auto-embedding)
curl -X POST http://localhost:7337/api/v1/collections/docs/text-upsert \
  -H "Content-Type: application/json" \
  -H "api-key: your_secret" \
  -d '{"points": [{"id": "1", "text": "Hello world"}]}'

# Search
curl -X POST http://localhost:7337/api/v1/collections/docs/text-search \
  -H "Content-Type: application/json" \
  -H "api-key: your_secret" \
  -d '{"query_text": "greeting", "limit": 10}'
```

## Project Structure

```
VectrixDB/
├── vectrixdb/
│   ├── core/           # Vector index, storage, search
│   │   ├── graphrag/   # Knowledge graph
│   │   └── search/     # Search algorithms
│   ├── api/            # FastAPI server
│   ├── models/         # Embedded ONNX models
│   ├── dashboard/      # Web UI
│   └── cli.py          # Command line
├── tests/
└── requirements.txt
```

## Install from Source

```bash
git clone https://github.com/knowusuboaky/VectrixDB.git
cd VectrixDB
pip install -e .
```

## Requirements

- Python 3.9+
- No API keys needed
- Models are bundled or auto-downloaded

## License

Apache 2.0

## Author

**Kwadwo Daddy Nyame Owusu - Boakye**

GitHub: [@knowusuboaky](https://github.com/knowusuboaky)
