"""MCP on the server: the collections an assistant may search, as the person it acts for.

    POST /mcp                                     the MCP endpoint, streamable HTTP
    GET  /.well-known/oauth-protected-resource    where an MCP client signs its person in

``vectrixdb serve`` answers MCP at ``/mcp`` when ``VECTRIXDB_MCP`` is on.
It is the same server as the REST API and the dashboard, and every tool
call is made to the REST API as the caller, with the caller's own key or
token, through the same front door. So whatever the server decides for a
request it decides for a tool: who the caller is, what their role allows,
which collections a scoped key reaches, each collection's policy, masking,
the access log and the audit trail. Nothing here decides any of it again.

Who may connect:

    a company's sign-in       a person's access token from the identity provider
                              (VECTRIXDB_OIDC_API_AUDIENCE set), found through
                              /.well-known/oauth-protected-resource, so a client
                              given only the URL sends its person to sign in
    a named API key           made on the Access page, as reader, searcher or
                              operator, and if asked for one collection only
    the server's own key      VECTRIXDB_API_KEY, an admin's

A browser's session cookie is not one of them: a page could make an
assistant's request for whoever has the dashboard open. A server with no
key and no sign-in answers MCP only from this machine.

The tools that read: ``whoami``, ``list_collections``,
``describe_collection``, ``list_documents``, ``search`` (with facets),
``similar`` and ``open_source``. The tools that write, only when
``VECTRIXDB_MCP_WRITES`` is on and then only for a role that may:
``create_collection``, ``add_document``, ``delete_document``, ``add_source``
and ``refresh_source``. Collections and their documents are resources too,
``vectrixdb://collections/{collection}`` and
``vectrixdb://collections/{collection}/documents/{document}``, read as the
caller. Every answer is text with an honest first line, cut to a token
budget, because it goes straight into a model's context and a list cut
silently reads as a complete one.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:  # The mcp extra. Without it the module imports, and build_server says what to install.
    from mcp.server.mcpserver import Context
except ImportError:  # pragma: no cover - the extra is not installed
    Context = Any  # type: ignore[misc,assignment,unused-ignore]

logger = logging.getLogger(__name__)

__all__ = [
    "MCP_PATH",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "PROMPTS",
    "RESOURCES",
    "enabled",
    "writes_enabled",
    "mount",
    "protected_resource",
    "render_hits",
]


# ============================================================================
# SETTINGS: on or off, writes, the endpoint
# ============================================================================
#
# MCP is off until VECTRIXDB_MCP says otherwise: a server that never asked for
# a new door does not get one. Writes are off until VECTRIXDB_MCP_WRITES says
# otherwise, whatever a caller's role allows: an assistant that may only
# search cannot be talked into changing a collection.

MCP_PATH = "/mcp"
WELL_KNOWN = "/.well-known/oauth-protected-resource"
_ON = {"1", "true", "yes", "on"}
#: What a tool result is cut to unless the call says otherwise.
DEFAULT_BUDGET = 2000
#: The most a call may ask for: a tool result is read by a model, every token of it.
MAX_BUDGET = 20000
#: The most results a search may ask for.
MAX_LIMIT = 50
#: Search modes, as the dashboard names them: the route each one is and anything it adds.
MODES: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "hybrid": ("text-hybrid-search", {}),
    "dense": ("text-search", {}),
    "keyword": ("keyword-search", {}),
    "rerank": ("text-search", {"rerank": True}),
}
#: The tools every caller is offered, in the order a client lists them.
READ_TOOLS = (
    "list_collections",
    "whoami",
    "describe_collection",
    "list_documents",
    "similar",
    "search",
    "open_source",
)
#: The tools offered only when VECTRIXDB_MCP_WRITES is on, and then judged by the caller's role.
WRITE_TOOLS = (
    "add_document",
    "create_collection",
    "delete_document",
    "add_source",
    "refresh_source",
)
#: The prompts a client offers its person.
PROMPTS = (
    "answer_from_documents",
    "summarise_document",
    "whats_in_collection",
    "compare_documents",
)
#: The resources, as URI templates, each read as the caller.
RESOURCES = (
    "vectrixdb://collections/{collection}",
    "vectrixdb://collections/{collection}/documents/{+document}",
)
#: The callers an MCP client can be. A session cookie is not one: see the module's account.
_CALLERS = ("key", "token")


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this server answers MCP: ``VECTRIXDB_MCP``."""
    values = os.environ if env is None else env
    return str(values.get("VECTRIXDB_MCP", "")).strip().lower() in _ON


def writes_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether ``add_document`` is offered at all: ``VECTRIXDB_MCP_WRITES``."""
    values = os.environ if env is None else env
    return str(values.get("VECTRIXDB_MCP_WRITES", "")).strip().lower() in _ON


def _scopes(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The scopes a client asks the identity provider for: ``VECTRIXDB_MCP_SCOPES``, or the API's own."""
    values = os.environ if env is None else env
    given = str(values.get("VECTRIXDB_MCP_SCOPES", "")).split()
    if given:
        return given
    audience = str(values.get("VECTRIXDB_OIDC_API_AUDIENCE", "")).strip()
    # Entra ID asks for an API's permissions as <audience>/.default; others take the audience's own scopes.
    return [f"{audience.rstrip('/')}/.default"] if audience.startswith("api://") else []


# ============================================================================
# ANSWERS AS TEXT
# ============================================================================
#
# INPUT   a search's results, as the REST API returns them, and a budget
# OUTPUT  text for a model: a first line that says how many and whether any
#         were cut, then each result with its relevance, id and citation
#
# Four characters a token, the estimate the library uses everywhere.


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _budget(asked: Optional[int]) -> int:
    if asked is None:
        return DEFAULT_BUDGET
    return max(200, min(MAX_BUDGET, int(asked)))


def _text_of(hit: Dict[str, Any]) -> str:
    """What a result says: its text, or its highlights when that is what came back."""
    for holder in (hit, hit.get("metadata") or {}, hit.get("payload") or {}):
        for key in ("text", "text_content", "content", "chunk"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())
    marks = hit.get("highlights")
    if isinstance(marks, list) and marks:
        return " … ".join(" ".join(str(m).split()) for m in marks)
    return ""


def _where(hit: Dict[str, Any]) -> str:
    """Where a result came from, as its citation says, or its document."""
    meta = hit.get("metadata") or hit.get("payload") or {}
    for key in ("_vx_citation", "_vx_source", "source", "_vx_doc"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _relevance(hit: Dict[str, Any]) -> str:
    """A result's relevance as a percentage when the server gave one, else its score."""
    meta = hit.get("metadata") or {}
    relevance = hit.get("relevance", meta.get("_vx_relevance"))
    if isinstance(relevance, (int, float)):
        return f"{round(float(relevance) * 100)}%"
    score = hit.get("score")
    return f"score {float(score):.3f}" if isinstance(score, (int, float)) else "unscored"


def render_hits(collection: str, hits: List[Dict[str, Any]], budget: int) -> str:
    """Search results as text for a model, cut to ``budget`` tokens, saying so when they were."""
    shown: List[str] = []
    used = 0
    for n, hit in enumerate(hits, start=1):
        where = _where(hit)
        doc = (hit.get("metadata") or {}).get("_vx_doc")
        line = (
            f"{n}. [{_relevance(hit)}] id={hit.get('id')}"
            + (f" source={where}" if where else "")
            + (f" document={doc}" if doc and doc != where else "")
            + f"\n   {_text_of(hit)}"
        )
        cost = _tokens(line)
        if shown and used + cost > budget:
            break
        shown.append(line)
        used += cost
    cut = len(hits) - len(shown)
    if not hits:
        return f"[i] Nothing in {collection} matched. Try another mode (hybrid finds both meaning and exact words) or fewer words."
    if cut:
        head = (
            f"[!] TRUNCATED: showing {len(shown)} of {len(hits)} results from {collection} "
            f"(~{budget}-token budget). The answer may be among the {cut} cut: raise token_budget or narrow the query."
        )
    else:
        head = f"[i] {len(hits)} result{'' if len(hits) == 1 else 's'} from {collection}, ~{used} of a {budget}-token budget."
    tail = "Cite a result by its source. open_source reads the document around it."
    return "\n".join([head, *shown, tail])


# ============================================================================
# THE REST API, AS THE CALLER
# ============================================================================
#
# INPUT   the MCP request a tool was called on; a method, a path and a body
# OUTPUT  the REST API's answer to the same caller, or a ToolError in words
#
# The call is made to this same application in this process, with the
# caller's own key or token and nothing else of theirs, from their address,
# so the access log and the rate limits see who it was.


def _incoming(ctx: Any) -> Request:
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    if request is None:
        raise _tool_error("This tool needs the server's HTTP endpoint, not a local transport.")
    return request


def _tool_error(message: str) -> Exception:
    from mcp.server.mcpserver.exceptions import ToolError

    return ToolError(message)


def _credentials(request: Request) -> Dict[str, str]:
    """The headers the caller proved who they are with: the key header and the token header, as the settings name them."""
    from .signin import _headers_of

    key_header, token_header = _headers_of(request)
    forwarded = {}
    for name in (key_header, token_header):
        value = request.headers.get(name)
        if value:
            forwarded[name] = value
    return forwarded


def _said(response: Any) -> str:
    """What a refusal says, in the REST API's own words."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:300] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
    return f"HTTP {response.status_code}"


async def _call(
    ctx: Any,
    method: str,
    path: str,
    *,
    body: Any = None,
    content: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    import httpx

    request = _incoming(ctx)
    client = request.scope.get("client") or ("127.0.0.1", 0)
    transport = httpx.ASGITransport(
        app=request.app, client=(client[0], client[1]), root_path=request.scope.get("root_path", "")
    )
    from .. import tracing

    sent = {**_credentials(request), **(headers or {}), "user-agent": "vectrixdb-mcp"}
    # The tool's own request joins the tool's trace, so one trace shows the call and its search.
    tracing.inject(sent)
    base = f"{request.url.scheme}://{request.headers.get('host') or 'localhost'}"
    async with httpx.AsyncClient(transport=transport, base_url=base) as http:
        response = await http.request(
            method, path, json=body, content=content, headers=sent, params=params
        )
    if response.status_code >= 400:
        raise _tool_error(f"{_said(response)} (HTTP {response.status_code})")
    return response


def _collection_path(name: str) -> str:
    return f"/api/v1/collections/{quote(str(name), safe='')}"


# ============================================================================
# THE TOOLS
# ============================================================================
#
# INPUT   the caller's MCP request and the tool's arguments
# OUTPUT  text, each tool one REST call or two made as the caller
#
# The bodies are plain coroutines over a context, so they can be tested with
# a stand-in for the MCP request; build_server gives them their names.


@contextlib.asynccontextmanager
async def _tool_span(tool: str, collection: Optional[str] = None) -> AsyncIterator[Any]:
    """A span for one tool call: which tool, which collection, and whether it was refused."""
    from ..tracing import span

    with span("mcp.tool", tool=tool, collection=collection) as current:
        try:
            yield current
        except Exception:
            current.set(refused=True)
            raise


async def tool_list_collections(ctx: Any) -> str:
    data = (await _call(ctx, "GET", "/api/v1/collections")).json()
    rows = (data.get("data") or data).get("collections", []) if isinstance(data, dict) else []
    if not rows:
        return "[i] No collections you can reach on this server."
    lines = [
        f"[i] {len(rows)} collection{'' if len(rows) == 1 else 's'} you can reach. search takes a collection by name."
    ]
    for row in rows:
        size = row.get("count", row.get("vectors_count", row.get("points_count")))
        note = (
            "policied: who may search it is decided per person"
            if row.get("entitlement_policy")
            else (f"{size} chunks" if isinstance(size, int) else "")
        )
        described = row.get("description") or ""
        lines.append(
            f"- {row.get('name')}"
            + (f": {note}" if note else "")
            + (f". {described}" if described else "")
        )
    return "\n".join(lines)


async def tool_search(
    ctx: Any,
    collection: str,
    query: str,
    mode: str = "hybrid",
    limit: int = 10,
    filter: Optional[Dict[str, Any]] = None,
    token_budget: Optional[int] = None,
    facets: Optional[List[str]] = None,
) -> str:
    if not str(query).strip():
        raise _tool_error("Give a query: the words to search for.")
    if mode not in MODES:
        raise _tool_error(f"mode is one of {', '.join(MODES)}, not {mode!r}.")
    route, extra = MODES[mode]
    body: Dict[str, Any] = {
        "query_text": query,
        "limit": max(1, min(MAX_LIMIT, int(limit))),
        **extra,
    }
    if filter:
        body["filter"] = filter
    note = ""
    try:
        reply = await _call(ctx, "POST", f"{_collection_path(collection)}/{route}", body=body)
    except Exception as refused:
        # Hybrid search reads the text index directly. A policied collection
        # refuses that, since no policy sees the index, and a collection made
        # without one (the REST API's plain default) has nothing to read. Meaning
        # alone works on both, and the answer says it is what was run: the
        # assistant chose nothing, so it should not be told to fix the collection.
        said = str(refused)
        if mode != "hybrid":
            raise
        if "entitlement policy" in said:
            note = (
                "[i] This collection's policy allows search by meaning or by exact words, "
                "not both at once: searched by meaning.\n"
            )
        elif "requires text index" in said:
            note = "[i] This collection has no index of exact words: searched by meaning.\n"
        else:
            raise
        reply = await _call(ctx, "POST", f"{_collection_path(collection)}/text-search", body=body)
    data = reply.json()
    payload = data.get("data", data) if isinstance(data, dict) else {}
    hits = payload.get("results", []) if isinstance(payload, dict) else []
    return note + render_hits(collection, hits, _budget(token_budget)) + _facets(hits, facets)


def _facets(hits: List[Dict[str, Any]], fields: Optional[List[str]]) -> str:
    """How the results spread over each named metadata field: the values and how many have each.

    Counted over the results this search returned, which the line says, so a
    model does not take it for the whole collection.
    """
    if not fields:
        return ""
    lines = [f"\n\n[i] Facets over these {len(hits)} results:"]
    for name in fields:
        counts: Dict[str, int] = {}
        for hit in hits:
            value = (hit.get("metadata") or {}).get(name)
            if value is None or isinstance(value, (dict, list)):
                continue
            counts[str(value)] = counts.get(str(value), 0) + 1
        shown = ", ".join(
            f"{value} {count}"
            for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        lines.append(f"- {name}: {shown or 'not on any of them'}")
    return "\n".join(lines)


async def tool_whoami(ctx: Any) -> str:
    data = (await _call(ctx, "GET", "/api/v1/whoami")).json()
    me = data.get("data", data) if isinstance(data, dict) else {}
    if me.get("method") == "none":
        return "[i] " + str(me.get("note"))
    reach = me.get("collections")
    reach_said = (
        "every collection your role allows"
        if not isinstance(reach, list)
        else "only " + ", ".join(str(c) for c in reach)
    )
    came_in = {"key": "an API key", "token": "your company's sign-in"}.get(
        str(me.get("method")), str(me.get("method"))
    )
    actions = me.get("actions") or []
    return (
        f"[i] You are {me.get('who')}, through {came_in}, with the role {me.get('role')}.\n"
        f"- You reach {reach_said}.\n"
        f"- You may: {', '.join(actions) if actions else 'nothing beyond looking'}."
    )


async def tool_describe_collection(ctx: Any, collection: str) -> str:
    data = (await _call(ctx, "GET", _collection_path(collection))).json()
    info = data.get("data", data) if isinstance(data, dict) else {}
    tags = [str(t) for t in (info.get("tags") or [])]
    fields = info.get("fields") or []
    lines = [f"[i] {collection}"]
    if info.get("description"):
        lines.append(str(info["description"]))
    lines.append(
        f"- {info.get('count', 0)} chunks, {info.get('dimension')} dimensions, {info.get('metric')}."
    )
    lines.append(
        f"- Search: {'hybrid works' if info.get('has_text_index') else 'by meaning only (no index of exact words)'}"
        + (f"; made as {', '.join(tags)}" if tags else "")
        + "."
    )
    lines.append(
        "- Filter on: "
        + (", ".join(fields) if fields else "no metadata fields found")
        + '. A filter is a JSON object, such as {"'
        + (fields[0] if fields else "department")
        + '": "legal"}.'
    )
    if info.get("entitlement_policy") or info.get("policy"):
        lines.append("- A policy decides document by document what each person may see.")
    return "\n".join(lines)


async def tool_list_documents(
    ctx: Any, collection: str, title: Optional[str] = None, limit: int = 50
) -> str:
    data = (await _call(ctx, "GET", f"{_collection_path(collection)}/documents")).json()
    payload = data.get("data", data) if isinstance(data, dict) else {}
    rows = payload.get("documents", []) if isinstance(payload, dict) else []
    wanted = str(title or "").strip().lower()
    if wanted:
        rows = [
            r
            for r in rows
            if wanted in str(r.get("doc_id", "")).lower()
            or wanted in str(r.get("title") or (r.get("metadata") or {}).get("title") or "").lower()
        ]
    total, limit = len(rows), max(1, min(200, int(limit)))
    if not rows:
        return f"[i] No documents{' matching ' + repr(title) if wanted else ''} in {collection}."
    head = (
        f"[i] {total} document{'' if total == 1 else 's'} in {collection}"
        + (f" matching {title!r}" if wanted else "")
        + (f", showing the first {limit}." if total > limit else ".")
    )
    lines = [head]
    for row in rows[:limit]:
        named = row.get("title") or (row.get("metadata") or {}).get("title")
        lines.append(
            f"- {row.get('doc_id')}"
            + (f": {named}" if named and named != row.get("doc_id") else "")
        )
    return "\n".join(lines)


async def tool_similar(
    ctx: Any,
    collection: str,
    id: str,
    limit: int = 10,
    filter: Optional[Dict[str, Any]] = None,
    token_budget: Optional[int] = None,
) -> str:
    body: Dict[str, Any] = {"id": id, "limit": max(1, min(MAX_LIMIT, int(limit)))}
    if filter:
        body["filter"] = filter
    data = (await _call(ctx, "POST", f"{_collection_path(collection)}/similar", body=body)).json()
    payload = data.get("data", data) if isinstance(data, dict) else {}
    hits = payload.get("results", []) if isinstance(payload, dict) else []
    return f"[i] Most like {id}:\n" + render_hits(collection, hits, _budget(token_budget))


async def tool_create_collection(
    ctx: Any, collection: str, description: Optional[str] = None, hybrid: bool = True
) -> str:
    body: Dict[str, Any] = {
        "name": collection,
        "dimension": 384,
        "metric": "cosine",
        "enable_text_index": bool(hybrid),
        "tags": ["hybrid"] if hybrid else ["dense"],
    }
    if description:
        body["description"] = description
    await _call(ctx, "POST", "/api/v2/collections", body=body)
    return f"Made {collection}" + (
        ", searchable by meaning and exact words." if hybrid else ", searchable by meaning."
    )


async def tool_delete_document(ctx: Any, collection: str, document: str) -> str:
    path = f"{_collection_path(collection)}/documents/{quote(str(document), safe='/')}"
    data = (await _call(ctx, "DELETE", path)).json()
    gone = data.get("chunks_removed") if isinstance(data, dict) else None
    return f"Deleted {document} from {collection}" + (
        f": {gone} chunks." if isinstance(gone, int) else "."
    )


async def tool_add_source(
    ctx: Any, collection: str, address: str, every: str = "6h", kind: Optional[str] = None
) -> str:
    body: Dict[str, Any] = {"address": address, "every": every}
    if kind:
        body["kind"] = kind
    data = (await _call(ctx, "POST", f"{_collection_path(collection)}/sources", body=body)).json()
    source = data.get("source") if isinstance(data, dict) else None
    said_id = source.get("id") if isinstance(source, dict) else None
    return (
        f"{collection} now keeps up with {address}, read every {every}."
        + (f" Its id is {said_id}." if said_id else "")
        + " Nothing is written until a refresh: refresh_source reads it now."
    )


async def tool_refresh_source(
    ctx: Any, collection: str, source: Optional[str] = None, force: bool = False
) -> str:
    body: Dict[str, Any] = {"force": bool(force)}
    if source:
        body["source"] = source
    report = (
        await _call(ctx, "POST", f"{_collection_path(collection)}/sources/refresh", body=body)
    ).json()
    if not isinstance(report, dict):
        report = {}
    said = ", ".join(
        f"{report[k]} {k}"
        for k in ("added", "updated", "unchanged", "removed", "failed")
        if isinstance(report.get(k), int)
    )
    return f"Refreshed {source or 'every due source'} in {collection}" + (
        f": {said}." if said else "."
    )


async def tool_open_source(
    ctx: Any,
    collection: str,
    document: str,
    around: Optional[str] = None,
    token_budget: Optional[int] = None,
) -> str:
    path = f"{_collection_path(collection)}/documents/{quote(str(document), safe='/')}"
    text = (await _call(ctx, "GET", path)).text
    budget = _budget(token_budget)
    room = budget * 4
    start = 0
    if around:
        found = text.find(around)
        if found >= 0:
            start = max(0, found - room // 3)
    piece = text[start : start + room]
    head = (
        f"[i] {document} in {collection}, the whole of it ({_tokens(text)} tokens)."
        if len(piece) == len(text)
        else f"[!] PART of {document} in {collection}: characters {start} to {start + len(piece)} of {len(text)} "
        f"(~{budget}-token budget). Pass around= with words from the part you need, or raise token_budget."
    )
    return head + "\n\n" + piece


async def tool_add_document(
    ctx: Any, collection: str, text: str, title: Optional[str] = None
) -> str:
    if not str(text).strip():
        raise _tool_error("Give the document's text.")
    name = (str(title).strip() if title else "") or "from-assistant"
    filename = name.replace("/", "-").replace("\\", "-")
    if not filename.lower().endswith((".md", ".txt")):
        filename += ".md"
    response = await _call(
        ctx,
        "POST",
        f"{_collection_path(collection)}/documents",
        content=str(text).encode("utf-8"),
        headers={"content-type": "application/octet-stream", "x-filename": quote(filename)},
    )
    data = response.json()
    payload = data.get("data", data) if isinstance(data, dict) else {}
    chunks = payload.get("chunks")
    return (
        f"Added {filename} to {collection}"
        + (f": {chunks} chunks" if isinstance(chunks, int) else "")
        + (f", replacing {payload['replaced']}" if payload.get("replaced") else "")
        + "."
    )


# ============================================================================
# THE SERVER
# ============================================================================
#
# INPUT   whether writes are on
# OUTPUT  an MCP server with the tools and a prompt, needing the mcp extra
#
# Instructions a client hands its model say what the tools are for and that
# an answer cites what it used.

INSTRUCTIONS = (
    "This server searches the user's document collections, as the user: it only ever returns "
    "what the user is allowed to read, and a refusal is the server's answer, not an error to "
    "work around. Start with list_collections when you do not know the collection's name, and "
    "describe_collection before filtering, so the filter names a field that exists. search with "
    "mode hybrid suits most questions; keyword suits names, codes and exact phrases; rerank is "
    "slower and orders the best first. similar finds more like a result you already have, by its "
    "id. open_source reads the document around a result when a snippet is not enough, and "
    "list_documents says what a collection holds. Answer from what the results say, cite each "
    "claim by its source, and say so when the results do not answer the question. Every "
    "answer's first line says whether anything was cut. Tools that change a collection are "
    "offered only when the server allows writes; ask the user before deleting anything."
)


def build_server(writes: bool = False) -> Any:
    """The MCP server: tools that call this server's REST API as the caller. Needs the ``mcp`` extra."""
    from importlib import import_module

    from ..exceptions import DependencyError

    try:
        server_class = import_module("mcp.server.mcpserver").MCPServer
        annotations = import_module("mcp.types").ToolAnnotations
    except (ImportError, AttributeError) as exc:
        raise DependencyError("mcp>=2", extra="mcp") from exc

    from .. import __version__

    server = server_class(
        name="vectrixdb", title="VectrixDB", version=__version__, instructions=INSTRUCTIONS
    )
    looks = annotations(read_only_hint=True, open_world_hint=False)

    @server.tool(
        name="list_collections",
        title="List collections",
        description="The collections you can reach on this server, with their size or that a policy decides who may search them.",
        annotations=looks,
    )
    async def list_collections(ctx: Context) -> str:
        async with _tool_span("list_collections", None):
            return await tool_list_collections(ctx)

    @server.tool(
        name="whoami",
        title="Who am I here",
        description="Who you are on this server, how you came in, your role, what it allows, and which collections you reach.",
        annotations=looks,
    )
    async def whoami(ctx: Context) -> str:
        async with _tool_span("whoami", None):
            return await tool_whoami(ctx)

    @server.tool(
        name="describe_collection",
        title="Describe a collection",
        description=(
            "A collection's size, how it can be searched, and the metadata fields a search can "
            "filter on. Call it before writing a filter."
        ),
        annotations=looks,
    )
    async def describe_collection(ctx: Context, collection: str) -> str:
        async with _tool_span("describe_collection", collection):
            return await tool_describe_collection(ctx, collection)

    @server.tool(
        name="list_documents",
        title="List a collection's documents",
        description=(
            "The documents a collection holds, by id and title. title narrows to those whose id or "
            "title contains it. Needs a server that keeps documents."
        ),
        annotations=looks,
    )
    async def list_documents(
        ctx: Context, collection: str, title: Optional[str] = None, limit: int = 50
    ) -> str:
        async with _tool_span("list_documents", collection):
            return await tool_list_documents(ctx, collection, title, limit)

    @server.tool(
        name="similar",
        title="More like this",
        description=(
            "The chunks most like one you already have, by the id a search result gives. filter "
            "narrows by metadata. Judged as you, like a search."
        ),
        annotations=looks,
    )
    async def similar(
        ctx: Context,
        collection: str,
        id: str,
        limit: int = 10,
        filter: Optional[Dict[str, Any]] = None,
        token_budget: int = DEFAULT_BUDGET,
    ) -> str:
        async with _tool_span("similar", collection):
            return await tool_similar(ctx, collection, id, limit, filter, token_budget)

    @server.tool(
        name="search",
        title="Search a collection",
        description=(
            "Search a collection, as you. mode: hybrid (meaning and exact words, the default), "
            "dense (meaning), keyword (exact words) or rerank (meaning, re-ranked). filter narrows by "
            'metadata, e.g. {"department": "legal"}. facets names metadata fields to count over '
            "the results. Results come with their relevance, id and source, cut to token_budget; "
            "the first line says whether any were cut."
        ),
        annotations=looks,
    )
    async def search(
        ctx: Context,
        collection: str,
        query: str,
        mode: str = "hybrid",
        limit: int = 10,
        filter: Optional[Dict[str, Any]] = None,
        token_budget: int = DEFAULT_BUDGET,
        facets: Optional[List[str]] = None,
    ) -> str:
        async with _tool_span("search", collection):
            return await tool_search(
                ctx, collection, query, mode, limit, filter, token_budget, facets
            )

    @server.tool(
        name="open_source",
        title="Open the document a result came from",
        description=(
            "Read the document a search result came from, as it was indexed. document is the "
            "result's document; around is words from the result, to read the part around them. "
            "Needs a role that may open whole documents."
        ),
        annotations=looks,
    )
    async def open_source(
        ctx: Context,
        collection: str,
        document: str,
        around: Optional[str] = None,
        token_budget: int = DEFAULT_BUDGET,
    ) -> str:
        async with _tool_span("open_source", collection):
            return await tool_open_source(ctx, collection, document, around, token_budget)

    if writes:

        @server.tool(
            name="add_document",
            title="Add a document",
            description=(
                "Add a text or Markdown document to a collection: the server reads, cuts and "
                "indexes it as it does any document. Needs a role that may write."
            ),
            annotations=annotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def add_document(
            ctx: Context,
            collection: str,
            text: str,
            title: Optional[str] = None,
        ) -> str:
            async with _tool_span("add_document", collection):
                return await tool_add_document(ctx, collection, text, title)

        writes = annotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )

        @server.tool(
            name="create_collection",
            title="Make a collection",
            description=(
                "Make an empty collection. hybrid (the default) searches by meaning and exact words. "
                "Needs a role that may make collections."
            ),
            annotations=writes,
        )
        async def create_collection(
            ctx: Context, collection: str, description: Optional[str] = None, hybrid: bool = True
        ) -> str:
            async with _tool_span("create_collection", collection):
                return await tool_create_collection(ctx, collection, description, hybrid)

        @server.tool(
            name="delete_document",
            title="Delete a document",
            description=(
                "Delete a document and every chunk of it from a collection, for good. Ask the user "
                "first. Needs a role that may write."
            ),
            annotations=annotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def delete_document(ctx: Context, collection: str, document: str) -> str:
            async with _tool_span("delete_document", collection):
                return await tool_delete_document(ctx, collection, document)

        @server.tool(
            name="add_source",
            title="Keep a collection up with a feed or a page",
            description=(
                "Have a collection keep up with a feed or a web page, read every `every` (30m, 6h, "
                "1d). kind is feed or page; left out, the server tells. Nothing is written until "
                "a refresh. Needs a role that may write."
            ),
            annotations=annotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def add_source(
            ctx: Context,
            collection: str,
            address: str,
            every: str = "6h",
            kind: Optional[str] = None,
        ) -> str:
            async with _tool_span("add_source", collection):
                return await tool_add_source(ctx, collection, address, every, kind)

        @server.tool(
            name="refresh_source",
            title="Refresh a collection's sources",
            description=(
                "Read a collection's sources now: one, by its id or address, or every one that is "
                "due; force reads every one. Needs a role that may write."
            ),
            annotations=annotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def refresh_source(
            ctx: Context, collection: str, source: Optional[str] = None, force: bool = False
        ) -> str:
            async with _tool_span("refresh_source", collection):
                return await tool_refresh_source(ctx, collection, source, force)

    @server.prompt(
        name="answer_from_documents",
        title="Answer from my documents",
        description="Answer a question from a collection, citing what it used.",
    )
    def answer_from_documents(question: str, collection: str = "") -> str:
        where = f"the {collection} collection" if collection else "my collections"
        return (
            f"Answer this from {where}: {question}\n\n"
            "Search first (list_collections if you need the name). Use only what the results say, "
            "cite every claim by its source, and say plainly if they do not answer it."
        )

    @server.prompt(
        name="summarise_document",
        title="Summarise a document",
        description="Summarise one document from a collection, citing where each point comes from.",
    )
    def summarise_document(collection: str, document: str) -> str:
        return (
            f"Summarise the document {document} in the {collection} collection. Read it with "
            "open_source (raise token_budget for a long one, or read it part by part with around=). "
            "Give the main points in plain words, each with where in the document it is, and say "
            "what the document does not cover if the reader would expect it to."
        )

    @server.prompt(
        name="whats_in_collection",
        title="What is in this collection",
        description="An overview of a collection: what it holds, how to search it, what to ask it.",
    )
    def whats_in_collection(collection: str) -> str:
        return (
            f"Tell me what the {collection} collection holds. Use describe_collection for its size "
            "and fields, list_documents for what is in it, and one or two searches for its main "
            "topics. End with three questions it can answer well, and cite what you found."
        )

    @server.prompt(
        name="compare_documents",
        title="Compare documents",
        description="Compare two documents from a collection on a question, citing both.",
    )
    def compare_documents(collection: str, first: str, second: str, question: str = "") -> str:
        about = f" on this: {question}" if question else ""
        return (
            f"Compare {first} and {second} in the {collection} collection{about}. Read both with "
            "open_source. Say where they agree, where they differ, and what one covers that the "
            "other does not, citing each point to the document it came from."
        )

    @server.resource(
        "vectrixdb://collections/{collection}",
        name="collection",
        title="A collection",
        description="A collection's size, how it is searched, and its filterable fields, as you see it.",
        mime_type="text/plain",
    )
    async def collection_resource(collection: str, ctx: Context) -> str:
        async with _tool_span("resource.collection", collection):
            return await tool_describe_collection(ctx, collection)

    @server.resource(
        "vectrixdb://collections/{collection}/documents/{+document}",
        name="document",
        title="A document",
        description="A document as it was indexed, for a person to attach to the conversation.",
        mime_type="text/markdown",
    )
    async def document_resource(collection: str, document: str, ctx: Context) -> str:
        async with _tool_span("resource.document", collection):
            path = f"{_collection_path(collection)}/documents/{quote(str(document), safe='/')}"
            return (await _call(ctx, "GET", path)).text

    return server


# ============================================================================
# THE DOOR
# ============================================================================
#
# INPUT   the ASGI request for /mcp, after the access middleware has decided
#         who it is
# OUTPUT  the MCP endpoint's answer, or a 401 that tells a client where to
#         sign its person in
#
# The middleware has already refused a bad key and a role that may not
# connect. This refuses what it lets through for a page: a guest, a session
# cookie, and on a server with no key and no sign-in, anybody not on this
# machine.


def protected_resource(request: Request) -> Dict[str, Any]:
    """RFC 9728's document: this endpoint, and the identity provider that signs its people in."""
    runtime = getattr(request.app.state, "signin", None)
    config = getattr(runtime, "config", None)
    oidc = getattr(config, "oidc", None)
    public = (getattr(config, "public_url", None) or str(request.base_url)).rstrip("/")
    document: Dict[str, Any] = {
        "resource": public + MCP_PATH,
        "resource_name": getattr(getattr(request.app.state, "brand", None), "name", None)
        or "VectrixDB",
        "bearer_methods_supported": ["header"],
        "authorization_servers": [],
    }
    if oidc is not None and getattr(oidc, "api_audience", None):
        document["authorization_servers"] = [oidc.issuer]
        scopes = _scopes()
        if scopes:
            document["scopes_supported"] = scopes
    return document


def _challenge(request: Request, message: str) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        {"ok": False, "message": message, "data": {"signin": True}},
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{base}{WELL_KNOWN}{MCP_PATH}"'},
    )


def refusal(request: Request) -> JSONResponse:
    """The 401 /mcp answers when the caller is nobody: what to connect with, and where to sign in."""
    return _challenge(
        request,
        "Connect with an API key (api-key header, or Authorization: Bearer) or with your organisation's sign-in.",
    )


def challenge_header(base_url: str) -> str:
    """The WWW-Authenticate a 401 from /mcp carries, for the middleware's own refusals."""
    return f'Bearer resource_metadata="{base_url.rstrip("/")}{WELL_KNOWN}{MCP_PATH}"'


class _Door:
    """The ASGI app at /mcp: refuses what the middleware lets through for a page, then hands over to MCP."""

    #: Where a server with no key and no sign-in takes MCP from: this machine.
    LOCAL = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

    def __init__(self, handler: Any) -> None:
        self.handler = handler

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        from .signin import caller_of, get_api_key, runtime_of

        request = Request(scope, receive)
        caller = caller_of(request)
        if runtime_of(request) is not None or get_api_key():
            if caller is None or caller.method not in _CALLERS:
                response = _challenge(
                    request,
                    "Connect with a key or your organisation's sign-in: a dashboard session is not one an assistant may use.",
                )
                return await response(scope, receive, send)
        elif (scope.get("client") or ("", 0))[0] not in self.LOCAL:
            response = _challenge(
                request,
                "This server has no key and no sign-in, so it answers MCP only from its own machine.",
            )
            return await response(scope, receive, send)
        await self.handler(scope, receive, send)


def mount(app: FastAPI) -> Any:
    """Put the MCP endpoint and its discovery document on ``app``; returns the server, whose session manager the lifespan runs."""
    from starlette.routing import Route

    from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
    from mcp.server.transport_security import TransportSecuritySettings

    server = build_server(writes=writes_enabled())
    # Stateless and JSON: every call stands alone, so any copy of the server behind
    # a load balancer can answer it, and no session outlives its request. Host
    # checking is the server's own (ALLOWED_HOSTS), not the MCP package's.
    server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    handler = StreamableHTTPASGIApp(server.session_manager)
    app.router.routes.append(
        Route(
            MCP_PATH,
            endpoint=_Door(handler),
            methods=["GET", "POST", "DELETE"],
            include_in_schema=False,
        )
    )

    async def discovery(request: Request) -> JSONResponse:
        return JSONResponse(protected_resource(request))

    for path in (WELL_KNOWN, WELL_KNOWN + MCP_PATH):
        app.add_api_route(path, discovery, methods=["GET"], include_in_schema=False)
    app.state.mcp = server
    logger.info("MCP at %s, %s", MCP_PATH, "with add_document" if writes_enabled() else "read only")
    return server


@contextlib.asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[None]:
    """The MCP session manager's lifetime, inside the app's."""
    server = getattr(app.state, "mcp", None)
    if server is None:
        yield
        return
    async with server.session_manager.run():
        yield


def describe(app: FastAPI) -> Dict[str, Any]:
    """What /api/v1/info and the dashboard may say about MCP: on or off, and whether it writes."""
    server = getattr(app.state, "mcp", None)
    return {
        "enabled": server is not None,
        "path": MCP_PATH,
        "writes": writes_enabled() and server is not None,
    }
