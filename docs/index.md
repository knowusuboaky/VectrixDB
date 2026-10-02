# VectrixDB

**The vector database that works with no network and no accounts.**

Embedded ONNX models, GraphRAG, and eight storage backends. Nothing to sign up
for, no API key, no service to run. `pip install` and search.

```bash
pip install vectrixdb
```

```python
from vectrixdb import Vectrix

db = Vectrix("my_docs")
db.add(["Python is great", "JavaScript powers the web", "Rust is fast"])

results = db.search("programming")
print(results.top.text)
```

That is the whole setup. The embedding model ships in the package.

`vectrixdb serve` adds a server with a dashboard, people who sign in as
themselves, and a record of who read what:

![The VectrixDB dashboard: search results, each led by its relevance](images/dashboard/search.png)

## Where to go next

<div class="grid cards" markdown>

- **[Getting started](tutorial/getting-started.md)**

    Learn the library by building something small, end to end.

- **[How-to guides](how-to/handle-errors.md)**

    Recipes for specific jobs: error handling, storage backends, the REST API.

- **[Reference](reference/easy.md)**

    Signatures and behaviour, generated from the source.

- **[Explanation](explanation/why-vectrixdb.md)**

    Why the search modes differ, and where the pure-Python index runs out.

- **[Conversation memory](how-to/conversation-memory.md)**

    Turns, pinned facts and a context block sized to a token budget.

- **[MCP server](how-to/mcp-server.md)**

    Give an assistant a collection to search and remember into.

- **[Use the dashboard](how-to/dashboard.md)**

    Every page of the server's dashboard, with pictures, and who sees which.

- **[Deploy the server](how-to/deploy.md)**

    One settings file, checked before a start, read by every command.

- **[Tips and tricks](how-to/tips.md)**

    The things that are easy to miss, in the library and the dashboard.

- **[What it does not do](explanation/limits.md)**

    Every limit in one place, with what to do about each.

</div>

## What is in the box

| | |
|---|---|
| **Search modes** | Dense, Hybrid, Ultimate, Graph (GraphRAG) |
| **Storage** | Memory, SQLite, Lakebase, Delta Lake, Cosmos DB, Azure AI Search, OpenSearch, Aurora PostgreSQL |
| **Models** | Bundled ONNX, or bring your own from HuggingFace |
| **Memory** | Conversation turns, pinned facts, recency and feedback weighting |
| **Extras** | Document index with chunking, visual dashboard, REST API, CLI, MCP server |

## Install what you need

The core install is deliberately small: vector search, and nothing else.

```bash
pip install vectrixdb              # core
pip install vectrixdb[api]         # + REST API and dashboard
pip install vectrixdb[signin]      # + people signing in: single sign-on, passkeys, codes
pip install vectrixdb[mcp]         # + MCP server for assistants
pip install vectrixdb[documents]   # + PDF, Word and Excel readers
pip install vectrixdb[extract]     # + every local reader: documents, OCR, speech, video
pip install vectrixdb[aws]         # + OpenSearch and Aurora
pip install vectrixdb[azure]       # + Azure AI Search and Cosmos DB
pip install vectrixdb[databricks]  # + Lakebase and Delta Lake
pip install vectrixdb[nlp]         # + spaCy for GraphRAG entity extraction
pip install vectrixdb[all]         # every backend, model and server extra; not extract or nlp
```
