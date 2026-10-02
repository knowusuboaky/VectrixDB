"""Every setting this service reads, in one place.

Read from the environment, and from a .env file beside the Backend when one is
there, with the environment winning: a platform's own settings are never
overridden by a file somebody copied. Nothing here is written down anywhere
else, and the upstream's key never leaves this process.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

#: The Backend folder, so the built pages are found beside it.
BACKEND = Path(__file__).resolve().parents[2]

#: The only paths forwarded to the retrieval service. Everything else this
#: service either answers itself or does not serve: a forwarder that takes any
#: path is a way into whatever the upstream can reach.
#:
#: /auth is not under /api: the sign-in, passkey and identity provider routes
#: sit at the root, and leaving them out breaks signing in with no clue why.
#:
#: /docs and /openapi.json are the retrieval service's own API reference, which
#: the account menu and the Console page both link to. This service documents
#: nothing of its own, so the reference a reader wants is the one upstream.
#:
#: /brand.json, /brand.css and /brand/ are the company's name, colours and
#: logo, set once on the retrieval service and shown by both dashboards.
FORWARDED: Tuple[str, ...] = ("/api", "/auth", "/health", "/docs", "/openapi.json", "/brand.json", "/brand.css", "/brand")


def forwardable(path: str, prefixes: Tuple[str, ...] = FORWARDED) -> bool:
    """Whether this path is one of the forwarded ones. ``/healthz`` is not ``/health``."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _names(value: str, what: str) -> str:
    """``/one/two``, or ``""``: a path of names, whatever it was written as."""
    parts = [part for part in str(value or "").strip().split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"{what} is a path of names, not {value!r}")
    return "/" + "/".join(parts) if parts else ""


def gateway_paths(value: str) -> Dict[str, str]:
    """Each route's own path on the gateway in front of the retrieval service, route=path, as its team hands them over.

    Read as the retrieval service reads its own VECTRIXDB_GATEWAY_PATHS: a
    route is named with no prefix, alone or as a family, and the longest name
    that fits a path is the one that counts.
    """
    paths: Dict[str, str] = {}
    for entry in str(value or "").split(","):
        if not entry.strip():
            continue
        route, equals, where = entry.partition("=")
        name, path = _names(route, "a route")[1:], _names(where, "a gateway path")
        if not equals or not name or not path:
            raise ValueError(f"UPSTREAM_GATEWAY_PATHS: a gateway path is route=path with both given, not {entry.strip()!r}")
        if name in paths:
            raise ValueError(f"UPSTREAM_GATEWAY_PATHS: {name} is given two gateway paths")
        paths[name] = path
    return paths


def _from_file(path: Path) -> dict:
    """NAME=value lines, as the walkthrough's own scripts read theirs. Comments and blanks are skipped."""
    found: dict = {}
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[name.strip()] = value
    return found


@dataclass(frozen=True)
class Settings:
    """Where the retrieval service is, what it is called with, and where the built pages are."""

    #: The retrieval service: the Function App that ingests documents and answers retrieval.
    upstream: str = ""
    #: Its key, held here and never sent to a browser. Empty when it needs none.
    key: str = ""
    #: The header its key goes in, which is what the library's key layer reads.
    key_header: str = "api-key"
    #: The Vite build this service serves at /.
    site: Path = BACKEND.parent / "Frontend" / "dist"
    #: Azure ends an HTTP request at 230 seconds, so waiting longer waits for nothing.
    timeout: float = 230.0
    forwarded: Tuple[str, ...] = FORWARDED
    #: Behind a gateway: the path every route of the retrieval service lives under, VECTRIXDB_PREFIX there.
    prefix: str = ""
    #: And each route's own gateway path, VECTRIXDB_GATEWAY_PATHS there. UPSTREAM is then the gateway's address alone.
    paths: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """The settings as the environment and the .env file give them."""
        source: dict = {**_from_file(BACKEND / ".env"), **(os.environ if env is None else dict(env))}
        settings = cls(
            upstream=str(source.get("UPSTREAM", "")).strip().rstrip("/"),
            key=str(source.get("UPSTREAM_KEY", "")).strip(),
            key_header=str(source.get("UPSTREAM_KEY_HEADER", "") or "api-key").strip(),
            timeout=float(source.get("TIMEOUT", "") or 230.0),
            prefix=_names(source.get("UPSTREAM_PREFIX", ""), "UPSTREAM_PREFIX"),
            paths=gateway_paths(source.get("UPSTREAM_GATEWAY_PATHS", "")),
        )
        where = str(source.get("SITE_DIR", "")).strip()
        return replace(settings, site=Path(where).expanduser().resolve()) if where else settings

    @property
    def ready(self) -> bool:
        """Whether there is an upstream to forward to. Without one, every forwarded call says so."""
        return bool(self.upstream)

    def upstream_path(self, path: str) -> str:
        """Where a route is on the retrieval service, as its gateway publishes it: the route's gateway path, the prefix, the route."""
        bare = path.split("?", 1)[0].strip("/")
        name = max((n for n in self.paths if bare == n or bare.startswith(n + "/")), key=len, default=None)
        return f"{self.paths[name] if name else ''}{self.prefix}{path}"

    @property
    def built(self) -> bool:
        """Whether the pages have been built yet."""
        return (self.site / "index.html").exists()
