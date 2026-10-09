// Package vectrixdb is the Go client for a VectrixDB server.
//
// It offers the same calls as the other VectrixDB SDKs: connect with a key or
// a sign-in token, create collections, add documents or texts, search with
// citations, and keep up with feeds and pages. Every call takes a
// context.Context first and returns a typed result or an *Error.
//
// The module is github.com/knowusuboaky/VectrixDB/sdk/go/v2; the package is
// vectrixdb:
//
//	import vectrixdb "github.com/knowusuboaky/VectrixDB/sdk/go/v2"
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
//
// A Client never prints its key or token: its String and GoString name only
// the address and the header the credential goes in.
type Client struct {
	base         string
	key          string
	token        string
	keyHeader    string
	tokenHeader  string
	headers      map[string]string
	prefix       string
	gatewayPaths map[string]string
	allowHTTP    bool
	agent        string
	timeout      time.Duration
	http         *http.Client
	// err is the first refusal of the client's settings; every call returns it.
	err error
}

// Option configures a Client.
type Option func(*Client)

// WithUserAgent puts a wrapper's name and version, "acme-vectors/1.4", before
// this client's own in User-Agent, so a gateway's log says which tool called.
func WithUserAgent(name string) Option {
	return func(c *Client) { c.agent = strings.TrimSpace(name) }
}

// WithKey sends the API key in the api-key header, or the one WithKeyHeader
// names.
func WithKey(key string) Option { return func(c *Client) { c.key = key } }

// WithToken sends a company sign-in token as Authorization: Bearer <token>
// in place of a key; WithTokenHeader names another header.
func WithToken(token string) Option { return func(c *Client) { c.token = token } }

// WithTimeout sets the per-request timeout. The default is 30 seconds.
func WithTimeout(d time.Duration) Option { return func(c *Client) { c.timeout = d } }

// WithHTTPClient sends requests through the given http.Client: the place for
// a private CA bundle (Transport's TLSClientConfig.RootCAs) and a client
// certificate (TLSClientConfig.Certificates). The client never follows a
// redirect, whatever h's own CheckRedirect would do (Go would carry the key
// header to wherever it points), so a shallow copy of h whose CheckRedirect
// refuses every redirect is used; h itself is not changed.
func WithHTTPClient(h *http.Client) Option { return func(c *Client) { c.http = h } }

// New returns a client for the server at url, for example
// New("https://vectors.example.com", WithKey(key)).
//
// New refuses a key or token for a plain http:// address unless the host is
// this machine (localhost, 127.0.0.0/8, ::1) or WithAllowHTTP is given, and
// refuses header names and gateway paths that do not read. Since New returns
// no error, the client keeps the refusal: Err reports it, and every call
// returns it, wrapping ErrConfig.
func New(url string, opts ...Option) *Client {
	c := &Client{
		base:        strings.TrimRight(url, "/"),
		keyHeader:   DefaultKeyHeader,
		tokenHeader: DefaultTokenHeader,
		timeout:     30 * time.Second,
	}
	for _, opt := range opts {
		opt(c)
	}
	if c.http == nil {
		// No Transport: http.DefaultTransport, which honours HTTPS_PROXY,
		// HTTP_PROXY and NO_PROXY and always checks certificates.
		c.http = &http.Client{CheckRedirect: noRedirects}
	} else {
		// The caller's CheckRedirect is replaced, never called.
		h := *c.http
		h.CheckRedirect = noRedirects
		c.http = &h
	}
	if !headerValueOK(c.key) {
		c.refuse(fmt.Errorf("%w: the key holds a character a header cannot carry", ErrConfig))
	}
	if !headerValueOK(c.token) {
		c.refuse(fmt.Errorf("%w: the token holds a character a header cannot carry", ErrConfig))
	}
	if !headerValueOK(c.agent) {
		c.refuse(fmt.Errorf("%w: the user agent holds a character a header cannot carry", ErrConfig))
	}
	c.refuse(c.checkUserinfo())
	c.refuse(c.checkPlainHTTP())
	return c
}

// noRedirects hands a 3xx back as it is: following it would send the key
// to wherever it points.
func noRedirects(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

// Err is the refusal of the client's settings New kept, or nil. Every call
// returns the same error.
func (c *Client) Err() error { return c.err }

// String names the address and where the credential goes, never the
// credential itself.
func (c Client) String() string {
	cred := "no key"
	switch {
	case c.token != "":
		cred = "token in " + c.tokenHeader
	case c.key != "":
		cred = "key in " + c.keyHeader
	}
	return fmt.Sprintf("vectrixdb.Client(%s, %s)", c.base, cred)
}

// GoString is String, so %#v does not print the credential either.
func (c Client) GoString() string { return c.String() }

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
	if c.err != nil {
		return nil, c.err
	}
	full := c.base + c.address(r.path)
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
	if out.status >= 300 && out.status < 400 {
		return nil, redirectError(out.status, full, out.header.Get("Location"))
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
	req.Header.Set("Accept", "application/json, text/markdown, text/plain")
	// Extra headers first, so the request's own, the user-agent and the
	// credential header are set after them and cannot be replaced.
	for k, v := range c.headers {
		req.Header.Set(k, v)
	}
	if r.contentType != "" {
		req.Header.Set("Content-Type", r.contentType)
	}
	for k, v := range r.headers {
		req.Header.Set(k, v)
	}
	if c.agent != "" {
		req.Header.Set("User-Agent", c.agent+" "+userAgent)
	} else {
		req.Header.Set("User-Agent", userAgent)
	}
	if c.token != "" {
		req.Header.Set(c.tokenHeader, "Bearer "+c.token)
	} else if c.key != "" {
		req.Header.Set(c.keyHeader, c.key)
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
// travels as %2F and reaches the server as one id. An empty segment, "." or
// ".." is refused, wrapping ErrConfig, before anything is sent: a URL's dot
// segments are collapsed on the way (escaping the dots does not help), so
// DeleteDocument(ctx, "c", "..") would otherwise delete collection c.
func join(parts ...string) (string, error) {
	var b strings.Builder
	for _, p := range parts {
		if p == "" || p == "." || p == ".." {
			return "", fmt.Errorf("%w: %q cannot be a collection name, document id or source id", ErrConfig, p)
		}
		b.WriteByte('/')
		b.WriteString(url.PathEscape(p))
	}
	return b.String(), nil
}
