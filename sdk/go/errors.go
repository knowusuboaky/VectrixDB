package vectrixdb

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
)

// The kinds of refusal, for errors.Is. Each is matched by the status of the
// *Error that carries it; ErrBusy is a 429 or a 503 that stayed so after
// the retries.
var (
	ErrAuth      = errors.New("vectrixdb: not authorized")
	ErrForbidden = errors.New("vectrixdb: forbidden")
	ErrNotFound  = errors.New("vectrixdb: not found")
	ErrConflict  = errors.New("vectrixdb: conflict")
	ErrTooLarge  = errors.New("vectrixdb: too large")
	ErrInvalid   = errors.New("vectrixdb: invalid request")
	ErrBusy      = errors.New("vectrixdb: server busy")
)

// Error is every refusal the server sends: the HTTP status, the one-sentence
// message, and the detail (a string, or on a 422 the list of field errors).
// A redirect (3xx) is one too, never followed, its message naming the status
// and the location it pointed to.
type Error struct {
	Status  int
	Message string
	Detail  any
	URL     string
}

func (e *Error) Error() string {
	return fmt.Sprintf("vectrixdb: %d %s", e.Status, e.Message)
}

// Is makes errors.Is(err, ErrNotFound) and the other kinds work.
func (e *Error) Is(target error) bool {
	switch target {
	case ErrAuth:
		return e.Status == http.StatusUnauthorized
	case ErrForbidden:
		return e.Status == http.StatusForbidden
	case ErrNotFound:
		return e.Status == http.StatusNotFound
	case ErrConflict:
		return e.Status == http.StatusConflict
	case ErrTooLarge:
		return e.Status == http.StatusRequestEntityTooLarge
	case ErrInvalid:
		return e.Status == http.StatusUnprocessableEntity
	case ErrBusy:
		return e.Status == http.StatusTooManyRequests || e.Status == http.StatusServiceUnavailable
	}
	return false
}

// newError reads {"ok": false, "message", "data", "detail"}. A body that is
// not that (an HTML page from a proxy) gives "<status> from <url>".
func newError(status int, url string, body []byte) *Error {
	e := &Error{Status: status, URL: url}
	var refusal struct {
		Message string `json:"message"`
		Detail  any    `json:"detail"`
	}
	if json.Unmarshal(body, &refusal) == nil {
		e.Message = refusal.Message
		e.Detail = refusal.Detail
		if e.Message == "" {
			// An older server sends only FastAPI's {"detail": "..."}.
			if s, ok := refusal.Detail.(string); ok {
				e.Message = s
			}
		}
	}
	if e.Message == "" {
		e.Message = fmt.Sprintf("%d from %s", status, url)
	}
	return e
}

// redirectError is a 3xx: the client never follows a redirect, since the key
// would go with it. It is the base kind, none of the Err* sentinels.
func redirectError(status int, url, location string) *Error {
	if location == "" {
		location = "no location"
	}
	return &Error{
		Status:  status,
		URL:     url,
		Message: fmt.Sprintf("%d redirect to %s refused: the client does not follow redirects, so the key does not go with one", status, location),
	}
}
