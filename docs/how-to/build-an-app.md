# Build an app on it

Somebody else's server, a key they gave you, and an app of your own: a chat
box over the company handbook, a support tool, a nightly job that files new
documents. Nothing here needs the Python package. The server is an HTTP API
that describes itself, so any language that can send a request can use it.

You need three things from whoever runs the server: **the address**, **a key**,
and **the name of the collection** you may reach.

## The key

Ask for a key made for your app: named after it, scoped to the collections you
need, and with an end date. An admin makes one under **API keys for scripts**
in the account menu, or over the API:

```bash
curl -s -X POST https://vectors.company.com/api/v1/keys \
  -H "api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d '{"name": "handbook-bot", "role": "searcher",
       "collections": ["handbook"], "expires_in_days": 90}'
```

```json
{"ok": true, "data": {
  "id": "9e621ba4", "name": "handbook-bot", "role": "searcher",
  "prefix": "vx_9e621ba4", "created_by": "ada@example.com",
  "created_at": 1789866004.71, "last_used": null,
  "collections": ["handbook"], "expires_at": 1797642004.71, "expired": false,
  "key": "vx_9e621ba4_QVhLb..."}}
```

`key` is shown this once. The server keeps a hash, so nobody can read it back
to you, including the admin who made it. `role` is `reader`, `searcher` or
`operator`: ask for the smallest one that does the job, which for a search
app is `searcher`.

## Two headers, same key

```bash
curl -H "api-key: $KEY"            https://vectors.company.com/api/v1/collections
curl -H "Authorization: Bearer $KEY" https://vectors.company.com/api/v1/collections
```

Either works. `Authorization: Bearer` is what a generated client, an HTTP
library and most gateways reach for, so a client built from the OpenAPI
document needs nothing added. When both are sent, `api-key` is the one read.

## Search it

```bash
curl -s -X POST https://vectors.company.com/api/v1/collections/handbook/text-search \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"query_text": "how long do I have to claim expenses", "limit": 2}'
```

```json
{"ok": true, "message": null, "data": {
  "results": [
    {"id": "a", "score": 0.8235020041465759,
     "metadata": {"source": "expenses.pdf", "_vx_build": "build_c5a38fbff3"},
     "text": "Expenses are claimed within 30 days.",
     "relevance": 0.823502, "relevance_kind": "similarity",
     "matched_by": ["meaning"]},
    {"id": "b", "score": 0.6013270020484924,
     "metadata": {"source": "leave.pdf", "_vx_build": "build_c5a38fbff3"},
     "text": "Parental leave is 18 weeks.",
     "relevance": 0.601327, "relevance_kind": "similarity",
     "matched_by": ["meaning"]}],
  "query_time_ms": 4.14, "total_searched": 2, "search_mode": "vector"}}
```

`text-search` embeds the question on the server, so your app never loads a
model. `relevance` is 0 to 1 and means the same on every engine and every
search mode, which `score` does not: see
[Score and relevance](../explanation/relevance.md). `matched_by` says whether
a result came from meaning, from the words, or both.

Python, with nothing but the standard library:

```python
import json, os, urllib.request

def search(question, limit=5):
    body = json.dumps({"query_text": question, "limit": limit}).encode()
    ask = urllib.request.Request(
        "https://vectors.company.com/api/v1/collections/handbook/text-search",
        data=body,
        headers={"Authorization": f"Bearer {os.environ['VECTRIX_KEY']}",
                 "content-type": "application/json"},
    )
    with urllib.request.urlopen(ask, timeout=30) as reply:
        return json.load(reply)["data"]["results"]

for hit in search("how long do I have to claim expenses"):
    print(round(hit["relevance"], 3), hit["metadata"].get("source"), hit["text"])
```

JavaScript, on a server of your own:

```js
const search = async (question, limit = 5) => {
  const reply = await fetch(
    'https://vectors.company.com/api/v1/collections/handbook/text-search',
    { method: 'POST',
      headers: { 'authorization': `Bearer ${process.env.VECTRIX_KEY}`,
                 'content-type': 'application/json' },
      body: JSON.stringify({ query_text: question, limit }) });
  const body = await reply.json();
  if (!reply.ok) throw new Error(body.message || JSON.stringify(body.detail));
  return body.data.results;
};
```

Other searches are on the same collection: `search` takes a vector you made
yourself, `keyword-search` matches words, `text-hybrid-search` does both and
`search/rerank` reorders with a cross-encoder. They are all in
[the REST reference](../reference/rest-api.md), and
[Search modes](../explanation/search-modes.md) says which to use when.

## Generate a client instead

Every server serves its own OpenAPI 3.1 document at `/openapi.json`, open and
without a key. Each release also ships the same document as
[`docs/reference/openapi.json`](../reference/openapi.json) and attaches it to
the GitHub release, so you can generate against a version.

```bash
curl -s https://vectors.company.com/openapi.json -o vectrix.json
npx @hey-api/openapi-ts -i vectrix.json -o src/vectrix    # TypeScript
openapi-generator-cli generate -i vectrix.json -g python -o ./client  # or anything else
```

The document is what the server actually routes, generated from the code, so a
route that is in it exists and one that is not does not. If the server sits
behind a gateway on a path, the document it serves carries that path, and the
generated client is right the first time.

## What a scoped key may reach

A key made for `handbook` reaches `handbook`, and the server describing
itself. Everything else is refused, including the routes that carry no
collection in their path:

| You ask for | You get |
| --- | --- |
| `/api/v1/collections/handbook/...` | the collection, as your role allows |
| `GET /api/v1/collections` | a listing holding only `handbook` |
| `GET /api/v1/info`, `/api/v1/models`, `/api/v1/extractors`, `/health`, `/openapi.json` | the server describing itself |
| `GET /api/v1/collections/payroll` | `404 No collection named 'payroll'` |
| `GET /api/v1/documents`, `/api/v1/audit` | `403`, naming what the key does reach |

A collection outside the scope is a 404 rather than a 403, because a refusal
that distinguishes "not yours" from "not here" tells you what else the server
holds. A cross-collection route is a 403 and says so plainly: the route is
real and it is in the document you built against, so pretending it is missing
would only waste your afternoon.

```json
{"ok": false, "message": "This key reaches handbook and nothing else, so GET /api/v1/documents is not its to make.", "data": null}
```

The rule is written the closed way round, so a route added to the server after
your key was made is refused until somebody decides it is safe for scoped
keys. Ask, rather than assuming it will appear.

## Replies, and what went wrong

A reply that worked is `{"ok": true, "message": null, "data": ...}`. Read
`ok`, then `data`.

Every refusal is one shape, whichever part of the server said no:

```json
{"ok": false, "message": "Collection 'nope' not found", "data": null,
 "detail": "Collection 'nope' not found"}
```

`message` is always one sentence meant for a person: show it or log it rather
than inventing your own wording. `detail` is what FastAPI would have sent on
its own, so a client written for that shape still works; on a 422 it is the
list of field errors and `message` says the same in words:

```json
{"ok": false, "message": "query_text: Field required", "data": null,
 "detail": [{"type": "missing", "loc": ["body", "query_text"], "msg": "Field required"}]}
```

`data` carries what a program can act on: `{"retry_after": 23}` on a 429,
`{"signin": true}` when nobody is signed in. Before 2.2 there were two shapes,
`message` from the layer that checks keys and `detail` from the routes, and a
client had to know which had refused it.

| Status | What it means | What to do |
| --- | --- | --- |
| 401 | the key is wrong, revoked, or past its end date | stop and get a new key; do not retry |
| 403 | the key is real but this is not its to do | you need a wider role or a wider scope |
| 404 | no such collection, document or chunk, **or** it is outside your scope | check the name, then check the scope |
| 413 | the upload is over `VECTRIXDB_MAX_UPLOAD_BYTES` | send a smaller file |
| 422 | the body does not match the schema | the reply names the field |
| 429 | the key's allowance for this minute is spent, or a gateway in front said so | wait `Retry-After` seconds, then retry |
| 503 | the server cannot serve this safely: the access log or audit trail cannot be written, or a text model is missing | retry with a short backoff, then tell somebody |

A key may have an allowance: so many requests a minute, set on the key or for
every key with `VECTRIXDB_KEY_REQUESTS_PER_MINUTE`. Past it the server answers
429 with a `Retry-After` header and the same number in `data.retry_after`,
which is the seconds left of the minute. The count is kept where every server
behind a load balancer shares it, so it is one allowance however many servers
answer. With neither set a key has no limit, as before.

Retry 429 and 503 with backoff. Never retry 401, 403 or 422: nothing about
the next attempt will be different.

## Act as the person using your app

A key is nobody in particular. What it reads is recorded under the key's
name, and a collection with an entitlement policy stays closed to it, because
a policy judges a person. When your app already signs people in with the same
identity provider the server uses, send their access token instead:

```bash
curl -s -X POST https://vectors.company.com/api/v1/collections/client-notes/text-search \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'content-type: application/json' \
  -d '{"query_text": "what did we agree on fees", "limit": 5}'
```

The server's operator turns this on with `VECTRIXDB_OIDC_API_AUDIENCE`, the
audience your token must carry, `api://vectrixdb` say. Ask your provider for a
token for that audience, not for your own app's. The person then has the role
their groups give them on the server, or the one `VECTRIXDB_OIDC_TOKEN_ROLE`
gives every token, `searcher` say; the policy judges the search as theirs, and
the access log names them. They need no place on the server's People list,
which is for the people who run the platform. Behind a gateway that keeps
`Authorization` for itself, send the token in the header the operator named
with `VECTRIXDB_TOKEN_HEADER`, as it is or after `Bearer`. A token issued for a sign-in, whose audience is
an application and not this API, is refused, and so is one from another
issuer, an expired one, and one signed with a key the provider does not
publish. Changes that ask a person to confirm it is them, deleting a
collection say, are refused to a token: those need the person at the
dashboard. See [Sign people in](sign-in.md#tokens-for-apps).

## Keep the key out of the browser

A key in front-end JavaScript is a key you have published. Anyone who opens
the page reads it, and it works from anywhere until it is revoked.

Sign-in cannot rescue this either. The session cookie is `__Host-` and
`SameSite`, deliberately, so a page on another origin cannot sign in to the
server or carry its cookie. That is the protection working, not a
configuration to loosen.

So a browser app puts a small backend of its own in between: your server holds
the key, your page talks to your server, and your server decides who may ask
what. That backend is also where your own sign-in lives, and where you add
anything the key cannot express, like which customer is asking. Run VectrixDB
[behind a gateway](behind-a-gateway.md) on the same origin if you would rather
not write one.

## Before you ship

- The key is in the environment or a secret store, never in the repository,
  and never in a page the browser loads.
- It is scoped to the collections you actually use, and it ends. Put a
  reminder where the renewal will be seen; the server will not email anybody.
- Timeouts on every call, and a backoff on 429 and 503.
- `message` from a refused reply is shown or logged, not swallowed.
- Somebody knows which key your app holds: it is named in the access log as
  `key:<name>` on every request, and that name is how it gets revoked without
  breaking anything else.
- A key is nobody in particular, so a collection with an entitlement policy
  stays closed to it. Serve those from your own service with `as_principal`:
  see [Restrict what a search can see](entitlements.md).

## Where to look next

- [Run the REST API](rest-api.md) for the server's own settings and shapes.
- [REST API reference](../reference/rest-api.md) for every route.
- [Sign people in](sign-in.md#api-keys) for what keys are and how roles work.
- [Put it behind a gateway](behind-a-gateway.md) if your app and the server
  meet through APIM or API Gateway.
- [Use it from an assistant over MCP](mcp-server.md) if the app you are building is
  an assistant.
