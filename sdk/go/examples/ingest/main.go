// Create a collection if needed and add a file to it.
//
//	VECTRIXDB_URL=http://127.0.0.1:8000 VECTRIXDB_KEY=... go run ./examples/ingest handbook ./handbook.md
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	vectrixdb "github.com/knowusuboaky/VectrixDB/sdk/go"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: ingest <collection> <file>")
		os.Exit(2)
	}
	collection, path := os.Args[1], os.Args[2]
	db := vectrixdb.New(os.Getenv("VECTRIXDB_URL"), vectrixdb.WithKey(os.Getenv("VECTRIXDB_KEY")))
	ctx := context.Background()

	if _, err := db.Describe(ctx, collection); errors.Is(err, vectrixdb.ErrNotFound) {
		if _, err := db.CreateCollection(ctx, collection, nil); err != nil {
			fail(err)
		}
	} else if err != nil {
		fail(err)
	}

	f, err := os.Open(path)
	if err != nil {
		fail(err)
	}
	defer f.Close()
	name := filepath.Base(path)
	added, err := db.AddDocument(ctx, collection, f, name, &vectrixdb.AddOptions{
		DocID:    name,
		Metadata: map[string]any{"team": "docs"},
	})
	if err != nil {
		fail(err)
	}
	fmt.Printf("%s: %d chunks (quality %.2f)\n", added.DocID, added.Chunks, added.Quality)
	for _, c := range added.Citations {
		fmt.Println("  ", c)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
