# VectrixDB for Go

The Go client for a VectrixDB server. It offers the same calls as the other
VectrixDB SDKs, needs nothing beyond the standard library, and builds with
Go 1.22 or newer.

## Install

```
go get github.com/knowusuboaky/VectrixDB/sdk/go
```

## Quickstart

Connect, add a document, search, and cite the answer:

```go
db := vectrixdb.New("http://127.0.0.1:8000", vectrixdb.WithKey(os.Getenv("VECTRIXDB_API_KEY")))
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

## Tests

`go test ./...` runs two tests. `spec_test.go` parses
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
programs that read `VECTRIXDB_URL` and `VECTRIXDB_API_KEY` (or
`VECTRIXDB_TOKEN`) from the environment:

```
go run ./examples/ingest handbook ./handbook.md
go run ./examples/search handbook "how are refunds paid?"
```
