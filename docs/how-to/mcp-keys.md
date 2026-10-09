# Keys for a team

A key is the simplest way to let an assistant in: one per person or app, made
by an admin, with a role and, if you want, a list of the collections it may
reach. The assistant then searches as that key, and the access log names it as
`key:<name>`.

![Who may do what over MCP: a key made for one collection sees only it, a reader may not search, and a call with no key is told where to sign in](../images/dashboard/tour-mcp-keys.gif)

## Roles

A named key is a `reader`, a `searcher` or an `operator`, never an admin. Each
tool call is checked against the role as a request of its own, so the role
decides what the assistant may do:

| Key role | Over MCP it may | It is refused |
| --- | --- | --- |
| `reader` | list and describe collections, list their documents, open a whole document with `open_source` | `search` and `similar` |
| `searcher` | list and describe collections, list their documents, `search` and `similar` | `open_source`, and every write |
| `operator` | what a searcher may, and the write tools when the server has `VECTRIXDB_MCP_WRITES=1` | `open_source` |

`open_source` opens a whole stored document, which says more than a search
result does, so only a role that holds `document.read` may call it. The
`reader` key holds it. An `operator` does not hold it by being an operator: an
admin gives it to people by name, and a key cannot be given it.

The write tools are `add_document`, `create_collection`, `delete_document`,
`add_source` and `refresh_source`. Without `VECTRIXDB_MCP_WRITES=1` they are not
offered to anyone, whatever the role.

Two keys are not named keys. The server's own `VECTRIXDB_API_KEY` is an admin's
and may do everything. `VECTRIXDB_READ_ONLY_API_KEY` is a `reader`.

## A key for named collections

A key can be made for named collections only. It then reaches those and
nothing else:

- `list_collections` lists only them.
- Any other collection answers as a collection that is not there, in the same
  words a missing collection gets, so the key cannot tell that the others
  exist.
- `whoami` says `You reach only` and the names.

Leave the collections out and the key reaches every collection its role
allows.

## Collections with access rules

A collection can have a policy that limits who may search it, for example to
named people or to a group. A key is nobody in particular, so a policy cannot
judge it as a person. Such a collection opens to a key only when the key was
made for that collection, or when it is the server's own key.
`list_collections` marks a collection like this as `policied: who may search it
is decided per person`.

For people to search a policied collection as themselves through an assistant,
use [company sign-in](mcp-sign-in.md). See
[Restrict what a search can see](entitlements.md) for how policies work.

## Make a key

On the dashboard, an admin opens **API keys for scripts** in the account menu
and makes the key: a name that says what uses it, a role,
and optionally the collections it may reach and how many days it works. The key
starts `vx_`, is shown once, and is kept only as a hash. Making a key asks the
admin to prove it is them again when their last proof is more than ten minutes
old. The Access page lists every key by when it was last used.

Over the API, with an admin's key:

```bash
curl -s -X POST https://vectors.company.com/api/v1/keys \
  -H "api-key: $ADMIN_KEY" -H 'content-type: application/json' \
  -d '{"name": "handbook-bot", "role": "searcher",
       "collections": ["handbook"], "expires_in_days": 90}'
```

| Field | What it does |
| --- | --- |
| `name` | What the access log calls the key, as `key:<name>`. |
| `role` | `reader`, `searcher` or `operator`. |
| `collections` | The collections it may reach. Left out, every collection its role allows. |
| `expires_in_days` | How long it works, up to 3650. Left out, until it is revoked. |
| `requests_per_minute` | How many requests a minute it may make. Left out, `VECTRIXDB_KEY_REQUESTS_PER_MINUTE`, and with that unset, no limit. |

`GET /api/v1/keys` lists the keys and `DELETE /api/v1/keys/<key id>` revokes
one. Named keys need sign-in on the server; see
[API keys](sign-in.md#api-keys) for the rest.

## Keep keys out of shared files

A key in a config file is a key for anyone who reads the file. Keep it in one of
these instead:

- **A prompt.** VS Code asks for the key when the server starts and keeps it, so
  `.vscode/mcp.json` holds only `${input:vectrixdb-key}` and can be committed.
- **An environment variable.** A command line client reads `$VECTRIXDB_KEY`.
- **A secret store**, for an app.

The config for each client is on [Connect your client](mcp-connect.md). A key
that leaked is revoked on its own, without touching anyone else's.
