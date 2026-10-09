# Call a server from your code

A VectrixDB server is an HTTP API, and it has a client in four languages:
Python, TypeScript and JavaScript, Go, and Rust. Each makes the same calls to
the same routes, as one caller, and turns the server's refusals into errors of
the same kinds. A search answers with each result's text, its relevance from 0
to 1 and its citation, so an app shows where an answer came from without
reading metadata.

![The same search from Python and from TypeScript against one server, each result with its relevance and citation](../images/terminal/terminal-sdk.gif)
<span class="vx-caption">The same search from Python and from TypeScript, against a server started for the clip.</span>

| Language | Where it lives | Needs |
| --- | --- | --- |
| Python | `vectrixdb.connect`, in the main package | `httpx`, from the `client` extra |
| TypeScript and JavaScript | `sdk/typescript` | Node 18 or later, a browser, Deno or Bun; no dependencies |
| Go | `sdk/go` | Go 1.22; the standard library only |
| Rust | `sdk/rust` | Rust 1.75; async on reqwest and tokio, TLS by rustls |

## Install

=== "Python"

    ```bash
    pip install "vectrixdb[client]"
    ```

=== "TypeScript"

    ```bash
    npm install vectrixdb
    ```

    The npm package is published with the 2.2.0 release. Until then, build it
    from `sdk/typescript` in a checkout.

=== "Go"

    ```bash
    go get github.com/knowusuboaky/VectrixDB/sdk/go/v2
    ```

=== "Rust"

    ```text
    [dependencies]
    vectrixdb = "2.2"
    ```

    The crate is published to crates.io with the 2.2.0 release. Until then,
    use a path dependency on `sdk/rust` in a checkout. The `blocking` feature
    gives the main calls without a runtime of your own.

## On a company network

Every client sends a key or a token over `https://`, or over `http://` to
this machine only: to another machine over `http://` it stops before
sending, because the key would cross the network in clear text. Each takes
an option for a network you trust, such as a private cluster network:
`allow_http=True` in Python, `allowHttp: true` in TypeScript,
`WithAllowHTTP()` in Go, `.allow_http()` in Rust. A key in the address
(`https://user:key@...`) is refused in all four, since logs and history keep
addresses.

A redirect is reported as a refusal naming where it pointed, and never
followed, so a key never goes to a second host.

Proxies are honoured as each platform honours them: `HTTPS_PROXY` and
`NO_PROXY` in Python, Go and Rust; in TypeScript, as the runtime's `fetch`
does. For a server whose certificate comes from the company's own authority,
or a proxy that inspects TLS:

=== "Python"

    ```python
    import vectrixdb

    db = vectrixdb.connect("https://vectors.company.com", key=KEY, verify="system")
    db = vectrixdb.connect("https://vectors.company.com", key=KEY, verify="/etc/ssl/company-ca.pem")
    ```

    `"system"` trusts what the operating system trusts, where a managed
    laptop's company certificates already are. `SSL_CERT_FILE` is honoured
    too.

=== "TypeScript"

    Node reads the authority from `NODE_EXTRA_CA_CERTS`, or the operating
    system's store with `node --use-system-ca` on Node 23.8 and later. A
    browser trusts what the browser trusts.

=== "Go"

    Go uses the operating system's store on Windows and macOS, and reads
    `SSL_CERT_FILE` and `SSL_CERT_DIR` on Linux. For anything else, pass an
    `http.Client` of your own with `WithHTTPClient`.

=== "Rust"

    The crate verifies with rustls and the Mozilla roots. For the company's
    authority, build a `reqwest::Client` with `add_root_certificate` and pass
    it with `.http(...)`.

A client of your own (`http=` in Python, `fetch` in TypeScript,
`WithHTTPClient` in Go, `.http()` in Rust) is yours to secure: its redirects,
its TLS and the plain http rule are as you set them.

## Connect

You need the server's address, a key or a token, and the name of a
collection your caller may reach.

=== "Python"

    ```python
    import os

    import vectrixdb

    db = vectrixdb.connect(
        "https://vectors.company.com", key=os.environ["VECTRIXDB_KEY"], collection="handbook"
    )
    ```

    Without `collection=`, `connect` gives the client itself, and
    `client.collection("handbook")` one collection. `db.client.close()`, or
    `with vectrixdb.connect(...) as client:`, closes its connections.

=== "TypeScript"

    ```ts
    import { connect } from "vectrixdb";

    const db = connect("https://vectors.company.com", { key: process.env.VECTRIXDB_KEY })
      .collection("handbook");
    ```

=== "Go"

    ```go
    client, err := vectrixdb.Connect("https://vectors.company.com", vectrixdb.WithKey(os.Getenv("VECTRIXDB_KEY")))
    if err != nil {
        log.Fatal(err)
    }
    db := client.Collection("handbook")
    ```

=== "Rust"

    ```rust
    let client = vectrixdb::Client::connect("https://vectors.company.com")
        .key(std::env::var("VECTRIXDB_KEY")?)
        .build()?;
    let db = client.collection("handbook");
    ```

| Option | Python | TypeScript | Go | Rust | Default |
| --- | --- | --- | --- | --- | --- |
| A key | `key=` | `key` | `WithKey` | `.key()` | |
| A token | `token=` | `token` | `WithToken`, `WithTokenSource` | `.token()`, `.token_source()` | |
| The header a key goes in | `key_header=` | `keyHeader` | `WithKeyHeader` | `.key_header()` | `api-key` |
| Tries after a busy answer | `retries=` | `retries` | `WithRetries` | `.retries()` | 3 |
| How long a request may take | `timeout=`, seconds | `timeoutMs` | `WithHTTPClient` | `.timeout()` | 60 seconds |
| Your own HTTP client | `http=`, an `httpx.Client` | `fetch` | `WithHTTPClient` | `.http()` | |

## Search

=== "Python"

    ```python
    found = db.search("how long do refunds take", limit=5)
    for result in found:
        print(result.relevance, result.readable_citation, result.text)
    best = found.top
    ```

=== "TypeScript"

    ```ts
    const found = await db.search("how long do refunds take", { limit: 5 });
    for (const r of found.results) {
      console.log(r.relevance, r.readableCitation, r.text);
    }
    const best = found.top;
    ```

=== "Go"

    ```go
    found, err := db.Search(ctx, "how long do refunds take", &vectrixdb.SearchOptions{Limit: 5})
    if err != nil {
        log.Fatal(err)
    }
    for _, r := range found.Results {
        fmt.Println(r.Relevance, r.ReadableCitation, r.Text)
    }
    best := found.Top()
    ```

=== "Rust"

    ```rust
    let found = db
        .search("how long do refunds take", vectrixdb::SearchOptions::default().limit(5))
        .await?;
    for r in &found.results {
        println!("{:?} {} {}", r.relevance, r.readable_citation, r.text);
    }
    let best = found.top();
    ```

A search takes a limit, 10 by default, a filter on metadata, and a mode. Each
mode is a route on the server:

| Mode | Route | What it finds |
| --- | --- | --- |
| `hybrid`, the default | `text-hybrid-search` | Meaning and exact words. Needs a collection made with its text index, as `create_collection` makes one unless told otherwise |
| `dense` | `text-search` | Meaning |
| `keyword` | `keyword-search` | Exact words |
| `rerank` | `text-search` with `rerank: true` | Meaning, reordered by the cross-encoder |

These are the server's modes. A collection opened with `Vectrix` on your own
machine has its own, described in [Search options](search-options.md).

Each result has its id, text, score, `relevance` and what it was matched by,
its metadata, `citation` and `readable_citation` (`readableCitation` in
TypeScript, `ReadableCitation` in Go). In Python a search answers with the same
`Results` and `Result` a local collection gives, so code written against a
collection on a laptop moves to a server by changing the line that opens it.
`decision_id` is set when a policy judged the search: the record in the audit
trail.

## Add a document

The server reads the document, cuts it and embeds every chunk, with the readers
and models it has, and answers with what it did.

=== "Python"

    ```python
    said = db.add_document("handbook.pdf")          # a path, bytes, or a string of Markdown
    print(said["chunks"], said["citations"])

    db.add(["Refunds are paid within ten working days."], metadata=[{"team": "finance"}])
    ```

=== "TypeScript"

    ```ts
    import { readFile } from "node:fs/promises";

    const said = await db.addDocument(await readFile("handbook.pdf"), {
      filename: "handbook.pdf",
      docId: "handbook.pdf",
    });
    console.log(said.chunks, said.citations);

    await db.add(["Refunds are paid within ten working days."], { metadata: [{ team: "finance" }] });
    ```

=== "Go"

    ```go
    content, err := os.ReadFile("handbook.pdf")
    if err != nil {
        log.Fatal(err)
    }
    said, err := db.AddDocument(ctx, content, &vectrixdb.DocumentOptions{Filename: "handbook.pdf", DocID: "handbook.pdf"})
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println(said["chunks"], said["citations"])

    if _, err := db.Add(ctx, vectrixdb.Record{Text: "Refunds are paid within ten working days.", Metadata: map[string]any{"team": "finance"}}); err != nil {
        log.Fatal(err)
    }
    ```

=== "Rust"

    ```rust
    let said = db
        .add_document(
            std::fs::read("handbook.pdf")?,
            vectrixdb::DocumentOptions {
                filename: Some("handbook.pdf".into()),
                doc_id: Some("handbook.pdf".into()),
                ..Default::default()
            },
        )
        .await?;
    println!("{} {}", said["chunks"], said["citations"]);

    db.add(vec![vectrixdb::Record::new("Refunds are paid within ten working days.").meta("team", "finance")])
        .await?;
    ```

The file's name says which reader reads it, so give one with bytes. A document
may also take `metadata` for every chunk, and `chunk`, `chunk_size` and
`overlap` for how it is cut. `add` adds short records as they are, one chunk
each; the server embeds them.

## When the server says no

Every refusal is a `ServerRefused` with the HTTP status and the server's own
words, and its kind says which:

| Kind | Status | What to do |
| --- | --- | --- |
| `ServerSignInRequired` | 401 | No key or token, or one the server does not take. Get a new one; do not retry |
| `ServerPermissionDenied` | 403 | The caller is known and may not do this: its role, its key's collections, or a policy |
| `ServerNotFound` | 404 | No such collection or document, or none this caller may see, which is the same answer |
| `ServerRejected` | 400, 422 and other 4xx | The request is wrong; the words say what to change |
| `ServerBusy` | 429 or 503, still, after the retries | Come back later |

Any other answer of 400 or more is a plain `ServerRefused`. In Go these are
`ErrSignInRequired`, `ErrPermissionDenied`, `ErrNotFound`, `ErrRejected` and
`ErrBusy`, matched with `errors.Is`; in Rust, `Error::Refused` with a `Kind`.

=== "Python"

    ```python
    from vectrixdb.exceptions import ServerNotFound, ServerRefused

    try:
        found = db.search("how long do refunds take")
    except ServerNotFound:
        found = None
    except ServerRefused as refused:
        print(refused.status, refused.said)
        raise
    ```

=== "TypeScript"

    ```ts
    import { ServerNotFound, ServerRefused } from "vectrixdb";

    try {
      await db.search("how long do refunds take");
    } catch (error) {
      if (error instanceof ServerNotFound) {
        // not there, or not this caller's to see
      } else if (error instanceof ServerRefused) {
        console.error(error.status, error.said);
      } else {
        throw error;
      }
    }
    ```

=== "Go"

    ```go
    _, err := db.Search(ctx, "how long do refunds take", nil)
    var refused *vectrixdb.ServerRefused
    switch {
    case errors.Is(err, vectrixdb.ErrNotFound):
        // not there, or not this caller's to see
    case errors.As(err, &refused):
        log.Println(refused.Status, refused.Said)
    }
    ```

=== "Rust"

    ```rust
    match db.search("how long do refunds take", vectrixdb::SearchOptions::default()).await {
        Ok(found) => println!("{} results", found.results.len()),
        Err(e) if e.kind() == Some(vectrixdb::Kind::NotFound) => {}
        Err(e) => return Err(e.into()),
    }
    ```

A busy answer, `429`, `502`, `503` or `504`, and a connection that drops are
tried again before anything is raised, up to `retries` more times. Each wait is
what `Retry-After` asks, or half a second doubled each time if that is longer,
and never more than 30 seconds.

## Every call

| What it does | Python | TypeScript | Go | Rust | Route |
| --- | --- | --- | --- | --- | --- |
| Whether the server is up | `health()` | `health()` | `Health` | `health` | `GET /health` |
| Whether its models are loaded | `ready()` | `ready()` | `Ready` | `ready` | `GET /ready` |
| Who this caller is: role, actions, collections | `whoami()` | `whoami()` | `WhoAmI` | `whoami` | `GET /api/v1/whoami` |
| The collections this caller reaches | `collections()` | `collections()` | `Collections` | `collections` | `GET /api/v1/collections` |
| Make a collection | `create_collection(name, hybrid=, description=, dimension=, metric=)` | `createCollection(name, { hybrid, description, dimension, metric })` | `CreateCollection(ctx, name, &CreateOptions{DenseOnly, Description, Dimension, Metric})` | `create_collection(name, hybrid, description)` | `POST /api/v2/collections` |
| Delete a collection, for good | `delete_collection(name)` | `deleteCollection(name)` | `DeleteCollection` | `delete_collection` | `DELETE /api/v1/collections/{name}` |
| One collection | `collection(name)` | `collection(name)` | `Collection(name)` | `collection(name)` | |
| Its size, how it is searched, its filterable fields | `describe()` | `describe()` | `Describe` | `describe` | `GET /api/v1/collections/{name}` |
| Search | `search(query, limit, mode=, filter=)` | `search(query, { limit, mode, filter })` | `Search(ctx, query, &SearchOptions{Limit, Mode, Filter})` | `search(query, SearchOptions)` | the mode's route |
| More like a result, never the result itself | `similar(id, limit, filter=)` | `similar(id, { limit, filter })` | `Similar` | `similar` | `POST .../similar` |
| Add records; the server embeds them | `add(texts, ids=, metadata=)` | `add(texts, { ids, metadata })` | `Add(ctx, records...)` | `add(records)` | `POST .../text-upsert` |
| Read, cut and index a document | `add_document(source, doc_id=, filename=, metadata=, chunk=, chunk_size=, overlap=)` | `addDocument(source, options)` | `AddDocument(ctx, bytes, &DocumentOptions{...})` | `add_document(bytes, DocumentOptions)` | `POST .../documents` |
| The documents it holds | `documents()` | `documents()` | `Documents` | `documents` | `GET .../documents` |
| A document's Markdown | `document(doc_id)` | `document(docId)` | `Document` | `document` | `GET .../documents/{doc_id}` |
| Delete a document and its chunks | `delete_document(doc_id)` | `deleteDocument(docId)` | `DeleteDocument` | `delete_document` | `DELETE .../documents/{doc_id}` |
| The feeds and pages it keeps up with | `sources()` | `sources()` | `Sources` | `sources` | `GET .../sources` |
| Keep up with a feed or a page | `add_source(address, every="6h", kind=)` | `addSource(address, { every, kind })` | `AddSource(ctx, address, every, kind)` | `add_source(address, every, kind)` | `POST .../sources` |
| Read its sources now | `refresh_sources(source=, force=)` | `refreshSources({ source, force })` | `RefreshSources(ctx, source, force)` | `refresh_sources(source, force)` | `POST .../sources/refresh` |

`...` is `/api/v1/collections/{name}`. Every route is one the server publishes
in its OpenAPI document, and each client lists the ones it uses, `OPERATIONS`
in Python and TypeScript and `Operations` in Go, which a test in each holds to
[`docs/reference/openapi.json`](../reference/openapi.json).

In Python, `vectrixdb.connect_async` gives the same calls as coroutines; see
[Async](async.md).

## Who calls

| Caller | Give it | For |
| --- | --- | --- |
| A script or a service | A key, made for it by an admin, scoped to the collections it needs | Batch jobs, back ends, pipelines |
| A person, through an app | Their access token from the company's identity provider, which the app holds for them | Anything a person uses: they search as themselves |

A token is a string, or a function that returns a fresh one, which the client
calls before each request. Give one or the other, never both: the server takes
one caller per request, and every client refuses both at once.

In a browser, use a token, never a key: anything a page holds, its reader
holds too. The server decides every call as the caller it names, with their
role, the collections their key reaches and each collection's policy, so the
client never needs to filter what comes back. See
[Build an app on it](build-an-app.md) for asking for a key, and
[Sign people in](sign-in.md) for tokens.

## Test a client against a real server

`sdk/conformance/serve.py` starts a fresh server on a free port, in a temporary
folder, with keys made for the run, runs a command against it, and stops it:

```bash
python sdk/conformance/serve.py -- node --test sdk/typescript/test/client.test.ts
python sdk/conformance/serve.py -- go test ./...      # from sdk/go
python sdk/conformance/serve.py -- cargo test         # from sdk/rust
```

The command runs with `VECTRIXDB_URL`, `VECTRIXDB_KEY`, and
`VECTRIXDB_READ_ONLY_KEY` for the refusal a read-only key gets, so every client
is asked the same questions of the same server. The server runs offline, with
documents kept, and nothing is left behind. Its exit code is the command's.

## A client in another language

Every server serves its OpenAPI 3.1 document at `/openapi.json`, with no key,
and each release ships the same document as
[`docs/reference/openapi.json`](../reference/openapi.json). Generate a client
from it with any OpenAPI generator:

```bash
curl -s https://vectors.company.com/openapi.json -o vectrixdb.json
openapi-generator-cli generate -i vectrixdb.json -g java -o ./vectrixdb-client
```

Behind a gateway, the document names the path the gateway serves the API under,
so a generated client calls the right address. A generated client sends the
key in the `api-key` header or as `Authorization: Bearer`; the server reads
either. See [Build an app on it](build-an-app.md#generate-a-client-instead).
