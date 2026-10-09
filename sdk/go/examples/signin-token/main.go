// Connect with a company sign-in token instead of an API key and say who
// the server takes you for.
//
//	VECTRIXDB_URL=http://127.0.0.1:8000 VECTRIXDB_TOKEN=... go run ./examples/signin-token
package main

import (
	"context"
	"errors"
	"fmt"
	"os"

	vectrixdb "github.com/knowusuboaky/VectrixDB/sdk/go/v2"
)

func main() {
	db := vectrixdb.New(os.Getenv("VECTRIXDB_URL"), vectrixdb.WithToken(os.Getenv("VECTRIXDB_TOKEN")))
	who, err := db.Whoami(context.Background())
	if errors.Is(err, vectrixdb.ErrAuth) {
		fmt.Fprintln(os.Stderr, "the token was not accepted:", err)
		os.Exit(1)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for k, v := range who {
		fmt.Printf("%s: %v\n", k, v)
	}
}
