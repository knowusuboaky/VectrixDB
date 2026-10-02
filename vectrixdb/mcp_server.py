"""A thin MCP server over a collection, so an assistant can use it as a tool.

Every tool takes a ``token_budget`` and every answer says when it was cut,
because a tool result goes straight into a model's context: a silently
shortened list reads as a complete one, and the model then reasons from an
absence that is not there. The header format follows Graphify's, which
learned the same lesson.

The tool bodies are plain functions over a ``Vectrix`` so they can be tested
without the ``mcp`` package. Only ``build_server`` and ``main`` import it.

Run it with ``vectrixdb-mcp --name notes --path ./data`` or ``vectrixdb mcp``.
"""

from __future__ import annotations

import argparse
import sys
from importlib import import_module
from typing import Any, Optional

from .easy import Results, SearchMode, Vectrix
from .exceptions import DependencyError


__all__ = [
    "DEFAULT_BUDGET",
    "render_results",
    "tool_search",
    "tool_recall",
    "tool_remember",
    "tool_feedback",
    "tool_context",
    "tool_forget",
    "build_server",
    "main",
]


# ============================================================================
# SETTINGS: the default token budget
# ============================================================================
#
# What a tool result is cut to unless the call says otherwise.

DEFAULT_BUDGET = 2000


# ============================================================================
# RESULTS AS TEXT
# ============================================================================
#
# INPUT   results
# OUTPUT  text for a model, with an honest header that says when it was cut
#
# A tool result goes straight into a model's context, so it says what it left
# out.


def render_results(results: Results) -> str:
    """Results as text for a model, with an honest header."""
    total = len(results) + results.cut_count
    if results.truncated:
        header = (
            f"[!] TRUNCATED: showing {len(results)} of {total} results "
            f"(~{results.token_budget}-token budget). The answer may be among the "
            f"{results.cut_count} cut. Raise token_budget or narrow the query."
        )
    elif results.token_budget is not None:
        header = (
            f"[i] {len(results)} results, ~{results.token_estimate} of a "
            f"{results.token_budget}-token budget."
        )
    else:
        header = f"[i] {len(results)} results, ~{results.token_estimate} tokens."
    if results.degraded:
        header += f"\n[i] Answered from vectors only: {results.degraded}."
    if not results.items:
        return header + "\nNo matches."
    lines = [header]
    for r in results.items:
        lines.append(f"- ({r.score:.3f}) [{r.id}] {r.text}")
    return "\n".join(lines)


# ============================================================================
# THE TOOLS
# ============================================================================
#
# INPUT   a collection, a query or a text, a session, and a budget
# OUTPUT  search, recall, remember, feedback, forget and context, each within
#         its budget; forget is the one memory tool that loses information
#
# Every tool takes a token budget and every answer says when it was cut.


def tool_search(
    db: Vectrix,
    query: str,
    limit: int = 10,
    token_budget: int = DEFAULT_BUDGET,
    mode: Optional[SearchMode] = None,
    score_gap: Optional[float] = None,
) -> str:
    return render_results(
        db.search(query, limit=limit, mode=mode, token_budget=token_budget, score_gap=score_gap)
    )


def tool_recall(
    db: Vectrix,
    query: str,
    session: Optional[str] = None,
    limit: int = 10,
    token_budget: int = DEFAULT_BUDGET,
) -> str:
    return render_results(db.recall(query, session=session, limit=limit, token_budget=token_budget))


def tool_remember(
    db: Vectrix,
    text: str,
    session: Optional[str] = None,
    role: str = "user",
    pinned: bool = False,
) -> str:
    memory_id = db.remember(text, session=session, role=role, pinned=pinned)
    what = "Pinned" if pinned else "Remembered"
    where = f" in session {session!r}" if session else ""
    return f"{what} {memory_id}{where}."


def tool_feedback(
    db: Vectrix,
    id: str,
    outcome: str,
    correction: Optional[str] = None,
) -> str:
    new_id = db.feedback(id, outcome, correction=correction)
    if new_id:
        return f"Recorded {outcome} on {id}; superseded by {new_id}."
    return f"Recorded {outcome} on {id}."


def tool_forget(
    db: Vectrix,
    session: Optional[str] = None,
    older_than_days: Optional[float] = None,
    ids: Optional[list] = None,
    superseded: bool = False,
) -> str:
    """Delete turns by age or ids; the one memory tool that loses information."""
    # ``is not None``, not truthiness. An agent whose filter matched nothing
    # sends ids=[], and reading that as "no ids given" fell through to the
    # branch below, which with no session deletes every turn in every
    # session. ConversationMemory.forget already gets this right; this
    # wrapper used to undo it.
    if ids is not None:
        gone = db.forget(ids=ids)
    else:
        gone = db.forget(session=session, older_than=older_than_days, superseded=superseded)
    where = f" in session {session!r}" if session else ""
    return f"Forgot {gone} memor{'y' if gone == 1 else 'ies'}{where}."


def tool_context(
    db: Vectrix,
    query: str,
    session: Optional[str] = None,
    token_budget: int = 1500,
    recent_turns: int = 6,
) -> str:
    block = db.context(query, session=session, token_budget=token_budget, recent_turns=recent_turns)
    header = (
        f"[!] TRUNCATED: {block.cut_count} items cut to fit ~{token_budget} tokens "
        f"(~{block.token_estimate} used). Raise token_budget for more."
        if block.truncated
        else f"[i] ~{block.token_estimate} of a {token_budget}-token budget."
    )
    body = block.text or "Nothing remembered yet."
    return header + "\n\n" + body


# ============================================================================
# THE SERVER
# ============================================================================
#
# INPUT   a collection and a name
# OUTPUT  an MCP server exposing the collection, needing the mcp extra,
#         version 1 or 2
#
# So an assistant can use a collection as a tool.


def build_server(db: Vectrix, name: str = "vectrixdb") -> Any:
    """An MCP server exposing the collection. Needs the ``mcp`` extra, version 1 or 2.

    mcp 2 renamed ``FastMCP`` to ``MCPServer`` and kept what is used here:
    the ``tool`` decorator, ``list_tools`` and ``run(transport=)``. A fresh
    ``pip install vectrixdb[mcp]`` gets version 2, and importing the old name
    there raises, so version 2's name is tried first.
    """
    server_class: Any
    try:
        server_class = import_module("mcp.server.mcpserver").MCPServer
    except (ImportError, AttributeError):
        try:
            server_class = import_module("mcp.server.fastmcp").FastMCP
        except (ImportError, AttributeError) as exc:
            raise DependencyError("mcp", extra="mcp") from exc

    server = server_class(name)

    @server.tool(
        name="search",
        description=(
            "Search the collection. Returns results as text, cut to token_budget; "
            "the first line says whether anything was cut."
        ),
    )
    def search(
        query: str,
        limit: int = 10,
        token_budget: int = DEFAULT_BUDGET,
        mode: Optional[SearchMode] = None,
    ) -> str:
        return tool_search(db, query, limit=limit, token_budget=token_budget, mode=mode)

    @server.tool(
        name="recall",
        description=(
            "Recall conversation memories relevant to a query, weighted by recency "
            "and past feedback. Pass session to stay within one conversation."
        ),
    )
    def recall(
        query: str,
        session: Optional[str] = None,
        limit: int = 10,
        token_budget: int = DEFAULT_BUDGET,
    ) -> str:
        return tool_recall(db, query, session=session, limit=limit, token_budget=token_budget)

    @server.tool(
        name="remember",
        description=(
            "Store a memory. Use session and role for conversation turns; "
            "pinned=true for a fact that should always be in context."
        ),
    )
    def remember(
        text: str,
        session: Optional[str] = None,
        role: str = "user",
        pinned: bool = False,
    ) -> str:
        return tool_remember(db, text, session=session, role=role, pinned=pinned)

    @server.tool(
        name="feedback",
        description=(
            "Grade a recalled memory: useful, dead_end or corrected. With a "
            "correction, the corrected text is stored and supersedes the old memory."
        ),
    )
    def feedback(id: str, outcome: str, correction: Optional[str] = None) -> str:
        return tool_feedback(db, id, outcome, correction=correction)

    @server.tool(
        name="forget",
        description=(
            "Delete memories for good: by ids, or the turns of a session older than "
            "older_than_days, or with superseded=true only facts a correction has "
            "already replaced. Nothing else in this server deletes."
        ),
    )
    def forget(
        session: Optional[str] = None,
        older_than_days: Optional[float] = None,
        ids: Optional[list] = None,
        superseded: bool = False,
    ) -> str:
        return tool_forget(
            db, session=session, older_than_days=older_than_days, ids=ids, superseded=superseded
        )

    @server.tool(
        name="context",
        description=(
            "Build a context block for a prompt: pinned facts, recent turns of the "
            "session, then relevant memories, all under one token budget."
        ),
    )
    def context(
        query: str,
        session: Optional[str] = None,
        token_budget: int = 1500,
        recent_turns: int = 6,
    ) -> str:
        return tool_context(
            db, query, session=session, token_budget=token_budget, recent_turns=recent_turns
        )

    return server


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   the command line
# OUTPUT  the server run
#
# What vectrixdb mcp runs.


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="vectrixdb-mcp", description="Serve a VectrixDB collection over MCP."
    )
    parser.add_argument("--name", default="default", help="Collection name")
    parser.add_argument("--path", default="./vectrixdb_data", help="Storage path")
    parser.add_argument(
        "--mode", default=None, help="Search mode: dense, hybrid, ultimate or graph"
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "sse", "streamable-http"),
        help="MCP transport (stdio is what a desktop assistant starts as a command)",
    )
    args = parser.parse_args(argv)
    db = Vectrix(args.name, path=args.path, mode=args.mode)
    try:
        build_server(db).run(transport=args.transport)
    except DependencyError as exc:
        # A plain install puts this command on the path but does not install
        # the `mcp` package, so this is the first thing a new user sees. A
        # stack trace is the wrong answer to "you need to install something":
        # the message already says what to run.
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":  # pragma: no cover
    main()
