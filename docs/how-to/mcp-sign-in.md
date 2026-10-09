# Company sign-in for MCP

With company sign-in, a person connects an assistant as themselves. The client
is given only the server's address. It finds out where to sign the person in,
sends them there, and comes back with their access token. Every tool call then
carries that token, and the server decides it as that person: the role their
groups give them, the collections they may reach, and each collection's policy
judging every search as theirs.

There is no key to hand out and none to revoke when somebody leaves: their
account does that.

## How the client finds the sign-in

1. The client calls `/mcp` with nothing. The server answers `401` with this
   header:

    ```text
    WWW-Authenticate: Bearer resource_metadata="https://vectors.company.com/.well-known/oauth-protected-resource/mcp"
    ```

2. The client reads that address. It is the protected-resource metadata of
   RFC 9728, and it names the endpoint and the identity provider that signs its
   people in:

    ```json
    {
      "resource": "https://vectors.company.com/mcp",
      "resource_name": "VectrixDB",
      "bearer_methods_supported": ["header"],
      "authorization_servers": ["https://login.microsoftonline.com/<tenant id>/v2.0"],
      "scopes_supported": ["api://vectrixdb/.default"]
    }
    ```

3. The client sends the person to that provider to sign in, asks for a token
   with those scopes, and calls `/mcp` again with `Authorization: Bearer <the token>`.

The same document is at `/.well-known/oauth-protected-resource` as well. It
holds nothing secret and needs no key. `resource_name` is the server's brand
name when it has one. `authorization_servers` is empty, and `scopes_supported`
left out, until the server takes tokens for apps.

## Set up the server

Company sign-in for MCP is the same setting the API uses for
[tokens for apps](sign-in.md#tokens-for-apps). Turn on single sign-on, name the
audience, and turn on MCP:

```bash
export VECTRIXDB_OIDC_ISSUER="https://login.microsoftonline.com/<tenant id>/v2.0"
export VECTRIXDB_OIDC_CLIENT_ID="<the server's application id>"
export VECTRIXDB_OIDC_API_AUDIENCE="api://vectrixdb"
export VECTRIXDB_OIDC_ROLE_MAP='{"<analysts group id>": "operator", "<everyone group id>": "viewer"}'
export VECTRIXDB_MCP=1
```

| Setting | What it does |
| --- | --- |
| `VECTRIXDB_OIDC_ISSUER` | The identity provider. The discovery document names it as the place to sign in. |
| `VECTRIXDB_OIDC_CLIENT_ID` | The server's own application id at the provider. |
| `VECTRIXDB_OIDC_API_AUDIENCE` | The audience a token must be for. Empty, no token is taken and the discovery document names no provider. |
| `VECTRIXDB_OIDC_ROLE_MAP` | JSON, group to role: `viewer`, `operator` or `admin`. |
| `VECTRIXDB_OIDC_TOKEN_ROLE` | One role for every app's token, whoever it is for: `reader`, `searcher`, `viewer` or `operator`, never `admin`. Unset, the token's groups decide. |
| `VECTRIXDB_OIDC_GRANT_MAP` | JSON, group to what its members are given by name, such as `document.read`, which `open_source` needs. |
| `VECTRIXDB_MCP_SCOPES` | The scopes a client asks the provider for, separated by spaces. Left out, `<audience>/.default` when the audience starts `api://`, and none otherwise. |
| `VECTRIXDB_MCP` | `1` answers MCP at `/mcp`. |

A person whose role is `viewer` may not connect an assistant: a viewer sees
that collections exist and never what is in them. With the map above, the analysts
connect as operators and everyone else is refused. To let every token search
and no more, set `VECTRIXDB_OIDC_TOKEN_ROLE=searcher`. What each role may do
over MCP is in [Keys for a team](mcp-keys.md#roles).

The full list of what a token is checked for, and what is refused, is in
[Sign people in](sign-in.md#tokens-for-apps).

## Register the API and the client

=== "Entra ID"

    1. In the server's app registration, **Expose an API** with the
       Application ID URI `api://vectrixdb`, and add a scope, `search`.
    2. Register the MCP client as an application of its own (public client,
       the redirect address the client documents), and under **API
       permissions** give it `api://vectrixdb/search`.
    3. Leave `VECTRIXDB_MCP_SCOPES` unset: the client asks for
       `api://vectrixdb/.default`, which includes it.

=== "Okta"

    1. Create an authorization server whose audience is `api://vectrixdb`, with
       a scope `vectrixdb.search` and an access policy for the MCP client.
    2. Register the MCP client as a native app with PKCE, and the redirect
       address the client documents.
    3. Set `VECTRIXDB_OIDC_ISSUER` to that authorization server, and
       `VECTRIXDB_MCP_SCOPES="vectrixdb.search"`.

=== "Any OIDC provider"

    The server checks the token's signature against the provider's published
    keys, its issuer, and that its audience is `VECTRIXDB_OIDC_API_AUDIENCE`.
    Register the audience as an API, register the client with PKCE, and name
    the scopes it asks for in `VECTRIXDB_MCP_SCOPES`.

A server with `VECTRIXDB_OIDC_API_CLIENTS` set takes tokens from the apps it
names alone, so the MCP client's own client id goes on that list too
([Run it inside your company's registry](inside-your-registry.md#require-the-companys-own-tool)).

## Connect

The client's config holds the address and nothing else:

```json
{
  "servers": {
    "vectrixdb": { "type": "http", "url": "https://vectors.company.com/mcp" }
  }
}
```

Each client's own format is on [Connect your client](mcp-connect.md). Once the
person has signed in, `whoami` answers that they came in through your company's
sign-in, with the role their groups gave them.

## Why a browser session is not a way in

A person signed in to the dashboard has a session cookie, and the browser sends
it with every request to the server, whoever made the request. If `/mcp` took
that cookie, any page open in the same browser could make an assistant's
request as whoever has the dashboard open. So `/mcp` takes a key or an access
token and nothing else. A call that arrives with only a cookie gets a `401`
with the sign-in header, like a call with nothing, and its message says why:

```text
Connect with a key or your organisation's sign-in: a dashboard session is not one an assistant may use.
```

## What is recorded

The access log names the person, by their address, with the method `token`.
A collection with an entitlement policy, closed to every key, answers the
person with what they may see, and the audit trail records each decision as
theirs. See [Restrict what a search can see](entitlements.md).
