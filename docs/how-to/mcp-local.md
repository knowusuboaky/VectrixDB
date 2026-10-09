# On your own machine

For one person and one collection there is a smaller MCP server that needs no
`vectrixdb serve`, no sign-in and no keys. The client starts it as a command,
it opens the collection from disk, and it offers search plus conversation
memory: remember, recall, forget.

There is no sign-in on this server. To share a collection with other people,
run `vectrixdb serve` with `VECTRIXDB_MCP=1` instead: see
[Use your collections from an assistant over MCP](mcp-server.md).

## Start it

```bash
pip install "vectrixdb[mcp]"
vectrixdb mcp --name notes --path ./data --mode hybrid
```

`vectrixdb-mcp` is the same server as a command of its own, which is what a
client's config names. The default transport is stdio, which is what a desktop
assistant starts as a command:

```json
{
  "mcpServers": {
    "vectrixdb": {
      "command": "vectrixdb-mcp",
      "args": ["--name", "notes", "--path", "/abs/path/to/data"]
    }
  }
}
```

Give the path in full: the client starts the command from a folder of its own
choosing.

| Option | What it does | Default |
| --- | --- | --- |
| `--name` | The collection. | `default` |
| `--path` | Where the database is. | `./vectrixdb_data`; for `vectrixdb mcp`, `VECTRIXDB_PATH` first |
| `--mode` | How it searches: `dense`, `hybrid`, `ultimate` or `graph`. | the library's default |
| `--transport` | `stdio`, `sse` or `streamable-http`. | `stdio` |
| `--allow-writes` | Over HTTP, also offer `remember`, `feedback` and `forget`. | off |

`vectrixdb mcp` also takes `--env-file`, a settings file read first; what the
environment already sets wins.

## The tools

| Tool | Arguments | What it does |
| --- | --- | --- |
| `search` | `query`, `limit`, `token_budget`, `mode` | Search the collection. |
| `recall` | `query`, `session`, `limit`, `token_budget` | Conversation memories relevant to the query, weighted by recency and past feedback. `session` keeps to one conversation. |
| `remember` | `text`, `session`, `role`, `pinned` | Store a turn, or with `pinned=true` a fact that should always be in context. |
| `feedback` | `id`, `outcome`, `correction` | Grade a recalled memory: `useful`, `dead_end` or `corrected`. With a correction, the corrected text is stored and supersedes the old memory. |
| `context` | `query`, `session`, `token_budget`, `recent_turns` | A ready context block: pinned facts, the session's recent turns, then relevant memories, under one budget. |
| `forget` | `session`, `older_than_days`, `ids`, `superseded`, `all_sessions` | Delete memories for good: by ids, by age within a session, or only facts a correction already replaced. A call with none of these deletes nothing unless `all_sessions=true`. The one tool that deletes. |

`token_budget` is 2000 unless given, and 1500 for `context`. `recent_turns` is
6. How the memory itself works is in
[Keep conversation memory without wasting tokens](conversation-memory.md).

## Over HTTP

`--transport sse` and `--transport streamable-http` serve it on this machine
for other programs to reach. Over HTTP, `remember`, `feedback` and `forget` are
left out unless `--allow-writes` is given, on either `vectrixdb mcp` or
`vectrixdb-mcp`, since anything on the machine that reaches the port may call
them. The server says so when it starts:

```text
Serving search, recall and context only: --allow-writes adds remember, feedback and forget. For a team, run vectrixdb serve with VECTRIXDB_MCP=1.
```

Over stdio they are always there: the client that started the server is the
only one talking to it.

## Every answer says whether it was cut

Every tool that returns results takes a `token_budget`, and every answer starts
with a line that says whether anything was cut:

```text
[!] TRUNCATED: showing 3 of 9 results (~200-token budget). The answer may be among the 6 cut. Raise token_budget or narrow the query.
```

or, when nothing was:

```text
[i] 9 results, ~410 of a 2000-token budget.
```

A tool result goes straight into the model's context, so a silently shortened
list would read as a complete one and the model would reason from an absence
that is not there. The header exists so that cannot happen. When a search fell
back to vectors alone, a second line says so and why.

## Use it from Python instead

The tool bodies are plain functions, so a program that already has a `Vectrix`
can call them without the `mcp` package:

```python
from vectrixdb import Vectrix
from vectrixdb.mcp_server import tool_context

db = Vectrix("memory", path="./data")
db.remember("we decided to ship on Friday", session="u42", role="user")

print(tool_context(db, "what did we decide", session="u42", token_budget=800))
```

`build_server(db)` returns the configured MCP server for a program that wants to
mount it itself; `build_server(db, writes=False)` leaves out the tools that
change the collection.

With [tracing](tracing.md) on, `vectrixdb-mcp` sends its spans to the OTLP
collector itself, since nothing else in its process would.
