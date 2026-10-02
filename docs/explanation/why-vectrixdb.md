# Why VectrixDB

There are more vector databases than there are good reasons to write one. This page says what VectrixDB is for, what it is not for, and how to tell in a minute whether it is the right tool for you.

## The one-line case

**Search over your own documents with nothing to sign up for, nothing to run, and nothing to download.** `pip install vectrixdb` gives you an embedding model, an ANN index, keyword search, a reranker, a knowledge graph and conversation memory, in one process, on one machine, offline.

Everything else on this page is that sentence unpacked.

## Who it is for

- **A Python program that needs search inside it.** A desktop app, a CLI, a notebook, a batch job, a service that already exists and does not want a second one beside it. VectrixDB is a library: `import` it and go.
- **Anyone who cannot ship data to an API.** Regulated data, air-gapped machines, or simply a laptop on a train. The English models ship in the wheel and nothing phones home; `VECTRIXDB_OFFLINE=1` makes that a hard guarantee.
- **Chat products that pay for tokens.** `remember()`, `recall()`, `feedback()` and `context()` turn a collection into a budgeted conversation memory, and `search(token_budget=...)` never returns more text than the model can afford to read. This is the reason the library exists in its current form.
- **RAG that needs more than nearest neighbours.** Hybrid search with real BM25, a cross-encoder reranker, ColBERT late interaction and GraphRAG with community summaries are all one keyword argument away, and `explain=True` shows what each stage contributed.

## Who it is not for

Being honest here saves you an afternoon.

- **More than one machine.** VectrixDB is single-node and embeddable. There is no sharding, no replication and no cluster. If your corpus or your traffic needs several boxes, you want a server product: Qdrant, Weaviate, Milvus, or a managed service.
- **Hundreds of millions of vectors.** The index is in memory when writing. Read-only opens are memory-mapped and can exceed RAM, but building a billion-vector index is not this library's job.
- **GPU-bound embedding at scale.** The bundled models run on CPU through ONNX Runtime. Embedding is the slowest thing VectrixDB does, and on a GPU box a dedicated embedding service will beat it by a wide margin. You can bring your own vectors and skip the model entirely.
- **A hosted API for many tenants.** The REST API and dashboard are for local use and demos. Authentication, multi-tenancy, metering and rate limits belong to whatever embeds the library, not to the library.

## How it compares

The [benchmarks page](benchmarks.md) has the measured numbers, reproduced by one script in the repository. The short version, as of the last run there:

- **Latency and recall** on an HNSW index are in the same band as Chroma, on the same vectors, on the same machine. Nobody wins by an order of magnitude; the index libraries underneath are all good.
- **Ingest** is dominated by embedding for everyone. VectrixDB's advantage is that the model is already there; its disadvantage is that the model is small and on the CPU.
- **Retrieval quality** on BEIR datasets is what a 384-dimensional small encoder gets you: bge-small-en-v1.5 scores 0.71 nDCG@10 on SciFact in dense mode and 0.74 in hybrid, above BM25, below the large models. Hybrid mode with BM25 helps on keyword-heavy sets. A larger model can be dropped in, at the cost of a download.

What the table does not show is the part that made us build it: the others give you an index, VectrixDB gives you the whole retrieval stack with a memory system on top, and it works on a plane.

## The design rules

These explain most decisions you will bump into.

1. **Offline is the default, not an option.** Nothing downloads at import or first use unless you ask by name. Bundled models are checksummed.
2. **It is a library.** No daemon, no ports, no background threads you did not start. Close it and it is gone. Two processes can share a collection through SQLite and the on-disk index; the [concurrency page](concurrency.md) says exactly what is safe.
3. **Every number is measured.** Recall against brute force, MRR on a fixture set, memory per vector, import time and wheel size are all checked in CI and fail on regression. The benchmark tables in the docs are pasted from script output and nothing else.
4. **Honest results.** `Results.truncated`, `Results.degraded` and `Result.explain` tell you when something was cut, fell back or was reranked, instead of returning a shorter list and hoping you do not notice.
5. **Plain files.** A collection is a directory of SQLite files and one index file. `export()` zips it. There is no proprietary format to migrate away from.

## Try it in a minute

```python
from vectrixdb import Vectrix

db = Vectrix("notes")
db.add(["Basalt forms when lava cools quickly at the surface.",
        "Sourdough is leavened by wild yeast and bacteria."])
print(db.search("which rock comes from fast-chilled lava").top.text)
```

If that is the shape of your problem, the [tutorial](../tutorial/getting-started.md) takes it from here. If it is not, the list above should have told you which tool to look at instead.
