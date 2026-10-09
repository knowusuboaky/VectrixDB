// Search a collection and print each hit with its citation.
//
//	VECTRIXDB_URL=http://127.0.0.1:8000 VECTRIXDB_KEY=... go run ./examples/search handbook "how long do refunds take?"
package main

import (
	"context"
	"fmt"
	"os"
	"strings"

	vectrixdb "github.com/knowusuboaky/VectrixDB/sdk/go/v2"
)

func main() {
	collection, query := "handbook", "how long do refunds take?"
	if len(os.Args) > 1 {
		collection = os.Args[1]
	}
	if len(os.Args) > 2 {
		query = strings.Join(os.Args[2:], " ")
	}
	url := os.Getenv("VECTRIXDB_URL")
	if url == "" {
		url = "http://127.0.0.1:8000"
	}
	db := vectrixdb.New(url, vectrixdb.WithKey(os.Getenv("VECTRIXDB_KEY")))
	hits, err := db.Search(context.Background(), collection, query, &vectrixdb.SearchOptions{Limit: 3})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, h := range hits {
		fmt.Printf("%.3f  %s\n       %s\n", h.Score, h.Citation, strings.Join(strings.Fields(h.Text), " "))
	}
}
