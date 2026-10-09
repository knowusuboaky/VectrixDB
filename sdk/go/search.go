package vectrixdb

import (
	"context"
	"encoding/json"
	"fmt"
)

// Search modes.
const (
	ModeMeaning = "meaning" // semantic search, the default
	ModeHybrid  = "hybrid"  // meaning and keywords together
)

// SearchOptions configures Search. Zero values mean limit 10, no filter,
// no reranking, ModeMeaning.
type SearchOptions struct {
	Limit  int
	Filter map[string]any // the simple form: {"team": "payroll", "price": {"$lt": 100}}
	Rerank bool
	Mode   string
}

// Text is one point for AddTexts.
type Text struct {
	ID       string
	Text     string
	Metadata map[string]any
}

// AddTexts embeds and stores the texts and returns how many were added.
func (c *Client) AddTexts(ctx context.Context, collection string, texts []Text) (int, error) {
	req := TextUpsertRequest{Points: make([]TextUpsertPoint, 0, len(texts))}
	for _, t := range texts {
		p := TextUpsertPoint{Id: t.ID, Text: t.Text}
		if t.Metadata != nil {
			// The SDK's metadata is the wire's payload.
			meta := t.Metadata
			p.Payload = &meta
		}
		req.Points = append(req.Points, p)
	}
	path, err := join("api", "v1", "collections", collection, "text-upsert")
	if err != nil {
		return 0, err
	}
	body, err := c.postJSON(ctx, path, req)
	if err != nil {
		return 0, err
	}
	data, err := dataOf(body, "text-upsert")
	if err != nil {
		return 0, err
	}
	var reply struct {
		Added int `json:"added"`
	}
	if err := decode(data, &reply, nil); err != nil {
		return 0, err
	}
	return reply.Added, nil
}

// Search runs a text query and returns the results, best first. opts may be nil.
func (c *Client) Search(ctx context.Context, collection, query string, opts *SearchOptions) ([]Result, error) {
	if opts == nil {
		opts = &SearchOptions{}
	}
	var route string
	switch opts.Mode {
	case "", ModeMeaning:
		route = "text-search"
	case ModeHybrid:
		route = "text-hybrid-search"
	default:
		return nil, fmt.Errorf("vectrixdb: unknown search mode %q (meaning or hybrid)", opts.Mode)
	}
	limit := opts.Limit
	if limit == 0 {
		limit = 10
	}
	rerank := opts.Rerank
	req := TextSearchRequest{QueryText: query, Limit: &limit, Rerank: &rerank}
	if opts.Filter != nil {
		filter := opts.Filter
		req.Filter = &filter
	}
	path, err := join("api", "v1", "collections", collection, route)
	if err != nil {
		return nil, err
	}
	body, err := c.postJSON(ctx, path, req)
	if err != nil {
		return nil, err
	}
	data, err := dataOf(body, route)
	if err != nil {
		return nil, err
	}
	var page struct {
		Results []json.RawMessage `json:"results"`
	}
	if err := decode(data, &page, nil); err != nil {
		return nil, err
	}
	out := make([]Result, 0, len(page.Results))
	for _, raw := range page.Results {
		var r Result
		if err := decode(raw, &r, &r.Raw); err != nil {
			return nil, err
		}
		if r.Text == "" {
			r.Text, _ = r.Metadata["text"].(string)
		}
		r.Citation = citationOf(r)
		out = append(out, r)
	}
	return out, nil
}

func citationOf(r Result) string {
	for _, key := range []string{"_vx_citation", "source"} {
		if s, ok := r.Metadata[key].(string); ok && s != "" {
			return s
		}
	}
	return r.ID
}
