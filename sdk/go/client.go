// Package vectrixdb is the Go client for a VectrixDB server.
//
// It offers the same calls as the other VectrixDB SDKs: connect with a key or
// a sign-in token, create collections, add documents or texts, search with
// citations, and keep up with feeds and pages. Every call takes a
// context.Context first and returns a typed result or an *Error.
package vectrixdb

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Version is this SDK's version, sent in every request's user-agent.
const Version = "2.2.0"

const userAgent = "vectrixdb-go/" + Version

// Client talks to one VectrixDB server. Make one with New.
type Client struct {
	base    string
	key     string
	token   string
	timeout time.Duration
	http    *http.Client
}

// Option configures a Client.
type Option func(*Client)

// WithKey sends the API key in the api-key header.
func WithKey(key string) Option { return func(c *Client) { c.key = key } }

// WithToken sends a company sign-in token as Authorization: Bearer <token>
// in place of a key.
func WithToken(token string) Option { return func(c *Client) { c.token = token } }

// WithTimeout sets the per-request timeout. The default is 30 seconds.
func WithTimeout(d time.Duration) Option { return func(c *Client) { c.timeout = d } }

// WithHTTPClient sends requests through the given http.Client.
func WithHTTPClient(h *http.Client) Option { return func(c *Client) { c.http = h } }

// New returns a client for the server at url, for example
// New("http://127.0.0.1:8000", WithKey(key)).
func New(url string, opts ...Option) *Client {
	c := &Client{
		base:    strings.TrimRight(url, "/"),
		timeout: 30 * time.Second,
		http:    http.DefaultClient,
	}
	for _, opt := range opts {
		opt(c)
	}
	return c
}

// URL is the server address the client was made with.
func (c *Client) URL() string { return c.base }

// request is one call to the server, before retries.
type request struct {
	method      string
	path        string // already escaped, starts with /
	query       url.Values
	body        []byte
	contentType string
	headers     map[string]string
}

// response is what came back once retries are done.
type response struct {
	status int
	body   []byte
	url    string
	header http.Header
}

// The retry schedule when the server sends no Retry-After.
var backoff = []time.Duration{time.Second, 2 * time.Second, 4 * time.Second}

// do sends a request, retrying a 429 or a 503 up to three times, and turns
// any other 4xx or 5xx into an *Error.
func (c *Client) do(ctx context.Context, r request) (*response, error) {
	full := c.base + r.path
	if len(r.query) > 0 {
		full += "?" + r.query.Encode()
	}
	var out *response
	for attempt := 0; ; attempt++ {
		var err error
		out, err = c.once(ctx, r, full)
		if err != nil {
			return nil, err
		}
		if (out.status != http.StatusTooManyRequests && out.status != http.StatusServiceUnavailable) || attempt >= len(backoff) {
			break
		}
		wait := backoff[attempt]
		if after, ok := retryAfter(out.header); ok {
			wait = after
		}
		select {
		case <-time.After(wait):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if out.status >= 400 {
		return nil, newError(out.status, full, out.body)
	}
	return out, nil
}

// retryAfter reads a Retry-After header given in seconds.
func retryAfter(h http.Header) (time.Duration, bool) {
	secs, err := strconv.ParseFloat(strings.TrimSpace(h.Get("Retry-After")), 64)
	if err != nil || secs < 0 {
		return 0, false
	}
	return time.Duration(secs * float64(time.Second)), true
}

func (c *Client) once(ctx context.Context, r request, full string) (*response, error) {
	if c.timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, c.timeout)
		defer cancel()
	}
	var body io.Reader
	if r.body != nil {
		body = bytes.NewReader(r.body)
	}
	req, err := http.NewRequestWithContext(ctx, r.method, full, body)
	if err != nil {
		return nil, fmt.Errorf("vectrixdb: %w", err)
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "application/json, text/markdown, text/plain")
	if r.contentType != "" {
		req.Header.Set("Content-Type", r.contentType)
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	} else if c.key != "" {
		req.Header.Set("api-key", c.key)
	}
	for k, v := range r.headers {
		req.Header.Set(k, v)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		// A connection failure is not retried; the address is in the message.
		var uerr *url.Error
		if errors.As(err, &uerr) {
			return nil, fmt.Errorf("vectrixdb: cannot reach %s: %w", c.base, uerr.Err)
		}
		return nil, fmt.Errorf("vectrixdb: cannot reach %s: %w", c.base, err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("vectrixdb: reading the reply from %s: %w", full, err)
	}
	return &response{status: resp.StatusCode, body: data, url: full, header: resp.Header}, nil
}

// The server answers in two envelopes. Most routes send
// {"ok": true, "message": ..., "data": ...} and the client returns data;
// a few list routes and the document routes send their fields at the top.
type envelope struct {
	OK      bool            `json:"ok"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

// getJSON sends a GET and returns the body.
func (c *Client) getJSON(ctx context.Context, path string, query url.Values) ([]byte, error) {
	out, err := c.do(ctx, request{method: http.MethodGet, path: path, query: query})
	if err != nil {
		return nil, err
	}
	return out.body, nil
}

// postJSON sends body as JSON and returns the reply's body.
func (c *Client) postJSON(ctx context.Context, path string, body any) ([]byte, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("vectrixdb: encoding the request: %w", err)
	}
	out, err := c.do(ctx, request{method: http.MethodPost, path: path, body: raw, contentType: "application/json"})
	if err != nil {
		return nil, err
	}
	return out.body, nil
}

// delete sends a DELETE and returns the reply's body.
func (c *Client) delete(ctx context.Context, path string, query url.Values) ([]byte, error) {
	out, err := c.do(ctx, request{method: http.MethodDelete, path: path, query: query})
	if err != nil {
		return nil, err
	}
	return out.body, nil
}

// dataOf unwraps the {"ok", "message", "data"} envelope.
func dataOf(body []byte, from string) (json.RawMessage, error) {
	var env envelope
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, fmt.Errorf("vectrixdb: %s did not answer with JSON: %w", from, err)
	}
	return env.Data, nil
}

// decode fills v from data and, when raw is given, keeps the whole object
// there too, so fields this SDK does not name stay reachable.
func decode(data []byte, v any, raw *map[string]any) error {
	if err := json.Unmarshal(data, v); err != nil {
		return fmt.Errorf("vectrixdb: unexpected reply shape: %w", err)
	}
	if raw != nil {
		_ = json.Unmarshal(data, raw)
	}
	return nil
}

// Path segments are escaped one by one, so a document id with a "/" in it
// travels as %2F and reaches the server as one id.
func join(parts ...string) string {
	var b strings.Builder
	for _, p := range parts {
		b.WriteByte('/')
		b.WriteString(url.PathEscape(p))
	}
	return b.String()
}
