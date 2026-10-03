# Put it behind a gateway

Azure API Management, AWS API Gateway, an ingress controller or a plain
reverse proxy. The server serves its own dashboard, so the browser and the API
are the same origin and stay that way behind a gateway: what changes is the
address people arrive at, the path the app is served under, and who the server
thinks is calling.

Two settings cover most of it, and four more cover a gateway that gives each
endpoint a path of its own.

| Setting | What it does |
| --- | --- |
| `VECTRIXDB_PUBLIC_URL` | the address people type, path included: `https://apim.company.com/vectrixdb` |
| `VECTRIXDB_TRUSTED_PROXIES` | addresses and networks whose `X-Forwarded-For` says who is calling: `10.0.0.0/8` |
| `VECTRIXDB_PREFIX` | the path every route lives under, your choice: `acme` |
| `VECTRIXDB_GATEWAY_PATHS` | each route's own gateway path, as the gateway team hands them over: `api/v1=/files/search, auth=/files/auth` |
| `VECTRIXDB_KEY_HEADER` | the header an app's key arrives in; `api-key` unless named |
| `VECTRIXDB_TOKEN_HEADER` | the header a person's access token arrives in; `Authorization` unless named |

`VECTRIXDB_ROOT_PATH` exists for the case where the gateway adds a path but no
public URL is set. Left out, the path of `VECTRIXDB_PUBLIC_URL` is the root
path, so an enterprise sets one thing and a plain install sets nothing.

```bash
VECTRIXDB_PUBLIC_URL=https://apim.company.com/vectrixdb
VECTRIXDB_TRUSTED_PROXIES=10.0.0.0/8
vectrixdb check --env-file vectrixdb.env   # says what a gateway deployment gets wrong
```

## Why the trusted proxies matter

Every request behind a gateway arrives from the gateway, so without this
setting the server thinks one caller is making all of them. Two things key
on the caller's address: the sign-in lockout and the access log. One person's
wrong codes would lock out everyone behind the gateway.

The header is a claim anybody can make, so it is believed only from the
addresses you name. With nothing named it is ignored, which is right for a
server on its own.

## What the gateway team needs from you

Send them the OpenAPI document. A running server serves it at `/openapi.json`
with no key, every release ships the same document as
[`docs/reference/openapi.json`](../reference/openapi.json), and the release
workflow attaches it to the release itself, so a policy can be rebuilt from a
version. APIM and API Gateway both import it and make their operations from
it, so nobody retypes a route list and a new route is a diff rather than an
email.

Ask them to publish these, as wildcards rather than one operation per
endpoint, because a per-operation policy has to be edited every time a route
is added:

| Publish | Methods | What it is |
| --- | --- | --- |
| `/api/v1/*` | GET, POST, PUT, DELETE | the data API |
| `/api/v2/*` | POST | create a collection, write points |
| `/auth/*` | GET, POST, DELETE | sign-in, needed when people sign in through the gateway, emergency sign-in included |
| `/dashboard/*`, `/brand.json`, `/brand.css`, `/brand/logo*` | GET | the dashboard itself, and its brand |
| `/`, `/health`, `/openapi.json`, `/docs` | GET | index, probe, schema, interactive docs |

Four routes worth naming to them:

- `GET /api/v1/audit` is the audit trail, and it is more sensitive than the
  index it audits. It is already refused unless a key is set and the request
  carries it; consider not publishing it outside at all.
- `POST /api/v1/collections/{name}/documents` and `POST /api/v1/documents`
  carry files, up to `VECTRIXDB_MAX_UPLOAD_BYTES`, 100 MiB by default. Size
  and timeout exceptions belong here.
- `GET /api/v1/evaluations/{run}/golden` hands back the golden questions, for
  an admin signed in as a person and never a key.
- `PUT /api/v1/collections/{name}/policy` decides who may search a
  collection, so it is the one to log or gate.

## What the policy must not do

- **Strip `Cookie` or `Set-Cookie`.** The session is a `__Host-` cookie;
  without it everyone arrives as a guest.
- **Strip `x-csrf-token`.** Every write from the dashboard carries it, and the
  server refuses writes that do not.
- **Strip `api-key` or `Authorization`.** Scripts sign in with one of those. A
  gateway that keeps them for itself can pass the key and the token in headers
  of their own instead, named by `VECTRIXDB_KEY_HEADER` and
  `VECTRIXDB_TOKEN_HEADER`; with a token header of its own, the server leaves
  `Authorization` to the gateway and never reads it.
- **Add its own CORS while the server adds CORS too.** Two sets of headers
  break browsers. Keep CORS in the gateway policy and leave
  `VECTRIXDB_CORS_ORIGINS` unset; the dashboard is same-origin and needs none.
- **Cap the body below your documents.** AWS API Gateway stops at 10 MB and
  about 29 seconds for an integration, which is under the 100 MiB the
  documents route takes; APIM forwards with a 300 second default. Either lower
  `VECTRIXDB_MAX_UPLOAD_BYTES` to what the gateway allows, or keep ingestion
  off the gateway path.

A subscription key authenticates the caller to the gateway, not the person to
VectrixDB. Keep sign-in or named API keys on: the gateway cannot express "this
person may read this collection", and the server can. An app on the other side
of the gateway wants a key scoped to its own collections, with an end date:
see [Build an app on it](build-an-app.md).

## The path, if there has to be one

A hostname of its own is simpler than a path: APIM with an empty API suffix,
or an AWS HTTP API on a custom domain with a `$default` stage. Where a path is
unavoidable, set it and the app handles both shapes, whether the gateway
forwards `/vectrixdb/api/v1/...` or strips the prefix and sends `/api/v1/...`.
The dashboard reads its own address to find the API, and the `/openapi.json` a
deployment serves carries the prefix, so an import through the gateway is
right the first time.

## A path for each endpoint

Some gateway teams hand over one address and, for each endpoint or family of
endpoints, a path of their own, and your prefix goes between the two:

```text
https://gateway.example.com / files/search / acme / api/v1/collections/handbook/text-search
        the gateway          its path       prefix   the route
```

```bash
VECTRIXDB_PUBLIC_URL=https://gateway.example.com
VECTRIXDB_PREFIX=acme
VECTRIXDB_GATEWAY_PATHS=api/v1=/files/search, auth=/files/auth, dashboard=/files/dash
```

- **Names.** A route is named as it is served with no prefix, alone or as a
  family: `api/v1` is every API route, and `api/v1/collections/handbook` is one
  collection's. The longest name that fits a path is the one that counts. A
  name no route falls under stops the server from starting, so a typing mistake
  is found then and not by a caller.
- **Both shapes.** Each listed route answers with its gateway path in front or
  without it, and with the prefix or without it, so the gateway may pass them
  on or take them off. A gateway path opens only its own routes: `/files/auth`
  in front of the search API is not found.
- **No redirects.** A slash too many is not found rather than redirected,
  because a redirect names the address the server sees and sends the caller
  round the gateway. The one exception is the dashboard's own address without
  its slash, which redirects relatively, so it holds through the gateway.
- **What the server writes.** Everything it hands out is written the way the
  caller reaches it: the address single sign-on returns to, the path its cookie
  is kept for, the links in sign-in emails and in `vectrixdb people add`, and a
  map the dashboard is told, so each call it makes goes to the gateway path its
  route was published under. Register the return address as
  `https://gateway.example.com/files/auth/acme/auth/oidc/callback` with the
  identity provider.
- **The document.** `/openapi.json` names the prefix as the server's base, which
  is what the gateway team imports.

`vectrixdb check` reads the four settings the way the server does, says where
each name is published, and refuses a name no route falls under.

## Checking it from outside

```bash
vectrixdb check --url https://apim.company.com/vectrixdb
vectrixdb check --url https://gateway.example.com --prefix acme --gateway-paths "api/v1=/files/search, auth=/files/auth"
```

The prefix and the gateway paths come from the server's own settings,
`VECTRIXDB_PREFIX` and `VECTRIXDB_GATEWAY_PATHS`, or `--env-file`, unless the
command is given them, and each route is then asked where it is published.

`vectrixdb check` reads settings; this asks the public address what a caller
would, because most of what goes wrong behind a gateway is not in the settings
at all. Every request is a GET, and the key it sends is wrong on purpose, to
see whose refusal comes back. It reports:

- whether the address reaches the server's `/health` at all;
- whether each gateway path reaches the server, in the server's own words;
- whether `/openapi.json` says the API is under the path the gateway serves
  it on, which is what a generated client and an imported policy will call;
- whether `api-key` and `Authorization` reach the server, by whether the
  server's own `Invalid API key` comes back, the gateway's error page does, or
  the server answers as if no key had been sent;
- whether the dashboard is served and a redirect keeps the path;
- whether two sets of CORS headers come back.

It exits 1 on an error, so it can close a deployment pipeline. It cannot see
the caller's address from outside; for that, read the access log.

With sign-in on, the access log is the proof that the addresses arrived: each
line names the person, the action and the collection, and the address it came
from should be the caller's, not the gateway's. See
[Sign people in](sign-in.md#the-access-log) and [Deploy the server](deploy.md).
