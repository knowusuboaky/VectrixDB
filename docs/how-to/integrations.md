# Use it from LangChain, LlamaIndex, any embedding API, or your own plugin

VectrixDB is a library, so the useful question is how it plugs into what you already run. Four answers.

## LangChain

```python
from vectrixdb.integrations.langchain import VectrixVectorStore

store = VectrixVectorStore("docs", path="./data")      # VectrixDB's bundled model, offline
store.add_texts(["Basalt forms when lava cools."], metadatas=[{"topic": "rocks"}])
store.similarity_search("volcanic rock", k=3)
retriever = store.as_retriever(search_kwargs={"k": 5, "mode": "hybrid"})
```

`pip install langchain-core` is the only extra. Pass `embedding=` with any LangChain `Embeddings` to use that model for documents and queries instead; the collection is created at its dimension. `filter=` takes the [VectrixDB filter grammar](../reference/filters.md), and every `search()` keyword (`mode`, `rerank`, `token_budget`, `parents`) passes through `search_kwargs`. `store.db` is the underlying `Vectrix` for memory, ingestion and everything else.

## LlamaIndex

```python
from llama_index.core import StorageContext, VectorStoreIndex
from vectrixdb.integrations.llamaindex import VectrixLlamaStore

store = VectrixLlamaStore("docs", path="./data")
index = VectorStoreIndex.from_documents(
    documents, storage_context=StorageContext.from_defaults(vector_store=store)
)
```

`pip install llama-index-core`. LlamaIndex embeds with its own `embed_model` and hands the vectors over, so the store never embeds and the collection takes that model's dimension on the first add. Deleting by `ref_doc_id` removes every node of the document.

## Any OpenAI-compatible embedding endpoint

```python
from vectrixdb import Vectrix

db = Vectrix("docs", dense_model="openai:text-embedding-3-small")     # api.openai.com, OPENAI_API_KEY
```

The same class reaches any server that speaks the OpenAI embeddings API:

```python
from vectrixdb.models import OpenAIEmbedder

ollama = OpenAIEmbedder("nomic-embed-text", base_url="http://localhost:11434/v1")
db = Vectrix("docs", embed_fn=ollama, dimension=ollama.dimension)
```

Ollama, vLLM, LM Studio and Text Embeddings Inference all work this way. The dimension is asked of the endpoint once. This is the one embedder in the package that needs a network, and the model name is recorded in the collection like any other, so a later open with a different model warns.

## Plugins

A package can add an embedder, reranker, extractor or storage backend by declaring an entry point; VectrixDB finds it by name and nothing in VectrixDB has to change.

```toml
# the plugin's pyproject.toml
[project.entry-points."vectrixdb.embedders"]
my-encoder = "my_package.encoders:MyEncoder"

[project.entry-points."vectrixdb.storage"]
my-store = "my_package.storage:MyStorage"
```

```python
db = Vectrix("docs", dense_model="plugin:my-encoder")
config = StorageConfig(backend="plugin:my-store")
```

An embedder is anything with `embed(texts) -> ndarray` and, ideally, a `dimension` attribute. A storage backend subclasses `vectrixdb.core.storage.BaseStorage`; the storage contract suite in the repository is the definition of done. `vectrixdb.plugins.available("embedders")` lists what is installed, and `register()` adds one in code for tests or for a program that ships its own.

## Timing hooks

Hosts meter their own way. Two callbacks give them the numbers without VectrixDB importing a metrics client:

```python
def meter(event):
    statsd.timing(f"vectrixdb.{event['op']}", event["ms"], tags=[f"collection:{event['collection']}"])

db = Vectrix("docs", on_search=meter, on_add=meter)
```

Each event is a small dict: `op` (`search` or `add`), `ms`, `count`, `mode` and `collection`. A hook that raises is logged and never fails the call.

## The command line

Every command and option is in the [command line reference](../reference/cli.md).

```bash
vectrixdb ingest ./docs --name manual --chunk markdown --parent-size 600 --dedupe 0.9
vectrixdb query "how do I export" --name manual --limit 5 --parents
vectrixdb stats --name manual
vectrixdb serve --port 7337
```

`ingest` takes files or directories (PDF, DOCX, HTML, Markdown, text) with every `add_document()` option; `query` prints a table or `--json`; `stats` shows count, mode, model and size on disk; `serve` starts the REST API and dashboard.
