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
- **6 Storage Backends** - Memory, SQLite, Lakebase, DeltaLake, CosmosDB, PostgreSQL
- **Embedded Models** - Works offline with bundled ONNX models
- **Model Selection** - Choose from bundled, HuggingFace, or GitHub release models
- **Document Index** - Hierarchical document storage with chunking
- **Visual Dashboard** - Built-in web UI for managing collections
- **Zero Config** - Just `pip install` and start using

---

## Installation

### From PyPI (Recommended)

```bash
pip install vectrixdb
```

### From GitHub (Latest)

```bash
pip install git+https://github.com/knowusuboaky/VectrixDB.git
```

### Specific Version from GitHub

```bash
pip install git+https://github.com/knowusuboaky/VectrixDB.git@v1.9.9
```

### From Source

```bash
git clone https://github.com/knowusuboaky/VectrixDB.git
cd VectrixDB
pip install -e .
```

### Optional Dependencies

```bash
# HuggingFace sentence-transformers
pip install vectrixdb[hf]

# FastEmbed (lightweight ONNX embeddings)
pip install vectrixdb[fastembed]

# All embedding providers
pip install vectrixdb[embeddings]

# Visualization (UMAP)
pip install vectrixdb[viz]

# Everything
pip install vectrixdb[all]
```

---

## Quick Start

```python
from vectrixdb import Vectrix

db = Vectrix("my_docs")
db.add(["Python is great", "JavaScript powers the web", "Rust is fast"])

results = db.search("programming")
print(results.top.text)
```

---

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

---

## Model Selection

Customize models for each component. Models load from 3 sources:

### 1. Bundled Models (Offline, No Downloads)

Pre-packaged ONNX models that work without internet (~100MB total):

```python
db = Vectrix(
    "docs",
    mode="ultimate",
    dense_model="e5-small",            # 384 dim, 33MB
    sparse_model="bm25",               # 1MB
    reranker_model="L12",              # 33MB
    late_interaction_model="colbert",  # 33MB
)
```

| Component | Alias | Model | Dimension | Size |
|-----------|-------|-------|-----------|------|
| Dense | `e5-small` | intfloat/e5-small-v2 | 384 | 33MB |
| Sparse | `bm25` | BM25 vocabulary | - | 1MB |
| Reranker | `L12` | ms-marco-MiniLM-L12-v2 | - | 33MB |
| ColBERT | `colbert` | answerai-colbert-small-v1 | 128 | 33MB |

### 2. GitHub Release Models (Auto-Downloaded)

Larger models hosted on GitHub releases (downloaded on first use):

```python
db = Vectrix(
    "docs",
    mode="ultimate",
    dense_model="bge-base",            # 768 dim, higher quality
    sparse_model="bm25",
    reranker_model="bge-reranker",     # Higher quality
    late_interaction_model="colbert-v2",
)
```

| Alias | Model | Dimension | Size |
|-------|-------|-----------|------|
| `bge-base` | BAAI/bge-base-en-v1.5 | 768 | 110MB |
| `bge-small` | BAAI/bge-small-en-v1.5 | 384 | 127MB |
| `bge-reranker` | BAAI/bge-reranker-base | - | 212MB |
| `colbert-v2` | colbert-ir/colbertv2.0 | 128 | 67MB |
| `splade` | SPLADE++ | - | 508MB |

### 3. HuggingFace Models

Use any compatible model from HuggingFace (requires `pip install vectrixdb[hf]`):

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

---

## Storage Backends

VectrixDB supports 6 storage backends:

| Backend | Type | Persistence | Best For |
|---------|------|-------------|----------|
| `memory` | In-Memory | No | Testing, small datasets |
| `sqlite` | File-based | Yes | Local development |
| `lakebase` | PostgreSQL + pgvector | Yes | Databricks Lakebase |
| `delta_lake` | Delta Lake | Yes | Databricks Unity Catalog |
| `cosmosdb` | Azure CosmosDB | Yes | Azure cloud |
| `postgresql` | PostgreSQL + pgvector | Yes | Self-hosted PostgreSQL |

### Memory Storage (Default)

```python
from vectrixdb import VectrixDB, StorageConfig, StorageBackend

# In-memory (default, no persistence)
db = VectrixDB()

# Or explicitly
config = StorageConfig(backend=StorageBackend.MEMORY)
db = VectrixDB(storage_config=config)
```

### SQLite Storage (Local Persistence)

```python
from vectrixdb import VectrixDB

# SQLite with file path
db = VectrixDB(path="./my_vectors")

# Creates: ./my_vectors/vectrix.db
```

### Lakebase Storage (Databricks)

```python
from vectrixdb import Vectrix, VectrixDB

# Connect to Lakebase (PostgreSQL + pgvector)
lakebase = VectrixDB.with_lakebase(
    host="your-lakebase-host.cloud.databricks.com",
    database="databricks_postgres",
    user="your-user",
    password="your-oauth-token",  # OAuth JWT from Lakebase Connect
    port=5432,
    schema="public",  # Optional, defaults to "public"
)

# Use with Vectrix
db = Vectrix(
    "products",
    mode="ultimate",
    storage_backend=lakebase,
)

db.add(texts=["Product A", "Product B"])
results = db.search("query")
```

### Delta Lake Storage (Databricks Unity Catalog)

```python
from vectrixdb import VectrixDB

# Connect to Delta Lake via Databricks SQL
delta = VectrixDB.with_delta_lake(
    workspace_url="https://your-workspace.cloud.databricks.com",
    token="dapi_your_token",
    catalog="main",
    schema="vectrixdb",
    warehouse_id="your_warehouse_id",
)

# Use with Vectrix
db = Vectrix("products", mode="hybrid", storage_backend=delta)
```

### CosmosDB Storage (Azure)

```python
from vectrixdb import VectrixDB, StorageConfig, StorageBackend

config = StorageConfig(
    backend=StorageBackend.COSMOSDB,
    cosmos_endpoint="https://your-account.documents.azure.com:443/",
    cosmos_key="your-primary-key",
    cosmos_database="vectrixdb",
)

db = VectrixDB(storage_config=config)
```

### Adaptive Schema

Schema adapts based on selected mode:

| Mode | Columns Created |
|------|-----------------|
| `dense` | `id`, `dense_embedding`, `metadata`, `text_content`, `created_at`, `updated_at` |
| `hybrid` | + `sparse_embedding` |
| `ultimate` | + `late_interaction_embedding` |
| `graph` | Same as ultimate + graph tables |

---

## Document Index

Hierarchical document storage with automatic chunking:

```python
from vectrixdb import DocumentIndex, chunk_text, chunk_with_context

# Create document index
doc_index = DocumentIndex("./docs_index")

# Chunk text (simple)
chunks = chunk_text(
    "Your long document text here...",
    chunk_size=1000,
    chunk_overlap=200,
)

# Chunk markdown with context (preserves headings)
chunks = chunk_with_context(
    markdown_text,
    chunk_size=1200,
    chunk_overlap=200,
)
# Returns: [{"content": "...", "heading": "Section Title", "level": 2}, ...]

# Build tree from markdown
from vectrixdb import build_tree_from_markdown, build_tree_from_pdf

tree = build_tree_from_markdown(markdown_content)
tree = build_tree_from_pdf(pdf_path)
```

### Document Index with Storage Backend

```python
from vectrixdb import DocumentIndex, VectrixDB

# Connect to storage
lakebase = VectrixDB.with_lakebase(...)

# Document index uses storage backend
doc_index = DocumentIndex(storage=lakebase)

# Save documents and nodes
doc_index.save_document({
    "doc_id": "doc_001",
    "title": "My Document",
    "doc_type": "markdown",
    "page_count": 5,
})

# Query documents
docs = doc_index.list_documents()
nodes = doc_index.get_document_nodes("doc_001")
```

---

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

# Filter by metadata
results = db.search("smartphone", filter={"brand": "Apple"})

# Complex filters
results = db.search("phone", filter={
    "brand": {"$in": ["Apple", "Samsung"]},
    "price": {"$lt": 1000}
})
```

---

## Advanced API

For full control, use the `VectrixDB` class directly:

```python
from vectrixdb import VectrixDB, Collection

# Create database
db = VectrixDB(path="./my_db")

# Create collection with specific dimension
coll = db.create_collection("products", dimension=384)

# Add vectors directly
coll.add(
    ids=["p1", "p2"],
    vectors=[[0.1, 0.2, ...], [0.3, 0.4, ...]],
    metadata=[{"name": "Product A"}, {"name": "Product B"}],
)

# Search with vectors
results = coll.search(query=[0.1, 0.2, ...], limit=10)

# List collections
collections = db.list_collections()

# Delete collection
db.delete_collection("products")
```

---

## Embedded Models API

Use embedding models directly:

```python
from vectrixdb import (
    DenseEmbedder,
    SparseEmbedder,
    RerankerEmbedder,
    LateInteractionEmbedder,
)

# Dense embeddings
dense = DenseEmbedder(model="e5-small")
vectors = dense.embed(["Hello world", "How are you?"])

# Sparse embeddings (BM25)
sparse = SparseEmbedder()
sparse_vectors = sparse.embed(["Hello world"])

# Reranker
reranker = RerankerEmbedder(model="L12")
scores = reranker.rerank("query", ["doc1", "doc2", "doc3"])

# Late interaction (ColBERT)
colbert = LateInteractionEmbedder(model="colbert")
token_embeddings = colbert.embed(["Hello world"])
```

---

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

---

## GraphRAG

Build knowledge graphs from documents:

```python
from vectrixdb import Vectrix, create_openai_config

# Create with graph mode
db = Vectrix("docs", mode="graph")

# Or with custom LLM config
config = create_openai_config(
    api_key="your-openai-key",
    model="gpt-4o-mini",
)

db = Vectrix(
    "docs",
    mode="graph",
    graphrag_config=config,
)

# Add documents (extracts entities & relationships)
db.add(["Apple announced the iPhone 15 in September 2023."])

# Search with graph reasoning
results = db.search("What products did Apple release?")
```

---

## Project Structure

```
VectrixDB/
├── vectrixdb/
│   ├── core/           # Vector index, storage, search
│   │   ├── storage.py  # All storage backends
│   │   ├── collection.py
│   │   ├── database.py
│   │   ├── document_index.py
│   │   ├── graphrag/   # Knowledge graph
│   │   └── search/     # Search algorithms
│   ├── api/            # FastAPI server
│   ├── models/         # Embedded ONNX models
│   │   └── data/       # Bundled model files
│   ├── dashboard/      # Web UI
│   ├── easy.py         # Vectrix simple API
│   └── cli.py          # Command line
├── tests/
└── pyproject.toml
```

---

## Requirements

- Python 3.9+
- No API keys needed (for bundled models)
- Models are bundled or auto-downloaded

---

## License

Apache 2.0

---

## Author

**Kwadwo Daddy Nyame Owusu - Boakye**

GitHub: [@knowusuboaky](https://github.com/knowusuboaky)
