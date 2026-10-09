# vectrixdb

The Rust client for VectrixDB: collections, documents, text search and
sources, over the server's HTTP API. It offers the same calls as the other
VectrixDB clients, spelled in snake_case, and is built from `docs/reference/openapi.json`.

## Install

Add it to `Cargo.toml`:

```toml
[dependencies]
vectrixdb = "2.2"
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

The client is async and runs on tokio. TLS comes from rustls; no OpenSSL is
needed.

## Quickstart

```rust
use vectrixdb::{AddOptions, Client, CreateOptions, SearchOptions};

#[tokio::main]
async fn main() -> vectrixdb::Result<()> {
    let db = Client::new("http://127.0.0.1:8000").key("your-key").build()?;
    db.create_collection("docs", CreateOptions::default()).await?;
    let bytes = std::fs::read("handbook.md").unwrap();
    db.add_document("docs", &bytes, "handbook.md", AddOptions::default()).await?;
    for hit in db.search("docs", "refunds", SearchOptions::default()).await? {
        println!("{}  {}", hit.citation, hit.text);
    }
    Ok(())
}
```

`Client::new(url)` returns a builder: `.key(..)` sends an API key in the
`api-key` header, `.token(..)` sends a company sign-in token as
`Authorization: Bearer`, `.timeout(..)` sets the per-request timeout (30 s by
default), and `.build()` makes the client. A key or token is never sent over
plain `http://` to a host other than this machine (`localhost`, `127.0.0.0/8`,
`::1`): `build()` refuses unless `.allow_http(true)` is set. The client never
follows a redirect, since the key would go with it; a 3xx is an
`Error::Api` of kind `Other` naming the status and the `location`. A 429 or 503 is retried up to three
times, waiting the server's `Retry-After` when it sends one, else 1 s, 2 s,
4 s. Every request carries `user-agent: vectrixdb-rust/2.2.0`.

Each result struct (`Collection`, `Hit`, `Document`, `Added`, `Source`,
`Refreshed`) names the fields the contract promises and keeps everything else
the server sent in `extra`. A `Hit` has a `citation`: `_vx_citation` from the
metadata, else `source`, else the id.

## Behind a company gateway

The builder has what a company's gateway, proxy and private CA ask for, the
same options as the other VectrixDB clients:

```rust
let db = Client::new("https://gateway.example.com")
    .token(token)
    .token_header("X-Person-Token")              // default `authorization`, always `Bearer <token>`
    .header("Ocp-Apim-Subscription-Key", sub)   // sent on every request
    .prefix("/acme")
    .gateway_paths("api/v1=/files/search, auth=/files/auth")
    .ca_certificate(&std::fs::read("company-ca.pem")?)
    .identity(&std::fs::read("client-cert-and-key.pem")?)
    .build()?;
```

- `.key_header(name)` (default `api-key`) and `.token_header(name)` (default
  `authorization`) name the headers the key and token go in. The server's
  `VECTRIXDB_KEY_HEADER` and `VECTRIXDB_TOKEN_HEADER` are the same settings.
- `.header(name, value)` adds a header to every request. It never replaces
  `user-agent` or the key or token header.
- `.prefix(p)` and `.gateway_paths(list)`: a request for a route goes to
  `<gateway path><prefix><route>`, the gateway path being that of the longest
  name the route falls under, so `/api/v1/collections` above goes to
  `/files/search/acme/api/v1/collections` and `/health` to `/acme/health`.
  The list is text or a map (`HashMap`, `BTreeMap`, or `(name, path)` pairs),
  read as the server reads its `VECTRIXDB_GATEWAY_PATHS`: slashes trimmed,
  doubled ones dropped, `.` and `..`, an empty side or a name given twice
  refused.
- `.ca_certificate(pem)` trusts a private CA (a PEM certificate or bundle) as
  well as the usual roots; `.identity(pem)` gives a client certificate (the
  certificate and its private key in one PEM). Certificates are always
  checked; there is no option to turn that off.
- `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` are honoured.

Every setting is checked by `build()`, which says what is wrong as an
`Error::Transport`: among them a key, token or header value with a control
character, and an address with a user name or password in it
(`https://user:pw@host`), neither repeated in the message. The key and token
never appear in an error message or in the `Debug` form of the builder or the
client. A collection name, document id or source id is sent as one path
segment (`/` as `%2F`); an empty one, `.` or `..` is refused with an
`Error::Transport` before anything is sent.

## The calls

| Call | What it does |
|---|---|
| `health()`, `ready()` | the process answers; the models are loaded |
| `whoami()` | who the key or token is |
| `collections()`, `describe(name)`, `create_collection(name, CreateOptions)`, `delete_collection(name)` | collections |
| `add_document(collection, bytes, filename, AddOptions)` | upload a file; the server extracts, chunks and embeds it |
| `add_texts(collection, Vec<Text>)` | embed and store texts; returns how many were added |
| `search(collection, query, SearchOptions)` | `Mode::Meaning` (dense) or `Mode::Hybrid`, with `limit`, `filter`, `rerank` |
| `documents(collection)`, `open_document(collection, doc_id)`, `delete_document(collection, doc_id)` | the kept documents |
| `sources(collection)`, `add_source(collection, address, SourceOptions)`, `refresh_sources(collection)`, `delete_source(collection, id, delete_documents)` | feeds and pages |

## Blocking

With the `blocking` feature, `Client::new(url).key(..).build_blocking()` gives
a `BlockingClient` with the same calls, each one synchronous. It drives the
async client on a runtime of its own, so call it from plain threads, not from
inside another async runtime.

```toml
vectrixdb = { version = "2.2", features = ["blocking"] }
```

## Errors

Every call returns `vectrixdb::Result<T>`. The server's refusals are
`Error::Api { status, kind, message, detail }`; a server that cannot be reached
is `Error::Transport`, and a request that outlives the timeout is
`Error::Timeout`. `kind` is one of:

| Status | `Kind` | Helper |
|---|---|---|
| 401 | `Auth` (no key, or a wrong one) | `is_auth()` |
| 403 | `Forbidden` (the key's role or scope says no) | `is_forbidden()` |
| 404 | `NotFound` | `is_not_found()` |
| 409 | `Conflict` | `is_conflict()` |
| 413 | `TooLarge` | `is_too_large()` |
| 422 | `Invalid` (`detail` is the field list) | `is_invalid()` |
| 429, 503 after the retries | `Busy` | `is_busy()` |
| any other 4xx or 5xx | `Other` | |

A `message` the server did not send (an HTML page from a proxy) is replaced
with `"<status> from <url>"`.

## Generated request types

`src/generated.rs` holds the request bodies, generated from `docs/reference/openapi.json`
by `scripts/generate.py`. After the spec changes, run
`python3 -I scripts/generate.py` from this directory. A unit test checks that
every route and body field the client uses is in the spec.

## Tests

```sh
cargo test                       # unit tests and the spec check
cargo test --test conformance    # the walk from sdk/CONTRACT.md, against a real server
```

The conformance test reads `VECTRIXDB_URL` and `VECTRIXDB_KEY`. When they are
unset it starts `sdk/conformance/serve.py` itself with the interpreter named
by `PYTHON` (default `python3`), which needs the `vectrixdb[api]` package
installed, and stops it at the end.

`Cargo.lock` is committed so CI builds are reproducible.

## Examples

```sh
cargo run --example search -- docs "refunds"
cargo run --example ingest -- docs handbook.md
VECTRIXDB_TOKEN=... cargo run --example signin_token
```
