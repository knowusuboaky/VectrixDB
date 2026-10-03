"""The path a gateway serves this app under, made consistent for everything below.

A gateway does one of two things with the path it publishes the app under:
it forwards ``/vectrixdb/api/v1/...``, or it strips the prefix and sends
``/api/v1/...``. ASGI's own shape is the first one, where ``path`` still holds
the prefix and ``root_path`` names it, and that is what the framework's mounts
and redirects are written against: with ``root_path`` set and the prefix
missing, the dashboard mount answers 404 and a redirect loses the prefix.

So a request that arrives without the prefix gets it back here, once, and
everything below sees one shape. What the layers around the routes need is the
opposite, the path as the app knows it: the sign-in layer looks a route up in
the role table, where a path it cannot place is admin only; the guest rules
match the search routes by pattern; the masking layer recognises a collection
route by pattern. Behind a prefix all three would quietly miss, so they ask
``route_path`` for the path with the prefix taken off.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

__all__ = ["RootPathMiddleware", "route_path"]


# ============================================================================
# THE PATH, WITH THE PREFIX OFF OR PUT BACK
# ============================================================================
#
# INPUT   a request's scope, forwarded whole or with the gateway's prefix
#         stripped
# OUTPUT  the path as the app knows it; a stripped request given its prefix
#         back, so one shape reaches the app
#
# A gateway either forwards /vectrixdb/api/v1/... or strips the prefix and
# sends /api/v1/...; both reach the routes the same way.


def _without_the_prefix(scope: dict) -> str:
    """The path with the scope's root path taken off, if it is on."""
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    if root and path.startswith(root):
        return path[len(root) :] or "/"
    return path


#: Starlette's own, which is what its routers match against. The fallback
#: above says the same thing, for a Starlette that keeps it somewhere else.
_starlette_route_path: Optional[Callable[[Any], str]]
try:
    from starlette import _utils as _starlette_utils
except ImportError:  # pragma: no cover - no Starlette
    _starlette_route_path = None
else:
    # getattr, not a from-import: an annotated name rebound by an import is a
    # redefinition to mypy, and Starlette may move it without notice.
    _starlette_route_path = getattr(_starlette_utils, "get_route_path", None)


def route_path(request: Any) -> str:
    """The path as the app knows it, with any gateway prefix taken off."""
    scope = getattr(request, "scope", request)
    reader = _starlette_route_path if _starlette_route_path is not None else _without_the_prefix
    try:
        return reader(scope) or "/"
    except Exception:  # pragma: no cover - a scope without the keys
        return str(getattr(getattr(request, "url", None), "path", "") or "/")


class RootPathMiddleware:
    """Give a stripped request its prefix back, so one shape reaches the app.

    ``prefix`` is the path the routes live under, ``VECTRIXDB_PREFIX``, inside
    the host's own path: the app's root path is the two together. A gateway
    may take off either one or both, and whichever is missing is put back.
    """

    def __init__(self, app: Callable[..., Any], root_path: str = "", prefix: str = "") -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")
        self.host = root_path.rstrip("/")
        self.root_path = self.host + self.prefix

    async def __call__(self, scope: dict, receive: Any, send: Any) -> Any:
        root = self.root_path
        if root and scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            if path != root and not path.startswith(root + "/"):
                rest = path
                for part in (self.host, self.prefix):
                    if part and (rest == part or rest.startswith(part + "/")):
                        rest = rest[len(part) :]
                        break
                scope = dict(scope)
                scope["path"] = root + rest
                raw = scope.get("raw_path")
                if isinstance(raw, bytes):
                    head = path[: len(path) - len(rest)].encode()
                    scope["raw_path"] = root.encode() + (
                        raw[len(head) :] if head and raw.startswith(head) else raw
                    )
        return await self.app(scope, receive, send)
