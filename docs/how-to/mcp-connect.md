# Connect your client

An MCP client needs two things: the server's address, `https://<your server>/mcp`,
and a way to say who is calling. With a key, the client sends it in a header on
every call. With [company sign-in](mcp-sign-in.md), it is given only the address
and signs the person in by itself.

The server must have MCP on first: see [Turn it on](mcp-server.md#turn-it-on).

## With a key

Have an admin make a key for each person or app (see
[Keys for a team](mcp-keys.md)), then add the server to the client.

=== "VS Code"

    In `.vscode/mcp.json` in the workspace. The key is asked for once, when
    the server starts, and kept by the editor, so the file can be shared:

    ```json
    {
      "servers": {
        "vectrixdb": {
          "type": "http",
          "url": "https://vectors.company.com/mcp",
          "headers": { "api-key": "${input:vectrixdb-key}" }
        }
      },
      "inputs": [
        { "id": "vectrixdb-key", "type": "promptString", "description": "VectrixDB key", "password": true }
      ]
    }
    ```

=== "Cursor"

    In `~/.cursor/mcp.json`, which is yours alone:

    ```json
    {
      "mcpServers": {
        "vectrixdb": {
          "url": "https://vectors.company.com/mcp",
          "headers": { "api-key": "<the key>" }
        }
      }
    }
    ```

=== "A desktop assistant"

    Most desktop assistants that take a JSON config read an `mcpServers` list.
    Where the file lives is in the assistant's own documentation:

    ```json
    {
      "mcpServers": {
        "vectrixdb": {
          "url": "https://vectors.company.com/mcp",
          "headers": { "api-key": "<the key>" }
        }
      }
    }
    ```

=== "A command line client"

    A client that takes a command line adds the server once, with the key from
    an environment variable:

    ```bash
    <client> mcp add --transport http vectrixdb https://vectors.company.com/mcp \
      --header "api-key: $VECTRIXDB_KEY"
    ```

The key goes in the `api-key` header or as `Authorization: Bearer <the key>`;
the server takes either, and it is the same key. A client that sends only
bearer tokens works with this:

```json
{
  "mcpServers": {
    "vectrixdb": {
      "url": "https://vectors.company.com/mcp",
      "headers": { "Authorization": "Bearer <the key>" }
    }
  }
}
```

A server behind a gateway that keeps those headers for itself can name others
with `VECTRIXDB_KEY_HEADER` and `VECTRIXDB_TOKEN_HEADER`. The tools forward
whichever headers the settings name.

Keep a key out of a file anyone else reads: use a prompt, a secret store or an
environment variable.

## With company sign-in

Give the client the address and nothing else:

```json
{
  "servers": {
    "vectrixdb": { "type": "http", "url": "https://vectors.company.com/mcp" }
  }
}
```

The first call is turned away with a `401` that says where the person signs in.
The client opens the company's sign-in page, the person signs in, and the client
comes back with their access token. How the server is set up for this is in
[Company sign-in for MCP](mcp-sign-in.md).

## Check that it worked

Ask the assistant to call `whoami`. It answers with who the server takes it for:

```text
[i] You are key:handbook-bot, through an API key, with the role searcher.
- You reach only handbook.
- You may: ...
```

The first line names the key or the person, how they came in, and the role. The
second says which collections they reach: `every collection your role allows`,
or `only` and the names. The third lists the actions the role holds. If the
role or the collections are not what you expected, look at the key or the
person's groups. The client is working.

`list_collections` is the next check: it lists the collections the caller can
reach, and nothing else.

What each answer means:

| The client says | Why |
| --- | --- |
| 401, with a link to sign in | No key or token arrived, or only a dashboard cookie did. Check the header name and that the key is filled in. |
| `Invalid API key` | The key is not one this server knows: mistyped, revoked or expired. |
| No tool named `add_document` | The server does not have `VECTRIXDB_MCP_WRITES=1`. |
| `This server has no key and no sign-in, so it answers MCP only from its own machine.` | The server is open, so it takes MCP only from `localhost`. Give it a key or sign-in. |
