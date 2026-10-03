# Run the REST API

The HTTP server is an optional extra, so that the core install does not drag in a
web stack:

```bash
pip install vectrixdb[api]
```

Without it, importing the API tells you what to do:

```python
>>> import vectrixdb.api
DependencyError: 'fastapi' is required for this feature.
Install it with: pip install vectrixdb[api]
```

## Start it

```bash
vectrixdb serve --host 127.0.0.1 --port 8000
```

Or from Python:

```python
from vectrixdb.api.server import run_server

run_server(host="127.0.0.1", port=8000)
```

Interactive API docs are at `/docs`. The dashboard is at **`/dashboard/`**; `/`
returns a small JSON index, not the UI.

With the settings kept in a file, `vectrixdb serve --env-file vectrixdb.env`
reads it first, and `vectrixdb check --env-file vectrixdb.env` says beforehand
what a start would refuse. See [Deploy the server](deploy.md).

`create_app()` builds an application you can mount or configure yourself, and
it can be called as many times as you like:

```python
from vectrixdb.api.server import create_app

app = create_app(db_path="./vectrixdb_data", enable_dashboard=False)
```

Or point uvicorn at the import string for the shared singleton:

```python
import uvicorn

uvicorn.run("vectrixdb.api.server:app", host="127.0.0.1", port=8000)
```

## Query it

Add some text and let the server embed it:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/collections/my_docs/text-upsert \
  -H 'Content-Type: application/json' \
  -d '{"points": [{"id": "d1", "text": "python is a programming language"}]}'
```

Then search by text:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/collections/my_docs/text-search \
  -H 'Content-Type: application/json' \
  -d '{"query_text": "programming", "limit": 5}'
```

`/text-search` embeds the query for you, and `"rerank": true` re-ranks the
top candidates with the bundled cross-encoder. `/search` is the lower-level
route and takes a `vector`, not a string. Every route lives under `/api/v1`; the same
paths under `/api` are kept for the dashboard and are not documented. Every
documented route, with who may call it, is in the
[REST API reference](../reference/rest-api.md).

## Read what a collection is

The dashboard's collection pages are built on read routes that describe a
collection rather than search it:

| Route | What it answers |
|---|---|
| `GET /api/v1/collections/{name}/health` | count, deleted-vector ratio and whether a rebuild is advised, language, model, current build |
| `GET /api/v1/collections/{name}/policy` | the entitlement policy as data, with its fingerprint |
| `GET /api/v1/collections/{name}/builds` | every index build that still has chunks, read off the chunks: how many, when it first wrote, their mean quality and how many are under the line |
| `GET /api/v1/collections/{name}/growth?days=30` | chunks written on each of the last days, today last, counted from the chunks that are here |
| `GET /api/v1/growth?days=14` | the same summed across every collection, with `low`, the chunks of each day under the quality line |
| `GET /api/v1/collections/{name}/quality` | the extraction-quality histogram, the count below the line, the worst chunks; `offset` pages through them |
| `GET /api/v1/access/daily?days=14` | searches a day and how long they took, for everyone; sign-ins, refusals and the searches a policy denied for whoever may read the access log |
| `GET /api/v1/collections/{name}/provenance/{id}` | where one chunk came from |
| `POST /api/v1/collections/{name}/rebuild` | rebuild the ANN index, dropping tombstones |
| `GET /api/v1/models` | every model the library knows and whether it is on this machine |
| `GET /api/v1/audit` | the audit trail, under the conditions below |

The evaluation runs the library saved are read with `GET /api/v1/evaluations`,
with `offset` for the next page, and `GET /api/v1/evaluations/{run}`, `latest`
for the newest. `GET /api/v1/evaluations/{run}/golden` is the golden file a run
read, for an admin signed in as a person and never a key; see
[Evaluate every setup](evaluate-setups.md#read-the-results).

A collection that carries an entitlement policy answers `health` and `policy`
with configuration only and refuses the rest with the same 403 every content
route uses: this API resolves no principals, so it cannot say who is entitled
to a count.

Every write through the API mints an index build id and stamps it on the
chunks it lands, the same `_vx_build` the library writes, so `builds` and
`provenance` cover API-written chunks too.

`/api/v1/audit` is served only when `VECTRIXDB_API_KEY` is set, the request
carries it, and `VECTRIXDB_AUDIT_JSONL` names a JSONL sink. Records go out
without the principal snapshot, the result ids and the undisclosable count.
The audit log is more sensitive than the index it audits, which is why an open
server never serves it.

Writing an app against this server, in any language, is
[Build an app on it](build-an-app.md).

## Before exposing it

!!! danger "Authentication is off until you turn it on"
    Nothing is required by default. Set `VECTRIXDB_API_KEY` and writes then need
    an `api-key` header; without one they answer 401. Reads stay open either
    way, which is deliberate and is the Qdrant convention, so a key alone does
    not make a collection private.

```bash
export VECTRIXDB_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
vectrixdb serve --host 127.0.0.1 --port 8000
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/collections \
  -H "api-key: $VECTRIXDB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "my_docs", "dimension": 384}'
```

`VECTRIXDB_READ_ONLY_API_KEY` sets a second key that may read but not write,
and `GET /` reports `auth_enabled` so you can tell which mode a server is in.
`VECTRIXDB_API_KEY_FILE` and `VECTRIXDB_READ_ONLY_API_KEY_FILE` read the keys
from files, and `VECTRIXDB_API_KEY_SHA256` and
`VECTRIXDB_READ_ONLY_API_KEY_SHA256` take a key's SHA-256 in place of the key,
so the server's environment never holds one that works.
With a key and no sign-in, a read still needs no key: anyone who reaches the
port lists the collections and reads the points, text and vectors included.
`VECTRIXDB_OPEN_READS=0` closes that, so every read, and the live feed at
`/ws`, asks for the full or the read-only key; only `/`, `/health`,
`/auth/status`, `/openapi.json` and the dashboard's own files stay public.
With sign-in on, an admin can also make named keys for scripts, each with its
own role, shown once and revoked on its own: see
[Sign people in](sign-in.md#api-keys).

Behind a gateway, the same routes are published by the team that owns it,
from the OpenAPI document this server serves at `/openapi.json`. See
[Put it behind a gateway](behind-a-gateway.md).

None of that is a substitute for a boundary. The server has no rate limiting
beyond the guests', and no tenancy, so keep it on a private network or behind
a proxy that terminates TLS. API keys are for machines: to let people in as
themselves, with roles and a record of who read what, see
[Sign people in](sign-in.md). The server listens on `127.0.0.1` by default and
refuses any other address with no key and no sign-in. Bind to `127.0.0.1` rather than `0.0.0.0` unless
something in front of it is doing access control.

## Documents

Send a file and the server reads it, cuts it, embeds it and writes it, the way `add_document()` does: the same chunk ids, citations, lineage stamps and figure links, because both call the same function.

```bash
curl -X POST "http://localhost:7337/api/v1/collections/docs/documents?chunk=markdown&chunk_size=1000" -H "X-Filename: msa.pdf" -H "api-key: $KEY" --data-binary @msa.pdf
```

The file is the request body and its name travels in `X-Filename`, which needs no form parser on either side. A multipart form with a `file` field works too when `python-multipart` is installed on the server. The query parameters are `doc_id` (the file's name when left out), `chunk` (`markdown`, `recursive` or `sentence`), `chunk_size`, `overlap`, `embed_heading`, `on_low_quality` and `metadata`, a JSON object put on every chunk. Sending the same id again replaces the document.

```json
{"ok": true, "doc_id": "msa.pdf", "replaced": 0, "chunks": 14, "quality": 0.93, "low_quality": false,
 "extractor": "built-in", "pages": 6, "figures": 1,
 "citations": ["msa.pdf#page=1", "msa.pdf#page=2"], "build": "build_5c1e0a97b2d44f10", "kept": true}
```

| Route | What it does |
|---|---|
| `POST /api/v1/collections/{name}/documents` | read, cut, embed and write one file |
| `GET /api/v1/collections/{name}/documents` | the documents the server has kept |
| `GET /api/v1/collections/{name}/documents/{doc_id}` | the Markdown a document was indexed from; `?raw=true` keeps the front matter |
| `DELETE /api/v1/collections/{name}/documents/{doc_id}` | its chunks, and its kept copy moved aside |
| `GET /api/v1/extractors` | which file types this server reads, and who reads them |

Settings, all environment variables:

| Variable | Meaning |
|---|---|
| `VECTRIXDB_KEEP_SOURCE=1` | keep the Markdown beside each collection; the two GET routes and the dashboard's Open document need it |
| `VECTRIXDB_EXTRACTOR_URL` | a service that reads file types for this server, as [`HttpExtractor`](extract-keep-index.md) does |
| `VECTRIXDB_EXTRACTOR_ROUTES` | JSON, suffix to path. Left out, `.pdf .docx .doc` go to `/extract/...` and `.png .jpg .jpeg .wav .mp3 .m4a .mp4` to `/transcribe/...`. For VectrixDB's own extraction service, `extraction_routes()` is the whole table |
| `VECTRIXDB_EXTRACTOR_BODY` | `raw`, the default, or `multipart` |
| `VECTRIXDB_EXTRACTOR_KEY`, `VECTRIXDB_EXTRACTOR_KEY_HEADER` | a key for that service and the header it goes in, `x-api-key` by default |
| `VECTRIXDB_MAX_UPLOAD_BYTES` | the largest file read, 100 MiB by default; a request that says it is bigger is refused before its body is read |

A service that fails is a `502` naming the route and its status, and nothing is written. A file type nothing here can read is a `501` naming the extra to install. Like every data route, these refuse a collection that carries an entitlement policy, and the server never fetches an address a request names. What the route does not do is what needs a `Vectrix`: parent sections, deduplication and semantic chunking.

## Listings and excerpts without the text

For a page that lists chunks, or any caller that should not be sent content it
has not asked for:

| Request | What comes back |
| --- | --- |
| `GET /api/v1/collections/{name}/points?index=true` | `rows`: id, source, document, page and quality for each id. Never text. |
| `POST .../search?snippet=200` (also `text-search`, `text-hybrid-search`, `keyword-search`) | Each result's text and highlights cut to 200 characters, with `snipped: true` when something was cut. |
| `GET .../quality?text=false` | The lowest chunks with `reasons` and `below_line`, and no excerpt. |
| `GET .../provenance/{id}?text=false` | Where the chunk came from, and no excerpt. |

Without the parameters every reply is what it always was. The text of one chunk
is `GET /api/v1/collections/{name}/points/{id}`.
