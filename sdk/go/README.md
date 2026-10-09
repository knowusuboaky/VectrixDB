# VectrixDB for Go

The client for a [VectrixDB](https://knowusuboaky.github.io/VectrixDB/) server: search a collection, add to it and read what it holds, as one caller. Standard library only.

```bash
go get github.com/knowusuboaky/VectrixDB/sdk/go/v2
```

```go
client, err := vectrixdb.Connect("https://vectors.company.com", vectrixdb.WithKey(os.Getenv("VECTRIXDB_KEY")))
if err != nil {
	log.Fatal(err)
}
found, err := client.Collection("handbook").Search(ctx, "how long do refunds take", &vectrixdb.SearchOptions{Limit: 5})
for _, r := range found.Results {
	fmt.Println(r.ReadableCitation, r.Text)
}
```

## Who calls

`WithKey` for a script or a service; `WithToken`, or `WithTokenSource` for one that expires, for a person or an app signed in with the company's identity provider. `WithKeyHeader` names the header a key goes in when the server set `VECTRIXDB_KEY_HEADER`.

## Every call

| Call | What it does |
| --- | --- |
| `Health`, `Ready` | Whether the server is up, and whether its models are loaded |
| `WhoAmI` | Who this client is, its role, what it may do, what it reaches |
| `Collections`, `CreateCollection`, `DeleteCollection` | The collections it reaches; make one; delete one |
| `Describe` | Size, how it is searched, and the fields it can be filtered on |
| `Search(ctx, query, &SearchOptions{Limit, Mode, Filter})` | `Hybrid` (default), `Dense`, `Keyword` or `Rerank` |
| `Similar(ctx, id, opts)` | More like a result, never the result itself |
| `Add(ctx, records...)` | Add records; the server embeds them |
| `AddDocument(ctx, bytes, &DocumentOptions{DocID, Filename, Metadata})` | Read, cut and index a document |
| `Documents`, `Document`, `DeleteDocument` | What it holds, a document's Markdown, deleting one |
| `Sources`, `AddSource`, `RefreshSources` | Feeds and pages it keeps up with |

## When the server says no

Every refusal is a `*vectrixdb.ServerRefused` with `Status`, `Said` (the server's words) and `Kind`. Match it with `errors.Is`: `ErrSignInRequired` (401), `ErrPermissionDenied` (403), `ErrNotFound` (404), `ErrRejected` (400, 422), `ErrBusy` (429 or 503, still, after asking again). A busy answer and a dropped connection are asked again (`WithRetries`, 3), waiting what `Retry-After` says.

## Quickstart

`go run ./quickstart/search "how long do refunds take"` with `VECTRIXDB_URL` and `VECTRIXDB_KEY` set.

## Tests

`go test ./...` runs the client's own tests; `python ../conformance/serve.py -- go test ./...` runs them against a real server too.

Author: Kwadwo Daddy Nyame Owusu - Boakye. Apache-2.0.
