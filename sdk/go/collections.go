package vectrixdb

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
)

const (
	pathHealth        = "/health"
	pathReady         = "/ready"
	pathWhoami        = "/auth/me"
	pathCollections   = "/api/v1/collections"
	pathCollectionsV2 = "/api/v2/collections"
)

// Health reports true when the process answers.
func (c *Client) Health(ctx context.Context) (bool, error) {
	if _, err := c.getJSON(ctx, pathHealth, nil); err != nil {
		return false, err
	}
	return true, nil
}

// Ready reports true when the models are loaded. A server without /ready
// (404) is asked /health instead.
func (c *Client) Ready(ctx context.Context) (bool, error) {
	_, err := c.getJSON(ctx, pathReady, nil)
	if errors.Is(err, ErrNotFound) {
		return c.Health(ctx)
	}
	// 503 after the retries: the models are still loading.
	if errors.Is(err, ErrBusy) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// Whoami returns the data object of GET /auth/me as the server sends it.
func (c *Client) Whoami(ctx context.Context) (map[string]any, error) {
	body, err := c.getJSON(ctx, pathWhoami, nil)
	if err != nil {
		return nil, err
	}
	data, err := dataOf(body, pathWhoami)
	if err != nil {
		return nil, err
	}
	var who map[string]any
	return who, decode(data, &who, nil)
}

// Collections lists the collections.
func (c *Client) Collections(ctx context.Context) ([]Collection, error) {
	body, err := c.getJSON(ctx, pathCollections, nil)
	if err != nil {
		return nil, err
	}
	// This list is sent at the top: {"collections": [...], "total": n}.
	var page struct {
		Collections []json.RawMessage `json:"collections"`
	}
	if err := decode(body, &page, nil); err != nil {
		return nil, err
	}
	out := make([]Collection, 0, len(page.Collections))
	for _, raw := range page.Collections {
		var col Collection
		if err := decode(raw, &col, &col.Raw); err != nil {
			return nil, err
		}
		out = append(out, col)
	}
	return out, nil
}

// Describe returns one collection.
func (c *Client) Describe(ctx context.Context, name string) (*Collection, error) {
	path, err := join("api", "v1", "collections", name)
	if err != nil {
		return nil, err
	}
	body, err := c.getJSON(ctx, path, nil)
	if err != nil {
		return nil, err
	}
	return collectionOf(body)
}

// CreateOptions configures CreateCollection. Zero values take the defaults:
// 384 dimensions, a text index, the cosine metric, no description.
type CreateOptions struct {
	Dimension   int
	TextIndex   *bool // nil means true; use Bool(false) to leave the text index out
	Metric      string
	Description string
}

// Bool returns a pointer to b, for CreateOptions.TextIndex.
func Bool(b bool) *bool { return &b }

// CreateCollection makes a collection and returns it. opts may be nil.
func (c *Client) CreateCollection(ctx context.Context, name string, opts *CreateOptions) (*Collection, error) {
	if opts == nil {
		opts = &CreateOptions{}
	}
	req := CreateCollectionRequestV2{Name: name, Dimension: opts.Dimension}
	if req.Dimension == 0 {
		req.Dimension = 384
	}
	textIndex := true
	if opts.TextIndex != nil {
		textIndex = *opts.TextIndex
	}
	req.EnableTextIndex = &textIndex
	if opts.Metric != "" {
		req.Metric = &opts.Metric
	}
	if opts.Description != "" {
		req.Description = &opts.Description
	}
	body, err := c.postJSON(ctx, pathCollectionsV2, req)
	if err != nil {
		return nil, err
	}
	return collectionOf(body)
}

// DeleteCollection removes a collection and everything in it.
func (c *Client) DeleteCollection(ctx context.Context, name string) error {
	path, err := join("api", "v1", "collections", name)
	if err != nil {
		return err
	}
	_, err = c.delete(ctx, path, nil)
	return err
}

func collectionOf(body []byte) (*Collection, error) {
	data, err := dataOf(body, pathCollections)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 || string(data) == "null" {
		return nil, fmt.Errorf("vectrixdb: the server sent no collection (%d bytes)", len(body))
	}
	var col Collection
	if err := decode(data, &col, &col.Raw); err != nil {
		return nil, err
	}
	return &col, nil
}
