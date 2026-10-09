"""Who may do what: one table, read by the server and rendered by the dashboard.

A request is first placed: its method and path name an action, such as
``content.read`` or ``search``. Then the table is asked whether the caller's
role holds that action. Denial is the default twice over. A path nothing
here recognises names no action, and only an admin may call it, so a route
added later is closed to everyone else until somebody decides who it is for.
And a role the table does not know holds nothing, so a damaged session fails
closed instead of inheriting whatever the first role happens to allow.

The roles, from least to most:

``viewer``
    Sees that collections exist, how big and how healthy they are, and which
    builds they hold. Sees chunk ids and sources in listings, never chunk
    text or metadata.
``operator``
    Everything a viewer sees, plus the content itself: search, chunk text,
    stored documents, and writes.
``admin``
    Everything, including the audit trail, the access log, the list of people
    and deleting a collection. Downloading the golden questions an evaluation
    used is an admin's, and only when signed in as a person: the server's own
    key holds the admin role too, and a key is a script.

``reader`` is the read-only API key: every GET an operator may make, and no
writes. It exists so that key keeps meaning what it has always meant. A named
key made from the dashboard is a ``reader``, a ``searcher`` (reads and
searches) or an ``operator``; never an admin, because a script has no business
managing people.

``guest`` is somebody who has not signed in, on a server that lets guests
browse. It may look at a collection and search it, and only a collection an
admin shared with everyone: the middleware turns every other one into a 404.

One action is given to people, not to a role. ``document.read`` opens a whole
stored document, which is a larger disclosure than a chunk of four hundred
characters, so an operator does not hold it by being an operator. An admin
holds it, and gives it to the operators who need it, by name. ``GRANTABLE``
lists what may be given that way, and nothing else may be.

Some changes need a recent proof that it is really the person: ``STEP_UP``.
Deleting a collection, changing who has access, sharing a collection, making
a key, or changing one's own ways to sign in all ask again for the code, the
passkey or single sign-on when the last proof is more than ten minutes old.
A session left open on a desk is then good for reading, not for taking over.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

__all__ = [
    "ACTIONS",
    "ADMIN",
    "GRANTABLE",
    "GRANTS",
    "GUEST",
    "KEY_ROLES",
    "OPERATOR",
    "READER",
    "ROLES",
    "SEARCHER",
    "STEP_UP",
    "VIEWER",
    "action_for",
    "can",
    "clean_grants",
    "needs_step_up",
    "sees_content",
    "table",
]


# ============================================================================
# SETTINGS: the roles, the grants, the actions, and the routes
# ============================================================================
#
# The six roles from least to most, what each holds, the actions a request can
# name, and the routes that name them, specific before general.

VIEWER = "viewer"
OPERATOR = "operator"
ADMIN = "admin"
READER = "reader"
SEARCHER = "searcher"
GUEST = "guest"

ROLES = (VIEWER, OPERATOR, ADMIN)
#: What a named API key may be, least to most.
KEY_ROLES = (READER, SEARCHER, OPERATOR)

_SEE = frozenset({"meta.read", "content.index", "evaluation.read"})
_READ = _SEE | {"content.read"}
#: What anybody signed in may do about their own ways in and their own sessions.
_SELF = frozenset({"self.manage", "self.secure"})

#: Actions an admin may give to one person on top of their role.
GRANTABLE = frozenset({"document.read"})

GRANTS: dict[str, frozenset[str]] = {
    VIEWER: frozenset(_SEE | _SELF),
    # The read-only key is a machine's, and it has always fetched documents.
    READER: frozenset(_READ | {"document.read", "mcp.connect"}),
    SEARCHER: frozenset(_READ | {"search", "mcp.connect"}),
    OPERATOR: frozenset(
        _READ
        | _SELF
        | {"search", "content.write", "collection.create", "collection.maintain", "mcp.connect"}
    ),
    # An admin holds every action by name as well as the unnamed ones, so the
    # table reads whole.
    ADMIN: frozenset(
        _READ
        | _SELF
        | {
            "search",
            "content.write",
            "collection.create",
            "collection.maintain",
            "collection.delete",
            "collection.share",
            "document.read",
            "audit.read",
            "access.read",
            "access.check",
            "evaluation.golden",
            "people.manage",
            "keys.manage",
            "cache.clear",
            "about.read",
            "mcp.connect",
        }
    ),
    # Somebody not signed in, on a server with guests on: what is shared, and how the setups scored. Never a search.
    GUEST: frozenset({"meta.read", "evaluation.read"}),
}

#: Changes that need a proof it is the person from the last ten minutes.
STEP_UP = frozenset(
    {"collection.delete", "collection.share", "people.manage", "keys.manage", "self.secure"}
)

#: What each action is, in words, for the dashboard's Access page.
ACTIONS: dict[str, str] = {
    "meta.read": "See collections, their size, health, builds and settings",
    "content.index": "List chunk ids and where a chunk came from, with the text hidden",
    "content.read": "Open a chunk to read its text and metadata, one at a time, each one recorded",
    "document.read": "Open a whole stored document (an admin may give this to a named operator)",
    "evaluation.read": "See how every setup scored against the golden questions, and the three picks",
    "evaluation.golden": "Download the golden questions a run used, their text and reference answers included, when signed in as a person",
    "search": "Run searches",
    "content.write": "Add, change and remove chunks and documents",
    "collection.create": "Create a collection",
    "collection.maintain": "Rebuild an index and extract a graph",
    "collection.delete": "Delete a collection",
    "collection.share": "Choose who may search a collection",
    "cache.clear": "Clear the cache",
    "audit.read": "Read the audit trail",
    "access.read": "Read the access log",
    "access.check": "Check whether somebody may retrieve from a collection, and why, without searching it",
    "people.manage": "Add and remove people, change roles, reset an authenticator",
    "keys.manage": "Make and revoke API keys for scripts",
    "self.manage": "See your own ways to sign in and where you are signed in, and sign out elsewhere",
    "self.secure": "Add or remove your own passkeys, authenticator, password and recovery codes",
    "about.read": "See which VectrixDB this is, its version, its licence and its notice",
    "mcp.connect": "Connect an assistant over MCP, where each tool it calls is checked as a request of its own",
}

_C = r"/api(?:/v[12])?/collections/[^/]+"

# First match wins, so the specific comes before the general.
_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", r"/api/v1/audit", "audit.read"),
    ("POST", r"/api/v1/access/check", "access.check"),
    # Counts a day are for everyone; the route itself keeps the sign-in counts for access.read.
    ("GET", r"/api/v1/access/daily", "meta.read"),
    ("GET", r"/api/v1/growth", "meta.read"),
    ("GET", r"/api/v1/access/readers", "access.read"),
    ("GET", r"/api/v1/access", "access.read"),
    ("*", r"/auth/people(?:/.*)?", "people.manage"),
    ("GET", r"/auth/me/(?:ways|sessions)", "self.manage"),
    ("DELETE", r"/auth/me/sessions/[^/]+", "self.manage"),
    ("POST", r"/auth/me/sessions/end-others", "self.manage"),
    ("POST", r"/auth/step-up(?:/passkey/(?:begin|finish))?", "self.manage"),
    (
        "*",
        r"/auth/me/(?:passkeys(?:/.*)?|authenticator(?:/(?:begin|confirm))?|recovery-codes|password)",
        "self.secure",
    ),
    ("*", r"/api/v1/keys(?:/[^/]+)?", "keys.manage"),
    ("GET", r"/api/v1/policies", "meta.read"),
    ("GET", r"/api/v1/about(?:/licence)?", "about.read"),
    ("GET", r"/api/v1/evaluations/[^/]+/golden", "evaluation.golden"),
    ("GET", r"/api/v1/evaluations(?:/[^/]+)?", "evaluation.read"),
    ("GET", r"/api/v1/chunking/[^/]+/golden", "evaluation.golden"),
    ("GET", r"/api/v1/chunking(?:/[^/]+)?", "evaluation.read"),
    ("PUT", _C + r"/policy", "collection.share"),
    ("DELETE", r"/api/v1/cache", "cache.clear"),
    # searches are POSTs that read
    (
        "POST",
        _C
        + r"/(?:search(?:/[a-z]+)?|text-search|hybrid-search|text-hybrid-search|keyword-search|sparse-search|dense-sparse-search|similar)",
        "search",
    ),
    ("POST", _C + r"/(?:rebuild|graph/extract)", "collection.maintain"),
    # The feeds and pages a collection keeps up with: listed with where its
    # chunks came from, changed and refreshed as writes.
    ("GET", _C + r"/sources", "content.index"),
    ("POST", _C + r"/sources(?:/refresh)?", "content.write"),
    ("DELETE", _C + r"/sources/[^/]+", "content.write"),
    ("POST", _C + r"/(?:points(?:/sparse)?|text-upsert|documents)", "content.write"),
    ("DELETE", _C + r"/(?:points|documents/.+)", "content.write"),
    ("GET", _C + r"/points", "content.index"),
    ("GET", _C + r"/provenance/.+", "content.index"),
    ("GET", _C + r"/documents/.+", "document.read"),
    ("GET", _C + r"/(?:points/.+|graph|documents|quality)", "content.read"),
    ("GET", _C + r"(?:/(?:health|policy|builds|growth))?", "meta.read"),
    ("DELETE", _C, "collection.delete"),
    ("POST", r"/api(?:/v[12])?/collections", "collection.create"),
    ("GET", r"/api(?:/v1)?/collections", "meta.read"),
    ("GET", r"/api/v1/documents/.+", "document.read"),
    ("GET", r"/api/v1/documents", "content.read"),
    ("POST", r"/api/v1/documents", "content.write"),
    ("DELETE", r"/api/v1/documents/.+", "content.write"),
    ("GET", r"/api/v1/?", "meta.read"),
    # Who the caller is and what they may do: theirs to read, whoever they are.
    ("GET", r"/api/v1/whoami", "meta.read"),
    ("GET", r"/api(?:/v1)?/info(?:/extended)?", "meta.read"),
    # Connecting is all the endpoint is: each tool call comes back through here as its own request.
    ("*", r"/mcp", "mcp.connect"),
    ("GET", r"/api/v1/(?:models|extractors|resources|cache/stats|ws/status)", "meta.read"),
)
_COMPILED = tuple(
    (method, re.compile(pattern + r"/?\Z"), action) for method, pattern, action in _ROUTES
)


# ============================================================================
# THE QUESTIONS
# ============================================================================
#
# INPUT   a request's method and path; a role and an action
# OUTPUT  the action a request names, or none; whether the role holds it,
#         denial the default twice over; whether chunk text may be shown to
#         it; whether a change needs a recent proof; what of a grant list may
#         actually be given; the table as rows for the page
#
# A route nothing here recognises is closed to everyone but an admin until
# somebody decides who it is for.


def action_for(method: str, path: str) -> Optional[str]:
    """The action a request names, or None when nothing here recognises it."""
    method = method.upper()
    if method in ("HEAD", "OPTIONS"):
        method = "GET"
    for wanted, pattern, action in _COMPILED:
        if wanted in ("*", method) and pattern.match(path):
            return action
    return None


def can(role: Optional[str], action: Optional[str], extra: Iterable[str] = ()) -> bool:
    """Whether this role holds this action. Unknown role, or unknown action for anyone but an admin: no.

    ``extra`` is what this person was given by name. Only a grantable action
    counts, whatever the list says, and only for somebody who may already
    read content: a grant widens what an operator sees, it does not make a
    viewer into one.
    """
    if role not in GRANTS:
        return False
    if action is None:
        return role == ADMIN
    if action in GRANTS[role]:
        return True
    return action in GRANTABLE and action in set(extra) and "content.read" in GRANTS[role]


def sees_content(role: Optional[str]) -> bool:
    """Whether chunk text and metadata may be shown to this role."""
    return can(role, "content.read")


def needs_step_up(action: Optional[str], method: str) -> bool:
    """Whether a request is a change that needs a recent proof. Looking never does."""
    return action in STEP_UP and method.upper() not in ("GET", "HEAD", "OPTIONS")


def clean_grants(grants: Optional[Iterable[str]]) -> list[str]:
    """What of this list may actually be given. Anything else is dropped, not stored."""
    return sorted({str(g) for g in (grants or ()) if str(g) in GRANTABLE})


def table() -> list[dict]:
    """The grant table as rows, for the page that shows it."""
    return [
        {"action": action, "means": means, **{role: can(role, action) for role in ROLES}}
        for action, means in ACTIONS.items()
    ]
