# Keys and roles

Every request to the server names an action, such as `search` or
`content.write`, and the caller's role either holds that action or does not.
People get their role when they sign in; see [Sign people in](sign-in.md).
Scripts and apps get theirs from the key they send. This page covers the roles,
the keys, and how to find out who a key is. Keys for assistants are in
[Keys for a team](mcp-keys.md).

## The roles

| Role | Who has it | In short |
| --- | --- | --- |
| `guest` | Somebody not signed in, on a server with `VECTRIXDB_GUESTS=on` | Sees the collections and how the setups scored. Never searches. |
| `viewer` | A person | Sees collections, their size, health and builds, and lists chunk ids. Never chunk text. |
| `reader` | The read-only key, or a named key | Reads chunks and whole documents. Never searches or writes. |
| `searcher` | A named key | Reads and searches. |
| `operator` | A person or a named key | Reads, searches and writes. |
| `admin` | A person, or the server's own key | Everything. |

A person is a `viewer`, an `operator` or an `admin`. A named key is a `reader`,
a `searcher` or an `operator`, never an admin, because a script has no
business managing people.

## What each role may do

The table lives in one place, `vectrixdb.signin.roles`, and the dashboard's
Access page is drawn from it.

| Action | What it allows | guest | viewer | reader | searcher | operator | admin |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `meta.read` | See collections, their size, health, builds and settings | yes | yes | yes | yes | yes | yes |
| `evaluation.read` | See how every setup scored against the golden questions | yes | yes | yes | yes | yes | yes |
| `content.index` | List chunk ids and where a chunk came from, with the text hidden | | yes | yes | yes | yes | yes |
| `content.read` | Open a chunk to read its text and metadata, one at a time | | | yes | yes | yes | yes |
| `document.read` | Open a whole stored document | | | yes | | if given | yes |
| `search` | Run searches | | | | yes | yes | yes |
| `mcp.connect` | Connect an assistant over MCP | | | yes | yes | yes | yes |
| `content.write` | Add, change and remove chunks and documents | | | | | yes | yes |
| `collection.create` | Create a collection | | | | | yes | yes |
| `collection.maintain` | Rebuild an index and extract a graph | | | | | yes | yes |
| `self.manage` | See your own ways to sign in and your sessions, and sign out elsewhere | | yes | | | yes | yes |
| `self.secure` | Add or remove your own passkeys, authenticator, password and recovery codes | | yes | | | yes | yes |
| `collection.delete` | Delete a collection | | | | | | yes |
| `collection.share` | Choose who may search a collection | | | | | | yes |
| `cache.clear` | Clear the cache | | | | | | yes |
| `audit.read` | Read the audit trail | | | | | | yes |
| `access.read` | Read the access log | | | | | | yes |
| `access.check` | Check whether somebody may retrieve from a collection, without searching it | | | | | | yes |
| `people.manage` | Add and remove people, change roles, reset an authenticator | | | | | | yes |
| `keys.manage` | Make and revoke API keys for scripts | | | | | | yes |
| `about.read` | See which VectrixDB this is, its version, its licence and its notice | | | | | | yes |
| `evaluation.golden` | Download the golden questions a run used | | | | | | yes, signed in as a person |

Three rows need a word more:

- **`document.read` is given by name.** A whole document says more than a
  chunk, so an operator does not hold it by being an operator. An admin ticks
  **Whole documents** beside a person on the Access page, or maps a group to
  it with `VECTRIXDB_OIDC_GRANT_MAP`. It is the only action that can be given
  this way, and only to a role that already reads content. See
  [Sign people in](sign-in.md#roles).
- **`evaluation.golden` is for a person.** The server's own key is an admin,
  and it still cannot download the golden questions, so the access log names
  who took them.
- **A guest sees less than `meta.read` suggests.** A guest reaches the list of
  collections, one collection and its health, the policies, the models, the
  daily counts and the evaluation scores. Anything inside a collection asks them to sign in. See
  [Guests](sign-in.md#guests).

### How a request names its action

The server matches the method and path against a route table, `_ROUTES` in
`vectrixdb.signin.roles`, and the first match wins. A few of its rows:

| Request | Action |
| --- | --- |
| `GET /api/v1/audit` | `audit.read` |
| `* /api/v1/keys` and `/api/v1/keys/{id}` | `keys.manage` |
| `PUT /api/v1/collections/{name}/policy` | `collection.share` |
| `POST /api/v1/collections/{name}/text-search`, `search`, `hybrid-search`, `similar` and the other searches | `search` |
| `POST /api/v1/collections/{name}/points`, `text-upsert`, `documents` | `content.write` |
| `GET /api/v1/collections/{name}/points` | `content.index` |
| `GET /api/v1/collections/{name}/documents/{id}` | `document.read` |
| `GET /api/v1/whoami` | `meta.read` |
| `* /mcp` | `mcp.connect` |

The [REST API reference](../reference/rest-api.md) lists the action for every
route. Denial is the default twice over:

- A route the table does not place names no action, and only an admin may call
  it. A route added later stays closed to everyone else until somebody decides
  who it is for.
- A role the table does not know holds nothing.

### Changes that ask again

Five actions need a proof from the last ten minutes that it is really the
person: `collection.delete`, `collection.share`, `people.manage`,
`keys.manage` and `self.secure`. Looking never does. The dashboard shows
**Confirm it's you** and asks for a passkey, a code or single sign-on. An app
acting with a person's token cannot give that proof, so these changes are
refused to it. A key is not asked. See
[Confirm it's you](sign-in.md#confirm-its-you).

## The server's own keys

Two keys are set on the server itself:

| Setting | Role | What it is for |
| --- | --- | --- |
| `VECTRIXDB_API_KEY` | `admin` | Everything, for scripts and deployment. Never rate limited. |
| `VECTRIXDB_READ_ONLY_API_KEY` | `reader` | Reads, for a script that should never write. |

Each can be given as a file instead, `VECTRIXDB_API_KEY_FILE` and
`VECTRIXDB_READ_ONLY_API_KEY_FILE`, which is how Docker and Kubernetes secrets
arrive. Or give the server only the SHA-256 of a key, and its environment never
holds a working one:

```bash
export VECTRIXDB_API_KEY_SHA256="$(printf %s "$KEY" | sha256sum | cut -d' ' -f1)"
export VECTRIXDB_READ_ONLY_API_KEY_SHA256="<sha-256 of the read-only key>"
```

Keys are compared in constant time.

With sign-in off there are no roles, only these two keys:

- No key set: the server is open, and listens on `127.0.0.1` only unless
  `VECTRIXDB_ALLOW_OPEN=1`.
- `VECTRIXDB_API_KEY` set: a write needs it. A read needs no key, unless
  `VECTRIXDB_OPEN_READS=0`, and then a read needs one of the two keys.
- The read-only key reads and searches, since a search is a read sent as a
  `POST`, and is refused every write with a `403`.

With sign-in on, both keys are judged by the role table like everyone else.

## Named keys

With sign-in on, an admin can make as many keys as there are scripts, one each,
so each can be limited, watched and revoked on its own. Named keys are kept in
the sign-in store, so they need sign-in on; without it the key routes answer
`404`.

Make one in the dashboard under **API keys for scripts** in the account menu,
or with `POST /api/v1/keys`:

```bash
curl -s -X POST https://vectors.example.com/api/v1/keys \
  -H "api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d '{"name": "handbook-bot", "role": "searcher",
       "collections": ["handbook"], "expires_in_days": 90,
       "requests_per_minute": 120}'
```

| Field | What it says | Left out |
| --- | --- | --- |
| `name` | What uses the key, so it is clear later. Up to 60 characters. Required. | |
| `role` | `reader`, `searcher` or `operator`. Required. | |
| `collections` | The collections the key may reach. | Every collection its role allows |
| `expires_in_days` | How long the key works, 1 to 3650 days. | Until it is revoked |
| `requests_per_minute` | How many requests a minute it may make, 1 to 100000. | `VECTRIXDB_KEY_REQUESTS_PER_MINUTE`, and with that unset, no limit |

The reply carries the key once:

```json
{"ok": true, "data": {"id": "9e621ba4", "name": "handbook-bot", "role": "searcher",
  "prefix": "vx_9e621ba4", "created_by": "ada@example.com", "created_at": 1789742000.1,
  "last_used": null, "collections": ["handbook"], "expires_at": 1797518000.1,
  "expired": false, "requests_per_minute": 120,
  "key": "vx_9e621ba4_..."}}
```

A named key starts `vx_`. The server keeps only its hash and its first eleven
characters, the `prefix`, so a lost key cannot be shown again: revoke it and
make another. Every key made and revoked is a line in the access log, which
also names each request the key makes as `key:<name>`.

From the server's console, for a deployment script, or for a server whose
people all use single sign-on and need a first key from somewhere:

```bash
vectrixdb keys add handbook-bot --role searcher --collection handbook --days 90 --per-minute 120
vectrixdb keys list
vectrixdb keys revoke 9e621ba4
```

The console reads the same sign-in settings as the server, and refuses when
sign-in is not on. The access log records these keys as made by the server
console.

### List and revoke

| Request | What it does |
| --- | --- |
| `GET /api/v1/keys` | Every key not revoked, newest first, with when each was last used, and the roles a key may have |
| `DELETE /api/v1/keys/{id}` | Revokes the key. Anything using it stops at once. |

The Access page lists keys by last use, ten a page, and marks a key not used
for thirty days. A key nobody uses is a key to revoke. An expired key reads as
a key that does not exist, and the keys page marks it **Expired**.

### Request limits

`requests_per_minute` on a key, or `VECTRIXDB_KEY_REQUESTS_PER_MINUTE` for
every key without its own number. Past it, the reply is `429` with
`Retry-After`, the seconds left in the minute. The count is kept in the sign-in
store, so three servers behind a load balancer give a key one allowance, and a
restart forgives nobody. The minute is a fixed one. The server's own
`VECTRIXDB_API_KEY` is never limited.

## What a scoped key can reach

A key made for named collections reaches those collections, and the few routes
where the server describes itself. Everything else is refused, and the rule is
written the closed way round, so a route added later is refused to scoped keys
until somebody decides otherwise.

| The request | The answer |
| --- | --- |
| A route under `/api/v1/collections/{name}` for a collection on the key's list | Judged by the key's role, as usual |
| The same for any other collection | `404`, word for word what a collection that does not exist gets |
| `GET /api/v1/collections` | Only the key's own collections |
| A read of `/`, `/health`, `/ready`, `/api/v1/whoami`, `/openapi.json`, `/docs`, `/redoc`, `/api/v1`, `/api/v1/info`, `/api/v1/models` or `/api/v1/extractors` | Allowed: `SCOPED_KEY_MAY_ALSO_READ` |
| `/mcp` | Allowed to connect. Each tool the assistant calls comes back as a request of its own, held to the key's collections. |
| Any other route, such as `/api/v1/documents` or `/api/v1/policies` | `403`: this key reaches handbook and nothing else |

### Keys and collection policies

A key is nobody in particular, so a collection policy cannot judge it the way
it judges a person. On a server with a collection store:

- `VECTRIXDB_API_KEY` passes, as the host's own hand.
- A named key passes for the collections it was made for.
- Every other key is refused, the read-only key and unscoped named keys
  included.

To let a script search a collection with a policy, make it a key for that
collection. See [Collection policies](collection-policies.md).

A collection with a per-document entitlement policy stays closed to every key,
since that policy needs a principal to judge. Serve it to people who sign in,
or from your own service; see
[Restrict what a search can see](entitlements.md).

## Sending a key

| Header | When |
| --- | --- |
| `api-key: <key>` | What the dashboard sends. `VECTRIXDB_KEY_HEADER` names another header. |
| `Authorization: Bearer <key>` | What a generated client, an HTTP library and most gateways send. `VECTRIXDB_TOKEN_HEADER` names another header. |

Both are read. When both arrive, the key header wins. With
`VECTRIXDB_TOKEN_HEADER` set to a header of your own, the server reads the key
there, with or without `Bearer`, and leaves `Authorization` to the gateway. See
[Put it behind a gateway](behind-a-gateway.md).

## Who is this key

`GET /api/v1/whoami` says who the caller is, how they came in, their role,
every action it holds, and which collections they reach. Every key and every
person signed in may ask, a scoped key included, and no secret is ever in the
answer.

```bash
curl -s https://vectors.example.com/api/v1/whoami -H "api-key: $HANDBOOK_KEY"
```

```json
{"ok": true, "data": {
  "who": "key:handbook-bot",
  "method": "key",
  "role": "searcher",
  "actions": ["content.index", "content.read", "evaluation.read", "mcp.connect", "meta.read", "search"],
  "collections": ["handbook"],
  "sees_content": true}}
```

A key with no list says `"collections": "every collection"`. A person signed
in is named by their address, and a server with no sign-in and no key answers
that anyone who reaches it may use it. An assistant asks the same question
with the MCP tool `whoami`.

## See also

- [Sign people in](sign-in.md): roles for people, sessions, tokens for apps
  and the access log.
- [Keys for a team](mcp-keys.md): keys for assistants over MCP.
- [Collection policies](collection-policies.md): who may search a collection
  at all.
- [Settings](../reference/settings.md#keys-for-scripts): every key setting.
