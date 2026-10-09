// Package vectrixdb is the client for a VectrixDB server: search a collection,
// add to it and read what it holds, as one caller.
//
//	client, err := vectrixdb.Connect("https://vectors.company.com", vectrixdb.WithKey(os.Getenv("VECTRIXDB_KEY")))
//	db := client.Collection("handbook")
//	found, err := db.Search(ctx, "how long do refunds take", &vectrixdb.SearchOptions{Limit: 5})
//	for _, r := range found.Results {
//		fmt.Println(r.ReadableCitation, r.Text)
//	}
//
// Every call is made to the server's REST API as one caller, a key or a
// person's access token, and the server decides it as it decides any request.
// A refusal comes back as a *ServerRefused, whose Kind says which; a busy
// server's 429 and 503 are asked again after what Retry-After says.
//
// A key or a token is only sent over https://, or to this machine over
// http://; WithAllowHTTP lifts that for a network you trust. A redirect is
// reported, never followed, so a key never goes to a second host. The
// environment's proxy settings (HTTPS_PROXY, NO_PROXY) are honoured, and
// SSL_CERT_FILE names a company's certificate authority on Linux.
//
// It needs nothing beyond the standard library.
//
// Author: Kwadwo Daddy Nyame Owusu - Boakye
package vectrixdb

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
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

// Version is the version of VectrixDB this client was written for.
const Version = "2.2.0"

// Mode is a search mode.
type Mode string

// The search modes: meaning and exact words, meaning, exact words, meaning re-ordered.
const (
	Hybrid  Mode = "hybrid"
	Dense   Mode = "dense"
	Keyword Mode = "keyword"
	Rerank  Mode = "rerank"
)

var modes = map[Mode]struct {
	route string
	extra map[string]any
}{
	Hybrid:  {"text-hybrid-search", nil},
	Dense:   {"text-search", nil},
	Keyword: {"keyword-search", nil},
	Rerank:  {"text-search", map[string]any{"rerank": true}},
}

// Operations is every request this client makes, as method and path in the
// server's OpenAPI document. A test holds it to docs/reference/openapi.json.
var Operations = [][2]string{
	{"GET", "/health"},
	{"GET", "/ready"},
	{"GET", "/api/v1/whoami"},
	{"GET", "/api/v1/collections"},
	{"POST", "/api/v2/collections"},
	{"GET", "/api/v1/collections/{name}"},
	{"DELETE", "/api/v1/collections/{name}"},
	{"POST", "/api/v1/collections/{name}/text-search"},
	{"POST", "/api/v1/collections/{name}/text-hybrid-search"},
	{"POST", "/api/v1/collections/{name}/keyword-search"},
	{"POST", "/api/v1/collections/{name}/similar"},
	{"POST", "/api/v1/collections/{name}/text-upsert"},
	{"POST", "/api/v1/collections/{name}/documents"},
	{"GET", "/api/v1/collections/{name}/documents"},
	{"GET", "/api/v1/collections/{name}/documents/{doc_id}"},
	{"DELETE", "/api/v1/collections/{name}/documents/{doc_id}"},
	{"GET", "/api/v1/collections/{name}/sources"},
	{"POST", "/api/v1/collections/{name}/sources"},
	{"POST", "/api/v1/collections/{name}/sources/refresh"},
}

// ---------------------------------------------------------------- refusals

// Kind says which refusal a ServerRefused is.
type Kind int

// The kinds of no a server gives.
const (
	Refused          Kind = iota // anything else
	SignInRequired               // 401: no key or token, or one it does not take
	PermissionDenied             // 403: the role, the key's collections, or a policy
	NotFound                     // 404: not there, or not this caller's to see
	Rejected                     // 400, 422 or another 4xx: the request is wrong; Said says what to change
	Busy                         // 429 or 503, still, after asking again
)

// ServerRefused is the server saying no: its HTTP status and its own words.
type ServerRefused struct {
	Status int
	Said   string
	Kind   Kind
}

func (e *ServerRefused) Error() string {
	return fmt.Sprintf("the server answered %d: %s", e.Status, e.Said)
}

// Is lets errors.Is(err, vectrixdb.ErrNotFound) and the rest match by kind.
func (e *ServerRefused) Is(target error) bool {
	t, ok := target.(*ServerRefused)
	return ok && t.Status == 0 && t.Kind == e.Kind
}

// Sentinels for errors.Is.
var (
	ErrSignInRequired   = &ServerRefused{Kind: SignInRequired}
	ErrPermissionDenied = &ServerRefused{Kind: PermissionDenied}
	ErrNotFound         = &ServerRefused{Kind: NotFound}
	ErrRejected         = &ServerRefused{Kind: Rejected}
	ErrBusy             = &ServerRefused{Kind: Busy}
)

func refusal(status int, said string) *ServerRefused {
	kind := Refused
	switch {
	case status == 401:
		kind = SignInRequired
	case status == 403:
		kind = PermissionDenied
	case status == 404:
		kind = NotFound
	case status == 429 || status == 503:
		kind = Busy
	case status >= 400 && status < 500:
		kind = Rejected
	}
	return &ServerRefused{Status: status, Said: said, Kind: kind}
}

// ---------------------------------------------------------------- the client

// Option sets how a Client calls.
type Option func(*Client)

// WithKey calls as an API key, sent in the "api-key" header.
func WithKey(key string) Option { return func(c *Client) { c.key = key } }

// WithKeyHeader names the header a key goes in, the server's VECTRIXDB_KEY_HEADER.
func WithKeyHeader(header string) Option { return func(c *Client) { c.keyHeader = header } }

// WithToken calls as an access token from the company's identity provider.
func WithToken(token string) Option {
	return func(c *Client) { c.token = func(context.Context) (string, error) { return token, nil } }
}

// WithTokenSource calls as a token fetched before each request, for one that expires.
func WithTokenSource(source func(context.Context) (string, error)) Option {
	return func(c *Client) { c.token = source }
}

// WithHTTPClient uses an http.Client of your own, for a proxy, a timeout or a test.
// It is then yours to secure: its redirects and its TLS are as you set them.
func WithHTTPClient(h *http.Client) Option {
	return func(c *Client) { c.http, c.ownHTTP = h, true }
}

// WithHeader sends a header with every request, such as a gateway's
// subscription key. Not the caller's own: that is WithKey or WithToken.
func WithHeader(name, value string) Option {
	return func(c *Client) {
		if c.extra == nil {
			c.extra = map[string]string{}
		}
		c.extra[name] = value
	}
}

// WithUserAgent puts a wrapper's name and version before this client's in User-Agent.
func WithUserAgent(agent string) Option { return func(c *Client) { c.userAgent = agent } }

// WithAllowHTTP lets the key or token go to another machine over plain http://.
// Off: for a network you trust.
func WithAllowHTTP() Option { return func(c *Client) { c.allowHTTP = true } }

// WithRetries sets how many times a busy answer or a dropped connection is asked again. 3.
func WithRetries(n int) Option { return func(c *Client) { c.retries = n } }

// Client is a VectrixDB server, as one caller.
type Client struct {
	URL       string
	key       string
	keyHeader string
	token     func(context.Context) (string, error)
	http      *http.Client
	retries   int
	sleep     func(context.Context, time.Duration) error
	ownHTTP   bool
	allowHTTP bool
	extra     map[string]string
	userAgent string
}

// Connect is a VectrixDB server at url.
func Connect(rawURL string, options ...Option) (*Client, error) {
	c := &Client{
		URL:       strings.TrimRight(rawURL, "/"),
		keyHeader: "api-key",
		http: &http.Client{
			Timeout: 60 * time.Second,
			// Never followed, so a key never goes to a second host: send reports where instead.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		retries: 3,
		sleep:   sleepContext,
	}
	for _, o := range options {
		o(c)
	}
	if c.key != "" && c.token != nil {
		return nil, errors.New("vectrixdb: give a key or a token, not both: the server takes one caller per request")
	}
	for name := range c.extra {
		if strings.EqualFold(name, "Authorization") || strings.EqualFold(name, c.keyHeader) {
			return nil, fmt.Errorf("vectrixdb: %s names the caller: give it with WithKey or WithToken, not WithHeader", name)
		}
	}
	parsed, err := url.Parse(c.URL)
	if err != nil {
		return nil, fmt.Errorf("vectrixdb: %w", err)
	}
	if (parsed.Scheme != "https" && parsed.Scheme != "http") || parsed.Host == "" {
		return nil, fmt.Errorf("vectrixdb: the server's address must start https:// (or http://): %q", rawURL)
	}
	if parsed.User != nil {
		return nil, errors.New("vectrixdb: put the key in WithKey, not in the address, where logs and history keep it")
	}
	sendsACaller := c.key != "" || c.token != nil
	if parsed.Scheme == "http" && sendsACaller && !c.allowHTTP && !c.ownHTTP && !onThisMachine(parsed.Hostname()) {
		return nil, fmt.Errorf(
			"vectrixdb: %s is reached over http://, which would send the key in clear text: use https://, or WithAllowHTTP on a network you trust",
			parsed.Hostname(),
		)
	}
	return c, nil
}

func onThisMachine(host string) bool {
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	return host == "localhost" || host == "::1" || strings.HasSuffix(host, ".localhost") ||
		strings.HasPrefix(host, "127.")
}

func sleepContext(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

type request struct {
	method  string
	path    string
	json    any
	body    []byte
	headers map[string]string
	query   url.Values
}

func wait(resp *http.Response, attempt int) time.Duration {
	backoff := time.Duration(500*(1<<attempt)) * time.Millisecond
	if resp != nil {
		if s, err := strconv.ParseFloat(resp.Header.Get("Retry-After"), 64); err == nil && s >= 0 {
			if asked := time.Duration(s * float64(time.Second)); asked > backoff || s == 0 {
				backoff = asked
			}
		}
	}
	if backoff > 30*time.Second {
		backoff = 30 * time.Second
	}
	return backoff
}

func said(body []byte, status int) string {
	var parsed map[string]any
	if json.Unmarshal(body, &parsed) == nil {
		for _, k := range []string{"detail", "message", "error"} {
			if s, ok := parsed[k].(string); ok && s != "" {
				return s
			}
		}
	}
	text := strings.TrimSpace(string(body))
	if len(text) > 300 {
		text = text[:300]
	}
	if text == "" {
		text = fmt.Sprintf("HTTP %d", status)
	}
	return text
}

func (c *Client) send(ctx context.Context, r request) ([]byte, error) {
	for attempt := 0; ; attempt++ {
		var body io.Reader
		var payload []byte
		if r.json != nil {
			var err error
			if payload, err = json.Marshal(r.json); err != nil {
				return nil, err
			}
		} else {
			payload = r.body
		}
		if payload != nil {
			body = bytes.NewReader(payload)
		}
		target := c.URL + r.path
		if len(r.query) > 0 {
			target += "?" + r.query.Encode()
		}
		req, err := http.NewRequestWithContext(ctx, r.method, target, body)
		if err != nil {
			return nil, err
		}
		if r.json != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		for k, v := range c.extra {
			req.Header.Set(k, v)
		}
		agent := "vectrixdb-go/" + Version
		if c.userAgent != "" {
			agent = c.userAgent + " " + agent
		}
		req.Header.Set("User-Agent", agent)
		for k, v := range r.headers {
			req.Header.Set(k, v)
		}
		if c.key != "" {
			req.Header.Set(c.keyHeader, c.key)
		} else if c.token != nil {
			token, err := c.token(ctx)
			if err != nil {
				return nil, fmt.Errorf("vectrixdb: the token source: %w", err)
			}
			req.Header.Set("Authorization", "Bearer "+token)
		}
		resp, err := c.http.Do(req)
		if err != nil {
			if attempt >= c.retries || ctx.Err() != nil {
				return nil, err
			}
			if err := c.sleep(ctx, wait(nil, attempt)); err != nil {
				return nil, err
			}
			continue
		}
		content, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()
		if readErr != nil {
			return nil, readErr
		}
		switch resp.StatusCode {
		case 429, 502, 503, 504:
			if attempt < c.retries {
				if err := c.sleep(ctx, wait(resp, attempt)); err != nil {
					return nil, err
				}
				continue
			}
		}
		if resp.StatusCode >= 300 && resp.StatusCode < 400 {
			where := resp.Header.Get("Location")
			if where == "" {
				where = "another address"
			}
			return nil, &ServerRefused{
				Status: resp.StatusCode,
				Said:   "the server sent this request to " + where + "; connect to that address instead",
				Kind:   Refused,
			}
		}
		if resp.StatusCode >= 400 {
			return nil, refusal(resp.StatusCode, said(content, resp.StatusCode))
		}
		return content, nil
	}
}

// data decodes an answer's payload, the "data" of the server's envelope or the body as it came, into out.
func data(content []byte, out any) error {
	if out == nil || len(content) == 0 {
		return nil
	}
	var envelope map[string]json.RawMessage
	if json.Unmarshal(content, &envelope) == nil {
		if inner, ok := envelope["data"]; ok {
			only := true
			for k := range envelope {
				if k != "ok" && k != "data" && k != "message" && k != "error" {
					only = false
				}
			}
			if only {
				return json.Unmarshal(inner, out)
			}
		}
	}
	return json.Unmarshal(content, out)
}

func collectionPath(name string, rest string) string {
	return "/api/v1/collections/" + url.PathEscape(name) + rest
}

func documentPath(name, docID string) string {
	parts := strings.Split(docID, "/")
	for i, p := range parts {
		parts[i] = url.PathEscape(p)
	}
	return collectionPath(name, "/documents/"+strings.Join(parts, "/"))
}

// Health says whether the server is up: /health, which needs no caller.
func (c *Client) Health(ctx context.Context) (map[string]any, error) {
	content, err := c.send(ctx, request{method: "GET", path: "/health"})
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, data(content, &out)
}

// Ready says whether the server's models are loaded and it takes searches: /ready.
func (c *Client) Ready(ctx context.Context) (bool, error) {
	_, err := c.send(ctx, request{method: "GET", path: "/ready"})
	if errors.Is(err, ErrBusy) {
		return false, nil
	}
	return err == nil, err
}

// WhoAmI is who this client is on the server: how it came in, its role, what it may do, what it reaches.
func (c *Client) WhoAmI(ctx context.Context) (map[string]any, error) {
	content, err := c.send(ctx, request{method: "GET", path: "/api/v1/whoami"})
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, data(content, &out)
}

// Collections are the collections this caller reaches, each with its size.
func (c *Client) Collections(ctx context.Context) ([]map[string]any, error) {
	content, err := c.send(ctx, request{method: "GET", path: "/api/v1/collections"})
	if err != nil {
		return nil, err
	}
	var wrapped struct {
		Collections []map[string]any `json:"collections"`
	}
	if err := data(content, &wrapped); err == nil && wrapped.Collections != nil {
		return wrapped.Collections, nil
	}
	var list []map[string]any
	return list, data(content, &list)
}

// CreateOptions shape a new collection.
type CreateOptions struct {
	// DenseOnly makes a collection searched by meaning alone. Left false it is hybrid.
	DenseOnly   bool
	Description string
	Dimension   int    // 384
	Metric      string // cosine
}

// CreateCollection makes a collection, searchable by meaning and exact words unless DenseOnly.
func (c *Client) CreateCollection(ctx context.Context, name string, o *CreateOptions) (*Collection, error) {
	if o == nil {
		o = &CreateOptions{}
	}
	dimension, metric, tag := o.Dimension, o.Metric, "hybrid"
	if dimension == 0 {
		dimension = 384
	}
	if metric == "" {
		metric = "cosine"
	}
	if o.DenseOnly {
		tag = "dense"
	}
	body := map[string]any{
		"name": name, "dimension": dimension, "metric": metric,
		"enable_text_index": !o.DenseOnly, "tags": []string{tag},
	}
	if o.Description != "" {
		body["description"] = o.Description
	}
	if _, err := c.send(ctx, request{method: "POST", path: "/api/v2/collections", json: body}); err != nil {
		return nil, err
	}
	return c.Collection(name), nil
}

// DeleteCollection deletes a collection and everything in it, for good.
func (c *Client) DeleteCollection(ctx context.Context, name string) error {
	_, err := c.send(ctx, request{method: "DELETE", path: collectionPath(name, "")})
	return err
}

// Collection is one collection.
func (c *Client) Collection(name string) *Collection { return &Collection{Client: c, Name: name} }

// ---------------------------------------------------------------- a collection

// Collection is one collection on a server: search it, add to it, read what it holds.
type Collection struct {
	Client *Client
	Name   string
}

// Result is one search result, as the server judged it for this caller.
type Result struct {
	ID        string         `json:"id"`
	Text      string         `json:"text"`
	Score     float64        `json:"score"`
	Relevance *float64       `json:"relevance"`
	Kind      string         `json:"relevance_kind"`
	Metadata  map[string]any `json:"metadata"`
	MatchedBy []string       `json:"matched_by"`
	// Citation is where it came from: report.pdf#page=3.
	Citation string `json:"-"`
	// ReadableCitation is the same place as a person reads it: report.pdf, p. 3.
	ReadableCitation string `json:"-"`
}

// SearchResults are a search's results, best first.
type SearchResults struct {
	Results    []Result `json:"results"`
	DecisionID string   `json:"decision_id"`
	Query      string   `json:"-"`
	Mode       string   `json:"-"`
}

// Top is the best result, or nil when nothing matched.
func (s *SearchResults) Top() *Result {
	if len(s.Results) == 0 {
		return nil
	}
	return &s.Results[0]
}

func (s *SearchResults) finish(query, mode string) {
	s.Query, s.Mode = query, mode
	for i := range s.Results {
		r := &s.Results[i]
		if r.Text == "" {
			if t, ok := r.Metadata["text"].(string); ok {
				r.Text = t
			}
		}
		r.Citation = r.ID
		if s, ok := r.Metadata["_vx_citation"].(string); ok && s != "" {
			r.Citation = s
		}
		r.ReadableCitation = r.Citation
		if s, ok := r.Metadata["_vx_readable_citation"].(string); ok && s != "" {
			r.ReadableCitation = s
		}
	}
}

// SearchOptions narrow a search.
type SearchOptions struct {
	Limit  int            // 10
	Mode   Mode           // Hybrid
	Filter map[string]any // {"department": "legal"}
}

// Search searches as this caller.
func (col *Collection) Search(ctx context.Context, query string, o *SearchOptions) (*SearchResults, error) {
	if o == nil {
		o = &SearchOptions{}
	}
	mode := o.Mode
	if mode == "" {
		mode = Hybrid
	}
	route, ok := modes[mode]
	if !ok {
		return nil, fmt.Errorf("vectrixdb: mode is hybrid, dense, keyword or rerank, not %q", mode)
	}
	limit := o.Limit
	if limit == 0 {
		limit = 10
	}
	body := map[string]any{"query_text": query, "limit": limit}
	for k, v := range route.extra {
		body[k] = v
	}
	if o.Filter != nil {
		body["filter"] = o.Filter
	}
	content, err := col.Client.send(ctx, request{method: "POST", path: collectionPath(col.Name, "/"+route.route), json: body})
	if err != nil {
		return nil, err
	}
	var found SearchResults
	if err := data(content, &found); err != nil {
		return nil, err
	}
	found.finish(query, string(mode))
	return &found, nil
}

// Similar finds the chunks most like one, by the id a search result gives; never that one.
func (col *Collection) Similar(ctx context.Context, id string, o *SearchOptions) (*SearchResults, error) {
	if o == nil {
		o = &SearchOptions{}
	}
	limit := o.Limit
	if limit == 0 {
		limit = 10
	}
	body := map[string]any{"id": id, "limit": limit}
	if o.Filter != nil {
		body["filter"] = o.Filter
	}
	content, err := col.Client.send(ctx, request{method: "POST", path: collectionPath(col.Name, "/similar"), json: body})
	if err != nil {
		return nil, err
	}
	var found SearchResults
	if err := data(content, &found); err != nil {
		return nil, err
	}
	found.finish(id, "similar")
	return &found, nil
}

// Record is a text to add, with its id and metadata, both optional.
type Record struct {
	ID       string
	Text     string
	Metadata map[string]any
}

func newID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// Add adds records, the server embedding each, and says how many were written.
func (col *Collection) Add(ctx context.Context, records ...Record) (int, error) {
	points := make([]map[string]any, 0, len(records))
	for _, r := range records {
		id := r.ID
		if id == "" {
			id = newID()
		}
		point := map[string]any{"id": id, "text": r.Text}
		if len(r.Metadata) > 0 {
			point["payload"] = r.Metadata
		}
		points = append(points, point)
	}
	content, err := col.Client.send(ctx, request{method: "POST", path: collectionPath(col.Name, "/text-upsert"), json: map[string]any{"points": points}})
	if err != nil {
		return 0, err
	}
	var out struct {
		Added *int `json:"added"`
	}
	if err := data(content, &out); err == nil && out.Added != nil {
		return *out.Added, nil
	}
	return len(records), nil
}

// DocumentOptions describe a document being added.
type DocumentOptions struct {
	DocID     string
	Filename  string
	Metadata  map[string]any
	Chunk     string // recursive, sentence, markdown or fixed
	ChunkSize int
	Overlap   int
}

// AddDocument reads, cuts and indexes a document's bytes, and returns the server's account of it.
func (col *Collection) AddDocument(ctx context.Context, content []byte, o *DocumentOptions) (map[string]any, error) {
	if o == nil {
		o = &DocumentOptions{}
	}
	name := o.Filename
	if name == "" {
		name = o.DocID
	}
	if name == "" {
		name = "document"
	}
	if !strings.Contains(name, ".") {
		name += ".md"
	}
	query := url.Values{}
	if o.DocID != "" {
		query.Set("doc_id", o.DocID)
	}
	if o.Metadata != nil {
		encoded, err := json.Marshal(o.Metadata)
		if err != nil {
			return nil, err
		}
		query.Set("metadata", string(encoded))
	}
	if o.Chunk != "" {
		query.Set("chunk", o.Chunk)
	}
	if o.ChunkSize > 0 {
		query.Set("chunk_size", strconv.Itoa(o.ChunkSize))
	}
	if o.Overlap > 0 {
		query.Set("overlap", strconv.Itoa(o.Overlap))
	}
	answer, err := col.Client.send(ctx, request{
		method: "POST", path: collectionPath(col.Name, "/documents"), body: content, query: query,
		headers: map[string]string{"Content-Type": "application/octet-stream", "X-Filename": url.PathEscape(name)},
	})
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, data(answer, &out)
}

// Documents are the documents it holds, on a server that keeps them.
func (col *Collection) Documents(ctx context.Context) ([]map[string]any, error) {
	content, err := col.Client.send(ctx, request{method: "GET", path: collectionPath(col.Name, "/documents")})
	if err != nil {
		return nil, err
	}
	var out struct {
		Documents []map[string]any `json:"documents"`
	}
	return out.Documents, data(content, &out)
}

// Document is a document's Markdown, as it was indexed.
func (col *Collection) Document(ctx context.Context, docID string) (string, error) {
	content, err := col.Client.send(ctx, request{method: "GET", path: documentPath(col.Name, docID)})
	return string(content), err
}

// DeleteDocument deletes a document and every chunk of it, for good, and says how many chunks went.
func (col *Collection) DeleteDocument(ctx context.Context, docID string) (int, error) {
	content, err := col.Client.send(ctx, request{method: "DELETE", path: documentPath(col.Name, docID)})
	if err != nil {
		return 0, err
	}
	var out struct {
		Removed int `json:"chunks_removed"`
	}
	return out.Removed, data(content, &out)
}

// Describe is its size, how it is searched, and the metadata fields it can be filtered on.
func (col *Collection) Describe(ctx context.Context) (map[string]any, error) {
	content, err := col.Client.send(ctx, request{method: "GET", path: collectionPath(col.Name, "")})
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, data(content, &out)
}

// Sources are the feeds and pages it keeps up with.
func (col *Collection) Sources(ctx context.Context) ([]map[string]any, error) {
	content, err := col.Client.send(ctx, request{method: "GET", path: collectionPath(col.Name, "/sources")})
	if err != nil {
		return nil, err
	}
	var out struct {
		Sources []map[string]any `json:"sources"`
	}
	return out.Sources, data(content, &out)
}

// AddSource keeps up with a feed or a page, read every `every` (30m, 6h, 1d). Nothing is written until a refresh.
func (col *Collection) AddSource(ctx context.Context, address, every, kind string) (map[string]any, error) {
	if every == "" {
		every = "6h"
	}
	body := map[string]any{"address": address, "every": every}
	if kind != "" {
		body["kind"] = kind
	}
	content, err := col.Client.send(ctx, request{method: "POST", path: collectionPath(col.Name, "/sources"), json: body})
	if err != nil {
		return nil, err
	}
	var out struct {
		Source map[string]any `json:"source"`
	}
	return out.Source, data(content, &out)
}

// RefreshSources reads its sources now: one, by id or address, or every one that is due; force reads every one.
func (col *Collection) RefreshSources(ctx context.Context, source string, force bool) (map[string]any, error) {
	body := map[string]any{"force": force}
	if source != "" {
		body["source"] = source
	}
	content, err := col.Client.send(ctx, request{method: "POST", path: collectionPath(col.Name, "/sources/refresh"), json: body})
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, data(content, &out)
}
