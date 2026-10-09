# VectrixDB for TypeScript and JavaScript

The client for a [VectrixDB](https://knowusuboaky.github.io/VectrixDB/) server: search a collection, add to it and read what it holds, as one caller, from Node 18 and later, browsers, Deno and Bun. No dependencies; it uses the platform's `fetch`.

```bash
npm install vectrixdb
```

```ts
import { connect } from "vectrixdb";

const db = connect("https://vectors.company.com", { key: process.env.VECTRIXDB_KEY })
  .collection("handbook");

const found = await db.search("how long do refunds take", { limit: 5 });
for (const r of found.results) {
  console.log(`[${Math.round((r.relevance ?? 0) * 100)}%] ${r.readableCitation}: ${r.text}`);
}
```

## Who calls

| Option | For |
| --- | --- |
| `key` | a script or a service: an API key made on the server's Access page, for some collections or all |
| `token` | a person or an app signed in with the company's identity provider: a string, or a function that returns a fresh one before each request |

In a browser, use a token, never a key: anything a page holds, its reader holds too. The server decides every call as that caller, with their role, the collections their key reaches, and each collection's policy.

## Every call

| Call | What it does |
| --- | --- |
| `client.health()`, `client.ready()` | Whether the server is up, and whether its models are loaded |
| `client.whoami()` | Who this client is: how it came in, its role, what it may do, what it reaches |
| `client.collections()` | The collections this caller reaches |
| `client.createCollection(name, { hybrid, description })` | Make a collection |
| `client.deleteCollection(name)` | Delete a collection, for good |
| `db.describe()` | Size, how it is searched, and the metadata fields it can be filtered on |
| `db.search(query, { limit, mode, filter })` | Search: `hybrid` (default), `dense`, `keyword` or `rerank` |
| `db.similar(id, { limit, filter })` | More like a result, never the result itself |
| `db.add(texts, { ids, metadata })` | Add records; the server embeds them |
| `db.addDocument(source, { docId, filename, metadata })` | Read, cut and index a document: a string of Markdown, bytes or a Blob |
| `db.documents()`, `db.document(id)`, `db.deleteDocument(id)` | What it holds, a document's Markdown, and deleting one |
| `db.sources()`, `db.addSource(address, { every })`, `db.refreshSources()` | Feeds and pages it keeps up with |

Each result has `id`, `text`, `relevance` (0 to 1), `citation`, `readableCitation`, `metadata` and `matchedBy`.

## When the server says no

A refusal is thrown as the kind it is, each a `ServerRefused` with the server's `status` and its own words in `said`:

| Error | Status |
| --- | --- |
| `ServerSignInRequired` | 401: no key or token, or one it does not take |
| `ServerPermissionDenied` | 403: the role, the key's collections, or a policy |
| `ServerNotFound` | 404: not there, or not this caller's to see |
| `ServerRejected` | 400 or 422: the request is wrong; `said` says what to change |
| `ServerBusy` | 429 or 503, still, after asking again |

A busy server's 429 and 503, and a dropped connection, are asked again (`retries`, 3), waiting what `Retry-After` says.

## Quickstarts

[`quickstart/`](quickstart/) has three: search, add a file, and sign in with a token.

Author: Kwadwo Daddy Nyame Owusu - Boakye. Apache-2.0.
