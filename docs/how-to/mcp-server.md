# Use a collection from an assistant over MCP

VectrixDB ships a small MCP server so an assistant, or any MCP client, can search a collection and keep conversation memory in it as tools.

## Install and run

```bash
pip install "vectrixdb[mcp]"
vectrixdb-mcp --name notes --path ./data
```

or, through the main CLI:

```bash
vectrixdb mcp --name notes --path ./data --mode hybrid
```

The default transport is stdio, which is what a desktop assistant starts as a command. `--transport sse` and `--transport streamable-http` are available for other hosts.

## Register it with your client

A client that starts its servers as commands takes the command and its arguments, most often in a settings file shaped like this:

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

## The tools

| Tool | What it does |
| --- | --- |
| `search(query, limit, token_budget, mode)` | Search the collection. |
| `recall(query, session, limit, token_budget)` | Conversation memories, weighted by recency and feedback. |
| `remember(text, session, role, pinned)` | Store a turn, or a pinned fact. |
| `feedback(id, outcome, correction)` | Grade a memory: `useful`, `dead_end`, `corrected`. |
| `context(query, session, token_budget, recent_turns)` | A ready context block: pinned facts, recent turns, relevant memories. |
| `forget(session, older_than_days, ids, superseded)` | Delete memories for good: by ids, by age within a session, or only superseded facts. The one tool that deletes. |

Every tool takes a `token_budget` and every answer starts with a line that says whether anything was cut:

```
[!] TRUNCATED: showing 3 of 9 results (~200-token budget). The answer may be among the 6 cut. Raise token_budget or narrow the query.
```

or, when nothing was:

```
[i] 9 results, ~410 of a 2000-token budget.
```

A tool result goes straight into the model's context, so a silently shortened list would read as a complete one and the model would reason from an absence that is not there. The header exists so that cannot happen.

## Use it from Python instead

The tool bodies are plain functions, so a host that already has a `Vectrix` can call them without the `mcp` package:

```python
from vectrixdb import Vectrix
from vectrixdb.mcp_server import tool_context

db = Vectrix("memory", path="./data")
db.remember("we decided to ship on Friday", session="u42", role="user")

print(tool_context(db, "what did we decide", session="u42", token_budget=800))
```

`build_server(db)` returns the configured `FastMCP` instance for hosts that want to mount it themselves.
