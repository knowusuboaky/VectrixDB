package vectrixdb

// The Go client: its own behaviour against a scripted server, then the same
// questions every SDK is asked, of a real one, when sdk/conformance/serve.py
// started it (VECTRIXDB_URL and VECTRIXDB_KEY set).

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type answer struct {
	status  int
	headers map[string]string
	body    any
}

func scripted(t *testing.T, answers ...answer) (*httptest.Server, *[]*http.Request) {
	t.Helper()
	var asked []*http.Request
	var next int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		asked = append(asked, r.Clone(context.Background()))
		a := answers[atomic.AddInt32(&next, 1)-1]
		for k, v := range a.headers {
			w.Header().Set(k, v)
		}
		w.WriteHeader(a.status)
		_ = json.NewEncoder(w).Encode(a.body)
	}))
	t.Cleanup(server.Close)
	return server, &asked
}

func quick(c *Client) *Client {
	c.sleep = func(context.Context, time.Duration) error { return nil }
	return c
}

func TestABusyServerIsAskedAgain(t *testing.T) {
	server, asked := scripted(t,
		answer{503, map[string]string{"Retry-After": "0"}, map[string]any{"detail": "busy"}},
		answer{200, nil, map[string]any{"ok": true, "data": map[string]any{"status": "healthy"}}},
	)
	c, _ := Connect(server.URL, WithKey("k"))
	got, err := quick(c).Health(context.Background())
	if err != nil || got["status"] != "healthy" || len(*asked) != 2 {
		t.Fatalf("got %v, %v after %d requests", got, err, len(*asked))
	}
}

func TestStillBusyIsErrBusy(t *testing.T) {
	server, asked := scripted(t,
		answer{429, nil, map[string]any{"detail": "slow down"}},
		answer{429, nil, map[string]any{"detail": "slow down"}},
	)
	c, _ := Connect(server.URL, WithKey("k"), WithRetries(1))
	_, err := quick(c).Collections(context.Background())
	var refused *ServerRefused
	if !errors.Is(err, ErrBusy) || !errors.As(err, &refused) || refused.Said != "slow down" || len(*asked) != 2 {
		t.Fatalf("got %v after %d requests", err, len(*asked))
	}
}

func TestARefusalIsNotAskedAgain(t *testing.T) {
	server, asked := scripted(t, answer{403, nil, map[string]any{"detail": "Your role does not allow this"}})
	c, _ := Connect(server.URL, WithKey("k"))
	_, err := quick(c).Collections(context.Background())
	if !errors.Is(err, ErrPermissionDenied) || len(*asked) != 1 {
		t.Fatalf("got %v after %d requests", err, len(*asked))
	}
}

func TestATokenSourceIsAskedBeforeEveryRequest(t *testing.T) {
	server, asked := scripted(t,
		answer{200, nil, map[string]any{"ok": true, "data": map[string]any{}}},
		answer{200, nil, map[string]any{"ok": true, "data": map[string]any{}}},
	)
	tokens := []string{"first", "second"}
	c, _ := Connect(server.URL, WithTokenSource(func(context.Context) (string, error) {
		t := tokens[0]
		tokens = tokens[1:]
		return t, nil
	}))
	ctx := context.Background()
	_, _ = c.WhoAmI(ctx)
	_, _ = c.WhoAmI(ctx)
	if (*asked)[0].Header.Get("Authorization") != "Bearer first" || (*asked)[1].Header.Get("Authorization") != "Bearer second" {
		t.Fatal("the token was not fetched for each request")
	}
}

func TestAKeyGoesInTheHeaderTheServerReads(t *testing.T) {
	server, asked := scripted(t, answer{200, nil, map[string]any{"ok": true, "data": map[string]any{}}})
	c, _ := Connect(server.URL, WithKey("k"), WithKeyHeader("x-vectrix-key"))
	_, _ = c.WhoAmI(context.Background())
	if (*asked)[0].Header.Get("x-vectrix-key") != "k" {
		t.Fatal("the key was not in the header named")
	}
}

func TestAKeyAndATokenTogetherAreRefused(t *testing.T) {
	if _, err := Connect("http://x.test", WithKey("k"), WithToken("t")); err == nil || !strings.Contains(err.Error(), "not both") {
		t.Fatalf("got %v", err)
	}
}

func TestEveryRequestIsInTheOpenAPIDocument(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "docs", "reference", "openapi.json"))
	if err != nil {
		t.Fatal(err)
	}
	var document struct {
		Paths map[string]map[string]any `json:"paths"`
	}
	if err := json.Unmarshal(raw, &document); err != nil {
		t.Fatal(err)
	}
	published := map[string]bool{}
	for path, methods := range document.Paths {
		for method := range methods {
			published[strings.ToUpper(method)+" "+strings.ReplaceAll(path, "{doc_id:path}", "{doc_id}")] = true
		}
	}
	for _, op := range Operations {
		if op[1] == "/health" || op[1] == "/ready" {
			continue
		}
		if !published[op[0]+" "+op[1]] {
			t.Errorf("the server does not publish %s %s", op[0], op[1])
		}
	}
}

// ---------------------------------------------------------------- a real server

func realServer(t *testing.T) (*Client, string) {
	t.Helper()
	url, key := os.Getenv("VECTRIXDB_URL"), os.Getenv("VECTRIXDB_KEY")
	if url == "" {
		t.Skip("set by sdk/conformance/serve.py")
	}
	c, err := Connect(url, WithKey(key))
	if err != nil {
		t.Fatal(err)
	}
	return c, fmt.Sprintf("go%d", time.Now().UnixNano())
}

func TestARealServer(t *testing.T) {
	c, name := realServer(t)
	ctx := context.Background()
	if ok, err := c.Ready(ctx); !ok || err != nil {
		t.Fatalf("not ready: %v", err)
	}
	db, err := c.CreateCollection(ctx, name, &CreateOptions{Description: "The staff handbook"})
	if err != nil {
		t.Fatal(err)
	}
	added, err := db.Add(ctx,
		Record{ID: "refunds", Text: "Refunds are paid by the billing team within ten working days.", Metadata: map[string]any{"team": "billing"}},
		Record{ID: "travel", Text: "Travel is booked through the office manager, economy class.", Metadata: map[string]any{"team": "office"}},
		Record{ID: "laptops", Text: "Laptops are replaced every three years by the IT desk.", Metadata: map[string]any{"team": "it"}},
	)
	if err != nil || added != 3 {
		t.Fatalf("added %d: %v", added, err)
	}
	for _, mode := range []Mode{Hybrid, Dense, Keyword, Rerank} {
		found, err := db.Search(ctx, "refunds", &SearchOptions{Mode: mode, Limit: 3})
		if err != nil || found.Top() == nil || found.Top().ID != "refunds" {
			t.Fatalf("%s: %v %v", mode, found, err)
		}
	}
	best, _ := db.Search(ctx, "when are refunds paid", &SearchOptions{Limit: 1})
	if r := best.Top(); r.Relevance == nil || *r.Relevance <= 0 || *r.Relevance > 1 || !strings.Contains(r.Text, "ten working days") {
		t.Fatalf("best: %+v", r)
	}
	filtered, _ := db.Search(ctx, "who does what", &SearchOptions{Filter: map[string]any{"team": "it"}, Limit: 3})
	if len(filtered.Results) != 1 || filtered.Results[0].ID != "laptops" {
		t.Fatalf("filtered: %+v", filtered.Results)
	}
	near, _ := db.Similar(ctx, "refunds", &SearchOptions{Limit: 2})
	for _, r := range near.Results {
		if r.ID == "refunds" {
			t.Fatal("similar returned the chunk itself")
		}
	}
	said, err := db.AddDocument(ctx, []byte("# Leave\n\nAnnual leave is twenty five days."), &DocumentOptions{DocID: "leave.md"})
	if err != nil || said["doc_id"] != "leave.md" {
		t.Fatalf("add document: %v %v", said, err)
	}
	text, _ := db.Document(ctx, "leave.md")
	if !strings.Contains(text, "twenty five days") {
		t.Fatalf("document: %q", text)
	}
	cited, _ := db.Search(ctx, "how many days of leave", &SearchOptions{Limit: 1})
	if !strings.HasPrefix(cited.Top().Citation, "leave.md") {
		t.Fatalf("citation: %q", cited.Top().Citation)
	}
	if n, err := db.DeleteDocument(ctx, "leave.md"); err != nil || n < 1 {
		t.Fatalf("delete: %d %v", n, err)
	}
	if _, err := db.DeleteDocument(ctx, "leave.md"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a second delete: %v", err)
	}
	about, _ := db.Describe(ctx)
	if fields, _ := about["fields"].([]any); !containsAny(fields, "team") {
		t.Fatalf("fields: %v", about["fields"])
	}
	wrong, _ := Connect(c.URL, WithKey("wrong"))
	if _, err := wrong.Collections(ctx); !errors.Is(err, ErrSignInRequired) {
		t.Fatalf("a wrong key: %v", err)
	}
	reader, _ := Connect(c.URL, WithKey(os.Getenv("VECTRIXDB_READ_ONLY_KEY")))
	if _, err := reader.Collection(name).Add(ctx, Record{Text: "A new rule."}); !errors.Is(err, ErrPermissionDenied) {
		t.Fatalf("a read-only key wrote: %v", err)
	}
	if err := c.DeleteCollection(ctx, name); err != nil {
		t.Fatal(err)
	}
}

func containsAny(list []any, want string) bool {
	for _, v := range list {
		if v == want {
			return true
		}
	}
	return false
}

func TestAKeyNeverCrossesANetworkInClearText(t *testing.T) {
	if _, err := Connect("http://vectors.example.com", WithKey("k")); err == nil || !strings.Contains(err.Error(), "clear text") {
		t.Fatalf("plain http elsewhere with a key: %v", err)
	}
	if _, err := Connect("http://vectors.example.com", WithToken("t")); err == nil || !strings.Contains(err.Error(), "clear text") {
		t.Fatalf("plain http elsewhere with a token: %v", err)
	}
	for _, u := range []string{"http://localhost:7337", "http://127.0.0.1:7337", "http://[::1]:7337", "http://app.localhost"} {
		if _, err := Connect(u, WithKey("k")); err != nil {
			t.Fatalf("%s: %v", u, err)
		}
	}
	if _, err := Connect("http://vectors.internal", WithKey("k"), WithAllowHTTP()); err != nil {
		t.Fatal(err)
	}
	if _, err := Connect("http://vectors.example.com"); err != nil {
		t.Fatal(err)
	}
	if _, err := Connect("https://user:secret@vectors.example.com"); err == nil || !strings.Contains(err.Error(), "not in the address") {
		t.Fatalf("a key in the address: %v", err)
	}
	if _, err := Connect("ftp://vectors.example.com", WithKey("k")); err == nil {
		t.Fatal("ftp was taken")
	}
}

func TestARedirectIsReportedNotFollowed(t *testing.T) {
	followed := false
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { followed = true }))
	defer elsewhere.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL+"/api", http.StatusFound)
	}))
	defer server.Close()
	c, _ := Connect(server.URL, WithKey("k"))
	_, err := c.Collections(context.Background())
	var refused *ServerRefused
	if !errors.As(err, &refused) || refused.Status != http.StatusFound || !strings.Contains(refused.Said, elsewhere.URL) {
		t.Fatalf("got %v", err)
	}
	if followed {
		t.Fatal("the redirect was followed, and the key with it")
	}
}

func TestAWrappersHeaders(t *testing.T) {
	var seen http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"data":{"who":"ama"}}`))
	}))
	defer server.Close()
	c, err := Connect(server.URL, WithKey("k"), WithHeader("Ocp-Apim-Subscription-Key", "sub-1"), WithUserAgent("acme-vectors/1.4"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.WhoAmI(context.Background()); err != nil {
		t.Fatal(err)
	}
	if seen.Get("Ocp-Apim-Subscription-Key") != "sub-1" || seen.Get("api-key") != "k" {
		t.Fatalf("headers: %v", seen)
	}
	if !strings.HasPrefix(seen.Get("User-Agent"), "acme-vectors/1.4 vectrixdb-go/") {
		t.Fatalf("user agent: %q", seen.Get("User-Agent"))
	}
	if _, err := Connect(server.URL, WithKey("k"), WithHeader("Authorization", "x")); err == nil || !strings.Contains(err.Error(), "names the caller") {
		t.Fatalf("a header carrying the caller: %v", err)
	}
	if _, err := Connect(server.URL, WithKey("k"), WithHeader("API-KEY", "x")); err == nil {
		t.Fatal("the key header was taken as an extra header")
	}
}
