package vectrixdb

import (
	"context"
	"encoding/json"
	"net/url"
)

// SourceOptions configures AddSource. Kind is "feed" or "page", or empty to
// let the server fetch the address once and tell. Every is how often it is
// read: "30m", "6h", "1d", or seconds; the server's default is "6h".
type SourceOptions struct {
	Kind  string
	Every string
}

// Sources lists the feeds and pages a collection keeps up with.
func (c *Client) Sources(ctx context.Context, collection string) ([]Source, error) {
	path, err := join("api", "v1", "collections", collection, "sources")
	if err != nil {
		return nil, err
	}
	body, err := c.getJSON(ctx, path, nil)
	if err != nil {
		return nil, err
	}
	// Sent at the top: {"sources": [...]}.
	var page struct {
		Sources []json.RawMessage `json:"sources"`
	}
	if err := decode(body, &page, nil); err != nil {
		return nil, err
	}
	out := make([]Source, 0, len(page.Sources))
	for _, raw := range page.Sources {
		var s Source
		if err := decode(raw, &s, &s.Raw); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, nil
}

// AddSource keeps a collection up with a feed or a page. Nothing is written
// until a refresh. opts may be nil.
func (c *Client) AddSource(ctx context.Context, collection, address string, opts *SourceOptions) (*Source, error) {
	if opts == nil {
		opts = &SourceOptions{}
	}
	req := AddSourceRequest{Address: address}
	if opts.Kind != "" {
		req.Kind = &opts.Kind
	}
	if opts.Every != "" {
		req.Every = &opts.Every
	}
	path, err := join("api", "v1", "collections", collection, "sources")
	if err != nil {
		return nil, err
	}
	body, err := c.postJSON(ctx, path, req)
	if err != nil {
		return nil, err
	}
	// A flat reply: {"ok": true, "source": {...}}.
	var reply struct {
		Source json.RawMessage `json:"source"`
	}
	if err := decode(body, &reply, nil); err != nil {
		return nil, err
	}
	var s Source
	if err := decode(reply.Source, &s, &s.Raw); err != nil {
		return nil, err
	}
	return &s, nil
}

// RefreshSources reads the sources that are due and reports what changed.
func (c *Client) RefreshSources(ctx context.Context, collection string) (*Refreshed, error) {
	path, err := join("api", "v1", "collections", collection, "sources", "refresh")
	if err != nil {
		return nil, err
	}
	body, err := c.postJSON(ctx, path, RefreshRequest{})
	if err != nil {
		return nil, err
	}
	// A flat reply: {"ok": true, "added": n, ...}.
	var r Refreshed
	if err := decode(body, &r, &r.Raw); err != nil {
		return nil, err
	}
	return &r, nil
}

// DeleteSource stops keeping up with a source. Its documents stay unless
// deleteDocuments is true.
func (c *Client) DeleteSource(ctx context.Context, collection, sourceID string, deleteDocuments bool) error {
	query := url.Values{}
	if deleteDocuments {
		query.Set("delete_documents", "true")
	}
	path, err := join("api", "v1", "collections", collection, "sources", sourceID)
	if err != nil {
		return err
	}
	_, err = c.delete(ctx, path, query)
	return err
}
