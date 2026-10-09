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

## Behind a company gateway

```ts
const client = new VectrixClient({
  url: "https://gateway.example.com",
  key: process.env.VECTRIXDB_KEY,
  keyHeader: "Ocp-Apim-Subscription-Key",
  prefix: "/acme",
  gatewayPaths: "api/v1=/files/search, auth=/files/auth",
});
// GET https://gateway.example.com/files/search/acme/api/v1/collections
await client.collections();
```

Each route goes to `<gateway path><prefix><route>`, the gateway path being
that of the longest name in `gatewayPaths` the route falls under (or none).
`gatewayPaths` may also be a map, `{ "api/v1": "/files/search" }`. A token
goes in `tokenHeader` (default `authorization`), always as `Bearer <token>`;
`headers` adds headers to every request but never replaces `user-agent` or
the key or token header.

A key or token is refused over plain `http://` unless the host is this
machine (`localhost`, `127.x.x.x`, `::1`) or `allowHttp: true` is set, and a
redirect is never followed: a 3xx is thrown as a `VectrixError` naming the
status and its `location`, even with a `fetch` of your own (every request
is sent with `redirect: "manual"`, and a response that followed one anyway is
refused). A key, token or header value with a control character, or an
address with a user name or password in it (`https://user:pw@host`), is
refused with a `TypeError` that does not repeat it. A collection name,
document id or source id is sent as one path segment (`/` as `%2F`); an empty
one, `.` or `..` is refused with a `TypeError` before anything is sent. For a
private CA, Node reads
`NODE_EXTRA_CA_CERTS`; for a proxy, run Node with `NODE_USE_ENV_PROXY=1` to
honour `HTTPS_PROXY`/`NO_PROXY`, or pass a `fetch` of your own.

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

`npm test` runs four files with Node's test runner:

- `test/openapi.test.ts` checks every route and body field the client uses
  against `../../docs/reference/openapi.json`, with no server.
- `test/client.test.ts` checks retries, error kinds and the wire shapes
  against a fake `fetch`.
- `test/safety.test.ts` checks the gateway paths, the HTTP and redirect
  rules, the header options, and that a key never shows in an error or the
  client's printed form, with the cases every language client shares.
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
