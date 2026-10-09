package vectrixdb

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

// The cases below are the same in every SDK.

func TestGatewayMapping(t *testing.T) {
	c := New("https://gateway.example.com",
		WithGatewayPaths("api/v1=/files/search, auth=/files/auth"), WithPrefix("/acme"))
	if err := c.Err(); err != nil {
		t.Fatal(err)
	}
	for route, want := range map[string]string{
		"/api/v1/collections": "/files/search/acme/api/v1/collections",
		"/auth/me":            "/files/auth/acme/auth/me",
		"/health":             "/acme/health",
		"/api/v1x":            "/acme/api/v1x",
	} {
		if got := c.address(route); got != want {
			t.Errorf("%s -> %s, want %s", route, got, want)
		}
	}

	longest := New("https://gateway.example.com", WithGatewayPaths("api=/a, api/v1=/b"))
	for route, want := range map[string]string{
		"/api/v1/c":  "/b/api/v1/c",
		"/api/other": "/a/api/other",
	} {
		if got := longest.address(route); got != want {
			t.Errorf("%s -> %s, want %s", route, got, want)
		}
	}

	// The map form reads the same way.
	m := New("https://gateway.example.com",
		WithGatewayPathMap(map[string]string{"api/v1": "/files/search", "auth": "files/auth/"}), WithPrefix("acme"))
	if got := m.address("/auth/me"); got != "/files/auth/acme/auth/me" {
		t.Error(got)
	}
	// An escaped id stays one segment and the query is not part of the match.
	if got := c.address(join("api", "v1", "collections", "c", "documents", "a/b c.md")); got != "/files/search/acme/api/v1/collections/c/documents/a%2Fb%20c.md" {
		t.Error(got)
	}
}

func TestGatewayNormalising(t *testing.T) {
	if p, err := routePrefix(" acme/ "); err != nil || p != "/acme" {
		t.Errorf("prefix %q %v", p, err)
	}
	paths, err := readGatewayPaths("/api//v1/ = files//search/")
	if err != nil || len(paths) != 1 || paths["api/v1"] != "/files/search" {
		t.Errorf("%v %v", paths, err)
	}
	if _, err := routePrefix("a/../b"); !errors.Is(err, ErrConfig) {
		t.Errorf("prefix with ..: %v", err)
	}
}

func TestGatewayRefusals(t *testing.T) {
	for _, list := range []string{"api/v1", "=/x", "api/v1=", "../x=/y", "api/v1=/a, api/v1/=/b"} {
		if _, err := readGatewayPaths(list); !errors.Is(err, ErrConfig) {
			t.Errorf("%q: %v", list, err)
		}
		c := New("https://gateway.example.com", WithGatewayPaths(list))
		_, err := c.Health(context.Background())
		if !errors.Is(err, ErrConfig) || c.Err() == nil {
			t.Errorf("%q: the client was made: %v", list, err)
		}
	}
	if _, err := readGatewayPathMap(map[string]string{"api/v1": "/a", "/api/v1/": "/b"}); !errors.Is(err, ErrConfig) {
		t.Errorf("map duplicate: %v", err)
	}
}

func TestPlainHTTPRule(t *testing.T) {
	refused := func(url string, opts ...Option) bool {
		return errors.Is(New(url, opts...).Err(), ErrConfig)
	}
	key := WithKey("secret-key")
	if !refused("http://vectors.example.com", key) {
		t.Error("key over http to a remote host was allowed")
	}
	if !refused("http://vectors.example.com", WithToken("secret-token")) {
		t.Error("token over http to a remote host was allowed")
	}
	if refused("http://vectors.example.com", key, WithAllowHTTP()) {
		t.Error("WithAllowHTTP did not allow it")
	}
	for _, url := range []string{"http://localhost:8000", "http://127.0.0.5", "http://[::1]:9", "HTTP://LocalHost:8000", "https://vectors.example.com"} {
		if refused(url, key) {
			t.Errorf("%s was refused", url)
		}
	}
	if !refused("http://localhost.evil.com", key) {
		t.Error("localhost.evil.com counted as this machine")
	}
	if refused("http://vectors.example.com") {
		t.Error("http without a key was refused")
	}
	err := New("http://vectors.example.com", key).Err()
	if !strings.Contains(err.Error(), "WithAllowHTTP") || strings.Contains(err.Error(), "secret-key") {
		t.Errorf("the refusal: %v", err)
	}
	// Every call returns it, and no request is sent.
	_, err = New("http://vectors.example.com", key).Collections(context.Background())
	if !errors.Is(err, ErrConfig) {
		t.Errorf("a call: %v", err)
	}
}

// A 302 is refused, names the status and the location, and the place it
// pointed to never hears from the client, so the key never left.
func TestNoRedirects(t *testing.T) {
	var reached atomic.Int32
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached.Add(1)
		w.Write([]byte(`{"status": "healthy"}`))
	}))
	defer elsewhere.Close()
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		http.Redirect(w, r, elsewhere.URL+"/health", http.StatusFound)
	}))
	defer srv.Close()

	check := func(c *Client) {
		t.Helper()
		calls.Store(0)
		_, err := c.Health(context.Background())
		var e *Error
		if !errors.As(err, &e) || e.Status != 302 {
			t.Fatalf("err=%v", err)
		}
		if !strings.Contains(err.Error(), "302") || !strings.Contains(err.Error(), elsewhere.URL+"/health") {
			t.Errorf("the message: %v", err)
		}
		for _, kind := range []error{ErrAuth, ErrForbidden, ErrNotFound, ErrConflict, ErrTooLarge, ErrInvalid, ErrBusy, ErrConfig} {
			if errors.Is(err, kind) {
				t.Errorf("a redirect is not %v", kind)
			}
		}
		if strings.Contains(err.Error(), "secret-key") {
			t.Error("the key is in the error")
		}
		if calls.Load() != 1 {
			t.Errorf("a redirect was retried: %d calls", calls.Load())
		}
	}
	check(New(srv.URL, WithKey("secret-key")))

	// A user's http.Client is not changed: the client works on a copy.
	mine := &http.Client{}
	check(New(srv.URL, WithKey("secret-key"), WithHTTPClient(mine)))
	if mine.CheckRedirect != nil {
		t.Error("the user's http.Client was changed")
	}
	if reached.Load() != 0 {
		t.Errorf("the redirect was followed %d times", reached.Load())
	}

	// A CheckRedirect of the user's own is kept.
	own := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("mine") }}
	_, err := New(srv.URL, WithHTTPClient(own)).Health(context.Background())
	if err == nil || !strings.Contains(err.Error(), "mine") {
		t.Errorf("the user's CheckRedirect was not kept: %v", err)
	}
}

func TestCredentialHeaders(t *testing.T) {
	var got http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		w.Write([]byte(`{"status": "healthy"}`))
	}))
	defer srv.Close()
	ctx := context.Background()
	call := func(opts ...Option) {
		t.Helper()
		got = nil
		if _, err := New(srv.URL, opts...).Health(ctx); err != nil {
			t.Fatal(err)
		}
	}

	call(WithKey("k"))
	if got.Get("api-key") != "k" || got.Get("Authorization") != "" {
		t.Errorf("default key header: %v", got)
	}
	call(WithKey("k"), WithKeyHeader("Ocp-Apim-Subscription-Key"))
	if got.Get("Ocp-Apim-Subscription-Key") != "k" || got.Get("api-key") != "" {
		t.Errorf("key header: %v", got)
	}
	call(WithToken("t"))
	if got.Get("Authorization") != "Bearer t" {
		t.Errorf("default token header: %v", got)
	}
	call(WithToken("t"), WithTokenHeader("X-Person-Token"))
	if got.Get("X-Person-Token") != "Bearer t" || got.Get("Authorization") != "" {
		t.Errorf("token header: %v", got)
	}

	// Extra headers go on every request and never replace the user-agent or
	// the credential header.
	call(WithToken("t"),
		WithHeader("Ocp-Apim-Subscription-Key", "sub"),
		WithHeader("Authorization", "Bearer forged"),
		WithHeader("User-Agent", "forged"))
	if got.Get("Ocp-Apim-Subscription-Key") != "sub" || got.Get("Authorization") != "Bearer t" || got.Get("User-Agent") != userAgent {
		t.Errorf("extra headers: %v", got)
	}
	call(WithKey("k"), WithKeyHeader("X-Key"), WithHeader("x-key", "forged"))
	if got.Get("X-Key") != "k" {
		t.Errorf("an extra header replaced the key: %v", got)
	}

	// Header names are RFC 9110 tokens.
	for _, opt := range []Option{WithKeyHeader("bad header"), WithTokenHeader(""), WithHeader("a:b", "v"), WithKeyHeader("é")} {
		if c := New(srv.URL, WithKey("k"), opt); !errors.Is(c.Err(), ErrConfig) {
			t.Errorf("a bad header name was taken: %v", c.Err())
		}
	}
	if c := New(srv.URL, WithHeader("X-Sub", "secret\r\nvalue")); !errors.Is(c.Err(), ErrConfig) || strings.Contains(c.Err().Error(), "secret") {
		t.Errorf("a bad header value: %v", c.Err())
	}
}

// The key never appears in an error or in the client's printed form.
func TestKeyNeverShown(t *testing.T) {
	const key = "sk-very-secret-0123"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		w.Write([]byte(`{"ok": false, "message": "Wrong key", "data": null, "detail": null}`))
	}))
	defer srv.Close()
	ctx := context.Background()
	clients := []*Client{
		New(srv.URL, WithKey(key)),
		New(srv.URL, WithToken(key)),
		New("http://vectors.example.com", WithKey(key)),
		New("http://127.0.0.1:1", WithKey(key)),
		New(srv.URL, WithKey(key+"\n")),
	}
	for _, c := range clients {
		_, err := c.Collections(ctx)
		if err == nil {
			t.Fatalf("%v: no error", c)
		}
		for _, shown := range []string{err.Error(), fmt.Sprintf("%v", c), fmt.Sprintf("%+v", c), fmt.Sprintf("%#v", c), fmt.Sprintf("%+v", *c), fmt.Sprint(c)} {
			if strings.Contains(shown, key) {
				t.Errorf("the key is shown: %s", shown)
			}
		}
	}
	if s := fmt.Sprintf("%+v", clients[0]); !strings.Contains(s, srv.URL) || !strings.Contains(s, "api-key") {
		t.Errorf("printed form: %s", s)
	}
}
