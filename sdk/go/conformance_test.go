package vectrixdb

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// server finds a VectrixDB to walk against: VECTRIXDB_URL and VECTRIXDB_KEY
// when set, else one started from ../conformance/serve.py with the Python
// named by PYTHON (default python3), stopped when the test ends.
func server(t *testing.T) (url, key string) {
	t.Helper()
	if url, key = os.Getenv("VECTRIXDB_URL"), os.Getenv("VECTRIXDB_KEY"); url != "" {
		return url, key
	}
	python := os.Getenv("PYTHON")
	if python == "" {
		python = "python3"
	}
	cmd := exec.Command(python, "../conformance/serve.py")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Skipf("VECTRIXDB_URL is not set and %s cannot be started (%v); set PYTHON to a Python with the api extra", python, err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})
	line, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil {
		t.Fatalf("serve.py printed no address: %v\n%s", err, stderr.String())
	}
	var where struct {
		URL string `json:"url"`
		Key string `json:"key"`
	}
	if err := json.Unmarshal([]byte(line), &where); err != nil {
		t.Fatalf("serve.py printed %q, not JSON: %v", line, err)
	}
	return where.URL, where.Key
}

// TestConformanceWalk runs the walk in sdk/CONTRACT.md, step by step.
func TestConformanceWalk(t *testing.T) {
	url, key := server(t)
	ctx := context.Background()
	// The first requests load models, which can take a while on a cold server.
	c := New(url, WithKey(key), WithTimeout(3*time.Minute))
	name := fmt.Sprintf("walk-%06d", rand.Intn(1000000))

	// 1
	if ok, err := c.Health(ctx); err != nil || !ok {
		t.Fatalf("health: %v %v", ok, err)
	}
	if ok, err := c.Ready(ctx); err != nil || !ok {
		t.Fatalf("ready: %v %v", ok, err)
	}

	// 2
	col, err := c.CreateCollection(ctx, name, nil)
	if err != nil {
		t.Fatalf("create_collection: %v", err)
	}
	t.Cleanup(func() { _ = c.DeleteCollection(ctx, name) })
	if !col.HasTextIndex || col.Count != 0 {
		t.Fatalf("new collection: has_text_index=%v count=%d", col.HasTextIndex, col.Count)
	}

	// 3
	if !contains(t, c, name) {
		t.Fatalf("collections() does not list %s", name)
	}
	if got, err := c.Describe(ctx, name); err != nil || got.Name != name {
		t.Fatalf("describe: %+v %v", got, err)
	}

	// 4
	handbook, err := os.Open("../conformance/handbook.md")
	if err != nil {
		t.Fatal(err)
	}
	defer handbook.Close()
	added, err := c.AddDocument(ctx, name, handbook, "handbook.md", &AddOptions{DocID: "handbook.md"})
	if err != nil {
		t.Fatalf("add_document: %v", err)
	}
	if added.Chunks < 2 || !added.Kept || !has(added.Citations, "handbook.md#Refunds") {
		t.Fatalf("add_document: %+v", added)
	}

	// 5
	n, err := c.AddTexts(ctx, name, []Text{
		{ID: "t1", Text: "Travel is booked by the office."},
		{ID: "t2", Text: "Salaries are paid on the 25th of the month.", Metadata: map[string]any{"team": "payroll"}},
	})
	if err != nil || n != 2 {
		t.Fatalf("add_texts: %d %v", n, err)
	}

	// 6
	hits, err := c.Search(ctx, name, "refunds", &SearchOptions{Limit: 3})
	if err != nil || len(hits) == 0 {
		t.Fatalf("search: %v %v", hits, err)
	}
	if hits[0].Citation != "handbook.md#Refunds" || !strings.Contains(hits[0].Text, "ten working days") {
		t.Fatalf("search: first hit %+v", hits[0])
	}

	// 7
	hits, err = c.Search(ctx, name, "salaries", &SearchOptions{Mode: ModeHybrid, Rerank: true, Limit: 2})
	if err != nil || len(hits) == 0 || hits[0].ID != "t2" {
		t.Fatalf("hybrid search: %v %v", hits, err)
	}
	hits, err = c.Search(ctx, name, "salaries", &SearchOptions{Filter: map[string]any{"team": "payroll"}})
	if err != nil || len(hits) != 1 || hits[0].ID != "t2" {
		t.Fatalf("filtered search: %v %v", hits, err)
	}

	// 8
	docs, err := c.Documents(ctx, name)
	if err != nil || len(docs) != 1 || docs[0].DocID != "handbook.md" {
		t.Fatalf("documents: %v %v", docs, err)
	}
	text, err := c.OpenDocument(ctx, name, "handbook.md")
	if err != nil || !strings.HasPrefix(text, "# Refunds") {
		t.Fatalf("open_document: %q %v", text, err)
	}

	// 9
	if srcs, err := c.Sources(ctx, name); err != nil || len(srcs) != 0 {
		t.Fatalf("sources: %v %v", srcs, err)
	}
	if r, err := c.RefreshSources(ctx, name); err != nil || r.Added != 0 {
		t.Fatalf("refresh_sources: %+v %v", r, err)
	}

	// 10
	if removed, err := c.DeleteDocument(ctx, name, "handbook.md"); err != nil || removed < 2 {
		t.Fatalf("delete_document: %d %v", removed, err)
	}
	if docs, err := c.Documents(ctx, name); err != nil || len(docs) != 0 {
		t.Fatalf("documents after delete: %v %v", docs, err)
	}

	// 11
	_, err = c.Describe(ctx, "no-such-collection")
	var e *Error
	if !errors.Is(err, ErrNotFound) || !errors.As(err, &e) || e.Status != 404 || !strings.Contains(e.Message, "not found") {
		t.Fatalf("describe(missing): %v", err)
	}

	// 12
	_, err = c.CreateCollection(ctx, "walk-bad", &CreateOptions{Dimension: -1})
	if !errors.Is(err, ErrInvalid) || !errors.As(err, &e) || e.Status != 422 {
		t.Fatalf("create_collection(dimension 0): %v", err)
	}
	if !namesField(e.Detail, "dimension") {
		t.Fatalf("422 detail does not name dimension: %v", e.Detail)
	}

	// 13
	wrong := New(url, WithKey("wrong"))
	_, err = wrong.Collections(ctx)
	if !errors.Is(err, ErrAuth) || !errors.As(err, &e) || e.Status != 401 {
		t.Fatalf("wrong key: %v", err)
	}

	// 14
	if err := c.DeleteCollection(ctx, name); err != nil {
		t.Fatalf("delete_collection: %v", err)
	}
	if contains(t, c, name) {
		t.Fatalf("collections() still lists %s", name)
	}
}

func contains(t *testing.T, c *Client, name string) bool {
	t.Helper()
	cols, err := c.Collections(context.Background())
	if err != nil {
		t.Fatalf("collections: %v", err)
	}
	for _, col := range cols {
		if col.Name == name {
			return true
		}
	}
	return false
}

func has(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// namesField reports whether a 422 detail, the list of field errors, has an
// entry whose loc ends in field.
func namesField(detail any, field string) bool {
	entries, ok := detail.([]any)
	if !ok {
		return false
	}
	for _, entry := range entries {
		m, _ := entry.(map[string]any)
		loc, _ := m["loc"].([]any)
		for _, part := range loc {
			if part == field {
				return true
			}
		}
	}
	return false
}
