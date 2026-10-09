package vectrixdb

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"
)

// The server's OpenAPI description, as the repository keeps it.
const specPath = "../../docs/reference/openapi.json"

type spec struct {
	Paths map[string]map[string]struct {
		Parameters []struct {
			Name string `json:"name"`
			In   string `json:"in"`
		} `json:"parameters"`
		RequestBody struct {
			Content map[string]struct {
				Schema json.RawMessage `json:"schema"`
			} `json:"content"`
		} `json:"requestBody"`
	} `json:"paths"`
	Components struct {
		Schemas map[string]struct {
			Properties map[string]json.RawMessage `json:"properties"`
		} `json:"schemas"`
	} `json:"components"`
}

func loadSpec(t *testing.T) *spec {
	t.Helper()
	raw, err := os.ReadFile(specPath)
	if err != nil {
		t.Fatalf("reading %s: %v", specPath, err)
	}
	var s spec
	if err := json.Unmarshal(raw, &s); err != nil {
		t.Fatalf("parsing %s: %v", specPath, err)
	}
	return &s
}

// Every route the client calls, with the method it uses and the query
// parameters it may send. /ready is not in the spec: Ready falls back to
// /health when the server answers 404, which the contract allows.
var routes = []struct {
	method, path string
	query        []string
	bodySchema   string
}{
	{"get", pathHealth, nil, ""},
	{"get", pathWhoami, nil, ""},
	{"get", pathCollections, nil, ""},
	{"get", "/api/v1/collections/{name}", nil, ""},
	{"post", pathCollectionsV2, nil, "CreateCollectionRequestV2"},
	{"delete", "/api/v1/collections/{name}", nil, ""},
	{"post", "/api/v1/collections/{name}/documents", []string{"doc_id", "metadata", "chunk", "chunk_size", "overlap"}, ""},
	{"post", "/api/v1/collections/{name}/text-upsert", nil, "TextUpsertRequest"},
	{"post", "/api/v1/collections/{name}/text-search", nil, "TextSearchRequest"},
	{"post", "/api/v1/collections/{name}/text-hybrid-search", nil, "TextSearchRequest"},
	{"get", "/api/v1/collections/{name}/documents", nil, ""},
	{"get", "/api/v1/collections/{name}/documents/{doc_id}", nil, ""},
	{"delete", "/api/v1/collections/{name}/documents/{doc_id}", nil, ""},
	{"get", "/api/v1/collections/{name}/sources", nil, ""},
	{"post", "/api/v1/collections/{name}/sources", nil, "AddSourceRequest"},
	{"post", "/api/v1/collections/{name}/sources/refresh", nil, "RefreshRequest"},
	{"delete", "/api/v1/collections/{name}/sources/{source_id}", []string{"delete_documents"}, ""},
}

func TestRoutesAreInTheSpec(t *testing.T) {
	s := loadSpec(t)
	for _, r := range routes {
		ops, ok := s.Paths[r.path]
		if !ok {
			t.Errorf("%s is not a path in the spec", r.path)
			continue
		}
		op, ok := ops[r.method]
		if !ok {
			t.Errorf("%s has no %s in the spec", r.path, strings.ToUpper(r.method))
			continue
		}
		for _, name := range r.query {
			found := false
			for _, p := range op.Parameters {
				if p.In == "query" && p.Name == name {
					found = true
				}
			}
			if !found {
				t.Errorf("%s %s has no query parameter %q", strings.ToUpper(r.method), r.path, name)
			}
		}
		if r.bodySchema != "" {
			body, ok := op.RequestBody.Content["application/json"]
			if !ok {
				t.Errorf("%s %s takes no JSON body in the spec", strings.ToUpper(r.method), r.path)
				continue
			}
			if !strings.Contains(string(body.Schema), `"#/components/schemas/`+r.bodySchema+`"`) {
				t.Errorf("%s %s does not take %s: %s", strings.ToUpper(r.method), r.path, r.bodySchema, body.Schema)
			}
		}
	}
}

// Every JSON field of the generated request types the client fills must be
// a property of its schema, so a renamed field fails here, before a server
// runs.
func TestRequestFieldsAreInTheSpec(t *testing.T) {
	s := loadSpec(t)
	bodies := map[string]any{
		"CreateCollectionRequestV2": CreateCollectionRequestV2{},
		"TextUpsertRequest":         TextUpsertRequest{},
		"TextUpsertPoint":           TextUpsertPoint{},
		"TextSearchRequest":         TextSearchRequest{},
		"AddSourceRequest":          AddSourceRequest{},
		"RefreshRequest":            RefreshRequest{},
	}
	for name, zero := range bodies {
		schema, ok := s.Components.Schemas[name]
		if !ok {
			t.Errorf("schema %s is not in the spec", name)
			continue
		}
		for _, field := range jsonFields(zero) {
			if _, ok := schema.Properties[field]; !ok {
				t.Errorf("%s has no property %q", name, field)
			}
		}
	}
	// The fields the client sets by hand on each body.
	must := map[string][]string{
		"CreateCollectionRequestV2": {"name", "dimension", "enable_text_index", "metric", "description"},
		"TextUpsertRequest":         {"points"},
		"TextUpsertPoint":           {"id", "text", "payload"},
		"TextSearchRequest":         {"query_text", "limit", "filter", "rerank"},
		"AddSourceRequest":          {"address", "kind", "every"},
	}
	for name, fields := range must {
		for _, field := range fields {
			if _, ok := s.Components.Schemas[name].Properties[field]; !ok {
				t.Errorf("%s has no property %q", name, field)
			}
		}
	}
}

func jsonFields(v any) []string {
	var out []string
	rt := reflect.TypeOf(v)
	for i := 0; i < rt.NumField(); i++ {
		tag := rt.Field(i).Tag.Get("json")
		if name, _, _ := strings.Cut(tag, ","); name != "" && name != "-" {
			out = append(out, name)
		}
	}
	return out
}
