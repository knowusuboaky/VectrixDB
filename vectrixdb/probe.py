"""Check a deployed server from the outside, through whatever stands in front of it.

``vectrixdb check`` reads settings and can only say what a start would do.
Most of what goes wrong behind Azure API Management, AWS API Gateway or an
ingress is not in the settings at all: a path the gateway does not publish, a
policy that strips a header, a document that says the wrong base address, a
gateway page where the server's own refusal should be. Those show only from
where the callers are, so this asks the public address the questions a caller
would, and says which answer is not the server's::

    vectrixdb check --url https://apim.company.com/vectrixdb

A gateway that publishes each part of the server under a path of its own is
asked the same way, each route where it is published, and each of its paths
is asked whether it reaches the server. The prefix and the paths come from
the server's own settings, ``VECTRIXDB_PREFIX`` and ``VECTRIXDB_GATEWAY_PATHS``,
or are given to the command::

    vectrixdb check --url https://gateway.example.com --prefix acme --gateway-paths "api/v1=/files/search, auth=/files/auth"

It reads and never writes: every request is a GET, and the one with a key
sends a key that is wrong on purpose, to see whose refusal comes back.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .check import Finding

__all__ = ["Fetch", "probe"]


# ============================================================================
# SETTINGS: the fetch type, the areas, and the shapes a refusal and a wrong key take
# ============================================================================
#
# What a fetch is, so the tests hand one in; the areas a report is grouped by;
# and what a refusal and a wrong key look like when they come back.

#: (url, headers) -> (status, headers lower-cased, body). Swapped for a stand-in in tests.
Fetch = Callable[[str, Mapping[str, str]], Tuple[int, Dict[str, str], bytes]]

AREA = "Gateway"
_REFUSAL = {"ok", "message", "data", "detail"}
_WRONG_KEY = "vx_00000000_a-key-that-is-wrong-on-purpose"
#: A route to ask for each family a gateway path may be given: one that answers a GET.
_ASK_FOR = {
    "api": "/api/v1/collections",
    "api/v1": "/api/v1/collections",
    "auth": "/auth/status",
    "health": "/health",
    "dashboard": "/dashboard/",
}


# ============================================================================
# FETCHING
# ============================================================================
#
# INPUT   a url and headers
# OUTPUT  the reply without following a redirect; its body as JSON
#
# A redirect is an answer too, and is reported as one.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _fetch(url: str, headers: Mapping[str, str]) -> Tuple[int, Dict[str, str], bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        url, headers={"User-Agent": "vectrixdb-check", **headers}, method="GET"
    )
    try:
        with opener.open(request, timeout=15) as reply:
            return (
                reply.status,
                {k.lower(): v for k, v in reply.headers.items()},
                reply.read(2_000_000),
            )
    except urllib.error.HTTPError as refused:
        return (
            refused.code,
            {k.lower(): v for k, v in refused.headers.items()},
            refused.read(2_000_000),
        )


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


# ============================================================================
# THE PROBE
# ============================================================================
#
# INPUT   a deployed server's public address
# OUTPUT  each answer a caller would get, reported by area
#
# vectrixdb check reads settings and can only say what a start would do; this
# asks the running server through whatever stands in front of it.


def probe(url: str, fetch: Optional[Fetch] = None, *, gateway: Any = None) -> List[Finding]:
    """Ask a deployed server's public address what a caller would, and report each answer.

    ``gateway`` is a :class:`vectrixdb.api.gateway.Gateway`, for a server with a
    prefix or gateway paths: each route is then asked where it is published.
    Left out, every route is asked under the address as given.
    """
    from .api.gateway import DEFAULT_TOKEN_HEADER, Gateway

    fetch = fetch or _fetch
    base = url.strip().rstrip("/")
    parts = urlsplit(base)
    found: List[Finding] = []

    def say(level: str, text: str) -> None:
        found.append(Finding(level, AREA, text))

    if parts.scheme not in ("http", "https") or not parts.netloc:
        say(
            "error",
            f"{url!r} is not an address. Give the one people type: https://apim.company.com/vectrixdb",
        )
        return found
    origin = f"{parts.scheme}://{parts.netloc}"
    here = Gateway.at(base)
    gateway = (
        here
        if gateway is None
        else Gateway(
            root=here.root,
            prefix=gateway.prefix,
            paths=gateway.paths,
            key_header=gateway.key_header,
            token_header=gateway.token_header,
            origin=origin,
        )
    )
    # What the OpenAPI document should name as the API's base: the address's own path and the prefix.
    prefix = gateway.root + gateway.prefix

    def ask(
        route: str, headers: Optional[Mapping[str, str]] = None
    ) -> Optional[Tuple[int, Dict[str, str], bytes]]:
        try:
            return fetch(origin + gateway.visible(route), headers or {})
        except (
            Exception
        ) as exc:  # a name that does not resolve, a refused connection, a bad certificate
            say(
                "error",
                f"GET {gateway.visible(route)} did not get an answer: {type(exc).__name__}: {exc}",
            )
            return None

    # 1. Is anything of ours there at all.
    health = ask("/health")
    if health is None:
        return found
    status, _, body = health
    if status == 200 and isinstance(_json(body), dict):
        say(
            "ok", f"{origin}{gateway.visible('/health')} answers, so the address reaches the server"
        )
    else:
        say(
            "error",
            f"GET {gateway.visible('/health')} answered {status}, and not as the server does. Is the path published, and does it reach the server's /health?",
        )
        return found

    # 1b. Each gateway path reaches the server, and in its words.
    for where in sorted(set(gateway.paths.values())):
        names = sorted(name for name, path in gateway.paths.items() if path == where)
        route = next((_ASK_FOR[name] for name in names if name in _ASK_FOR), "/" + names[0])
        reply = ask(route)
        if reply is None:
            continue
        status, _, body = reply
        words = _json(body)
        page = status == 200 and route.endswith("/")  # the dashboard: a page, not JSON
        ours = page or (isinstance(words, dict) and (status == 200 or _REFUSAL <= set(words)))
        if ours:
            say(
                "ok",
                f"the gateway path {where} reaches the server: {gateway.visible(route)} answered {status} as the server does",
            )
        else:
            say(
                "error",
                f"the gateway path {where} did not reach the server: GET {gateway.visible(route)} answered {status}, not as the server does. Is {where} published, and sent on to the server?",
            )

    # 2. The document a client or a gateway team imports says where to call.
    doc = ask("/openapi.json")
    if doc is not None:
        status, _, body = doc
        schema = _json(body) if status == 200 else None
        if not isinstance(schema, dict) or "paths" not in schema:
            say(
                "error",
                f"GET /openapi.json answered {status} and is not the OpenAPI document. Publish it: it is what a client is generated from",
            )
        else:
            servers = [
                str(s.get("url", "")).rstrip("/")
                for s in schema.get("servers") or []
                if isinstance(s, dict)
            ]
            if not prefix:
                say("ok", f"/openapi.json is served, {len(schema['paths'])} paths")
            elif prefix in servers or base in servers or origin + prefix in servers:
                say(
                    "ok",
                    f"/openapi.json is served and says the API is under {prefix}, so a generated client calls the right address",
                )
            elif gateway.prefix and here.root + gateway.prefix not in servers:
                say(
                    "error",
                    f"/openapi.json does not say the API is under {prefix} (it says {servers or 'nothing'}). Set VECTRIXDB_PREFIX={gateway.prefix.strip('/')} on the server, the same as here",
                )
            else:
                say(
                    "error",
                    f"/openapi.json does not say the API is under {prefix} (it says {servers or 'nothing'}). Set VECTRIXDB_PUBLIC_URL={base} on the server, so a generated client and an imported policy get the right base",
                )

    # 3 and 4. Does a key reach the server, and do the server's own words come back.
    # A wrong key is a 401 "Invalid API key" from the server whenever it asks for
    # keys at all. With the header stripped on the way in, the server sees nobody:
    # it answers as it does to nobody, which is a different sentence or a 200.
    def key_reaches(headers: Mapping[str, str], name: str) -> None:
        reply = ask("/api/v1/collections", headers)
        if reply is None:
            return
        status, _, body = reply
        words = _json(body)
        ours = isinstance(words, dict) and _REFUSAL <= set(words)
        if status == 401 and ours and "Invalid API key" in str(words.get("message")):
            say(
                "ok",
                f"a wrong key sent as {name} is refused by the server itself, so the header reaches it and its replies come back whole",
            )
        elif status == 401 and ours:
            say(
                "error",
                f"a wrong key sent as {name} was answered as if no key had been sent ({words.get('message')!r}): the header is being stripped on the way in",
            )
        elif status in (401, 403):
            say(
                "warn",
                f"a wrong key sent as {name} is refused with {status}, but not in the server's words: the gateway answered, or it rewrites error bodies. An app then cannot read why it was refused",
            )
        elif status == 200:
            say(
                "warn",
                f"a wrong key sent as {name} got 200. Either this server asks for no key, or the header is stripped on the way in and the server sees nobody",
            )
        else:
            say(
                "warn",
                f"a wrong key sent as {name} got {status}, which is not an answer the server gives to one",
            )

    key_reaches({gateway.key_header: _WRONG_KEY}, gateway.key_header)
    if gateway.token_header == DEFAULT_TOKEN_HEADER:
        key_reaches({"Authorization": f"Bearer {_WRONG_KEY}"}, "Authorization: Bearer")
    else:
        key_reaches(
            {gateway.token_header: f"Bearer {_WRONG_KEY}"}, f"{gateway.token_header}: Bearer"
        )

    # 5. The dashboard, and that a redirect keeps the path.
    page = ask("/dashboard/")
    if page is not None:
        status, headers, body = page
        if status == 200 and b"<html" in body[:2000].lower():
            say("ok", "the dashboard is served")
        elif status == 404:
            say(
                "warn",
                "GET /dashboard/ is a 404. Fine if only the API is published; if people should see the dashboard, publish /dashboard/*, /brand.json and /auth/*",
            )
        else:
            say("warn", f"GET /dashboard/ answered {status}")
    bare = ask("/dashboard")
    if bare is not None and (prefix or gateway.paths):
        status, headers, _ = bare
        target = headers.get("location", "")
        if status in (301, 302, 307, 308) and target:
            # A relative redirect is resolved against the address asked, which is how a browser reads it.
            landed = urlsplit(urljoin(origin + gateway.visible("/dashboard"), target)).path
            if landed == gateway.visible("/dashboard/"):
                say("ok", "a redirect keeps the path the gateway serves the app under")
            else:
                say(
                    "error",
                    f"GET {gateway.visible('/dashboard')} redirects to {target}, which has lost {prefix or 'its gateway path'}. Set VECTRIXDB_PUBLIC_URL={base} on the server",
                )

    # 6. Two sets of CORS headers break a browser, and one set is only needed for another origin.
    cors = ask("/health", {"Origin": "https://another-origin.example"})
    if cors is not None:
        allow = cors[1].get("access-control-allow-origin", "")
        if "," in allow:
            say(
                "error",
                f"two Access-Control-Allow-Origin values came back ({allow}): the gateway and the server are both adding CORS, and a browser refuses that. Keep it in the gateway policy and leave VECTRIXDB_CORS_ORIGINS unset",
            )
        else:
            # "*" or the asking origin echoed back: which of the two a server
            # that allows any origin sends depends on the Starlette release,
            # so the two read the same. A keys-only server allows any origin
            # on purpose; with sign-in on it names them or sends none.
            say("ok", "one CORS answer at most, so a browser is not given two")
    return found
