# VectrixDB for Go

The Go client for a VectrixDB server. It offers the same calls as the other
VectrixDB SDKs, needs nothing beyond the standard library, and builds with
Go 1.22 or newer.

## Install

```
go get github.com/knowusuboaky/VectrixDB/sdk/go/v2
```

```go
import vectrixdb "github.com/knowusuboaky/VectrixDB/sdk/go/v2"
```

The SDK is versioned 2.x, so the module path ends in `/v2`. It lives in a
subdirectory of the repository, so its releases are tagged `sdk/go/v2.2.0`
(not `v2.2.0`), which is what `go get ...@v2.2.0` resolves.

## Quickstart

Connect, add a document, search, and cite the answer:

```go
db := vectrixdb.New("http://127.0.0.1:8000", vectrixdb.WithKey(os.Getenv("VECTRIXDB_KEY")))
ctx := context.Background()
col, _ := db.CreateCollection(ctx, "handbook", nil)
f, _ := os.Open("handbook.md")
added, _ := db.AddDocument(ctx, col.Name, f, "handbook.md", &vectrixdb.AddOptions{DocID: "handbook.md"})
fmt.Println(added.Chunks, "chunks written")
hits, _ := db.Search(ctx, col.Name, "how are refunds paid?", &vectrixdb.SearchOptions{Limit: 3})
for _, h := range hits {
	fmt.Printf("%.2f %s: %s\n", h.Score, h.Citation, h.Text)
}
```

A company sign-in token goes in `WithToken(token)` in place of the key.
`WithTimeout` sets the per-request timeout (30 s by default) and
`WithHTTPClient` supplies your own `http.Client`.

Every call takes a `context.Context` first: `Health`, `Ready`, `Whoami`,
`Collections`, `Describe`, `CreateCollection`, `DeleteCollection`,
`AddDocument`, `AddTexts`, `Search`, `Documents`, `OpenDocument`,
`DeleteDocument`, `Sources`, `AddSource`, `RefreshSources`, `DeleteSource`.
The result types (`Collection`, `Result`, `Document`, `Added`, `Source`,
`Refreshed`) name the fields every SDK offers and keep the whole object the
server sent in `Raw`. A `Result`'s `Citation` is its `_vx_citation`, else its
`source`, else its id.

A 429 or a 503 is retried up to three times, waiting the server's
`Retry-After` when it sends one, else 1 s, 2 s, 4 s. Nothing else is retried.

## Errors

Every refusal is one `*vectrixdb.Error` with `Status`, `Message` and `Detail`
(on a 422, the list of field errors). `errors.Is` tells the kind:

| Status | Kind |
|---|---|
| 401 | `vectrixdb.ErrAuth` (no key, or a wrong one) |
| 403 | `vectrixdb.ErrForbidden` (the key's role or scope says no) |
| 404 | `vectrixdb.ErrNotFound` |
| 409 | `vectrixdb.ErrConflict` |
| 413 | `vectrixdb.ErrTooLarge` |
| 422 | `vectrixdb.ErrInvalid` |
| 429, 503 after the retries | `vectrixdb.ErrBusy` |
| anything else 4xx/5xx | a plain `*vectrixdb.Error` |

```go
_, err := db.Describe(ctx, "nope")
if errors.Is(err, vectrixdb.ErrNotFound) {
	var e *vectrixdb.Error
	errors.As(err, &e)
	fmt.Println(e.Status, e.Message)
}
```

A connection failure is returned at once, with the server's address in the
message.

A redirect is never followed, since the key would go with it to wherever it
points: a 3xx is a plain `*vectrixdb.Error` whose message names the status
and the `Location`, and it is not retried.

## Safety

- **No key over plain HTTP.** A key or token for an `http://` address is
  refused unless the host is this machine (`localhost`, `127.0.0.0/8`,
  `::1`) or `WithAllowHTTP()` is given. Without a key or token, `http://` is
  fine. `New` returns no error, so the client keeps the refusal: `db.Err()`
  reports it at once, and every call returns it. It wraps
  `vectrixdb.ErrConfig`, as do refused header names and gateway paths.

  ```go
  db := vectrixdb.New("http://vectors.internal", vectrixdb.WithKey(key))
  if err := db.Err(); err != nil {
  	log.Fatal(err) // ... use an https:// address, or WithAllowHTTP() ...
  }
  ```

- **TLS** is always checked. A private CA and a client certificate go in the
  `http.Client` you hand to `WithHTTPClient`:

  ```go
  pool, _ := x509.SystemCertPool()
  ca, _ := os.ReadFile("company-ca.pem")
  pool.AppendCertsFromPEM(ca)
  cert, err := tls.LoadX509KeyPair("client.crt", "client.key")
  if err != nil {
  	log.Fatal(err)
  }
  transport := http.DefaultTransport.(*http.Transport).Clone() // keeps the proxy settings
  transport.TLSClientConfig = &tls.Config{RootCAs: pool, Certificates: []tls.Certificate{cert}}
  db := vectrixdb.New("https://vectors.example.com",
  	vectrixdb.WithKey(key),
  	vectrixdb.WithHTTPClient(&http.Client{Transport: transport}))
  ```

  The client sets `CheckRedirect` on a copy of that `http.Client` so it never
  follows a redirect: a `CheckRedirect` you set yourself is replaced on the
  copy and never called (Go would carry the key header to wherever a redirect
  points), and your own value is not changed.

- **Names and ids are one path segment.** A `/` in a document id travels as
  `%2F`. An empty name or id, `.` or `..` is refused before anything is sent,
  with an error wrapping `vectrixdb.ErrConfig`: dot segments are collapsed on
  the way, so `DeleteDocument(ctx, "c", "..")` would otherwise reach the
  delete-collection route.
- A key, token or `WithHeader` value with a control character (CR, LF, NUL,
  tab, the rest of C0, DEL) is refused, and so is an address with a user name
  or password in it (`https://user:pw@host`); neither message repeats it.
- **Proxies**: `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` are honoured, as
  `http.DefaultTransport` does.
- The key and token never appear in an error, and `fmt.Print(db)` (`%v`,
  `%+v`, `%#v`) shows only the address and the header the credential goes in.

## Behind a company gateway

A gateway that publishes each part of the server under a path of its own,
wants the key in a header of its own, or wants a subscription key as well as
the person's token:

```go
db := vectrixdb.New("https://gateway.example.com",
	vectrixdb.WithToken(token),
	vectrixdb.WithHeader("Ocp-Apim-Subscription-Key", subscription),
	vectrixdb.WithPrefix("/acme"),
	vectrixdb.WithGatewayPaths("api/v1=/files/search, auth=/files/auth"),
)
```

- `WithKeyHeader(name)` (default `api-key`) and `WithTokenHeader(name)`
  (default `Authorization`; the token always goes as `Bearer <token>`) name
  the headers; the server's `VECTRIXDB_KEY_HEADER` and
  `VECTRIXDB_TOKEN_HEADER` are the same settings on its side.
- `WithHeader(name, value)` adds a header to every request. It never
  replaces the user-agent or the key or token header.
- `WithPrefix` is the path every route lives under; `WithGatewayPaths` is
  the list the gateway team hands over (`WithGatewayPathMap` takes it as a
  map). A request goes to `<gateway path><prefix><route>`, the gateway path
  being that of the longest name the route equals or falls under
  (`name/...`), else none. With the settings above, `/api/v1/collections`
  goes to `/files/search/acme/api/v1/collections`, `/auth/me` to
  `/files/auth/acme/auth/me`, and `/health` to `/acme/health`.
- Names and paths are read as the server reads them: slashes trimmed,
  doubled ones dropped, `.` and `..` refused, both sides of `=` required, a
  name given twice refused. One list serves both sides.

## Tests

`go test ./...` runs the unit tests (`client_test.go`, and `safety_test.go`
for the options above, with the same cases as the other SDKs) and two
more. `spec_test.go` parses
`docs/reference/openapi.json` and checks that every route and request field
the client uses is in it, so a renamed route fails without a server.
`conformance_test.go` runs the walk in `sdk/CONTRACT.md` against a real
server: set `VECTRIXDB_URL` and `VECTRIXDB_KEY` to use one that is running,
or leave them unset and the test starts `sdk/conformance/serve.py` with the
Python named by `PYTHON` (default `python3`, which needs the `api` extra)
and stops it at the end. It is skipped only when that Python cannot be
started.

```
cd sdk/go
go test ./...
PYTHON=/path/to/venv/bin/python go test -run TestConformanceWalk -v .
```

The request types in `generated_types.go` come from the OpenAPI file; after
the spec changes, `go generate ./...` refreshes them. The generator is
fetched for that run only and is not a dependency of the module.

## Examples

`examples/search`, `examples/ingest` and `examples/signin-token` are small
programs that read `VECTRIXDB_URL` and `VECTRIXDB_KEY` (or
`VECTRIXDB_TOKEN`) from the environment:

```
go run ./examples/ingest handbook ./handbook.md
go run ./examples/search handbook "how are refunds paid?"
```
