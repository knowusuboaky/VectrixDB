package vectrixdb

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// A 429 is retried, waiting the server's Retry-After, and every request
// carries the key and the user-agent.
func TestRetryHonoursRetryAfter(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Header.Get("api-key") != "k" || r.Header.Get("User-Agent") != "vectrixdb-go/"+Version {
			t.Errorf("headers: %v", r.Header)
		}
		if calls == 1 {
			w.Header().Set("Retry-After", "0")
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		w.Write([]byte(`{"status": "healthy"}`))
	}))
	defer srv.Close()
	ok, err := New(srv.URL, WithKey("k")).Health(context.Background())
	if err != nil || !ok || calls != 2 {
		t.Fatalf("ok=%v err=%v calls=%d", ok, err, calls)
	}
}

// After three retries a 503 is ErrBusy.
func TestBusyAfterRetries(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Retry-After", "0")
		w.WriteHeader(http.StatusServiceUnavailable)
		w.Write([]byte(`{"ok": false, "message": "Starting up", "data": null, "detail": "Starting up"}`))
	}))
	defer srv.Close()
	_, err := New(srv.URL).Health(context.Background())
	var e *Error
	if !errors.Is(err, ErrBusy) || !errors.As(err, &e) || e.Status != 503 || e.Message != "Starting up" || calls != 4 {
		t.Fatalf("err=%v calls=%d", err, calls)
	}
}

// A refusal without the JSON envelope (a proxy's HTML page) gets
// "<status> from <url>" as its message.
func TestMessageFallback(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		w.Write([]byte("<html>bad gateway</html>"))
	}))
	defer srv.Close()
	_, err := New(srv.URL).Collections(context.Background())
	var e *Error
	if !errors.As(err, &e) || e.Status != 502 || e.Message != "502 from "+srv.URL+pathCollections {
		t.Fatalf("%v", err)
	}
	for _, kind := range []error{ErrAuth, ErrForbidden, ErrNotFound, ErrConflict, ErrTooLarge, ErrInvalid, ErrBusy} {
		if errors.Is(err, kind) {
			t.Errorf("a 502 is not %v", kind)
		}
	}
}

// A connection failure is not retried and names the address.
func TestConnectionFailure(t *testing.T) {
	c := New("http://127.0.0.1:1", WithTimeout(2*time.Second))
	_, err := c.Health(context.Background())
	if err == nil || !strings.Contains(err.Error(), "127.0.0.1:1") {
		t.Fatalf("%v", err)
	}
}

// Ids are escaped one segment at a time, "/" included.
func TestPathEscaping(t *testing.T) {
	if got, err := join("api", "v1", "collections", "c", "documents", "a/b c.md"); err != nil || got != "/api/v1/collections/c/documents/a%2Fb%20c.md" {
		t.Fatal(got, err)
	}
}
