"""Headers every reply carries, so a browser holds each one to what it is.

=====================================  ======================================
``Content-Security-Policy``            The dashboard may load its own files,
                                       the fonts and the one chart library it
                                       names, and nothing else; it may not be
                                       framed, may not post a form elsewhere
                                       and may not change its base address.
                                       A reply that is not a page may do
                                       nothing at all.
``Strict-Transport-Security``          Over https: a year, subdomains too.
``X-Content-Type-Options``             ``nosniff``: a reply is what it says.
``X-Frame-Options``                    ``DENY``, for browsers older than
                                       ``frame-ancestors``.
``Referrer-Policy``                    ``no-referrer``: an address with a
                                       collection's name stays here.
``Permissions-Policy``                 No camera, microphone, location,
                                       payment or USB for any page.
``Cross-Origin-Opener-Policy``         A page opened from here keeps no handle
                                       on it.
``Cache-Control``                      ``no-store`` on the API and sign-in
                                       replies, which carry chunks and codes.
=====================================  ======================================

The dashboard still has inline event handlers on its buttons, so inline
script is allowed on it; a script from anywhere but here, the fonts service
and the chart library's host is not. A company that shows the dashboard
inside its own portal names that portal in ``VECTRIXDB_FRAME_ANCESTORS``.
The interactive API reference at ``/docs`` loads its viewer from a CDN and
keeps its own rules.

A header the route already set is left alone: a logo's stricter policy stays.
"""

from __future__ import annotations

import os
import re
from typing import Any, Awaitable, Callable, Mapping, Optional

from ..exceptions import ConfigurationError

__all__ = ["SecurityHeadersMiddleware", "dashboard_policy", "frame_ancestors_from_env"]


# ============================================================================
# SETTINGS: the hosts, the policies, and the origin
# ============================================================================
#
# Where scripts, styles and fonts may load from, the API's own policy, the
# HSTS header, and the origin the rules are written for.

#: Where the dashboard's page loads anything from besides this server.
SCRIPT_HOSTS = "https://cdnjs.cloudflare.com"
STYLE_HOSTS = "https://fonts.googleapis.com"
FONT_HOSTS = "https://fonts.gstatic.com"
#: A reply that is not a page: nothing to run, nothing to frame.
API_POLICY = "default-src 'none'; frame-ancestors {ancestors}"
HSTS = "max-age=31536000; includeSubDomains"
_OWN_RULES = ("/docs", "/redoc")
_ORIGIN = re.compile(r"^https://[a-z0-9.-]+(?::\d+)?$|^'self'$", re.I)


# ============================================================================
# THE POLICIES
# ============================================================================
#
# INPUT   the environment; the host the dashboard talks to
# OUTPUT  who may show the dashboard in a frame, nobody unless origins are
#         named; the dashboard page's policy, with its live connection let
#         back in
#
# Named origins only: a frame is how a dashboard gets copied into somebody
# else's page.


def frame_ancestors_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """Who may show the dashboard in a frame: nobody, unless origins are named."""
    env = os.environ if env is None else env
    raw = str(env.get("VECTRIXDB_FRAME_ANCESTORS", "") or "").split()
    if not raw:
        return "'none'"
    wrong = [value for value in raw if not _ORIGIN.match(value)]
    if wrong:
        raise ConfigurationError(
            f"VECTRIXDB_FRAME_ANCESTORS names {wrong[0]!r}. Name https origins, separated by spaces, "
            "for example https://portal.example.com"
        )
    return " ".join(raw)


def dashboard_policy(host: str, ancestors: str = "'none'") -> str:
    """The dashboard page's policy. ``host`` lets its live connection back in."""
    live = f" ws://{host} wss://{host}" if host and re.fullmatch(r"[A-Za-z0-9.:\[\]-]+", host) else ""
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' {SCRIPT_HOSTS}",
            f"style-src 'self' 'unsafe-inline' {STYLE_HOSTS}",
            f"font-src 'self' {FONT_HOSTS}",
            "img-src 'self' data: blob:",
            f"connect-src 'self'{live}",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            f"frame-ancestors {ancestors}",
        ]
    )


# ============================================================================
# THE MIDDLEWARE
# ============================================================================
#
# INPUT   every HTTP reply
# OUTPUT  the headers added to each reply that does not already carry them
#
# So a browser holds each reply to what it is: the dashboard may load its own
# files and the fonts it names, and nothing else runs.


class SecurityHeadersMiddleware:
    """Adds the headers above to every HTTP reply that does not already carry them."""

    def __init__(self, app: Callable[..., Awaitable[Any]], *, ancestors: str = "'none'", https: bool = False) -> None:
        self.app = app
        self.ancestors = ancestors
        self.https = https

    async def __call__(self, scope: dict, receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        asked = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        secure = self.https or scope.get("scheme") == "https" or asked.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
        host = asked.get("host", "")

        async def wrapped(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {k.decode("latin-1").lower() for k, _ in headers}

                def put(name: str, value: str) -> None:
                    if name not in have:
                        headers.append((name.encode("latin-1"), value.encode("latin-1")))
                        have.add(name)

                kind = next((v.decode("latin-1") for k, v in headers if k.decode("latin-1").lower() == "content-type"), "")
                page = kind.startswith("text/html")
                # A 304 has no body and no type, and the browser copies its headers
                # onto the page it kept: a policy here would replace the page's own.
                fresh = message.get("status") != 304
                if fresh and not path.startswith(_OWN_RULES):
                    put("content-security-policy", dashboard_policy(host, self.ancestors) if page else API_POLICY.format(ancestors=self.ancestors))
                put("x-content-type-options", "nosniff")
                if self.ancestors == "'none'":
                    put("x-frame-options", "DENY")
                put("referrer-policy", "no-referrer")
                put("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
                if page and fresh:
                    put("cross-origin-opener-policy", "same-origin")
                if secure:
                    put("strict-transport-security", HSTS)
                if path.startswith(("/api/", "/auth/")):
                    put("cache-control", "no-store")
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)
