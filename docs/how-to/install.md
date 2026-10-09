# Install what you need

`pip install vectrixdb` gives vector search and nothing else. Everything a
deployment adds, the server, sign-in, document readers, cloud stores, comes as
an extra, so a machine carries only what it uses.

## The core

```bash
pip install vectrixdb
```

Python 3.9 or later. It brings five packages:

| Package | For |
| --- | --- |
| `numpy` | Vectors |
| `usearch` | The vector index |
| `onnxruntime`, below 2 | Running the models on this machine |
| `rich`, `typer` | The `vectrixdb` command |

The wheel carries the English embedding model, the reranker, the sparse model
and the late-interaction model, so a collection can be made, filled and
searched in every mode with no network and no key. The multilingual models are
fetched once with `vectrixdb download-models`; see
[Run without a network](offline.md).

With the core alone you can open a `Vectrix`, add text and Markdown, HTML, CSV,
PowerPoint and plain text documents, search, and run `vectrixdb check` and
`vectrixdb doctor`.

Extras go in square brackets, several at once:

```bash
pip install "vectrixdb[api,signin,documents]"
```

## The server and its parts

| Extra | Adds | For |
| --- | --- | --- |
| `api` | `fastapi`, `uvicorn[standard]`, `pydantic` | The REST API and the dashboard: `vectrixdb serve`, and `vectrixdb extract-serve` |
| `signin` | `pyjwt[crypto]`, `qrcode` | Signing people in, with `VECTRIXDB_SIGNIN`: single sign-on, email, authenticator apps and passkeys |
| `mcp` | `mcp` 2 or later, `httpx` | The server's `/mcp` endpoint with `VECTRIXDB_MCP=1`, and `vectrixdb mcp` |
| `tracing` | `opentelemetry-api`, `opentelemetry-sdk`, the OTLP HTTP exporter | A span for every search, ingestion and evaluation run; see [Trace searches and ingestion](tracing.md) |
| `client` | `httpx` | `vectrixdb.connect` and `connect_async`, for calling a server; see [Call a server from your code](clients.md) |
| `jobs-azure` | `azure-storage-blob`, `azure-storage-queue`, `azure-identity` | The extraction service's long jobs on Azure, with `VECTRIXDB_EXTRACT_JOBS=azure` |

A server for a team is usually `api`, `signin`, `mcp` and `tracing`, plus the
readers and the store it needs. The container image installs
`api,signin,mcp,tracing,documents,feeds,azure,aws,postgres,jobs-azure`.

## Documents and recordings

The readers for Markdown, HTML, CSV, PowerPoint and text need nothing. The rest
are named for what the file becomes.

| Extra | Adds | Reads |
| --- | --- | --- |
| `documents` | `pypdf`, `pypdfium2`, `python-docx`, `openpyxl`, `olefile`, `xlrd` | PDFs with a text layer, Word, Excel, and Word and Excel 97-2003 |
| `ocr` | `rapidocr-onnxruntime`, `pypdfium2`, `pillow`, `pypdf` | Pictures and scanned PDF pages, on this machine |
| `asr` | `faster-whisper` | Recordings, on this machine. The speech model downloads the first time |
| `ffmpeg` | `imageio-ffmpeg` | ffmpeg alone, about 90 MB: the sound out of a video, and a long recording cut into pieces, for a host whose transcribing is done by a service |
| `video` | `ffmpeg` and `faster-whisper` | Videos, on this machine |
| `extract` | `documents`, `ocr`, `asr` and `video` | Every local reader |
| `youtube` | `yt-dlp` | YouTube addresses |
| `feeds` | `feedparser` | RSS 2.0, RSS 1.0 and Atom feeds a collection keeps up with. JSON Feed and pages need nothing |
| `ocr-azure` | `azure-ai-documentintelligence` | Pictures and scans through Azure Document Intelligence |
| `asr-azure` | nothing | Azure Speech is one HTTPS request, so it needs no package |
| `ocr-aws`, `asr-aws` | `boto3` | The same jobs through AWS |
| `masking` | `presidio-analyzer`, `spacy` | Masking names, addresses and national ids before a document is indexed, in this process. The language models are a separate step: `vectrixdb download-models --type masking` |

`vectrixdb check` says which kinds of file this machine reads and which extra
reads the rest; see [Check and doctor](troubleshoot.md).

## Storage

| Extra | Adds | For |
| --- | --- | --- |
| `azure` | `azure-cosmos`, `azure-search-documents`, `azure-identity`, `openai` | Cosmos DB and Azure AI Search, keyless sign-in to Azure, the second vector from Azure OpenAI, and `cosmos://` stores for sign-in, collection records and chunks |
| `aws` | `opensearch-py`, `boto3`, `requests-aws4auth`, `psycopg2-binary` | OpenSearch, Aurora PostgreSQL, `dynamodb://` stores, `s3://` addresses, Amazon Comprehend |
| `postgres` | `psycopg2-binary` | PostgreSQL, Lakebase, and `postgresql://` stores for sign-in, collection records and audit |
| `databricks` | `databricks-sql-connector` | Delta Lake |

See [Choose a storage backend](storage-backends.md) for each.

## Models

| Extra | Adds | For |
| --- | --- | --- |
| `hf` | `sentence-transformers` | Embedding models from Hugging Face by name, through sentence-transformers |
| `fastembed` | `fastembed` | Embedding models through FastEmbed |
| `embeddings` | `fastembed` and `sentence-transformers` | Both |
| `nlp` | `spacy` | Entity extraction for graph mode on ordinary prose. Without it a pattern reader finds capitalised phrases, acronyms, quoted terms and repeated phrases. The model is a separate step: `python -m spacy download en_core_web_sm` |
| `setup-models` | `torch`, `transformers`, `optimum[onnxruntime]` | Converting a model to ONNX, which `vectrixdb download-models` asks for when it is missing |

## Tools

| Extra | Adds | For |
| --- | --- | --- |
| `viz` | `umap-learn`, `scikit-learn` | Nothing in VectrixDB 2.2 imports them; install them for code of your own |
| `all` | `api`, `signin`, `mcp`, `documents`, `viz`, `hf`, `fastembed`, `aws`, `azure`, `databricks`, `postgres`, `tracing`, `feeds` | Everything at run time except the local readers in `extract`, whose speech models are large enough to ask for by name |

## Which command needs which

| You run | Install |
| --- | --- |
| `Vectrix(...)`, `add`, `search` | the core |
| `add_document` on a PDF, Word or Excel file | `documents` |
| `add_document` on a picture, a scan, a recording or a video | `ocr`, `asr` or `video`, or an extraction service |
| `vectrixdb serve` | `api`, and `signin` with sign-in on, `mcp` with `VECTRIXDB_MCP=1`, `tracing` with tracing on |
| `vectrixdb extract-serve` | `api` and `documents`, with the readers and services it uses |
| `vectrixdb mcp`, `vectrixdb-mcp` | `mcp` |
| `vectrixdb sources refresh` on an XML feed | `feeds` |
| `vectrixdb download-models --type masking` | `masking` |
| `vectrixdb.connect`, `connect_async` | `client` |
| `vectrixdb.load_youtube` | `youtube` |
| `vectrixdb check`, `vectrixdb doctor` | the core; each says which extra a setting needs |

A call that needs an extra that is not installed raises `DependencyError`,
which names the package and the extra: see
[Handle errors](handle-errors.md).
