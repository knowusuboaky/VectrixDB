package vectrixdb

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
)

// AddOptions configures AddDocument. Zero values are left to the server's
// defaults: the file name as the id, the markdown chunker, 1000-character
// chunks with 200 of overlap.
type AddOptions struct {
	DocID     string
	Metadata  map[string]any // put on every chunk
	Chunk     string         // the chunker: "markdown" by default
	ChunkSize int
	Overlap   int
}

// AddDocument reads the file from r, sends it under filename and returns
// what was written. The same DocID sent again replaces the document.
func (c *Client) AddDocument(ctx context.Context, collection string, r io.Reader, filename string, opts *AddOptions) (*Added, error) {
	if opts == nil {
		opts = &AddOptions{}
	}
	// The whole file is read first so a retried request can send it again.
	data, err := io.ReadAll(r)
	if err != nil {
		return nil, fmt.Errorf("vectrixdb: reading %s: %w", filename, err)
	}
	params := AddDocumentApiV1CollectionsNameDocumentsPostParams{}
	if opts.DocID != "" {
		params.DocId = &opts.DocID
	}
	if opts.Chunk != "" {
		params.Chunk = &opts.Chunk
	}
	if opts.ChunkSize != 0 {
		params.ChunkSize = &opts.ChunkSize
	}
	if opts.Overlap != 0 {
		params.Overlap = &opts.Overlap
	}
	if opts.Metadata != nil {
		// Metadata travels as a JSON string in the query.
		meta, err := json.Marshal(opts.Metadata)
		if err != nil {
			return nil, fmt.Errorf("vectrixdb: encoding metadata: %w", err)
		}
		s := string(meta)
		params.Metadata = &s
	}
	query := url.Values{}
	setString(query, "doc_id", params.DocId)
	setString(query, "chunk", params.Chunk)
	setInt(query, "chunk_size", params.ChunkSize)
	setInt(query, "overlap", params.Overlap)
	setString(query, "metadata", params.Metadata)

	out, err := c.do(ctx, request{
		method:      http.MethodPost,
		path:        join("api", "v1", "collections", collection, "documents"),
		query:       query,
		body:        data,
		contentType: "application/octet-stream",
		// The file's name goes in a header, URL-encoded.
		headers: map[string]string{"X-Filename": url.PathEscape(filename)},
	})
	if err != nil {
		return nil, err
	}
	// A flat reply: {"ok": true, "doc_id", "chunks", ...}, no data envelope.
	var added Added
	if err := decode(out.body, &added, &added.Raw); err != nil {
		return nil, err
	}
	return &added, nil
}

// Documents lists the kept documents of a collection.
func (c *Client) Documents(ctx context.Context, collection string) ([]Document, error) {
	body, err := c.getJSON(ctx, join("api", "v1", "collections", collection, "documents"), nil)
	if err != nil {
		return nil, err
	}
	// Sent at the top: {"documents": [...]}.
	var page struct {
		Documents []json.RawMessage `json:"documents"`
	}
	if err := decode(body, &page, nil); err != nil {
		return nil, err
	}
	out := make([]Document, 0, len(page.Documents))
	for _, raw := range page.Documents {
		var doc Document
		if err := decode(raw, &doc, &doc.Raw); err != nil {
			return nil, err
		}
		out = append(out, doc)
	}
	return out, nil
}

// OpenDocument returns the Markdown a document was indexed from.
func (c *Client) OpenDocument(ctx context.Context, collection, docID string) (string, error) {
	out, err := c.do(ctx, request{
		method: http.MethodGet,
		path:   join("api", "v1", "collections", collection, "documents", docID),
	})
	if err != nil {
		return "", err
	}
	return string(out.body), nil
}

// DeleteDocument removes a document and returns how many chunks went.
func (c *Client) DeleteDocument(ctx context.Context, collection, docID string) (int, error) {
	body, err := c.delete(ctx, join("api", "v1", "collections", collection, "documents", docID), nil)
	if err != nil {
		return 0, err
	}
	var reply struct {
		ChunksRemoved int `json:"chunks_removed"`
	}
	if err := decode(body, &reply, nil); err != nil {
		return 0, err
	}
	return reply.ChunksRemoved, nil
}

func setString(q url.Values, key string, v *string) {
	if v != nil {
		q.Set(key, *v)
	}
}

func setInt(q url.Values, key string, v *int) {
	if v != nil {
		q.Set(key, strconv.Itoa(*v))
	}
}
