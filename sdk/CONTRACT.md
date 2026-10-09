# One surface, four languages

Every VectrixDB client, whatever the language, offers the same calls with the
same names, spelled the language's way (`add_document`, `addDocument`,
`AddDocument`). All four are built from `docs/reference/openapi.json`, which
`scripts/make_reference.py` writes from the code and a test keeps current.

## Connecting

- `connect(url, key)` / `new VectrixClient({ url, key })` / `vectrixdb.New(url, key)` /
  `Client::new(url, key)`. The key goes in the `api-key` header. A company
  sign-in token goes in `Authorization: Bearer <token>` instead: `token=`
  in place of `key=`.
- Every request carries `user-agent: vectrixdb-<language>/<sdk version>`.
- Retries: a 429 or a 503 is retried up to three times, waiting the server's
  `Retry-After` seconds when it sends one, else 1 s, 2 s, 4 s. Nothing else is
  retried. Connection failures raise at once with the address in the message.
- Timeout: 30 s per request by default, settable.

## The calls

| Call | Route | Returns |
|---|---|---|
| `health()` | `GET /health` | `true` when the process answers |
| `ready()` | `GET /ready`, falling back to `/health` when the server has no `/ready` (404, servers before 2.2); a 503 means not ready yet | `true` when models are loaded |
| `whoami()` | `GET /auth/me` | the `data` object as the server sends it |
| `collections()` | `GET /api/v1/collections` | list of `Collection` |
| `describe(name)` | `GET /api/v1/collections/{name}` | one `Collection` |
| `create_collection(name, dimension=384, text_index=true, metric="cosine", description=null)` | `POST /api/v2/collections` with `enable_text_index` | the new `Collection` |
| `delete_collection(name)` | `DELETE /api/v1/collections/{name}` | nothing |
| `add_document(collection, bytes, filename, doc_id=null, metadata=null, chunk=null, chunk_size=null, overlap=null)` | `POST /api/v1/collections/{name}/documents` with `content-type: application/octet-stream`, `x-filename`, query `doc_id`, `metadata` (JSON string), `chunk`, `chunk_size`, `overlap` | `Added` |
| `add_texts(collection, [{id, text, metadata}])` | `POST /api/v1/collections/{name}/text-upsert` with `{"points": [{"id", "text", "payload"}]}`: the SDK's `metadata` is sent as `payload` | the count added |
| `search(collection, query, limit=10, filter=null, rerank=false, mode="meaning")`; `filter` is the simple form, `{"team": "payroll", "price": {"$lt": 100}}` | `mode="meaning"`: `POST .../text-search`; `mode="hybrid"`: `POST .../text-hybrid-search`; body `{query_text, limit, filter, rerank}` | list of `Result` |
| `documents(collection)` | `GET /api/v1/collections/{name}/documents` | list of `Document` |
| `open_document(collection, doc_id)` | `GET /api/v1/collections/{name}/documents/{doc_id}` | the Markdown text |
| `delete_document(collection, doc_id)` | `DELETE /api/v1/collections/{name}/documents/{doc_id}` | the number of chunks removed |
| `sources(collection)` | `GET /api/v1/collections/{name}/sources` | list of `Source` |
| `add_source(collection, address, kind=null, every=null)` | `POST /api/v1/collections/{name}/sources` with `{address, kind, every}` | `Source` |
| `refresh_sources(collection)` | `POST /api/v1/collections/{name}/sources/refresh` with `{}` | `Refreshed` |
| `delete_source(collection, source_id, delete_documents=false)` | `DELETE /api/v1/collections/{name}/sources/{source_id}` | nothing |

`doc_id` and `source_id` are URL-encoded in the path, `/` included.

## The shapes

The server answers in two envelopes. Most routes send
`{"ok": true, "message": ..., "data": {...}}` and the client returns `data`.
Three list routes send the list at the top: `{"collections": [...], "total": n}`,
`{"documents": [...]}`, `{"sources": [...]}`. Document upload (`doc_id`, `chunks`, ...) and delete (`chunks_removed`), and
sources refresh, send a flat object with `ok` and their fields. Adding a
source sends `{"ok": true, "source": {...}}`; read `source`, else `data`.
Text upsert's count is `data.added`.

- `Collection`: `name`, `dimension`, `metric`, `count`, `size_bytes`,
  `description`, `has_text_index`, `tags`, `created_at`, `updated_at`,
  `indexed_fields`. Keep the rest of the object reachable (`raw`/`extra`).
- `Result`: `id`, `score`, `text`, `metadata` (object), and `citation` read from
  `metadata["_vx_citation"]` when present, else `metadata["source"]`, else `id`.
- `Document`: `doc_id`, `filename`, `kind`, `source`, `version`, `extracted_at`,
  `chunking` (object).
- `Added`: `doc_id`, `chunks`, `replaced`, `quality`, `low_quality`,
  `citations` (list), `kept`.
- `Source`: `id`, `address`, `kind`, `every`, plus the rest.
- `Refreshed`: `added`, `updated`, `unchanged`, `removed`, `failed` (list).

## Errors

Every refusal is `{"ok": false, "message": "<one sentence>", "data": null,
"detail": ...}`. The client raises one error type with `status`, `message`
and `detail`, and these named kinds of it:

| Status | Name |
|---|---|
| 401 | `AuthError` (no key, or a wrong one) |
| 403 | `ForbiddenError` (the key's role or scope says no) |
| 404 | `NotFoundError` |
| 409 | `ConflictError` |
| 413 | `TooLargeError` |
| 422 | `InvalidError` (`detail` is the field list) |
| 429, 503 after the retries | `BusyError` |
| anything else 4xx/5xx | the base `VectrixError` |

A `message` that the server did not send (an HTML error page from a proxy)
is replaced with `"<status> from <url>"`.

## The conformance walk

Every SDK has one test that runs this against a real server, in this order,
and the same test is what CI runs. `sdk/conformance/serve.py` starts such a
server and prints `{"url": ..., "key": ...}`; the test reads
`VECTRIXDB_URL` and `VECTRIXDB_KEY` from the environment, or starts
`serve.py` itself when they are unset and Python is on the path.

1. `health()` is true; `ready()` is true.
2. `create_collection("walk-<random>")` returns a collection with
   `has_text_index` true and `count` 0.
3. `collections()` contains it; `describe()` returns it.
4. `add_document` with the bytes of `sdk/conformance/handbook.md` as
   `handbook.md`, `doc_id="handbook.md"`: `chunks` ≥ 2, `kept` true,
   `citations` contains `"handbook.md#Refunds"`.
5. `add_texts` with two points, ids `t1` and `t2`, one with
   `metadata {"team": "payroll"}`: returns 2.
6. `search("refunds", limit=3)` (meaning): the first result's `citation` is
   `"handbook.md#Refunds"`, its `text` contains `"ten working days"`.
7. `search("salaries", mode="hybrid", rerank=true, limit=2)`: the first
   result's `id` is `"t2"` (the salaries point); `filter={"team": "payroll"}`
   on a meaning search returns only `t2`.
8. `documents()` lists `handbook.md`; `open_document("handbook.md")`
   starts with `"# Refunds"`.
9. `sources()` is empty; `refresh_sources()` returns `added` 0.
10. `delete_document("handbook.md")` returns 2 or more chunks removed;
    `documents()` is then empty.
11. `describe("no-such-collection")` raises `NotFoundError` with status 404
    and a message containing `"not found"`.
12. `create_collection("walk-bad", dimension=0)` raises `InvalidError` (422) whose `detail` is a list naming `dimension`.
13. A client made with key `"wrong"`: `collections()` raises `AuthError` (401).
14. `delete_collection()`; `collections()` no longer contains it.
