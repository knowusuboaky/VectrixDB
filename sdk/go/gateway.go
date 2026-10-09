package vectrixdb

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"sort"
	"strings"
)

// This file holds the options for a client that works behind a company's
// gateway: the headers a key and a token go in, extra headers, a route
// prefix and each route's gateway path, and the rule that a key does not
// travel over plain HTTP. The reading of a prefix and of gateway paths is
// the server's own (vectrixdb/api/gateway.py), so one list serves both sides.

// ErrConfig is what every call returns when the client was made with
// settings it refuses: a key or token for a plain http:// address that is
// not this machine, a header name that cannot be one, or a gateway path list
// that does not read. New cannot return an error, so the client keeps the
// first such refusal and every call returns it, wrapping ErrConfig; Err
// reports it at once.
var ErrConfig = errors.New("vectrixdb: the client's settings are refused")

// The headers a key and a token go in unless WithKeyHeader or WithTokenHeader
// name others; the server's VECTRIXDB_KEY_HEADER and VECTRIXDB_TOKEN_HEADER.
const (
	DefaultKeyHeader   = "api-key"
	DefaultTokenHeader = "Authorization"
)

// WithAllowHTTP lets a key or token travel over plain http:// to a host that
// is not this machine. Without it, New refuses that: anyone on the way could
// read the key.
func WithAllowHTTP() Option { return func(c *Client) { c.allowHTTP = true } }

// WithKeyHeader names the header the key goes in, for a gateway that wants
// its own (Ocp-Apim-Subscription-Key). The default is api-key.
func WithKeyHeader(name string) Option {
	return func(c *Client) {
		if c.refuse(checkHeaderName(name, "WithKeyHeader")) {
			return
		}
		c.keyHeader = strings.TrimSpace(name)
	}
}

// WithTokenHeader names the header a sign-in token goes in, always as
// "Bearer <token>". The default is Authorization.
func WithTokenHeader(name string) Option {
	return func(c *Client) {
		if c.refuse(checkHeaderName(name, "WithTokenHeader")) {
			return
		}
		c.tokenHeader = strings.TrimSpace(name)
	}
}

// WithHeader sends an extra header on every request, for a gateway that
// wants a subscription key as well as the person's token. It never replaces
// the user-agent or the header the key or token goes in.
func WithHeader(name, value string) Option {
	return func(c *Client) {
		if c.refuse(checkHeaderName(name, "WithHeader")) {
			return
		}
		name = strings.TrimSpace(name)
		if !headerValueOK(value) {
			// The value may be a secret, so the refusal names only the header.
			c.refuse(fmt.Errorf("%w: WithHeader: the value for %s holds a character a header cannot carry", ErrConfig, name))
			return
		}
		if c.headers == nil {
			c.headers = map[string]string{}
		}
		c.headers[name] = value
	}
}

// WithPrefix sets the path every route lives under, for example "/acme".
// It is read as a path of names: slashes trimmed, doubled ones dropped, "."
// and ".." refused.
func WithPrefix(prefix string) Option {
	return func(c *Client) {
		p, err := routePrefix(prefix)
		if c.refuse(err) {
			return
		}
		c.prefix = p
	}
}

// WithGatewayPaths sets each route's own gateway path, written the way a
// gateway team hands them over: "api/v1=/files/search, auth=/files/auth".
// A request for a route goes to <gateway path><prefix><route>, the gateway
// path being that of the longest name the route falls under.
func WithGatewayPaths(list string) Option {
	return func(c *Client) {
		paths, err := readGatewayPaths(list)
		if c.refuse(err) {
			return
		}
		c.gatewayPaths = paths
	}
}

// WithGatewayPathMap is WithGatewayPaths given as a map from name to path,
// {"api/v1": "/files/search"}.
func WithGatewayPathMap(paths map[string]string) Option {
	return func(c *Client) {
		read, err := readGatewayPathMap(paths)
		if c.refuse(err) {
			return
		}
		c.gatewayPaths = read
	}
}

// refuse keeps the first refusal of the client's settings; it reports
// whether err was one.
func (c *Client) refuse(err error) bool {
	if err == nil {
		return false
	}
	if c.err == nil {
		c.err = err
	}
	return true
}

// isTokenChar reports whether b is an RFC 9110 token character, what an
// HTTP header's name may be made of.
func isTokenChar(b byte) bool {
	switch {
	case 'a' <= b && b <= 'z', 'A' <= b && b <= 'Z', '0' <= b && b <= '9':
		return true
	}
	return strings.IndexByte("!#$%&'*+-.^_`|~", b) >= 0
}

func checkHeaderName(name, option string) error {
	name = strings.TrimSpace(name)
	ok := name != ""
	for i := 0; ok && i < len(name); i++ {
		ok = isTokenChar(name[i])
	}
	if !ok {
		return fmt.Errorf("%w: %s: %q cannot be the name of an HTTP header", ErrConfig, option, name)
	}
	return nil
}

// headerValueOK refuses the control characters a header value cannot carry.
func headerValueOK(v string) bool {
	for i := 0; i < len(v); i++ {
		if b := v[i]; (b < 0x20 && b != '\t') || b == 0x7f {
			return false
		}
	}
	return true
}

// names reads a path of names, "/one/two" or "", whatever it was written as.
func names(value, what string) (string, error) {
	var parts []string
	for _, p := range strings.Split(strings.TrimSpace(value), "/") {
		if p == "" {
			continue
		}
		if p == "." || p == ".." {
			return "", fmt.Errorf("%w: %s is a path of names, not %q", ErrConfig, what, value)
		}
		parts = append(parts, p)
	}
	if len(parts) == 0 {
		return "", nil
	}
	return "/" + strings.Join(parts, "/"), nil
}

// routePrefix reads a prefix: "/acme", or "" for routes at the root.
func routePrefix(value string) (string, error) { return names(value, "a route prefix") }

// readGatewayPaths reads "api/v1=/files/search, auth=/files/auth" into
// {"api/v1": "/files/search", "auth": "/files/auth"}.
func readGatewayPaths(list string) (map[string]string, error) {
	var pairs [][2]string
	for _, entry := range strings.Split(list, ",") {
		if strings.TrimSpace(entry) == "" {
			continue
		}
		route, path, found := strings.Cut(entry, "=")
		if !found {
			return nil, fmt.Errorf("%w: a gateway path is route=path, not %q", ErrConfig, strings.TrimSpace(entry))
		}
		pairs = append(pairs, [2]string{route, path})
	}
	return gatewayPairs(pairs)
}

func readGatewayPathMap(paths map[string]string) (map[string]string, error) {
	keys := make([]string, 0, len(paths))
	for k := range paths {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	pairs := make([][2]string, 0, len(keys))
	for _, k := range keys {
		pairs = append(pairs, [2]string{k, paths[k]})
	}
	return gatewayPairs(pairs)
}

func gatewayPairs(pairs [][2]string) (map[string]string, error) {
	out := map[string]string{}
	for _, pair := range pairs {
		name, err := names(pair[0], "a route")
		if err != nil {
			return nil, err
		}
		where, err := names(pair[1], "a gateway path")
		if err != nil {
			return nil, err
		}
		name = strings.TrimPrefix(name, "/")
		if name == "" || where == "" {
			return nil, fmt.Errorf("%w: a gateway path is route=path with both given, not %q",
				ErrConfig, strings.TrimSpace(pair[0])+"="+strings.TrimSpace(pair[1]))
		}
		if _, dup := out[name]; dup {
			return nil, fmt.Errorf("%w: %s is given two gateway paths", ErrConfig, name)
		}
		out[name] = where
	}
	return out, nil
}

// address is the path a request for route goes to:
// <gateway path><prefix><route>. route is escaped and starts with "/"; the
// gateway path is that of the longest name the route falls under (the route
// without its leading "/" equals the name or starts with name + "/").
func (c *Client) address(route string) string {
	bare := strings.TrimPrefix(route, "/")
	if i := strings.IndexByte(bare, '?'); i >= 0 {
		bare = bare[:i]
	}
	best, gateway := -1, ""
	for name, where := range c.gatewayPaths {
		if len(name) > best && (bare == name || strings.HasPrefix(bare, name+"/")) {
			best, gateway = len(name), where
		}
	}
	return gateway + c.prefix + route
}

// isLoopback reports whether host is this machine: localhost, 127.0.0.0/8
// or ::1. localhost.evil.com is not.
func isLoopback(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// checkPlainHTTP refuses a key or token for an http:// address whose host
// is not this machine, unless WithAllowHTTP was given.
func (c *Client) checkPlainHTTP() error {
	if c.allowHTTP || (c.key == "" && c.token == "") {
		return nil
	}
	u, err := url.Parse(c.base)
	if err != nil || !strings.EqualFold(u.Scheme, "http") || isLoopback(u.Hostname()) {
		return nil
	}
	return fmt.Errorf("%w: a key or token would go over plain HTTP to %s, where anyone on the way can read it; "+
		"use an https:// address, or WithAllowHTTP() if this network is trusted", ErrConfig, u.Hostname())
}
