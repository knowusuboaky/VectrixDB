// Search a collection and print each answer with where it came from.
//
//	VECTRIXDB_URL=https://vectors.company.com VECTRIXDB_KEY=... go run ./quickstart/search "how long do refunds take"
package main

import (
	"context"
	"fmt"
	"log"
	"os"

	vectrixdb "github.com/knowusuboaky/VectrixDB/sdk/go/v2"
)

func main() {
	client, err := vectrixdb.Connect(os.Getenv("VECTRIXDB_URL"), vectrixdb.WithKey(os.Getenv("VECTRIXDB_KEY")))
	if err != nil {
		log.Fatal(err)
	}
	query := "how long do refunds take"
	if len(os.Args) > 1 {
		query = os.Args[1]
	}
	found, err := client.Collection("handbook").Search(context.Background(), query, &vectrixdb.SearchOptions{Limit: 5})
	if err != nil {
		log.Fatal(err)
	}
	for _, r := range found.Results {
		relevance := 0.0
		if r.Relevance != nil {
			relevance = *r.Relevance
		}
		fmt.Printf("[%.0f%%] %s\n  %s\n\n", relevance*100, r.ReadableCitation, r.Text)
	}
}
