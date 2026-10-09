# VectrixDB for Rust

The client for a [VectrixDB](https://knowusuboaky.github.io/VectrixDB/) server: search a collection, add to it and read what it holds, as one caller. Async on reqwest and tokio, TLS by rustls; the `blocking` feature gives the same calls without a runtime of your own.

```toml
[dependencies]
vectrixdb = "2.2"
```

```rust
let client = vectrixdb::Client::connect("https://vectors.company.com")
    .key(std::env::var("VECTRIXDB_KEY")?)
    .build()?;
let found = client
    .collection("handbook")
    .search("how long do refunds take", vectrixdb::SearchOptions::default().limit(5))
    .await?;
for r in &found.results {
    println!("{}: {}", r.readable_citation, r.text);
}
```

## Who calls

`.key(...)` for a script or a service; `.token(...)`, or `.token_source(|| ...)` for one that expires, for a person or an app signed in with the company's identity provider. `.key_header(...)` names the header a key goes in when the server set `VECTRIXDB_KEY_HEADER`.

## Every call

| Call | What it does |
| --- | --- |
| `health`, `ready` | Whether the server is up, and whether its models are loaded |
| `whoami` | Who this client is, its role, what it may do, what it reaches |
| `collections`, `create_collection`, `delete_collection` | The collections it reaches; make one; delete one |
| `describe` | Size, how it is searched, and the fields it can be filtered on |
| `search(query, SearchOptions::default().limit(5).mode(Mode::Keyword).filter("team", "it"))` | Search as this caller |
| `similar(id, options)` | More like a result, never the result itself |
| `add(records)` | Add records; the server embeds them |
| `add_document(bytes, DocumentOptions { doc_id, filename, .. })` | Read, cut and index a document |
| `documents`, `document`, `delete_document` | What it holds, a document's Markdown, deleting one |
| `sources`, `add_source`, `refresh_sources` | Feeds and pages it keeps up with |

## When the server says no

A refusal is `Error::Refused { status, kind, said }`; `error.kind()` is `Kind::SignInRequired` (401), `PermissionDenied` (403), `NotFound` (404), `Rejected` (400, 422) or `Busy` (429 or 503, still, after asking again). A busy answer and a dropped connection are asked again (`.retries(n)`, 3), waiting what `Retry-After` says.

## Tests

`cargo test` runs the client's own tests; `python ../conformance/serve.py -- cargo test` runs them against a real server too.

Author: Kwadwo Daddy Nyame Owusu - Boakye. Apache-2.0.
