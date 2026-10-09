---
hide:
  - navigation
  - toc
---

<div class="vx-hero" markdown>

# The vector database that works <em>with no network and no accounts</em>

<p class="lead">Embedded ONNX models, GraphRAG, MCP for assistants, and eight storage backends.
Nothing to sign up for, no API key, no service to run. <code>pip install</code> and search.</p>

<div class="vx-actions" markdown>
[Get started](tutorial/getting-started.md){ .md-button .md-button--primary }
[Connect an assistant](how-to/mcp-server.md){ .md-button }
[Use the dashboard](how-to/dashboard.md){ .md-button }
[GitHub](https://github.com/knowusuboaky/VectrixDB){ .md-button }
</div>

![A question typed into the dashboard, and every result saying how well it matched and what found it](images/dashboard/tour-search.gif)
<span class="vx-caption">A question goes in, and every result says how well it matched and what found it: meaning, keywords, or both.</span>

</div>

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

That is the whole setup. The embedding model ships in the package, so the first
search works offline on a machine that has never seen an API key.

## New in 2.2

<div class="vx-tours" markdown>

<div class="vx-tour" markdown>
![An assistant connects over MCP, lists the collections it may reach, and searches, every answer with its citation](images/dashboard/tour-mcp.gif)
### An assistant searches over MCP <span class="vx-new">2.2</span>
As the person it works for: their role, their collections, every policy. [MCP](how-to/mcp-server.md)
</div>

<div class="vx-tour" markdown>
![A key made for one collection sees only that one, a reader may not search, and a call with no key is told where to sign in](images/dashboard/tour-mcp-keys.gif)
### A key for one collection <span class="vx-new">2.2</span>
It cannot tell the others exist, and every refusal comes back in words. [Keys for a team](how-to/mcp-keys.md)
</div>

<div class="vx-tour" markdown>
![Compose starting the server, the extraction service and Jaeger, then a scan read and a search that finds its words](images/containers/compose.gif)
### Containers <span class="vx-new">2.2</span>
Compose starts the server, the extraction service and Jaeger. A scan goes in and a search finds it. [Run it in containers](how-to/containers.md)
</div>

<div class="vx-tour" markdown>
![Jaeger with the scan's trace, from the server into the extraction service, and a search's attributes](images/containers/trace.gif)
### Traces <span class="vx-new">2.2</span>
One trace from the server into the extraction service: counts and timings, never the text. [Tracing](how-to/tracing.md)
</div>

</div>

Also new: clients for [Python, TypeScript, Go and Rust](how-to/clients.md), a server
that [stays up under load](how-to/sizing.md), collections that keep up with
[feeds and pages](how-to/sources.md), and [`vectrixdb doctor`](how-to/troubleshoot.md).

## See it work

`pip install "vectrixdb[api]"` and `vectrixdb serve` give the same collections
a REST API and a dashboard. These are the real pages and the real containers,
filmed by the scripts that keep the pictures current.

<div class="vx-tours" markdown>

<div class="vx-tour" markdown>
![A file added on the Ingest page, its chunks counted as they are written](images/dashboard/tour-ingest.gif)
### Ingest
A PDF goes in. The server says what it read: pages, chunks, and a citation for each.
</div>

<div class="vx-tour" markdown>
![Collections, one opened to its Points, Policy, Builds and Quality tabs](images/dashboard/tour-collections.gif)
### Collections
Every collection with its state, mode and model, and what it holds, tab by tab.
</div>

<div class="vx-tour" markdown>
![The Evaluate page: three picks, found in the top 10 against search time, and the chunking runs](images/dashboard/tour-evaluate.gif)
### Evaluate
Every setup ranked on your own questions, and three picks: most found, best balance, fastest.
</div>

<div class="vx-tour" markdown>
![The Access page: who may read, write and administer, and the state badge in the sidebar](images/dashboard/tour-access.gif)
### Access
People who sign in as themselves, roles, and a record of who read what.
</div>

<div class="vx-tour" markdown>
![The Console: a request built, sent, and the curl it shows](images/dashboard/tour-console.gif)
### Console
Build a request, send it, and copy the curl that does the same.
</div>

<div class="vx-tour" markdown>
![A walk through Overview, Collections, Evaluate and Audit](images/dashboard/tour-pages.gif)
### The whole walk
Overview, Collections, the evaluation run with its picks, and Audit.
</div>

</div>

## Where to go next

### Start

<div class="grid cards vx-three" markdown>

- **[Getting started](tutorial/getting-started.md)**

    Build something small, end to end.

- **[Install what you need](how-to/install.md)**

    Which extra adds what.

- **[Add it with your coding agent](how-to/coding-agents.md)**

    One prompt, a skill, and llms.txt.

</div>

### Assistants and agents

<div class="grid cards vx-three" markdown>

- **[MCP](how-to/mcp-server.md)**

    An assistant searches as the person it works for.

- **[Connect your client](how-to/mcp-connect.md)**

    VS Code, Cursor, a desktop assistant, a command line.

- **[Conversation memory](how-to/conversation-memory.md)**

    Turns, pinned facts, a context sized to a budget.

</div>

### Ingest, search, evaluate

<div class="grid cards vx-three" markdown>

- **[Ingest documents](how-to/ingest-documents.md)**

    PDF, Office, scans, speech and video, cut and cited.

- **[Keep in step with feeds and pages](how-to/sources.md)**

    A collection that keeps itself current.

- **[Measure retrieval](how-to/measure-retrieval.md)**

    Golden questions written from your own documents.

</div>

### Secure and run

<div class="grid cards vx-three" markdown>

- **[Sign people in](how-to/sign-in.md)**

    Company single sign-on, passkeys, roles.

- **[Deploy the server](how-to/deploy.md)**

    One settings file, checked, then tried with doctor.

- **[Run it in containers](how-to/containers.md)**

    Two images, Compose and Kubernetes.

</div>

### Build on it

<div class="grid cards vx-three" markdown>

- **[Clients and SDKs](how-to/clients.md)**

    Python, TypeScript, Go and Rust, the same calls in each.

- **[Run the REST API](how-to/rest-api.md)**

    Every route, and the OpenAPI document to generate from.

- **[What it does not do](explanation/limits.md)**

    Every limit in one place, with what to do about each.

</div>

## What is in the box

| | |
|---|---|
| **Search modes** | Dense, Hybrid, Ultimate, Graph (GraphRAG) |
| **Storage** | Memory, SQLite, Lakebase, Delta Lake, Cosmos DB, Azure AI Search, OpenSearch, Aurora PostgreSQL |
| **Models** | Bundled ONNX, or bring your own from HuggingFace |
| **Readers** | PDF, Word, Excel, Markdown, scans by sight, speech, video |
| **Memory** | Conversation turns, pinned facts, recency and feedback weighting |
| **Extras** | Document index with chunking, dashboard, REST API, CLI, MCP server |

## Install what you need

The core install is deliberately small: vector search, and nothing else.

```bash
pip install vectrixdb              # core
pip install vectrixdb[api]         # + REST API and dashboard
pip install vectrixdb[signin]      # + people signing in: single sign-on, passkeys, codes
pip install vectrixdb[mcp]         # + MCP server for assistants
pip install vectrixdb[client]      # + the client for a VectrixDB server, vectrixdb.connect
pip install vectrixdb[tracing]     # + OpenTelemetry spans
pip install vectrixdb[documents]   # + PDF, Word and Excel readers
pip install vectrixdb[extract]     # + every local reader: documents, OCR, speech, video
pip install vectrixdb[aws]         # + OpenSearch and Aurora
pip install vectrixdb[azure]       # + Azure AI Search and Cosmos DB
pip install vectrixdb[databricks]  # + Lakebase and Delta Lake
pip install vectrixdb[nlp]         # + spaCy for GraphRAG entity extraction
pip install vectrixdb[all]         # every backend, model and server extra; not extract or nlp
```
