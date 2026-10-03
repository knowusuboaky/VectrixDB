"""The address a caller uses, when a gateway publishes each part of the server under a path of its own.

A gateway team usually hands over one address and, for each endpoint or
family of endpoints, a path of its own in front of the company's prefix:

    https://gateway.example.com / files/search / acme  / api/v1/collections/handbook/text-search
            the gateway          its path       prefix   the route

Four settings describe that, and the host is never one of them:

===============================  ==============================================
``VECTRIXDB_PREFIX``             The path every route lives under: ``/acme``.
                                 Empty, the routes are at the root.
``VECTRIXDB_GATEWAY_PATHS``      Each route's own gateway path, as the gateway
                                 team hands them over: ``api/v1=/files/search,
                                 auth=/files/auth``. A name is one route or a
                                 family, the longest that fits wins.
``VECTRIXDB_KEY_HEADER``         The header an app's key arrives in. ``api-key``.
``VECTRIXDB_TOKEN_HEADER``       The header a person's access token arrives in.
                                 ``Authorization``. Named otherwise, the server
                                 leaves ``Authorization`` to the gateway.
===============================  ==============================================

A request is answered with its gateway path in front or without it, so the
gateway may pass the path on or take it off, and a gateway path opens only its
own routes. The prefix is answered with or without too, the way the root path
always has been, because a gateway may take that off as well. Nothing is
redirected for a slash too many: a redirect names the address the server
sees, and behind a gateway that sends the caller round it.

What the server hands out is written the way the caller reaches it: the
return address single sign-on sends people back to, the path its cookie is
kept for, the links in sign-in emails, and the map the dashboard reads, so
each of its calls goes to the gateway path its route was published under.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

from ..exceptions import ConfigurationError

__all__ = [
    "DEFAULT_KEY_HEADER",
    "DEFAULT_TOKEN_HEADER",
    "Gateway",
    "GatewayPathsMiddleware",
    "declared_paths",
    "read_gateway_paths",
    "route_prefix",
]


# ============================================================================
# SETTINGS: the header names, and what a header name may be
# ============================================================================
#
# The two headers a caller proves itself with, unless the settings name others.

DEFAULT_KEY_HEADER = "api-key"
DEFAULT_TOKEN_HEADER = "authorization"
#: RFC 9110 token characters: what an HTTP header's name may be made of.
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


# ============================================================================
# READING THE SETTINGS
# ============================================================================
#
# INPUT   a prefix and a list of gateway paths, as written
# OUTPUT  ``/one/two`` or ``""``; each name's gateway path
#
# The same reading the extraction service gives its own two settings, so a
# gateway team's list reads the same way for both.


def _names(value: Any, what: str) -> str:
    """``/one/two``, or ``""``: a path of names, whatever it was written as."""
    parts = [part for part in str(value or "").strip().split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ConfigurationError(f"{what} is a path of names, not {value!r}")
    return "/" + "/".join(parts) if parts else ""


def route_prefix(value: Optional[str]) -> str:
    """``/one/two``, or ``""``: a prefix as the routes want it, whatever it was written as."""
    return _names(value, "a route prefix")


def read_gateway_paths(
    value: Union[None, str, Mapping[str, str]], setting: Optional[str] = None
) -> Dict[str, str]:
    """Each route's own gateway path, ``{"api/v1": "/files/search"}``, whatever it was written as.

    The written form is the one a gateway's team hands over, ``route=path``
    pairs with commas between: ``api/v1=/files/search, auth=/files/auth``. A
    route is named as it is served with no prefix. Whether each one is served
    is checked when the app is built. ``setting`` is where the list was read
    from, so a refusal names it.
    """
    lead = f"{setting}: " if setting else ""
    if not value:
        return {}
    if isinstance(value, Mapping):
        pairs: List[Tuple[Any, Any]] = list(value.items())
    else:
        pairs = []
        for entry in str(value).split(","):
            if not entry.strip():
                continue
            route, equals, path = entry.partition("=")
            if not equals:
                raise ConfigurationError(
                    f"{lead}a gateway path is route=path, not {entry.strip()!r}"
                )
            pairs.append((route, path))
    paths: Dict[str, str] = {}
    for route, path in pairs:
        try:
            name, where = _names(route, "a route")[1:], _names(path, "a gateway path")
        except ConfigurationError as exc:
            raise ConfigurationError(f"{lead}{exc}") from None
        if not name or not where:
            given = f"{str(route or '').strip()}={str(path or '').strip()}"
            raise ConfigurationError(
                f"{lead}a gateway path is route=path with both given, not {given!r}"
            )
        if name in paths:
            raise ConfigurationError(f"{lead}{name} is given two gateway paths")
        paths[name] = where
    return paths


def declared_paths(routes: Iterable[Any], prefix: str = "") -> List[str]:
    """Every path an app declares, the routes of the routers it included too, however the framework keeps them."""
    found: List[str] = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found += declared_paths(
                inner.routes,
                prefix + str(getattr(getattr(route, "include_context", None), "prefix", "") or ""),
            )
        elif isinstance(getattr(route, "path", None), str):
            found.append(prefix + route.path)
    return found


def _falls_under(name: List[str], route: List[str]) -> bool:
    """Whether a route as declared begins with these names: ``{name}`` stands for any one, ``{rest:path}`` for the rest."""
    for at, part in enumerate(name):
        if at >= len(route):
            return False
        declared = route[at]
        if declared.startswith("{") and declared.endswith("}"):
            if declared.endswith(":path}"):
                return True
            continue
        if declared != part:
            return False
    return True


def _header(env: Mapping[str, str], name: str, default: str) -> str:
    given = str(env.get(name, "") or "").strip()
    if not given:
        return default
    if not _HEADER_NAME.match(given):
        raise ConfigurationError(f"{name} is {given!r}, which cannot be the name of an HTTP header")
    return given.lower()


# ============================================================================
# THE GATEWAY
# ============================================================================
#
# INPUT   the environment; a route as the app knows it
# OUTPUT  the gateway's settings, checked; the path a caller uses for a route,
#         and the whole address; the map the dashboard is told
#
# One place says how a route is reached, so the sign-in cookies, the emails,
# the redirects and the dashboard cannot disagree.


@dataclass(frozen=True)
class Gateway:
    #: The path of the public address, or VECTRIXDB_ROOT_PATH: the host's own, in front of everything.
    root: str = ""
    prefix: str = ""
    paths: Mapping[str, str] = field(default_factory=dict)
    key_header: str = DEFAULT_KEY_HEADER
    token_header: str = DEFAULT_TOKEN_HEADER
    #: ``https://gateway.example.com``: the public address's scheme and host, with no path.
    origin: str = ""

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *, root: str = "") -> "Gateway":
        source = os.environ if env is None else env
        public = str(source.get("VECTRIXDB_PUBLIC_URL", "") or "").strip().rstrip("/")
        parts = urlsplit(public)
        return cls(
            root=root,
            prefix=route_prefix(source.get("VECTRIXDB_PREFIX", "")),
            paths=read_gateway_paths(
                source.get("VECTRIXDB_GATEWAY_PATHS", ""), "VECTRIXDB_GATEWAY_PATHS"
            ),
            key_header=_header(source, "VECTRIXDB_KEY_HEADER", DEFAULT_KEY_HEADER),
            token_header=_header(source, "VECTRIXDB_TOKEN_HEADER", DEFAULT_TOKEN_HEADER),
            origin=f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else "",
        )

    @classmethod
    def at(cls, public_url: Optional[str]) -> "Gateway":
        """No prefix and no gateway paths: every route at the public address, the way a server without these settings has them."""
        parts = urlsplit(str(public_url or "").strip().rstrip("/"))
        return cls(
            root=_names(parts.path, "the public address's path"),
            origin=f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else "",
        )

    @property
    def shaped(self) -> bool:
        """Whether a prefix or a gateway path is in play, which is what turns slash redirects off."""
        return bool(self.prefix or self.paths)

    @property
    def dressed(self) -> bool:
        """Whether the dashboard's page has to be told the map: a path in front of the routes, or a key header of the settings' own."""
        return bool(self.shaped or self.root) or self.key_header != DEFAULT_KEY_HEADER

    def name_of(self, route: str) -> Optional[str]:
        """The longest name in the list that ``route`` falls under, or None."""
        path = route.split("?", 1)[0].split("#", 1)[0].strip("/")
        best: Optional[str] = None
        for name in self.paths:
            if (path == name or path.startswith(name + "/")) and (
                best is None or len(name) > len(best)
            ):
                best = name
        return best

    def visible(self, route: str) -> str:
        """The path a caller uses for ``route``: the host's own path, the route's gateway path, the prefix, the route."""
        name = self.name_of(route)
        return f"{self.root}{self.paths.get(name, '') if name else ''}{self.prefix}{route}"

    def address(self, route: str, public_url: Optional[str] = None) -> str:
        """The whole address for ``route``, from the public address's scheme and host."""
        origin = self.origin
        if not origin and public_url:
            parts = urlsplit(public_url)
            origin = f"{parts.scheme}://{parts.netloc}"
        return f"{origin}{self.visible(route)}"

    def check(self, routes: Iterable[str]) -> None:
        """Refuse a name no route falls under, so a typing mistake stops the start and is not found by a caller.

        ``routes`` are as the app declares them, ``/api/v1/collections/{name}/text-search``,
        and a part in braces stands for any name, so one collection's route may be listed on its own.
        """
        served = [[part for part in route.strip("/").split("/") if part] for route in routes]
        unknown = sorted(
            name
            for name in self.paths
            if not any(_falls_under(name.split("/"), route) for route in served)
        )
        if unknown:
            raise ConfigurationError(
                f"VECTRIXDB_GATEWAY_PATHS names {', '.join(unknown)}, which no route here falls under. "
                "Name a route as it is served with no prefix, api/v1/collections, or a family of them, api/v1"
            )

    def page_data(self) -> dict:
        """What the dashboard is told, so each call it makes goes where its route was published."""
        return {
            "root": self.root,
            "prefix": self.prefix,
            "paths": dict(self.paths),
            "key_header": self.key_header,
        }

    def page_script(self) -> str:
        """The map as data in the page. Data, not a script: the page's policy runs no inline script."""
        data = json.dumps(self.page_data()).replace("<", "\\u003c")
        return f'<script type="application/json" id="vx-gateway-data">{data}</script>'

    def render_index(self, page: str) -> str:
        """The dashboard's page told the map, with the one link it writes itself pointed where it is published."""
        if not self.dressed:
            return page
        page = page.replace('href="/docs"', f'href="{self.visible("/docs")}"')
        return page.replace("</head>", f"{self.page_script()}</head>", 1)


# ============================================================================
# A REQUEST WITH ITS GATEWAY PATH TAKEN OFF
# ============================================================================
#
# INPUT   a request, with a route's gateway path in front of it or without
# OUTPUT  the same request without it, so the root path and the prefix are
#         what the app sees in front of every route; a gateway path in front
#         of a route it was not given, not found
#
# Outermost, so the root path layer and everything inside it see one shape.


class GatewayPathsMiddleware:
    def __init__(self, app: Any, gateway: Gateway) -> None:
        self.app = app
        self.gateway = gateway
        # Longest first, so /files/search/extra is never read as /files.
        self._wheres = sorted(set(gateway.paths.values()), key=len, reverse=True)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> Any:
        if scope.get("type") in ("http", "websocket") and self._wheres:
            path = scope.get("path", "")
            # The host's own path may still be in front, when the gateway passes it on.
            root = self.gateway.root
            lead = root if root and (path == root or path.startswith(root + "/")) else ""
            inner = path[len(lead) :] or "/"
            for where in self._wheres:
                if inner == where or inner.startswith(where + "/"):
                    rest = inner[len(where) :] or "/"
                    route = rest
                    prefix = self.gateway.prefix
                    if prefix and (route == prefix or route.startswith(prefix + "/")):
                        route = route[len(prefix) :] or "/"
                    name = self.gateway.name_of(route)
                    if name is None or self.gateway.paths.get(name) != where:
                        # A gateway path opens only its own routes.
                        return await self._not_found(scope, receive, send)
                    scope = dict(scope)
                    scope["path"] = lead + rest
                    raw = scope.get("raw_path")
                    if isinstance(raw, bytes):
                        head = (lead + where).encode()
                        if raw.startswith(head):
                            scope["raw_path"] = lead.encode() + (raw[len(head) :] or b"/")
                    break
        return await self.app(scope, receive, send)

    @staticmethod
    async def _not_found(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 4404})
            return
        body = json.dumps(
            {"ok": False, "message": "Not Found", "data": None, "detail": "Not Found"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
