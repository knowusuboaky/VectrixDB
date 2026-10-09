package vectrixdb

// Collection describes one collection. Raw holds the whole object as the
// server sent it.
type Collection struct {
	Name          string   `json:"name"`
	Dimension     int      `json:"dimension"`
	Metric        string   `json:"metric"`
	Count         int      `json:"count"`
	SizeBytes     int64    `json:"size_bytes"`
	Description   string   `json:"description"`
	HasTextIndex  bool     `json:"has_text_index"`
	Tags          []string `json:"tags"`
	CreatedAt     string   `json:"created_at"`
	UpdatedAt     string   `json:"updated_at"`
	IndexedFields []string `json:"indexed_fields"`
	Raw           map[string]any
}

// Result is one search hit. Citation is metadata["_vx_citation"] when
// present, else metadata["source"], else the id.
type Result struct {
	ID       string         `json:"id"`
	Score    float64        `json:"score"`
	Text     string         `json:"text"`
	Metadata map[string]any `json:"metadata"`
	Citation string
	Raw      map[string]any
}

// Document is one kept document, as Documents lists them.
type Document struct {
	DocID       string         `json:"doc_id"`
	Filename    string         `json:"filename"`
	Kind        string         `json:"kind"`
	Source      string         `json:"source"`
	Version     string         `json:"version"`
	ExtractedAt string         `json:"extracted_at"`
	Chunking    map[string]any `json:"chunking"`
	Raw         map[string]any
}

// Added is what AddDocument reports.
type Added struct {
	DocID      string   `json:"doc_id"`
	Chunks     int      `json:"chunks"`
	Replaced   int      `json:"replaced"`
	Quality    float64  `json:"quality"`
	LowQuality bool     `json:"low_quality"`
	Citations  []string `json:"citations"`
	Kept       bool     `json:"kept"`
	Raw        map[string]any
}

// Source is a feed or a page a collection keeps up with.
type Source struct {
	ID      string `json:"id"`
	Address string `json:"address"`
	Kind    string `json:"kind"`
	Every   string `json:"every"`
	Raw     map[string]any
}

// Refreshed is what RefreshSources reports.
type Refreshed struct {
	Added     int   `json:"added"`
	Updated   int   `json:"updated"`
	Unchanged int   `json:"unchanged"`
	Removed   int   `json:"removed"`
	Failed    []any `json:"failed"`
	Raw       map[string]any
}
