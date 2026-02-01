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
| 4 Search Tiers | Yes | No | No | No |
| GraphRAG Built-in | Yes | No | No | No |
| Zero Config | Yes | No | Yes | Yes |
| No API Keys Needed | Yes | Yes | No | No |
| Open Source | Yes | Yes | Yes | No |

## Quick Start

```bash
pip install vectrixdb
```

```python
from vectrixdb import Vectrix

# Create database with hybrid search (uses bundled English models)
db = Vectrix("my_docs", tier="hybrid", language="en")

# Add documents
db.add([
    "Python is great for data science",
    "JavaScript powers the web",
    "Rust is known for memory safety"
])

# Search
results = db.search("programming languages")
print(results.top.text)  # Best match
```

## 4-Tier System

| Tier | Features | Use Case |
|------|----------|----------|
| **dense** | Vector similarity | Fast semantic search |
| **hybrid** | + BM25 sparse | Better keyword matching |
| **ultimate** | + ColBERT late interaction | Maximum accuracy |
| **graph** | + Knowledge graph | Complex reasoning (GraphRAG) |

```python
# Dense tier (fastest)
db = Vectrix("docs", tier="dense")

# Hybrid tier (balanced)
db = Vectrix("docs", tier="hybrid")

# Ultimate tier (best quality)
db = Vectrix("docs", tier="ultimate")

# Graph tier (GraphRAG)
db = Vectrix("docs", tier="graph")
```

## Search Modes

```python
# Dense - vector similarity
results = db.search("AI", mode="dense")

# Sparse - BM25 keyword
results = db.search("machine learning", mode="sparse")

# Hybrid - combined
results = db.search("neural networks", mode="hybrid")

# Rerank - with cross-encoder
results = db.search("deep learning", mode="rerank")
```

## With Metadata

```python
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
```

## Embedded Models

VectrixDB bundles English models (~386MB) - no downloads needed:

| Model | Purpose | Size |
|-------|---------|------|
| e5-small-v2 | Dense embeddings | 129MB |
| ms-marco-MiniLM | Reranking | 129MB |
| answerai-colbert-small | Late interaction | 129MB |
| BM25 vocab | Sparse search | 17KB |

### Multilingual Models (auto-download)

For 100+ languages, models download from GitHub on first use:

```python
# Multilingual (downloads ~450MB on first use)
db = Vectrix("docs", tier="hybrid")  # or language="multi"

# English only (bundled, no download)
db = Vectrix("docs", tier="hybrid", language="en")
```

| Model | Purpose | Languages |
|-------|---------|-----------|
| multilingual-e5-small | Dense | 100+ |
| mmarco-mMiniLMv2 | Reranking | 15+ |
| BGE-M3 | Late interaction | 100+ |
| mREBEL | GraphRAG extraction | 18 |

## REST API & Dashboard

```bash
# Start server
VECTRIXDB_API_KEY=your_key vectrixdb serve --port 7337

# Open dashboard
# http://localhost:7337/dashboard
```

```bash
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
│   │   └── data/       # Bundled English models
│   ├── dashboard/      # Web UI
│   └── cli.py          # Command line
├── tests/              # Jupyter notebooks
└── requirements.txt
```

## Installation from Source

```bash
git clone https://github.com/knowusuboaky/VectrixDB.git
cd VectrixDB
pip install -e .
```

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
