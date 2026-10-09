# vectrixdb

The TypeScript client for VectrixDB. It offers the same calls as the other
language clients (see `sdk/CONTRACT.md`), typed from the server's OpenAPI
description, with no runtime dependencies: it uses the `fetch` of Node 18+
and of browsers.

## Install

```
npm install vectrixdb
```

Both ESM (`import`) and CommonJS (`require`) are published.

## Quickstart

```ts
import { VectrixClient } from "vectrixdb";
import { readFileSync } from "node:fs";

const client = new VectrixClient({ url: "http://localhost:8000", key: process.env.VECTRIXDB_KEY });

await client.createCollection("handbook");
const added = await client.addDocument("handbook", readFileSync("handbook.md"), "handbook.md");
console.log(`${added.chunks} chunks, cited as ${added.citations.join(", ")}`);

const results = await client.search("handbook", "how long do refunds take?", { limit: 3 });
for (const r of results) console.log(r.score.toFixed(2), r.citation, "-", r.text.slice(0, 80));
```

`citation` is read from the chunk's metadata (`_vx_citation`, else `source`,
else the id), so it is the string to show next to an answer.

## The calls

| Call | What it returns |
|---|---|
| `health()`, `ready()` | `true` when the process answers / the models are loaded |
| `whoami()` | the `data` object of `GET /auth/me` |
| `collections()`, `describe(name)` | `Collection[]`, `Collection` |
| `createCollection(name, { dimension, textIndex, metric, description })` | the new `Collection` |
| `deleteCollection(name)` | nothing |
| `addDocument(collection, bytes, filename, { docId, metadata, chunk, chunkSize, overlap })` | `Added` |
| `addTexts(collection, [{ id, text, metadata }])` | the count added |
| `search(collection, query, { limit, filter, rerank, mode })` | `Result[]`; `mode` is `"meaning"` (default) or `"hybrid"` |
| `documents(collection)` | `Document[]` |
| `openDocument(collection, docId)` | the Markdown text |
| `deleteDocument(collection, docId)` | the number of chunks removed |
| `sources(collection)` | `Source[]` |
| `addSource(collection, address, { kind, every })` | `Source` |
| `refreshSources(collection)` | `Refreshed` |
| `deleteSource(collection, sourceId, deleteDocuments)` | nothing |

`addDocument` takes a `Uint8Array`, `ArrayBuffer`, `Blob` or `string`. Every
typed result keeps the whole server object in `raw`.

Options: `timeoutMs` (default 30 000 per request) and `fetch` (your own
implementation, for proxies or tests). A 429 or 503 is retried up to three
times, waiting the server's `Retry-After` or 1 s, 2 s, 4 s.

## In a browser

Pass `token` (a company sign-in token, sent as `Authorization: Bearer`),
never `key`. An API key in page code is visible to everyone who loads the
page. The client sets no `user-agent` header in browsers, since they do not
allow it.

```ts
const client = new VectrixClient({ url: "https://vectrix.example.com", token });
```

Generated types for every route are exported as `paths` and `components`
for callers that reach routes the client does not wrap.

## Errors

Every refusal is thrown as a `VectrixError` with `status`, `message` and
`detail`, or one of its named kinds:

| Status | Class |
|---|---|
| 401 | `AuthError` (no key, or a wrong one) |
| 403 | `ForbiddenError` (the key's role or scope says no) |
| 404 | `NotFoundError` |
| 409 | `ConflictError` |
| 413 | `TooLargeError` |
| 422 | `InvalidError` (`detail` is the field list) |
| 429, 503 after the retries | `BusyError` |
| anything else 4xx/5xx | `VectrixError` |
| could not connect, or timed out | `VectrixError` with `status` 0 and the address in the message |

```ts
import { NotFoundError } from "vectrixdb";
try {
  await client.describe("missing");
} catch (err) {
  if (err instanceof NotFoundError) console.log(err.status, err.message);
  else throw err;
}
```

## Tests

```
npm install
npm run typecheck
npm test
```

`npm test` runs three files with Node's test runner:

- `test/openapi.test.ts` checks every route and body field the client uses
  against `../../docs/reference/openapi.json`, with no server.
- `test/client.test.ts` checks retries, error kinds and the wire shapes
  against a fake `fetch`.
- `test/conformance.test.ts` runs the conformance walk from
  `sdk/CONTRACT.md` against a real server: `VECTRIXDB_URL` and
  `VECTRIXDB_KEY` when set, else it starts `sdk/conformance/serve.py` with
  the Python named by `PYTHON` (default `python3`), which needs the
  package's `api` extra installed.

`npm run generate` rewrites `src/generated/schema.ts` from
`../../docs/reference/openapi.json`; the generated file is committed so users need no
generator.

## Examples

Runnable with `npx tsx`, reading `VECTRIXDB_URL` and `VECTRIXDB_KEY` (or
`VECTRIXDB_TOKEN`) from the environment:

- `examples/search.ts`: search a collection and print citations.
- `examples/ingest.ts`: add the files named on the command line.
- `examples/signin-token.ts`: connect with a sign-in token and call `whoami`.
