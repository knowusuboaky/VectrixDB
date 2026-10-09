# Embedding models

Every search mode runs on models: a dense model turns text into a vector, BM25 weighs words, a reranker reads a question beside each result, a late-interaction model compares them token by token, and graph mode can pull relations out of text. The English set ships inside the wheel and runs on the machine, with no network and no key. Everything else is a download you ask for by name, or a model you bring.

For what each mode does with its models, see [Search modes](../explanation/search-modes.md). For air-gapped hosts, checksums and where a download comes from, see [Run without a network](offline.md).

## What ships and what is a download

Every model the library knows is in `vectrixdb.models.embedded.MODEL_CONFIG`, keyed by type. `WHEEL_MODEL_DIRS` names the four directories the wheel carries; everything else is a download.

| Role | Model | Type | Dimension | Languages | Where | In `Vectrix` |
| --- | --- | --- | ---: | --- | --- | --- |
| Dense, the default | bge-small-en-v1.5, INT8 | `bge_small_en` | 384 | English | in the wheel | `dense_model="bge-small"`, or nothing |
| Dense, the default before 2.2 | e5-small-v2, INT8 | `dense_en` | 384 | English | `--type dense_en` | `dense_model="e5-small"` |
| Dense, higher quality | bge-base-en-v1.5, INT8 | `bge_base_en` | 768 | English | `--type bge_base_en` | `dense_model="bge-base"` |
| Dense | e5-small-v2, FP32 | `e5_small` | 384 | English | not fetched by `download-models` | `dense_model="e5-small-fp32"` |
| Dense, multilingual | multilingual-e5-small, INT8 | `dense` | 384 | 100+ | `--type dense` | `dense_model="vectrixdb/multilingual"` |
| Sparse | BM25 vocabulary | `sparse` | | any | in the wheel | `sparse_model="bm25"`, the default in hybrid and up |
| Sparse, learned | SPLADE++ | `splade_pp_en` | 30,522 terms | English | not fetched by `download-models` | `sparse_model="splade"`, with a storage backend only |
| Reranker | ms-marco-MiniLM-L-12-v2, INT8 | `reranker_en` | | English | in the wheel | `reranker_model="L12"`, the default in hybrid and up |
| Reranker, smaller | ms-marco-MiniLM-L-6-v2 | `reranker_en_l6` | | English | not fetched by `download-models` | `reranker_model="L6"` |
| Reranker, higher quality | bge-reranker-base, INT8 | `bge_reranker_base` | | English | `--type bge_reranker_base` | `reranker_model="bge-reranker"` |
| Reranker, multilingual | mmarco-mMiniLMv2-L12-H384-v1, INT8 | `reranker` | | 15+ | `--type reranker` | `reranker_model="multilingual"` |
| Late interaction | answerai-colbert-small-v1, INT8 | `late_interaction_en` | 128 a token | English | in the wheel | `late_interaction_model="colbert"`, the default in ultimate and graph |
| Late interaction, higher quality | colbertv2.0, INT8 | `colbert_v2` | 128 a token | English | `--type colbert_v2` | `late_interaction_model="colbert-v2"` |
| Late interaction, multilingual | bge-m3, INT8 | `late_interaction` | 1,024 a token | 100+ | `--type late_interaction` | `vectrixdb.models.LateInteractionEmbedder()` |
| Graph relations | mrebel-base, INT8 | `rebel` | | 18 | `--type rebel` | graph mode's hybrid extractor, when it is installed |

"Where" is how the model gets onto the machine: in the wheel, or `vectrixdb download-models --type <type>`. The ones not fetched by `download-models` are unpacked into the models directory by hand, from the project's releases page.

`vectrixdb models-info` lists every type and whether it is installed here. The server answers the same at `GET /api/v1/models`, saying for each whether it came in the wheel or was downloaded, and nothing downloads because of either.

A few things the table does not say:

- **The default is bge-small.** A collection created since 2.2 records `vectrixdb/bge-small-en-v1.5`. On SciFact it scores nDCG@10 0.72 against 0.65 for e5-small-v2, which it replaced.
- **A collection written before 2.2** has no model recorded and was built with e5-small-v2, so it opens with e5-small-v2. With `VECTRIXDB_AUTO_DOWNLOAD=1` the first search fetches it once; without, it raises and names `vectrixdb download-models --type dense_en`. `Vectrix(name, dense_model="bge-small").reembed()` moves it to the current default instead.
- **Pooling.** bge-small takes its first token's vector as the text's; e5-small and the multilingual model take the mean of every token's. Late chunking needs the mean, so it works on e5-small and not on the default: see [Late chunking](chunking.md#late-chunking).
- **SPLADE is for a store.** On a local collection the sparse half of hybrid search is always the BM25 index, and naming a learned sparse model there warns that it is ignored.
- **`language=` does not switch these.** It is passed to the bundled embedders, but every model a `Vectrix` uses is named, by you or by its mode's default, and a name wins over a language. `Vectrix(name, language="multi")` alone stays on the English models. Name the multilingual ones.

## Choose them

```python
from vectrixdb import Vectrix

db = Vectrix("notes")                                     # bge-small, in the wheel
hybrid = Vectrix("policies", mode="hybrid")               # + BM25 and the L12 reranker, in the wheel
ultimate = Vectrix("research", mode="ultimate")           # + the ColBERT model, in the wheel
```

Multilingual collections, after `vectrixdb download-models` has fetched the two multilingual models, about 230 MB:

```python
vertraege = Vectrix(
    "vertraege",
    mode="hybrid",
    dense_model="vectrixdb/multilingual",
    reranker_model="multilingual",
    text_language="de",
)
```

`text_language` is for the BM25 half, which stems and drops stopwords in English only: see [Languages](../explanation/search-modes.md#languages).

`dense_model` also takes a list. The first is the collection's own, and each of the others gets an index beside it, fused at search time: see [More than one dense model](../explanation/search-modes.md#more-than-one-dense-model).

## Download what you name

`vectrixdb download-models` fetches models once, so later runs need no network:

```bash
vectrixdb download-models                      # the multilingual dense model and reranker
vectrixdb download-models --type dense_en      # one type, by its name in the table
vectrixdb download-models --type rebel --force # again, over what is there
```

```python
from vectrixdb.models import download_models, is_models_installed

if not is_models_installed("dense", exact=True):
    download_models("dense")
```

`download_models(model_type="all", force=False, progress=True)` takes the same types; `"all"` is the multilingual dense model, the BM25 vocabulary when it is missing, and the multilingual reranker. `is_models_installed(model_type, exact=False)` answers for a kind: `"reranker"` is true on a fresh install because the English reranker is here. `exact=True` asks about that type's own directory, the multilingual reranker itself, which is what a download asks.

A download comes from the project's GitHub release for that model, checked against the checksum manifest, and from Hugging Face, converted to ONNX on your machine, when that fails; the second needs `pip install "vectrixdb[setup-models]"`. See [Where a download comes from](offline.md#where-a-download-comes-from).

## The settings

| Setting | What it does |
| --- | --- |
| `VECTRIXDB_MODELS_DIR` | where models are read from and downloaded to; left out, the package's own `models/data` folder, which is where the wheel's models are. `get_models_dir()` answers it. Point it at a folder copied from a machine with a network, or at a shared volume |
| `VECTRIXDB_OFFLINE=1` | refuses every download, the explicit ones included; what would have downloaded raises `ModelDownloadError` |
| `VECTRIXDB_AUTO_DOWNLOAD=1` | allows downloads on first use: a model fetched because a search found it missing. Off by default, so a first use that needs a download raises and names the command to run |
| `VECTRIXDB_THREADS` | how many threads each ONNX session uses. Left out, the CPUs this process may use, the container's CPU quota when it has one, at most four. On a container held to one or two CPUs, more threads than cores makes every model call slower |

A folder set by `VECTRIXDB_MODELS_DIR` must hold every model the collection uses, the wheel's four included: copy the whole `models/data` folder across, then add downloads to it.

## Models from other libraries

Three extras bring other libraries' models in by name. They download from Hugging Face the first time, through their own library, and `VECTRIXDB_OFFLINE` does not stop them.

| Extra | Brings | Named like |
| --- | --- | --- |
| `hf` | sentence-transformers | `dense_model="BAAI/bge-large-en-v1.5"`, `"sentence-transformers/all-mpnet-base-v2"`, `"BAAI/bge-m3"`, or any Hugging Face model id |
| `fastembed` | Qdrant FastEmbed, ONNX | `dense_model="qdrant/bge-base-en-v1.5"`, `"qdrant/bge-small-en-v1.5"` |
| `embeddings` | both | |

```bash
pip install "vectrixdb[hf]"
```

The dimension of a name the library knows, `BAAI/bge-large-en-v1.5` at 1,024 say, is filled in for you. For any other, pass `dimension=`. A reranker or a ColBERT model named by its Hugging Face id, one with a `/` in it, is loaded the same way.

## Bring your own

`embed_fn=` is any callable from a list of strings to an array of vectors, one row a text. Pass `dimension=` with it, the width of those vectors, and name the model with `dense_model=`, so the name is recorded with the collection:

```python
import hashlib

import numpy as np

from vectrixdb import Vectrix


def hashed_words(texts):
    """A stand-in for a real model: each word hashed into one of 256 places."""
    out = np.zeros((len(texts), 256), dtype=np.float32)
    for row, text in enumerate(texts):
        for word in text.lower().split():
            out[row, int(hashlib.sha1(word.encode()).hexdigest(), 16) % 256] += 1.0
    return out


db = Vectrix("own", embed_fn=hashed_words, dimension=256, dense_model="hashed-words-v1")
db.add(["The invoice is due in thirty days.", "Interest accrues monthly on overdue balances."])
db.search("when is the invoice due", limit=1).top.text
db.embedding_model                  # 'hashed-words-v1'
```

Without `dimension`, the collection is 384 wide, and vectors of another width are refused at the first `add()`. Without a name, every function is recorded as `custom`, and a collection cannot tell one of yours from another.

A hosted model is the same, through a client you make:

- **Any OpenAI-compatible endpoint**, OpenAI, Azure OpenAI, Ollama, vLLM, Text Embeddings Inference: `dense_model="openai:text-embedding-3-small"`, or `embed_fn=OpenAIEmbedder(...)` from `vectrixdb.models`. See [LangChain, LlamaIndex, plugins, CLI](integrations.md).
- **Amazon Bedrock**: `BedrockEmbedder` from `vectrixdb.models.bedrock`, around a `bedrock-runtime` client you make. See [Choose a storage backend](storage-backends.md).
- **An installed plugin**: `dense_model="plugin:<name>"`.
- **Anything else**: wrap its client in a function with the shape above.

The embedding cache, which keeps vectors by content so re-adding unchanged text never embeds it again, is off for custom functions: VectrixDB cannot tell when yours changes.

## The question and the documents, one model

A question is embedded with the collection's model, and its vector is compared with vectors made by the model the documents were embedded with. Two models put the same text in different places, so a question embedded with another model scores every document as noise, with no error.

So a collection records the model it was built with, `db.embedding_model`, and opening it with a different one raises `ModelMismatchWarning`:

```text
ModelMismatchWarning: Collection 'own' was built with embedding model 'hashed-words-v1' but is being
opened with 'hashed-words-v2'. Search scores will be wrong until you call reembed(), or open it with
the original model.
```

Open it with the model it names. To move to another model of the same width, `reembed()` embeds every stored text again with the current model and records it. A model of another width needs a new collection, filled with `export()` and `add()`: see [Back up, move and restore](export-import.md).

A collection on Azure AI Search with `embeddings="azure"`, or on OpenSearch with Bedrock embeddings, takes the store's embedder as its own when no model is named, so questions and documents go through the same deployment: see [Choose a storage backend](storage-backends.md).
