# Choose a storage backend

SQLite is the default and is the right answer for most single-machine use. Reach
for something else when you need multiple machines reading the same data.

## Install the extra first

Each cloud backend needs its driver, which is not in the core install:

```bash
pip install vectrixdb[aws]         # OpenSearch, Aurora PostgreSQL
pip install vectrixdb[azure]       # Cosmos DB
pip install vectrixdb[databricks]  # Lakebase, Delta Lake
pip install vectrixdb[postgres]    # PostgreSQL
```

## Configure

```python
from vectrixdb import StorageBackend, StorageConfig, create_storage

config = StorageConfig(
    backend=StorageBackend.SQLITE,
    sqlite_path="./data",
    sqlite_wal_mode=True,  # committed writes survive an abrupt shutdown
)
storage = create_storage(config)
storage.connect()
```

Swap `backend` and fill in that backend's fields:

```python
StorageConfig(
    backend=StorageBackend.COSMOSDB,
    cosmos_endpoint="https://account.documents.azure.com:443/",
    cosmos_key=os.environ["COSMOS_KEY"],
    cosmos_database="vectrixdb",
)
```

!!! warning "Credentials live in the config object"
    `StorageConfig` holds keys and passwords as plain attributes. Read them from
    the environment or a secret manager, and do not log the config.

## Azure AI Search

```python
import os
from vectrixdb import Vectrix, VectrixDB

azure = VectrixDB.with_azure_search(
    endpoint="https://my-service.search.windows.net",
    key=os.environ["AZURE_SEARCH_KEY"],
)
db = Vectrix("products", mode="hybrid", storage_backend=azure)
```

`pip install vectrixdb[azure]` brings `azure-search-documents` and `azure-identity`, and `openai`, which the second vector below calls its deployment through. Leave `key=None` to authenticate with `DefaultAzureCredential`: managed identity on Azure, your CLI login on a laptop, environment variables in CI.

What you get: one Azure index per collection, named `<prefix>-<collection>`, with a vector field on Azure's HNSW, a BM25 text field, and a JSON field for your metadata. Dense mode is a vector query; hybrid mode is one request with text and vector that the service fuses with reciprocal rank fusion. Keyword mode is the collection's own words index while it holds anything; a handle opened to search an index another process filled holds nothing here, so its keyword search is the service's own. A policied hybrid search goes through the service too when the policy's fields are promoted (below), scope rules with the query and redaction rules decided locally with exact counts; otherwise it takes the local path.

A server beside an ingest worker is a second reader of the same backend. Each process keeps its own list of collections, so a database opened plainly knows only what it made itself. `VectrixDB(..., follow_shared=True)` also opens the collections the backend holds that another process made: at start, when it lists collections, and when a name is looked up that it does not know. The API server is made that way. `db.open_shared()` does the same on demand. It is off by default because `Vectrix` creates its collection, with its own options, when the lookup misses.

`semantic=True` gives the index a semantic configuration, on tiers that include Azure's semantic ranker, and makes the ranker the default for hybrid search: the search is scored inside Azure, the score on each hit is the ranker's, `explain=True` reports it as `semantic`, and the local cross-encoder does not run a second pass. Each search can choose for itself, whichever way the store was opened:

| `rerank=` | Keyword or hybrid search is reranked by |
|---|---|
| `"semantic"` | Azure's semantic ranker, in the service |
| `"cross-encoder"` | MiniLM, here, and not the service |
| `False` | nothing |
| left out | hybrid: the service's ranker on a store opened with `semantic=True`, MiniLM on one opened without it; keyword: nothing |

An index keeps its semantic configuration whichever way a handle opens it, and one made in the portal is used as it is; `semantic=True` adds one only to an index that has none. Asking for the ranker on an index with no configuration, on a dense search, or on a collection that is not on Azure AI Search is an error that says why.

Name the metadata paths a filter or a policy will run on, and they become real filterable fields on every collection index, beside the JSON blob that still carries everything:

```python
azure = VectrixDB.with_azure_search(
    endpoint="https://my-service.search.windows.net",
    key=os.environ["AZURE_SEARCH_KEY"],
    filter_fields={"client_id": "string", "roles": "strings", "classification": "number"},
)
```

Kinds are `"string"`, `"strings"` (a list), `"number"` and `"boolean"`; dotted paths work and land as `f_entitlements__allowed_roles`. A filter over promoted fields is compiled to OData and runs inside the service, and an entitlement policy whose rules all name promoted fields runs there too: `pushdown_mode` is `ENGINE`, `require_pushdown=True` opens, and the service serves every policied search. On the service an absent field and a null one are the same thing, so a null promoted field fails every rule, walls included; the local engine lets a present null pass a wall, and the closed reading is the one kept where the two cannot be told apart. A filter or policy naming a field that is not promoted is applied on the client, as before.

What you do not get: late interaction and the knowledge graph, which stay local. Writes are searchable once the service has indexed them, usually within a second or two, so a read straight after a write can miss.

Set `VECTRIXDB_LIVE_BACKENDS=azure_search` with `VECTRIXDB_AZURE_SEARCH_ENDPOINT` and `VECTRIXDB_AZURE_SEARCH_KEY` to run the storage contract suite against a real service; without them the suite runs against an in-memory stand-in for the SDK, which is how the backend is tested in CI.

### Whose vectors: yours, Azure's, or both

Azure AI Search owns no embedding model. What people mean by "Azure's embeddings" is an Azure OpenAI deployment, which the service can call through a vectorizer. `embeddings=` says whose dense vectors the index holds:

| `embeddings=` | Index fields | Who embeds a chunk | Who embeds the question |
|---|---|---|---|
| `"vectrixdb"`, the default | `dense_embedding` | the collection's model, here | the collection's model, here |
| `"azure"` | `dense_embedding` | the deployment, called from here | the service, through its vectorizer |
| `"both"` | `dense_embedding` and `dense_azure` | both | both, in one request |

```python
store = VectrixDB.with_azure_search(
    "https://your-service.search.windows.net",
    key=SEARCH_KEY,
    semantic=True,
    embeddings="both",
    azure_embedding={"endpoint": "https://your-aoai.openai.azure.com", "deployment": "text-embedding-3-large", "dimensions": 3072},
    vector_weights={"vectrixdb": 1.0, "azure": 1.0},
)
docs = Vectrix("docs", storage_backend=store, mode="hybrid")
```

With `"both"` the service runs BM25 and both vector searches in one request, fuses them, applies one filter and one ranker, and writes one audit record. That is why it is one index with two fields and not two indexes kept in step. `dimensions` has to be what the deployment produces; a vector of another width is refused at the first write. `api_key` goes in `azure_embedding` when the deployment is not keyless. The second vector is embedded through the collection's embedding cache, so re-indexing unchanged text does not pay for it twice. The deployment is called through the `openai` package; without it, a store that names a deployment and no `embed_fn` is refused when it opens, not at its first write, which would come after every document had been read and cut.

An index built with `"both"` answers all three ways, which is what makes a comparison fair: the same chunks, the same filter, the same ranker, and only the vectors differ.

```python
# docs is the collection above, on VectrixDB.with_azure_search(embeddings="both")
docs.search(question, vectors="vectrixdb")
docs.search(question, vectors="azure")
hits = docs.search(question, vectors="both", explain=True)   # the default on this index
hits.top.explain["vector_ranks"]                             # {"vectrixdb": 3, "azure": 1}
```

The service fuses and does not say who ranked what, so `explain=True` asks each vector on its own, one extra request each; nothing else pays for that. Asking for a vector the index does not hold is an error that says what it holds. The result cache is off on an index with two vectors, because its key is the query, the filter and the limit, and not which vector answered.

What it costs: the second vector is stored for every chunk, and a service tier caps vector storage. At 3,072 dimensions a vector is about 12 KiB before the service's own overhead, so a million chunks is on the order of 12 GiB for that field alone. Check the tier before choosing `"both"` for a large corpus, and consider the deployment's shorter `dimensions` option.

## OpenSearch

```python
store = VectrixDB.with_opensearch(
    "https://your-collection.us-east-1.aoss.amazonaws.com",
    region="us-east-1",
    filter_fields={"client_id": "string", "classification": "number"},
    knn_engine="faiss",
)
```

One index per collection, named `<prefix>_<collection>`, with a k-NN vector field, a BM25 text field, and your metadata in an object that OpenSearch stores and does not index. That last part is why nothing in the metadata could be filtered on: `filter_fields` maps the paths you name as real fields, `f_client_id` and so on, and a filter or an entitlement policy over them then runs inside OpenSearch. Kinds are `"string"`, `"strings"`, `"number"` and `"boolean"`, a dotted path reaches into nested metadata, and it is decided when the index is created.

With every field a policy names promoted, `pushdown_mode` is `ENGINE` and `require_pushdown=True` opens, exactly as on Azure AI Search: the scope rules travel with the query, the redaction rules are decided here so their count stays exact, and a document outside the scope never leaves the service. A null field is not indexed, so to OpenSearch null and absent are the same thing; every clause requires the field to exist, and a null fails the rule, walls included. A value of the wrong type, `"high"` in a number field, is stored as null for the same reason: a filter field that lies is worse than one that is missing.

`knn_engine` is the engine new indexes are built with. `"nmslib"`, the default and what every index so far has, cannot filter while it searches: the filter runs over the neighbours it found, so more are asked for, and a selective filter can still come back short. `"lucene"` and `"faiss"` filter during the search and return `k` of what passes. Either way nothing outside the filter leaves the service; the difference is recall, not enforcement.

### Whose vectors: yours, Bedrock's, or both

```python
import boto3
from vectrixdb.models.bedrock import BedrockEmbedder, BedrockReranker

titan = BedrockEmbedder(boto3.client("bedrock-runtime"), dimensions=1024)
store = VectrixDB.with_opensearch("https://your-collection.us-east-1.aoss.amazonaws.com", embeddings="both", embed_fn=titan)
docs = Vectrix("docs", storage_backend=store, mode="hybrid",
               reranker=BedrockReranker(boto3.client("bedrock-agent-runtime"), region="us-east-1"))
```

`embeddings=` means what it means on Azure. `"vectrixdb"` is the collection's own model. `"bedrock"` puts a Bedrock model in its place, one field. `"both"` keeps the two side by side in one index, `dense_embedding` and `dense_bedrock`, fused by rank, and `search(vectors="vectrixdb" | "bedrock" | "both")` picks between them with `explain=True` reporting `vector_ranks`. The one difference from Azure is who embeds the question: OpenSearch has nothing here that does it, so Bedrock is called from your process at ingest and at query. Titan takes one text a call, so a batch is a few calls at a time; the width you ask for, 1024, 512 or 256, has to be the width the index field was made for.

`reranker=` on `Vectrix` takes anything with `rerank(query, texts, top=None)` returning `(index, score)` pairs, and it takes the place of the bundled cross-encoder in every mode that reranks. `BedrockReranker` is one, around a `bedrock-agent-runtime` client; a long candidate list goes up in slices of a hundred. A reranker that fails leaves the fused order in place and says so once, as the bundled one does.

## Two homes

A collection kept on a store is written twice: every add, delete and metadata change reaches the service and the index beside your process, under the same ids. So it has two homes, and a search can say which to ask.

```python
from vectrixdb import Vectrix, VectrixDB

db = Vectrix("memos", storage_backend=VectrixDB.with_azure_search("https://your-service.search.windows.net", key="YOUR_KEY"), mode="hybrid")
db.search("covenant thresholds", homes="store")   # the service, and an error when it does not answer
db.search("covenant thresholds", homes="local")   # the index here; nothing leaves the machine
found = db.search("covenant thresholds", homes="both")
found.degraded                                       # None, or why the local index answered alone
```

`"both"` asks each and fuses the two lists by rank; `explain=True` adds `home_ranks`, where each home placed a result. When the service does not answer, a timeout, a refused connection, a throttle, the local list comes back alone and `degraded` says so, which is the case this exists for: a collection that must answer with the service unreachable, or from inside a network the service is outside. A mistake in the call, a mode the handle was not opened for, is raised and never taken for the service being away. Left out, nothing changes. Dense, keyword and hybrid searches; not under an entitlement policy, where the store stays the one engine a decision record names. Once the host scales out, each process has its own local index, filled only by its own writes, so give writers a shared volume or keep one writer.

## The collection pages, once the host scales out

The dashboard's collection pages count what the table beside the process holds: the chunks, the builds, growth by day, extraction quality, the points, a chunk's provenance and a document's chunks. On one machine that is everything. Once the host scales out it is only what that process wrote, and the instance drawing the page is rarely the one that ingested, so the pages come up empty. A chunk store is the copy every process writes and the pages read instead. Nothing searches it.

```python
from vectrixdb import Vectrix, VectrixDB

db = Vectrix(
    "memos",
    storage_backend=VectrixDB.with_azure_search("https://your-service.search.windows.net"),
    chunk_store="cosmos://your-account.documents.azure.com/vectrixdb/chunks",
)
```

Give every writer and the server the same address. On the server it is `VECTRIXDB_CHUNK_STORE`, with `VECTRIXDB_CHUNK_STORE_KEY` for the account's key. Left without a key the managed identity is used, and it needs a Cosmos DB data role on the account, which Cosmos keeps apart from Azure's own roles: the account's Owner reads none of its data.

Where it lives is your choice. Cosmos DB for NoSQL is the one built in: one container for every collection, partitioned by `/collection`, made for you when the credential may make one. Anything else is a class. An object whose `collection(name)` returns something with the methods of `vectrixdb.chunk_store.CollectionChunks`, passed as `chunk_store=` to `Vectrix`, `VectrixDB` or `create_app`, is a store. It answers a count, a page of ids, one chunk and one document's chunks, so a database suits it, DynamoDB or a PostgreSQL table. An object store, S3 or Blob, would have to read every file on every page.

What it costs: one write a chunk, with its text and metadata and no vector. Python's Cosmos SDK does not run GROUP BY, so the builds, the days and the quality bins are added up from a small projection of every chunk, read once a page, and those pages cost more as the collection grows. A store that refuses a write fails that write rather than letting the pages fall quietly behind, and the collection and the index already have it, so writing it again is the fix.

The kept Markdown works the same way. `VECTRIXDB_KEEP_SOURCE` takes a folder, `s3://bucket/prefix` or a Blob address as well as `1`, with one folder a collection inside it, and the Documents page then opens what the ingesting process kept.

## Which one

| Backend | Use when |
|---|---|
| `MEMORY` | Tests, caches, anything disposable |
| `SQLITE` | One machine. The default, and hard to beat there |
| `POSTGRESQL` | You already run Postgres and want one system to operate |
| `AURORA_POSTGRESQL` | Postgres on AWS with managed failover |
| `OPENSEARCH` | You need OpenSearch's own querying alongside vectors |
| `COSMOSDB` | Azure, global distribution |
| `LAKEBASE` / `DELTA_LAKE` | Databricks, vectors beside your lakehouse tables |

## Threading

The SQLite backend gives each thread its own connection, which is what WAL mode
is built for: concurrent readers alongside a writer, with real isolation between
connections. You can share one `SQLiteStorage` across threads safely.

Do not share a *connection* across processes. Open the storage separately in each.

## Failures raise

Since 2.2.0, a read that cannot reach its backend raises `StorageOperationError`
rather than returning an empty result:

```python
from vectrixdb import StorageOperationError

try:
    rows = storage.list_documents()
except StorageOperationError as exc:
    log.error("%s", exc)  # names the backend and operation
    raise
```

An empty list now genuinely means the collection is empty.

## How far each backend is tested

Every backend with an implementation of its own passes the same parametrised
contract suite, `tests/unit/test_storage_contract.py`, on every push. The six
that speak to a cloud service do it against an in-memory stand-in for their
driver, so no account is needed to run the suite:

| Backend | On every push | Against the real service |
| --- | --- | --- |
| Memory, SQLite | yes | not applicable |
| Cosmos DB, Delta Lake, Azure AI Search, OpenSearch | yes, against a fake driver | by hand, gated; nightly for OpenSearch |
| Lakebase, Aurora PostgreSQL | yes, against a fake driver | no path yet, see below |
| `postgresql` | not covered | not covered |

`postgresql` is a deprecated alias that warns and resolves to
`aurora_postgresql`, so the row above it is what actually runs. It has no
contract entry of its own and is removed in 2.3.

Lakebase and Aurora PostgreSQL have no live entry in the suite at all, so
there is no way to run them against a real service even with credentials to
hand. That is a gap rather than a decision.

A fake driver proves the backend's own logic: the statements it builds, how it
reads rows back, what it returns for a missing id. It cannot prove the service
accepts those statements. For that, set `VECTRIXDB_LIVE_BACKENDS` to the
backend name along with its connection variables and run the same suite. That
works for Cosmos DB, Delta Lake, Azure AI Search and OpenSearch; the nightly
workflow does it for OpenSearch when the repository has an endpoint secret.

This is worth knowing because the fakes were written late. The four that had
never been run under any test until 2.2 each failed the contract on the first
attempt, and the nine bugs are listed in the changelog. Treat a backend as
proven to the level its row above claims, and no further.
