"""The server's door: who is asking, and whether they may.

Every request that is not a public page passes four checks, in this order,
and the order is the design:

1. **Who.** An API key in the ``api-key`` header (or the one
   ``VECTRIXDB_KEY_HEADER`` names), or a session cookie. With
   sign-in on and neither present, 401, unless the server lets guests browse,
   in which case the caller is a guest and may look at, never search, the
   collections shared with everyone, and read how the setups scored. The dashboard answers a 401 by showing
   the sign-in page.
2. **Forgery.** A browser attaches a cookie to a request whether or not the
   person meant to send it, so a request that changes something and arrives
   on a cookie must also carry the session's token in ``X-CSRF-Token``. A
   page on another site cannot read that token. Missing or wrong, 403. This
   comes after the first check so that somebody whose session has merely
   run out gets the 401 that sends them to sign in, not a 403 that tells
   them nothing.
3. **Role.** The method and path name an action, and the role either holds
   it or does not: :mod:`vectrixdb.signin.roles`. 403. A change that matters
   (deleting a collection, changing who has access, sharing a collection,
   making a key, changing one's own ways in) also needs a proof from the last
   ten minutes that it is the person: 403 with ``step_up``, and the page asks
   for the code, the passkey or single sign-on again.
4. **Entitlement.** For a collection that carries a policy, the person's
   principal goes to the collection with the query and the policy decides
   document by document. That happens in the route, because only the
   collection can do it. What the policy withholds is simply absent.

Then, before the handler runs, a read, a search or a write is written to the
access log. If the log cannot be written the request is refused.

With sign-in off none of this changes what the server has always done: no
key configured means open, a key configured means reads without it and
writes with it. Keys are compared in constant time now; they were not.
"""

from __future__ import annotations

import collections
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

from .replies import collection_not_found, refusal, refusal_content
from .gateway import DEFAULT_KEY_HEADER, DEFAULT_TOKEN_HEADER, Gateway
from .rootpath import route_path

from ..collection_access import Decision, decide
from ..exceptions import CollectionStoreUnavailable, ConfigurationError
from ..signin import SignInConfig, SignInRefused, SignInStore, roles
from ..signin import passkeys as pk
from ..brand import Brand
from ..signin import passwords, qr, totp
from ..signin.access import EVENTS, AccessLog, AccessLogUnavailable
from ..signin.keys import env_secret, key_matches
from ..signin.mail import enrolment_email, lock_email, password_email
from ..signin.oidc import OidcClient, new_verifier
from ..signin.store import LINK_MINUTES, LOCAL_METHODS, STEP_UP_SECONDS, normalise_email

__all__ = [
    "AccessMiddleware",
    "Caller",
    "SignInRuntime",
    "caller_of",
    "device_of",
    "outside_the_scope",
    "presented_key",
    "principal_of",
    "reads_are_open",
    "router",
    "runtime_of",
    "sees_content",
    "session_of",
]


# ============================================================================
# SETTINGS: the cookies, the methods, the public paths, and the guest paths
# ============================================================================
#
# The cookie names, the methods a read-only key may use and what a scoped key
# may also read, the paths anyone may reach, the guest paths, and the window a
# passkey may be made in.

logger = logging.getLogger("vectrixdb.signin")

SID_COOKIE = "vx_sid"
CSRF_COOKIE = "vx_csrf"
OIDC_COOKIE = "vx_oidc"
CSRF_HEADER = "x-csrf-token"


def cookie_name(base: str, secure: bool) -> str:
    """The name a cookie goes by. Over https the session and its forgery token
    are ``__Host-``: the browser then keeps them for this host alone, secure,
    for every path, and no neighbouring subdomain can plant one of its own. The
    single sign-on cookie is limited to one path, which ``__Host-`` forbids,
    so it is ``__Secure-``."""
    if not secure:
        return base
    return ("__Secure-" if base == OIDC_COOKIE else "__Host-") + base


READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}

#: What a key scoped to collections may read besides its own collections: the
#: server describing itself. Nothing here names a collection or returns text.
#: ``/api/v1/collections`` is the listing, and a scoped key's listing holds
#: only its own. Read-only methods only, so making a collection is not on it.
SCOPED_KEY_MAY_ALSO_READ = frozenset(
    {
        "/",
        "/health",
        "/ready",
        "/api/v1/whoami",
        "/openapi.json",
        "/docs",
        "/redoc",
        "/api/v1",
        "/api/v1/",
        "/api/v1/info",
        "/api/v1/models",
        "/api/v1/extractors",
        "/api/v1/collections",
        "/api/info",
        "/api/collections",
    }
)

#: Reached without a key or a session. None of them says anything about what is stored.
PUBLIC_PATHS = {
    "/",
    "/auth/status",
    "/health",
    "/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/auth/me",
    "/brand.json",
    "/brand.css",
    "/brand/logo",
    "/brand/logo-dark",
    "/auth/break-glass",
    "/auth/developer",
    # Where an MCP client is told to sign its person in: RFC 9728, nothing stored in it.
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
}
#: How a session opened by emergency sign-in says how it came in.
BREAK_GLASS = "break_glass"
#: How a session opened by Developer Access says how it came in.
DEVELOPER = "developer"
_PUBLIC_PREFIXES = (
    "/dashboard",
    "/auth/oidc/",
    "/auth/email/",
    "/auth/passkey/",
    "/auth/password/",
)

_COLLECTION = re.compile(r"/api(?:/v[12])?/collections/([^/]+)")
#: The one chunk or document a request opens, so the log can name it.
_ITEM = re.compile(r"/(?:points|provenance|documents)/(.+?)(?:/chunks)?/?\Z")
_LOGGED = {
    "content.index": "read",
    "content.read": "read",
    "document.read": "read",
    "search": "search",
    "content.write": "write",
    "collection.create": "write",
    "collection.maintain": "write",
    "collection.delete": "write",
    "cache.clear": "write",
    "audit.read": "read",
    "access.read": "read",
    "evaluation.golden": "read",
}
#: What a collection's policy gates: reading what is in it. Managing it is the role's business.
RETRIEVAL_ACTIONS = frozenset({"search", "content.index", "content.read", "document.read"})
#: What a guest may reach that is not one collection: the list, who may search each, and which models are here.
_GUEST_PATHS = {
    "/api/v1/collections",
    "/api/collections",
    "/api/v1/models",
    "/api/v1/policies",
    "/api/v1/access/daily",
    "/api/v1/growth",
}
#: The evaluation runs, the Retrieval and Chunking tabs: a guest reads how the setups scored, never the golden questions.
_GUEST_EVALUATIONS = re.compile(r"/api/v1/(?:evaluations|chunking)(?:/[^/]+)?/?\Z")
#: Wrong tries from one address, across every account, before that address waits. High, because an office is one address.
_FROM_ONE_ADDRESS = 50
#: What a guest may reach inside a shared collection: itself and its health. A search is for people who sign in.
_GUEST_INSIDE = re.compile(r"/api(?:/v[12])?/collections/[^/]+(?:/health)?/?\Z")
#: All a person may reach while a server that asks for passkeys waits for their first one.
_WHILE_MAKING_A_PASSKEY = {
    "/auth/me/ways",
    "/auth/me/passkeys/begin",
    "/auth/me/passkeys/finish",
    "/auth/signout",
    "/auth/step-up",
}


# ============================================================================
# THE KEYS, THE CALLER, AND THE RUNTIME
# ============================================================================
#
# INPUT   the api-key header or a Bearer token; the request
# OUTPUT  the full and read-only keys, hashed and compared in constant time;
#         whoever is behind a request once the first check has passed; what
#         the routes and the middleware share, the configuration and the
#         things built from it; the runtime and the caller of a request;
#         whether chunk text may be shown; whether this caller may retrieve
#         from a collection, or None when nothing is gated; the principal a
#         policy judges by; a device in words
#
# Who, then what, then which collection, then a fresh check: the four checks,
# in this order, and the order is the design.


def get_api_key() -> Optional[str]:
    return env_secret(os.environ, "VECTRIXDB_API_KEY")


def get_read_only_key() -> Optional[str]:
    return env_secret(os.environ, "VECTRIXDB_READ_ONLY_API_KEY")


def _hashed(name: str) -> Optional[str]:
    return (os.environ.get(name) or "").strip().lower() or None


def full_key_configured() -> bool:
    return bool(get_api_key() or _hashed("VECTRIXDB_API_KEY_SHA256"))


def reads_are_open() -> bool:
    """Whether, with a key and no sign-in, a read goes through with no key at all.

    That is what the server has always done, so it stays the default; a
    server that should hand its text and vectors to nobody it does not know
    sets ``VECTRIXDB_OPEN_READS=0`` and then every read, the live feed at
    ``/ws`` included, needs the full or the read-only key.
    """
    return (os.environ.get("VECTRIXDB_OPEN_READS", "") or "").strip().lower() not in (
        "0",
        "no",
        "false",
        "off",
    )


def _same(given: Optional[str], wanted: Optional[str]) -> bool:
    return (
        bool(given)
        and bool(wanted)
        and hmac.compare_digest(str(given).encode(), str(wanted).encode())
    )


@dataclass
class Caller:
    """Whoever is behind a request, once the first check has passed."""

    who: str
    role: str
    method: str  # "key", "token", "oidc", "email", "passkey", "break_glass", "developer" or "guest"
    principal: dict = field(default_factory=dict)
    name: Optional[str] = None
    csrf: Optional[str] = None
    grants: list = field(default_factory=list)
    verified_at: float = 0.0
    session_key: str = ""
    email: Optional[str] = None
    subject: Optional[str] = None
    #: The security groups their sign-in said they are in: what a collection's policy reads.
    groups: list = field(default_factory=list)
    #: For a named key scoped to collections: the ones it may reach. Empty is
    #: every collection the role allows, which is what a person always gets.
    collections: tuple = ()
    #: Signed in with single sign-on and on the People list, so they may keep ways of their own too.
    listed: bool = False

    @property
    def sees_content(self) -> bool:
        return roles.sees_content(self.role)

    @property
    def local(self) -> bool:
        """Somebody on this server's own list, as opposed to single sign-on or a key."""
        return self.method in LOCAL_METHODS

    def can(self, action: Optional[str]) -> bool:
        return roles.can(self.role, action, self.grants)


class SignInRuntime:
    """What the routes and the middleware share: the configuration and the things built from it."""

    def __init__(
        self,
        config: SignInConfig,
        *,
        oidc_transport: Any = None,
        product: str = "VectrixDB",
        gateway: Optional[Gateway] = None,
    ) -> None:
        self.config = config
        self.product = product
        #: How a caller reaches each route, which the return address, the cookie's path and the links are written with.
        self.gateway = gateway if gateway is not None else Gateway.at(config.public_url)
        self.store = SignInStore(
            config.store_url or config.store_path, config.secrets, key=config.store_key
        )
        logger.info("sign-in is kept in %s", self.store.where)
        self.store.seed(config.users)
        self.access = AccessLog(config.access_log)
        self.sender = config.make_sender()
        self.oidc = (
            OidcClient(config.oidc, transport=oidc_transport) if config.oidc is not None else None
        )
        if self.oidc is not None:
            # The People list is what single sign-on is checked against too, and a person's record gives their role.
            self.oidc.listed = self.store.person
        #: When a collection was made, by name, or None if there is no such collection. Set by the server.
        self.stamp_of: Callable[[str], Optional[str]] = lambda name: None
        if "email" in config.methods and not getattr(self.sender, "configured", True):
            if config.local:
                logger.warning(
                    "email sign-in is on and no mail server is configured: sign-in links go to this log, which is fine on this machine."
                )
            else:
                logger.warning(
                    "email sign-in is on and no mail server is configured, so sign-in emails are not sent. Set VECTRIXDB_SMTP_URL "
                    "and VECTRIXDB_MAIL_FROM, or make set-up links on the server with: vectrixdb people add / vectrixdb people reset"
                )
        if "email" in config.methods and not self.store.admins():
            logger.warning(
                "No admin yet. To add the first one, run this on the server: vectrixdb people add you@company.com --role admin"
            )
        if config.oidc is not None and config.oidc.allowed_emails == ("*",):
            logger.warning(
                "single sign-on lets in everyone the identity provider puts in a mapped group, because VECTRIXDB_OIDC_ALLOWED_EMAILS "
                "is *. Not everyone in a group should sign in: put them on the People list instead"
            )
        if config.sso_pending:
            logger.warning(
                "single sign-on is asked for and not set up: VECTRIXDB_OIDC_ISSUER and VECTRIXDB_OIDC_CLIENT_ID are empty. Until they are "
                "set, people on the People list sign in with a code by email%s",
                "" if config.own_passkeys else ", and nobody keeps a passkey here",
            )
        if config.developer is not None:
            logger.warning(
                "Developer Access is on, for this machine only: %s",
                ", ".join(
                    f"{name} ({role})" for name, role in sorted(config.developer.accounts.items())
                ),
            )
        if config.break_glass is not None and config.break_glass.open():
            logger.warning(
                "emergency sign-in is on, for %s, until %s. Turn it off when the usual sign-in is back",
                config.break_glass.admin,
                config.break_glass.until_iso,
            )
        elif self.store.close_emergencies():
            # Off, or past its time: the emergency is over, and the password used in it is spent.
            logger.info(
                "emergency sign-in is off, so the password used while it was on is spent: the next emergency needs a new one"
            )

    def close(self) -> None:
        self.store.close()

    def forget(self, name: str) -> None:
        """A collection is gone: its record goes with it, so one made again under the name starts with nobody."""
        self.store.forget_collection(name)

    def cookie(self, base: str) -> str:
        """The name this server gives the cookie ``base``: see ``cookie_name``."""
        return cookie_name(base, self.config.secure_cookies)

    def methods(self) -> dict:
        out: dict = {}
        if self.oidc is not None and self.config.oidc is not None:
            out["oidc"] = {"label": self.config.oidc.label}
        elif self.config.sso_pending:
            # Drawn, and said not to be set up yet: nobody is sent to a provider that is not named.
            out["oidc"] = {"label": self.config.sso_label, "pending": True}
        if "email" in self.config.methods:
            out["email"] = {
                "qr": qr.available(),
                "passkeys": self.config.own_passkeys,
                "passwords": self.config.passwords,
            }
            if self.config.email_stands_in:
                # Drawn only once the single sign-on button has been pressed and found nothing to open.
                out["email"]["stands_in"] = True
            if self.config.require_passkey:
                out["email"]["require"] = "passkey"
        return out

    @property
    def break_glass(self) -> Any:
        """Emergency sign-in while it is on and its time has not run out, else None."""
        glass = self.config.break_glass
        return glass if glass is not None and glass.open() else None

    def session(self, sid: Optional[str]) -> Any:
        """The session this id names, if it is still good.

        One that emergency sign-in opened ends when emergency sign-in does, and
        one that Developer Access opened ends when Developer Access is turned off.
        """
        session = self.store.session(sid, idle_minutes=self.config.idle_minutes)
        if session is not None and (
            (session.method == BREAK_GLASS and self.break_glass is None)
            or (session.method == DEVELOPER and self.config.developer is None)
        ):
            self.store.close_session(sid)
            return None
        return session

    def glass_problem(self) -> Optional[str]:
        """Why emergency sign-in may not start as it is set, or None: its password was used in an emergency that is over."""
        glass = self.break_glass
        if glass is None or not self.store.emergency_spent(glass.admin, glass.mark):
            return None
        return (
            "VECTRIXDB_BREAK_GLASS is on with a password that was used in an earlier emergency, and a password works for one. "
            "Make a new one, keep it in the key vault, and set its hash: vectrixdb break-glass hash"
        )

    def visible(self, route: str) -> str:
        """The path a caller uses for ``route``: its gateway path and the prefix in front, when there are any."""
        return self.gateway.visible(route)

    def address(self, route: str) -> str:
        """The whole address a caller uses for ``route``: where single sign-on returns to, and what an email links to."""
        return self.gateway.address(route, self.config.public_url)

    def needs_a_list(self) -> Optional[str]:
        """Why single sign-on may not start, or None.

        Not everyone in a group should sign in, so somebody has to be named: on
        the People list, or in VECTRIXDB_OIDC_ALLOWED_EMAILS. ``*`` there says
        outright that the groups decide on their own.
        """
        oidc = self.config.oidc
        if oidc is None or oidc.allowed_emails or self.store.people():
            return None
        return (
            "single sign-on is on and nobody is named who may sign in, and not everyone in a group should. Put them on the "
            "People list, with VECTRIXDB_SIGNIN_USERS=you@company.com:admin or on the server: vectrixdb people add "
            "you@company.com --role admin. Or name them in VECTRIXDB_OIDC_ALLOWED_EMAILS. "
            "VECTRIXDB_OIDC_ALLOWED_EMAILS=* lets the groups decide on their own"
        )

    def on_this_machine(self, request: Any) -> bool:
        """Whether the caller is on this machine, going by the connection itself. A header can claim any address."""
        import ipaddress

        if not self.config.local:
            return False
        host = str(getattr(getattr(request, "client", None), "host", "") or "")
        try:
            return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            return False

    def fresh(self, caller: Caller) -> bool:
        return bool(caller.verified_at) and time.time() - caller.verified_at <= STEP_UP_SECONDS

    def step_up_ways(self, caller: Caller) -> list:
        if caller.method in (BREAK_GLASS, DEVELOPER):
            return ["password"]
        sso = ["sso"] if caller.method == "oidc" else []
        if caller.method == "oidc" and not (caller.listed and "email" in self.config.methods):
            return sso
        person = self.store.person(caller.email or "") if caller.email else None
        if person is None or person.disabled:
            return sso
        if not self.config.own_passkeys:
            return sso + (["code"] if person.authenticator else [])
        if self.config.require_passkey and person.passkeys:
            return sso + ["passkey"]
        # Somebody on the list who came in with single sign-on may confirm with their own passkey or code as well.
        return (
            sso
            + (["passkey"] if person.passkeys else [])
            + (["code"] if person.authenticator else [])
        )

    def must_add_passkey(self, email: Optional[str], method: str) -> bool:
        """Somebody on the list who came in with a code on a server that asks for passkeys, and has none yet."""
        if not self.config.require_passkey or method not in LOCAL_METHODS or not email:
            return False
        return not self.store.passkey_ids(email)

    @property
    def rp_id(self) -> str:
        return pk.rp_id_of(self.config.public_url or "")

    @property
    def origin(self) -> str:
        return pk.origin_of(self.config.public_url or "")


def runtime_of(request: Any) -> Optional[SignInRuntime]:
    app = getattr(request, "app", None)
    return getattr(getattr(app, "state", None), "signin", None)


def caller_of(request: Request) -> Optional[Caller]:
    return getattr(request.state, "caller", None)


def session_of(request: Any) -> Any:
    """The session a request's cookie names, if it is still good and this caller may use it. None with sign-in off."""
    runtime = runtime_of(request)
    if runtime is None:
        return None
    sid = runtime.store.unsign(request.cookies.get(runtime.cookie(SID_COOKIE)))
    return _usable(runtime, request, runtime.session(sid))


def sees_content(request: Request) -> bool:
    """Whether chunk text may be shown. True when sign-in is off, since then there are no roles."""
    if runtime_of(request) is None:
        return True
    caller = caller_of(request)
    return caller is not None and caller.sees_content


def decide_for(request: Request, name: str, caller: Optional[Caller]) -> Optional[Decision]:
    """Whether this caller may retrieve from the collection ``name``, or None when nothing is gated.

    Gated means a collection store is configured: the record every server
    reads is where a collection's policy is. Without one, the server serves
    collections as it always has. Raises ``CollectionStoreUnavailable`` when
    the rules cannot be read, which is not a refusal: the caller is told to
    try again, not told no.
    """
    records = getattr(request.app.state, "collection_store", None)
    if records is None:
        return None
    record = records.get(name)
    policy = record.policy_object() if record is not None else None
    if caller is None:
        return decide(policy, collection=name, method="guest")
    return decide(
        policy,
        collection=name,
        method=caller.method,
        email=caller.email or (caller.who if "@" in caller.who else None),
        subject=caller.subject,
        groups=caller.groups,
        host_key=caller.method == "key" and caller.who == "api-key",
        key_scope=caller.collections,
    )


def principal_of(request: Request) -> Optional[dict]:
    """The principal a policy should judge this request by, or None if there is nobody to judge."""
    caller = caller_of(request)
    if runtime_of(request) is None or caller is None or caller.method in ("key", "guest"):
        return None
    principal = dict(caller.principal)
    principal.setdefault("id", caller.who)
    return principal


def device_of(user_agent: Optional[str]) -> str:
    """A browser and a system, in words: "Edge on Windows". Enough to recognise a device, no more."""
    ua = user_agent or ""
    browser = next(
        (
            name
            for token, name in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if token in ua
        ),
        "A browser",
    )
    system = next(
        (
            name
            for token, name in (
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Android", "Android"),
                ("Windows", "Windows"),
                ("Mac OS X", "Mac"),
                ("Macintosh", "Mac"),
                ("Linux", "Linux"),
            )
            if token in ua
        ),
        None,
    )
    return f"{browser} on {system}" if system else browser


# ============================================================================
# THE MIDDLEWARE'S HELPERS: timing, tokens, refusals, scope, and the address
# ============================================================================
#
# INPUT   a search; what was presented; a status and a message; a key's scope
#         against a path; the peer and a trusted gateway's header
# OUTPUT  a search timed and its line written before the reply goes; whether
#         what was presented is a signed token and no key; a 429 that says how
#         long to wait; the one refusal shape; why a scoped key may not have
#         this route, or None; who is calling
#
# A refusal is one shape everywhere, and a wait is said in seconds so a client
# backs off by it.


async def _timed_search(
    request: Request, call_next: Any, runtime: "SignInRuntime", **line: Any
) -> Response:
    """Run a search, then write its line with how long it took, and only then let the reply go.

    Every other read is written down before it is served. A search is written
    after, because the time it took is only known then, and a trend of search
    time is worth having. Nothing is lost by the order: the reply is still
    here when the line is written, and if the line cannot be written the reply
    is a 503 and the results never leave.
    """
    started = time.perf_counter()
    response = await call_next(request)
    took = (time.perf_counter() - started) * 1000.0
    try:
        runtime.access.record("search", status=response.status_code, took_ms=took, **line)
    except AccessLogUnavailable as exc:
        logger.error("%s", exc)
        return _refuse(503, "The access log cannot be written, so this request was not served")
    return response


def _looks_like_a_token(given: str) -> bool:
    """Three runs of base64url with a dot between, which is what a signed token is and no key is."""
    parts = given.split(".")
    return len(parts) == 3 and all(parts) and not given.startswith("vx_")


def _too_many(message: str, wait: int) -> JSONResponse:
    """A 429 that says how long to wait, which is what a client backs off by."""
    return refusal(429, message, data={"retry_after": wait}, headers={"Retry-After": str(wait)})


def _refuse(status: int, message: str, **data: Any) -> JSONResponse:
    headers = {"WWW-Authenticate": "ApiKey"} if status == 401 else None
    return refusal(status, message, data=data or None, headers=headers)


def outside_the_scope(path: str, method: str, scope: Sequence[str]) -> Optional[JSONResponse]:
    """Why a key scoped to collections may not have this route, or None if it may.

    Two rules. A route that names a collection is the key's when the key was
    made for that collection. Every other route is refused unless it is on
    :data:`SCOPED_KEY_MAY_ALSO_READ`, which is the server describing itself.

    Refusing by default is the point. ``/api/v1/documents`` lists every
    document on the server and ``/api/v1/policies`` names every collection:
    neither carries a collection in its path, so a rule written the other way
    round would hand both to a key made for one collection. So would any route
    added after this was written, which nobody would notice.
    """
    asked = _COLLECTION.match(path)
    if asked is not None:
        # Already decoded: the server percent-decodes the path once, and a
        # second unquote would let a key for 'handbook%41' reach 'handbookA'.
        wanted = asked.group(1)
        if wanted in scope:
            return None
        # The reply a collection that is not there gets, to the letter, because
        # to this caller it is not: any difference between the two would say
        # what else this server holds.
        return collection_not_found(wanted)
    if path in SCOPED_KEY_MAY_ALSO_READ and method in READ_ONLY_METHODS:
        return None
    if path == "/mcp":
        # The MCP endpoint names no collection: each tool it runs comes back
        # through this check as its own request, with this key, and is held to
        # the key's collections there.
        return None
    # The route is real and the app was built from a document that lists it, so
    # this one says what is wrong rather than pretending the route is missing.
    named = ", ".join(scope)
    return _refuse(
        403, f"This key reaches {named} and nothing else, so {method} {path} is not its to make."
    )


def _headers_of(request: Any) -> tuple:
    """The header a key arrives in and the one a token arrives in, as the settings name them."""
    gateway = getattr(getattr(getattr(request, "app", None), "state", None), "gateway", None)
    if gateway is None:
        return DEFAULT_KEY_HEADER, DEFAULT_TOKEN_HEADER
    return gateway.key_header, gateway.token_header


def presented_key(request: Any) -> Optional[str]:
    """The key on a request, or on a WebSocket: the ``api-key`` header, or a Bearer token.

    ``api-key`` is the header Qdrant uses and what the dashboard sends.
    ``Authorization: Bearer`` is what a generated client, an HTTP library and
    most gateways reach for, so an app written against the OpenAPI document
    works without a custom header. Whichever is given, it is the same key.

    ``VECTRIXDB_KEY_HEADER`` and ``VECTRIXDB_TOKEN_HEADER`` name others, for
    a gateway that keeps these two for itself. A token header of the
    settings' own takes the token as it is or after ``Bearer``, and then
    ``Authorization`` is the gateway's and is not read at all.
    """
    key_header, token_header = _headers_of(request)
    given = request.headers.get(key_header)
    if given:
        return given
    raw = (request.headers.get(token_header) or "").strip()
    scheme, _, value = raw.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    if token_header != DEFAULT_TOKEN_HEADER and raw and " " not in raw:
        return raw
    return None


def _address(request: Request) -> Optional[str]:
    """Who is calling: the peer, or the address a trusted gateway saw.

    The lockout, the guest limit and the access log all key on this. Behind a
    proxy the peer is the proxy for everybody, so one person's wrong codes
    would lock out everyone; ``VECTRIXDB_TRUSTED_PROXIES`` says whose
    ``X-Forwarded-For`` may be believed, and with nothing named the header is
    ignored.
    """
    from .forwarded import client_address

    networks = getattr(request.app.state, "trusted_proxies", None)
    return client_address(request, networks)


# ============================================================================
# THE MIDDLEWARE: every request through the door
# ============================================================================
#
# INPUT   every request
# OUTPUT  the public pages through; a key, a session or a token identified;
#         the role and the scope checked; the reply, or one refusal shape
#
# Azure is the building and this is the door: one place, so no route decides
# for itself.


class AccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await self._dispatch(request, call_next)
        if response.status_code == 401 and route_path(request) == "/mcp":
            # An MCP client reads the header to find where its person signs in, and
            # its person reads the words, which are about an assistant, not a page.
            from .mcp import refusal

            return refusal(request)
        return response

    async def _dispatch(self, request: Request, call_next: Any) -> Response:
        # The path as the app knows it. Behind a gateway that serves the app
        # under a path, the raw one carries the prefix, the role table cannot
        # place it, and a route nothing places is admin only: every operator,
        # viewer and guest would be refused.
        path, method = route_path(request), request.method
        runtime: Optional[SignInRuntime] = getattr(request.app.state, "signin", None)
        request.state.caller = None

        if path in PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        full_key, read_key = get_api_key(), get_read_only_key()
        full_hash, read_hash = (
            _hashed("VECTRIXDB_API_KEY_SHA256"),
            _hashed("VECTRIXDB_READ_ONLY_API_KEY_SHA256"),
        )
        given_key = presented_key(request)
        if runtime is None and not (full_key or full_hash):
            # No key and no sign-in: open, as it has always been, unless a
            # collection store gates retrieval, when nobody is somebody.
            refused = self._gate(request, None, None, path, method)
            return refused if refused is not None else await call_next(request)

        # ---- 1 · who
        caller: Optional[Caller] = None
        if given_key:
            if key_matches(given_key, full_key, full_hash):
                caller = Caller(who="api-key", role=roles.ADMIN, method="key")
            elif key_matches(given_key, read_key, read_hash):
                caller = Caller(who="read-only-key", role=roles.READER, method="key")
            elif runtime is not None and (named := runtime.store.api_key(given_key)) is not None:
                caller = Caller(
                    who=f"key:{named.name}",
                    role=named.role,
                    method="key",
                    collections=named.collections,
                )
                # The key's own number, or the server's for every key, or none.
                allowance = named.per_minute or runtime.config.key_requests_per_minute
                if allowance:
                    allowed, wait = runtime.store.within_rate(f"key:{named.key_id}", allowance)
                    if not allowed:
                        return _too_many(
                            f"The key {named.name} may make {allowance} requests a minute. Wait {wait} seconds.",
                            wait,
                        )
            elif (
                runtime is not None
                and runtime.oidc is not None
                and runtime.config.oidc is not None
                and runtime.config.oidc.api_audience
                and not request.headers.get(_headers_of(request)[0])
                and _looks_like_a_token(given_key)
            ):
                # An app, for the person using it. They are who the token says,
                # with the role their groups give them here, and what they read
                # is recorded under their name and judged by the policy as
                # theirs. A key is nobody; this is somebody.
                try:
                    known = await run_in_threadpool(runtime.oidc.token_identity, given_key)
                except SignInRefused as exc:
                    runtime.access.record(
                        "signin_failed", method="token", reason=exc.code, address=_address(request)
                    )
                    return _refuse(401, exc.reason)
                caller = Caller(
                    who=known.email or known.subject,
                    role=known.role,
                    method="token",
                    principal=known.principal,
                    name=known.name,
                    grants=roles.clean_grants(known.grants),
                    email=known.email,
                    subject=known.subject,
                    groups=list(known.groups),
                )
            else:
                return _refuse(401, "Invalid API key")
        elif runtime is not None:
            sid = runtime.store.unsign(request.cookies.get(runtime.cookie(SID_COOKIE)))
            session = _usable(runtime, request, runtime.session(sid))
            if session is not None:
                caller = Caller(
                    who=session.email or session.subject,
                    role=session.role,
                    method=session.method,
                    principal=session.principal,
                    name=session.name,
                    csrf=session.csrf,
                    grants=session.grants,
                    verified_at=session.verified_at,
                    session_key=session.key,
                    email=session.email,
                    subject=session.subject,
                    groups=list(session.groups),
                    listed=session.listed,
                )
        request.state.caller = caller

        # A key made for one collection is not a key to the others, nor to the
        # routes that cut across them.
        if caller is not None and caller.collections:
            refused = outside_the_scope(path, method, caller.collections)
            if refused is not None:
                return refused

        if runtime is None:
            return await self._keys_only(request, call_next, caller, full_key or full_hash)

        if caller is None:
            if runtime.config.guests:
                return await self._guest(request, call_next, runtime)
            return _refuse(401, "Sign in to continue", signin=True)

        if path not in _WHILE_MAKING_A_PASSKEY and runtime.must_add_passkey(
            caller.email, caller.method
        ):
            return _refuse(
                403,
                "Add a passkey to continue: this server signs people in with passkeys.",
                must_add_passkey=True,
            )

        # ---- 2 · forgery
        # A forged request rides on a cookie the browser sends by itself. A key
        # and a token are put on the request by whoever made it, so there is
        # nothing to forge.
        if caller.method not in ("key", "token") and method not in READ_ONLY_METHODS:
            if not _same(request.headers.get(CSRF_HEADER), caller.csrf):
                return _refuse(403, "This request did not carry the session's forgery token")

        # Signing out is for whoever is signed in, whatever their role.
        if path == "/auth/signout":
            return await call_next(request)

        # ---- 3 · role
        action = roles.action_for(method, path)
        found = _COLLECTION.match(path)
        collection = found.group(1) if found else None
        opened = _ITEM.search(path)
        item = opened.group(1) if opened and method in READ_ONLY_METHODS else None
        address = _address(request)
        try:
            if not caller.can(action):
                runtime.access.record(
                    "denied",
                    who=caller.who,
                    role=caller.role,
                    method=caller.method,
                    action=action or "unlisted",
                    collection=collection,
                    item=item,
                    route=f"{method} {path}",
                    status=403,
                    address=address,
                )
                return _refuse(
                    403, "Your role does not allow this", role=caller.role, action=action
                )
            if caller.method == "token" and roles.needs_step_up(action, method):
                # These want a proof from the last ten minutes, and an app cannot give one.
                return _refuse(
                    403,
                    "This change needs the person themselves, signed in at the dashboard. An app acting for them cannot make it.",
                )
            if (
                caller.method != "key"
                and roles.needs_step_up(action, method)
                and not runtime.fresh(caller)
            ):
                return _refuse(
                    403,
                    "Confirm it's you to make this change",
                    step_up=True,
                    ways=runtime.step_up_ways(caller),
                )
            # ---- 4 · the collection's own policy: who may retrieve from it
            refused = self._gate(request, runtime, caller, path, method)
            if refused is not None:
                return refused
            if action == "search":
                # Written once the search has run, so the line can say how long
                # it took. The rule it was written first to keep still holds:
                # the reply has not left yet, and it does not leave unrecorded.
                return await _timed_search(
                    request,
                    call_next,
                    runtime,
                    who=caller.who,
                    role=caller.role,
                    method=caller.method,
                    action=action,
                    collection=collection,
                    route=f"{method} {path}",
                    address=address,
                )
            if action in _LOGGED:
                runtime.access.record(
                    _LOGGED[action],
                    who=caller.who,
                    role=caller.role,
                    method=caller.method,
                    action=action,
                    collection=collection,
                    item=item,
                    route=f"{method} {path}",
                    address=address,
                )
        except AccessLogUnavailable as exc:
            logger.error("%s", exc)
            return _refuse(503, "The access log cannot be written, so this request was not served")

        return await call_next(request)

    @staticmethod
    async def _guest(request: Request, call_next: Any, runtime: SignInRuntime) -> Response:
        """Somebody not signed in, on a server that lets guests browse: every collection's name and size, and how the setups scored. Never a search."""
        path, method = route_path(request), request.method
        action = roles.action_for(method, path)
        found = _COLLECTION.match(path)
        request.state.caller = Caller(who="guest", role=roles.GUEST, method="guest")
        if found is None:
            if method in READ_ONLY_METHODS and (
                path.rstrip("/") in _GUEST_PATHS or _GUEST_EVALUATIONS.match(path)
            ):
                return await call_next(request)
            return _refuse(401, "Sign in to continue", signin=True)
        name = found.group(1)
        if not (action == "meta.read" and _GUEST_INSIDE.match(path)):
            # A search, a chunk, a document: sign in first.
            return _refuse(401, "Sign in to continue", signin=True)
        return await call_next(request)

    @staticmethod
    def _gate(
        request: Request,
        runtime: Optional[SignInRuntime],
        caller: Optional[Caller],
        path: str,
        method: str,
    ) -> Optional[Response]:
        """The collection's policy, before anything in it is read: None to go on, or the refusal.

        A collection answers the people its policy names and nobody else, and
        a collection with no policy answers nobody. Only reading what is in it
        is gated: that a collection exists, its name, its size and its health
        are everyone's, so a refusal can say why, with the code the page
        reads. The rules that could not be read are a 503, to try again,
        never no.
        """
        found = _COLLECTION.match(path)
        if found is None:
            return None
        name = found.group(1)
        action = roles.action_for(method, path)
        if action not in RETRIEVAL_ACTIONS:
            return None
        try:
            decision = decide_for(request, name, caller)
        except CollectionStoreUnavailable as exc:
            return refusal(503, str(exc))
        except ConfigurationError as exc:
            return refusal(500, str(exc))
        if decision is None or decision.allowed:
            return None
        if runtime is not None:
            runtime.access.record(
                "denied",
                who=caller.who if caller is not None else "nobody",
                role=caller.role if caller is not None else None,
                method=caller.method if caller is not None else None,
                action=action or "unlisted",
                collection=name,
                route=f"{method} {path}",
                status=403,
                reason=decision.code,
                address=_address(request),
            )
        # Everyone sees that the collection exists, so the refusal can say why: restricted, with the code the page reads,
        # and which kind of policy said it, a token's groups or a store's list.
        return _refuse(
            403, decision.because, code=decision.code, collection=name, policy=decision.method
        )

    @staticmethod
    async def _keys_only(
        request: Request, call_next: Any, caller: Optional[Caller], full_key: Optional[str]
    ) -> Response:
        """Sign-in is off: the behaviour the server has always had, and the collection's policy if one gates it."""
        refused = AccessMiddleware._gate(request, None, caller, route_path(request), request.method)
        if refused is not None:
            return refused
        if not full_key or (caller is not None and caller.role == roles.ADMIN):
            return await call_next(request)
        if request.method in READ_ONLY_METHODS:
            # Open unless VECTRIXDB_OPEN_READS says otherwise: then a read
            # needs a key too, since a GET hands out the text and the vectors.
            if caller is not None or reads_are_open():
                return await call_next(request)
            return _refuse(401, "API key required. Provide api-key header.")
        if caller is not None and roles.action_for(request.method, route_path(request)) in (
            "search",
            "mcp.connect",
        ):
            # A search is a read sent as a POST. Refusing it left the read-only
            # key unable to do the one thing it is for.
            return await call_next(request)
        if caller is not None:
            return _refuse(403, "Read-only API key cannot perform write operations")
        if (
            not reads_are_open()
            and roles.action_for(request.method, route_path(request)) == "search"
        ):
            return _refuse(401, "API key required. Provide api-key header.")
        return _refuse(401, "API key required for write operations. Provide api-key header.")


# ============================================================================
# ROUTES: the session's cookies, and who is signed in
# ============================================================================
#
# INPUT   a request; a session id and its forgery token
# OUTPUT  the runtime, or a refusal when sign-in is off; the session and CSRF
#         cookies set and cleared; a return target that is only ever a path on
#         this server; who is signed in and what they may do, which the
#         dashboard asks first; a new session for somebody on the list
#
# An address somebody else chose is how a sign-in page becomes a phishing
# page, so the return is only ever a path here.

router = APIRouter()


def _need(request: Request) -> SignInRuntime:
    runtime = runtime_of(request)
    if runtime is None:
        raise HTTPException(status_code=404, detail="sign-in is not turned on for this server")
    return runtime


def _set_session_cookies(runtime: SignInRuntime, response: Response, sid: str, csrf: str) -> None:
    age = int(runtime.config.session_hours * 3600)
    secure, same = runtime.config.secure_cookies, runtime.config.samesite
    response.set_cookie(
        runtime.cookie(SID_COOKIE),
        runtime.store.sign(sid),
        max_age=age,
        httponly=True,
        secure=secure,
        samesite=same,
        path="/",
    )
    # Readable on purpose: the page copies it into a header, which a page on another site cannot do.
    response.set_cookie(
        runtime.cookie(CSRF_COOKIE),
        csrf,
        max_age=age,
        httponly=False,
        secure=secure,
        samesite=same,
        path="/",
    )


def _oidc_cookie_path(runtime: SignInRuntime) -> str:
    """The one path the single sign-on cookie is sent to: the return address, as the browser sees it."""
    return runtime.visible("/auth/oidc/callback")


def _clear_cookies(runtime: SignInRuntime, response: Response) -> None:
    secure = runtime.config.secure_cookies
    for base in (SID_COOKIE, CSRF_COOKIE):
        response.delete_cookie(runtime.cookie(base), path="/", secure=secure)
    response.delete_cookie(
        runtime.cookie(OIDC_COOKIE), path=_oidc_cookie_path(runtime), secure=secure
    )


def _safe_return(target: Optional[str], fallback: str = "/dashboard/") -> str:
    """Only ever a path on this server. An address somebody else chose is how a sign-in page becomes a phishing page."""
    target = str(target or "")
    if (
        target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
        and "\n" not in target
        and "\r" not in target
    ):
        return target
    return fallback


def _signin_page(runtime: SignInRuntime, reason: str) -> str:
    """The dashboard's sign-in page, saying why the last try did not finish."""
    return f"{runtime.visible('/dashboard/')}#/signin?error={quote(reason)}"


def _usable(runtime: SignInRuntime, request: Any, session: Any) -> Any:
    """The session, unless Developer Access opened it and the caller is not on this machine."""
    if session is not None and session.method == DEVELOPER and not runtime.on_this_machine(request):
        return None
    return session


def _current_sid(runtime: SignInRuntime, request: Request) -> Optional[str]:
    return runtime.store.unsign(request.cookies.get(runtime.cookie(SID_COOKIE)))


@router.get("/auth/me", tags=["auth"])
async def me(request: Request) -> Any:
    """Who is signed in, and what they may do. The dashboard asks this first."""
    runtime = runtime_of(request)
    if runtime is None:
        return {"ok": True, "data": {"signin": False}}
    session = _usable(runtime, request, runtime.session(_current_sid(runtime, request)))
    if session is None:
        data: dict = {"signin": True, "methods": runtime.methods()}
        if runtime.config.developer is not None and runtime.on_this_machine(request):
            # Said only to a caller on this machine, the one place it answers.
            data["methods"]["developer"] = {}
        if runtime.config.guests:
            data["guests"] = True
        return JSONResponse(
            status_code=401, content=refusal_content("Sign in to continue", data=data)
        )
    from .. import __version__

    out = {
        "signin": True,
        "person": session.public(),
        "csrf": session.csrf,
        "actions": sorted(a for a in roles.ACTIONS if roles.can(session.role, a, session.grants)),
        "sees_content": roles.sees_content(session.role),
        # The People list says who may sign in whichever way they come, so it is there with single sign-on too.
        "people": "email" in runtime.config.methods or runtime.oidc is not None,
        "version": __version__,
        "guests": runtime.config.guests,
        "passwords": runtime.config.passwords,
        "require_passkey": runtime.config.require_passkey,
        "must_add_passkey": runtime.must_add_passkey(session.email, session.method),
        # Whether people keep ways of their own here, which a reset forgets.
        "own_ways": "email" in runtime.config.methods,
        "passkeys": runtime.config.own_passkeys,
        "sso_pending": runtime.config.sso_pending,
    }
    if session.role == roles.ADMIN and "email" in runtime.config.methods:
        out["admins"] = runtime.store.admins()
    # Said to admins, so emergency sign-in is not left on by mistake.
    if session.role == roles.ADMIN and runtime.break_glass is not None:
        out["break_glass"] = {"until": runtime.break_glass.until_iso}
    return {"ok": True, "data": out}


def _sso_first(
    runtime: SignInRuntime, request: Request, person: Any, method: str
) -> Optional[Response]:
    """With VECTRIXDB_SSO_RECHECK_DAYS set: a refusal for somebody who has not signed in with single sign-on lately, or None.

    Their passkey and their code are theirs, not the company's, so the
    identity provider is asked again every so often: somebody who has left
    cannot sign in there, and then not here either.
    """
    days = runtime.config.sso_recheck_days
    if not days or (
        person.last_sso_at is not None and time.time() - float(person.last_sso_at) <= days * 86400
    ):
        return None
    runtime.access.record(
        "signin_failed",
        who=person.email,
        method=method,
        reason="sso_recheck",
        address=_address(request),
    )
    said = (
        "Sign in with single sign-on first. After that, your passkey or code works here too."
        if person.last_sso_at is None
        else f"Sign in with single sign-on again: it has been more than {days} days. After that, your passkey or code works here too."
    )
    return _refuse(403, said, code="sso_recheck", sso=True)


def _signed_in(
    runtime: SignInRuntime, request: Request, email: str, method: str, extra: Optional[dict] = None
) -> Response:
    """A new session for somebody on the list, however they proved it was them."""
    person = runtime.store.person(email)
    assert person is not None
    refused = _sso_first(runtime, request, person, method)
    if refused is not None:
        return refused
    runtime.store.close_session(_current_sid(runtime, request))
    # Somebody on the server's own list is in the groups an admin wrote on
    # their record, which is what a collection's policy reads for them.
    sid, session = runtime.store.open_session(
        subject=person.email,
        email=person.email,
        name=None,
        role=person.role,
        principal=person.principal,
        method=method,
        hours=runtime.config.session_hours,
        grants=person.grants,
        user_agent=request.headers.get("user-agent"),
        address=_address(request),
        groups=[str(g) for g in (person.principal.get("groups") or [])]
        if isinstance(person.principal.get("groups"), (list, tuple))
        else [],
    )
    runtime.access.record(
        "signin", who=person.email, role=person.role, method=method, address=_address(request)
    )
    response = JSONResponse({"ok": True, "data": {"person": session.public(), **(extra or {})}})
    _set_session_cookies(runtime, response, sid, session.csrf)
    return response


def _admin_needs_sso(runtime: SignInRuntime, email: str) -> Optional[Response]:
    person = runtime.store.person(email)
    if runtime.config.admins_use_sso and person is not None and person.role == roles.ADMIN:
        return _refuse(403, "Admins sign in with SSO on this server.", sso=True)
    return None


def _count_failure(
    runtime: SignInRuntime, request: Request, key: str, email: str, reason: str
) -> None:
    """A wrong try: counted against the address and the place it came from, and the person told when the door shuts."""
    shut = runtime.store.failed(key)
    runtime.store.failed(f"from:{_address(request)}", limit=_FROM_ONE_ADDRESS, escalate=False)
    runtime.access.record(
        "signin_failed", who=email, method="email", reason=reason, address=_address(request)
    )
    if shut and runtime.store.person(email) is not None:
        minutes = runtime.store.lock_minutes(key)
        runtime.access.record(
            "locked", who=email, reason=key.split(":", 1)[0], address=_address(request)
        )
        try:
            runtime.sender(email, *lock_email(minutes, runtime.product))
        except Exception as exc:  # noqa: BLE001 - a mail server can fail in any way it likes
            logger.error("the warning to %s could not be sent: %s", email, exc)


def _locked_out(runtime: SignInRuntime, request: Request, key: str) -> Optional[Response]:
    for held in (key, f"from:{_address(request)}"):
        if runtime.store.locked(held):
            return _refuse(
                429,
                f"Too many wrong tries. Wait {runtime.store.lock_minutes(held)} minutes and try again.",
            )
    return None


# ============================================================================
# ROUTES: single sign-on
# ============================================================================
#
# INPUT   a start, with where to return; the provider's code and state
# OUTPUT  the redirect to the provider; the person signed in, or refused when
#         not in the group, with a wrong try counted and the door shut after
#         too many
#
# An admin on a server that signs people in with SSO signs in with SSO and
# nothing else.


@router.get("/auth/oidc/start", tags=["auth"], include_in_schema=False)
async def oidc_start(
    request: Request, to: Optional[str] = None, prompt: Optional[str] = None
) -> Response:
    runtime = _need(request)
    if runtime.oidc is None:
        raise HTTPException(status_code=404, detail="single sign-on is not turned on")
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), new_verifier()
    # Where the browser comes back to, as it reaches it: through the gateway, when there is one.
    redirect_uri = runtime.address("/auth/oidc/callback")
    try:
        url = runtime.oidc.authorization_url(
            redirect_uri=redirect_uri, state=state, nonce=nonce, verifier=verifier, prompt=prompt
        )
    except SignInRefused as exc:
        return RedirectResponse(_signin_page(runtime, exc.code), status_code=302)
    response = RedirectResponse(url, status_code=302)
    to = _safe_return(to, runtime.visible("/dashboard/"))
    held = json.dumps(
        {"s": state, "n": nonce, "v": verifier, "to": to, "at": time.time()}, separators=(",", ":")
    )
    # Lax, not Strict: the provider sends the person back by a navigation from
    # another site, and a Strict cookie is withheld on exactly that request.
    response.set_cookie(
        runtime.cookie(OIDC_COOKIE),
        runtime.store.sign(held),
        max_age=600,
        httponly=True,
        secure=runtime.config.secure_cookies,
        samesite="lax",
        path=_oidc_cookie_path(runtime),
    )
    return response


@router.get("/auth/oidc/callback", tags=["auth"], include_in_schema=False)
async def oidc_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    runtime = _need(request)
    if runtime.oidc is None:
        raise HTTPException(status_code=404, detail="single sign-on is not turned on")

    def back(reason: str, who: Optional[str] = None) -> Response:
        runtime.access.record(
            "signin_failed", method="oidc", who=who, reason=reason, address=_address(request)
        )
        response = RedirectResponse(_signin_page(runtime, reason), status_code=302)
        response.delete_cookie(
            runtime.cookie(OIDC_COOKIE),
            path=_oidc_cookie_path(runtime),
            secure=runtime.config.secure_cookies,
        )
        return response

    raw = runtime.store.unsign(request.cookies.get(runtime.cookie(OIDC_COOKIE)))
    try:
        held = json.loads(raw) if raw else None
    except ValueError:
        held = None
    if error:
        return back("provider_" + re.sub(r"[^a-z_]", "", str(error).lower())[:40])
    if not held or not code or not state or time.time() - float(held.get("at", 0)) > 600:
        return back("expired")
    if not _same(state, held.get("s")):
        return back("state")
    try:
        identity = runtime.oidc.redeem(
            code=code,
            redirect_uri=runtime.address("/auth/oidc/callback"),
            verifier=held["v"],
            nonce=held["n"],
        )
    except SignInRefused as exc:
        logger.warning("single sign-on refused: %s (%s)", exc.reason, exc.code)
        return back(exc.code)

    # A new session id on every sign-in: one that existed before it proves nothing.
    runtime.store.close_session(_current_sid(runtime, request))
    # Somebody on the People list has the role their record gives, and their
    # session ends when the record changes, as a local one does.
    sid, session = runtime.store.open_session(
        subject=identity.subject,
        email=identity.email,
        name=identity.name,
        role=identity.role,
        principal=identity.principal,
        method="oidc",
        hours=runtime.config.session_hours,
        grants=identity.grants,
        user_agent=request.headers.get("user-agent"),
        address=_address(request),
        groups=identity.groups,
        listed=identity.listed,
    )
    if identity.listed and identity.email:
        runtime.store.stamp_sso(identity.email)
    runtime.access.record(
        "signin",
        who=identity.email or identity.subject,
        role=identity.role,
        method="oidc",
        subject=identity.subject,
        address=_address(request),
    )
    response = RedirectResponse(
        _safe_return(held.get("to"), runtime.visible("/dashboard/")), status_code=302
    )
    response.delete_cookie(
        runtime.cookie(OIDC_COOKIE),
        path=_oidc_cookie_path(runtime),
        secure=runtime.config.secure_cookies,
    )
    _set_session_cookies(runtime, response, sid, session.csrf)
    return response


# ============================================================================
# ROUTES: emergency sign-in
# ============================================================================
#
# INPUT   nothing, to ask whether it is on; the username and the password
# OUTPUT  when it turns itself off, or a 404 while it is off, the same as a
#         route that is not there; the emergency admin signed in, recorded as
#         break_glass_used, or refused with a wrong try counted
#
# For while the usual sign-in is down, whichever that is. Its session ends when it does.


class EmergencyRequest(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


_WRONG_ALL = "That username and password were not accepted together. Check both and try again."
_SPENT = "That password was used in an earlier emergency, and a password works for one. An operator sets a new one."


def _emergency(request: Request) -> tuple[SignInRuntime, Any]:
    runtime = runtime_of(request)
    glass = runtime.break_glass if runtime is not None else None
    if runtime is None or glass is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return runtime, glass


def _same_text(given: str, wanted: str) -> bool:
    """Compared as hashes, so neither the length nor the first wrong letter shows in the time taken."""
    import hashlib

    return hmac.compare_digest(
        hashlib.sha256(given.encode()).digest(), hashlib.sha256(wanted.encode()).digest()
    )


@router.get("/auth/break-glass", tags=["auth"], include_in_schema=False)
async def break_glass_state(request: Request) -> Any:
    """Whether emergency sign-in is on, and until when. Off, this answers 404."""
    _, glass = _emergency(request)
    return {"ok": True, "data": {"until": glass.until_iso}}


@router.post("/auth/break-glass", tags=["auth"], include_in_schema=False)
async def break_glass_signin(request: Request, body: EmergencyRequest) -> Any:
    runtime, glass = _emergency(request)
    key = f"emergency:{glass.admin}"
    held = _locked_out(runtime, request, key)
    if held is not None:
        runtime.access.record(
            "signin_failed",
            who=glass.admin,
            method=BREAK_GLASS,
            reason="locked",
            address=_address(request),
        )
        return held
    # Both are checked whatever the first says, and the password against its hash, which is all the server holds.
    right = passwords.check(body.password, glass.password_hash) & _same_text(
        body.username.strip(), glass.admin
    )
    # Said only to somebody who gave the right password: it worked for one emergency, and that one is over.
    spent = right and not runtime.store.use_emergency(glass.admin, glass.mark, glass.until)
    if not right or spent:
        runtime.store.failed(key)
        runtime.store.failed(f"from:{_address(request)}", limit=_FROM_ONE_ADDRESS, escalate=False)
        runtime.access.record(
            "signin_failed",
            who=body.username.strip()[:64] or None,
            method=BREAK_GLASS,
            reason="spent" if spent else "wrong",
            address=_address(request),
        )
        return _refuse(401, _SPENT if spent else _WRONG_ALL)
    runtime.store.succeeded(key)
    runtime.store.close_session(_current_sid(runtime, request))
    # Never longer than emergency sign-in itself is on.
    hours = max(1 / 60, min(runtime.config.session_hours, (glass.until - time.time()) / 3600))
    sid, session = runtime.store.open_session(
        subject=f"break-glass:{glass.admin}",
        email=None,
        name="Emergency admin",
        role=roles.ADMIN,
        principal={},
        method=BREAK_GLASS,
        hours=hours,
        user_agent=request.headers.get("user-agent"),
        address=_address(request),
    )
    runtime.access.record(
        "break_glass_used",
        who=glass.admin,
        role=roles.ADMIN,
        method=BREAK_GLASS,
        address=_address(request),
    )
    logger.warning(
        "emergency sign-in used by %s from %s; it is on until %s",
        glass.admin,
        _address(request),
        glass.until_iso,
    )
    response = JSONResponse({"ok": True, "data": {"person": session.public()}})
    _set_session_cookies(runtime, response, sid, session.csrf)
    return response


# ============================================================================
# ROUTES: Developer Access
# ============================================================================
#
# INPUT   nothing, to ask whether it is on; a username and the password
# OUTPUT  the accounts it takes, or a 404 while it is off or the caller is not
#         on this machine; the account signed in with the role the settings
#         give it, or refused with a wrong try counted
#
# For trying the roles on one machine. The public address has to be this
# machine's for the server to start with it, and the connection has to come
# from this machine for it to answer: a header claiming so counts for nothing.


class DeveloperRequest(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


_WRONG_DEVELOPER = (
    "That username and password were not accepted together. Check both and try again."
)


def _developer(request: Request) -> tuple[SignInRuntime, Any]:
    runtime = runtime_of(request)
    access = runtime.config.developer if runtime is not None else None
    if runtime is None or access is None or not runtime.on_this_machine(request):
        raise HTTPException(status_code=404, detail="Not Found")
    return runtime, access


@router.get("/auth/developer", tags=["auth"], include_in_schema=False)
async def developer_state(request: Request) -> Any:
    """The accounts Developer Access takes, to a caller on this machine. Off, or from anywhere else, this answers 404."""
    _, access = _developer(request)
    return {
        "ok": True,
        "data": {
            "accounts": [
                {"username": name, "role": role} for name, role in sorted(access.accounts.items())
            ]
        },
    }


@router.post("/auth/developer", tags=["auth"], include_in_schema=False)
async def developer_signin(request: Request, body: DeveloperRequest) -> Any:
    runtime, access = _developer(request)
    name = body.username.strip().lower()
    key = f"developer:{name[:64]}"
    held = _locked_out(runtime, request, key)
    if held is not None:
        runtime.access.record(
            "signin_failed",
            who=name[:64] or None,
            method=DEVELOPER,
            reason="locked",
            address=_address(request),
        )
        return held
    role = access.accounts.get(name)
    # The password is checked whatever the name, so a wrong name and a wrong password take the same time.
    right = _same_text(body.password, access.password) & (role is not None)
    if not right:
        runtime.store.failed(key)
        runtime.store.failed(f"from:{_address(request)}", limit=_FROM_ONE_ADDRESS, escalate=False)
        runtime.access.record(
            "signin_failed",
            who=name[:64] or None,
            method=DEVELOPER,
            reason="wrong",
            address=_address(request),
        )
        return _refuse(401, _WRONG_DEVELOPER)
    runtime.store.succeeded(key)
    runtime.store.close_session(_current_sid(runtime, request))
    sid, session = runtime.store.open_session(
        subject=f"developer:{name}",
        email=None,
        name=name,
        role=role,
        principal={},
        method=DEVELOPER,
        hours=runtime.config.session_hours,
        user_agent=request.headers.get("user-agent"),
        address=_address(request),
    )
    runtime.access.record(
        "signin", who=name, role=role, method=DEVELOPER, address=_address(request)
    )
    response = JSONResponse({"ok": True, "data": {"person": session.public()}})
    _set_session_cookies(runtime, response, sid, session.csrf)
    return response


# ============================================================================
# ROUTES: email, the link, and enrolment
# ============================================================================
#
# INPUT   an address; the emailed link's token; a code
# OUTPUT  a link sent, the same answer whether or not the address is listed;
#         the link spent, and what either way of signing in needs handed back;
#         the enrolment confirmed; signed out
#
# Asking for a link is limited per address, listed or not, so the list cannot
# be probed.


class BeginRequest(BaseModel):
    email: str


class VerifyRequest(BaseModel):
    email: str
    code: str
    password: Optional[str] = None


class TokenRequest(BaseModel):
    token: str


class ConfirmRequest(BaseModel):
    ticket: str
    code: str
    password: Optional[str] = None


_NO_PASSKEYS = "Passkeys are not used on this server"


def _passkeys_on(runtime: SignInRuntime) -> None:
    """Beside single sign-on nobody keeps a passkey, so there its routes are not there: 404, as for anything a server does not have."""
    if not runtime.config.own_passkeys:
        raise HTTPException(status_code=404, detail=_NO_PASSKEYS)


def _email_on(request: Request) -> SignInRuntime:
    runtime = _need(request)
    if "email" not in runtime.config.methods:
        raise HTTPException(status_code=404, detail="email sign-in is not turned on")
    return runtime


#: One answer for every address, listed or not, enrolled or not. The page
#: shows a code field and says a link is on its way if this is a first visit,
#: so the reply teaches nobody which addresses have access.
_BEGIN_REPLY = {"ok": True, "data": {"next": "code", "note": "first_time_link"}}
_WRONG = "That code was not accepted. Codes change every 30 seconds, and each works once."
_WRONG_BOTH = "That password and code were not accepted together. Check both and try again."


def _asking(runtime: SignInRuntime, request: Request, email: str, kind: str) -> bool:
    """A limit on asking for a link, counted whether or not the address is listed. False when used up."""
    throttle = [f"{kind}:{email}", f"{kind}-from:{_address(request)}"]
    if any(runtime.store.locked(key) for key in throttle):
        return False
    for key in throttle:
        runtime.store.failed(key, escalate=False)
    return True


@router.post("/auth/email/begin", tags=["auth"])
async def email_begin(request: Request, body: BeginRequest) -> Any:
    runtime = _email_on(request)
    email = normalise_email(body.email)
    if not _asking(runtime, request, email, "begin"):
        return _BEGIN_REPLY
    person = runtime.store.person(email)
    if person is not None and not person.disabled and not person.enrolled:
        token = runtime.store.new_link(email, "enrol")
        link = f"{runtime.address('/dashboard/')}#/enrol?token={token}"
        subject, text = enrolment_email(
            link, LINK_MINUTES, runtime.product, passkeys=runtime.config.own_passkeys
        )
        try:
            runtime.sender(email, subject, text)
        except Exception as exc:  # noqa: BLE001 - a mail server can fail in any way it likes
            logger.error("the sign-in email to %s could not be sent: %s", email, exc)
    return _BEGIN_REPLY


@router.post("/auth/email/verify", tags=["auth"])
async def email_verify(request: Request, body: VerifyRequest) -> Any:
    runtime = _email_on(request)
    email = normalise_email(body.email)
    code = body.code.strip()
    recovering = any(ch.isalpha() for ch in code)
    # A recovery code has its own count, so somebody locked out of codes by a
    # guesser can still get in with one.
    key = f"{'recovery' if recovering else 'code'}:{email}"
    held = _locked_out(runtime, request, key)
    if held is not None:
        runtime.access.record(
            "signin_failed", who=email, method="email", reason="locked", address=_address(request)
        )
        return held
    person = runtime.store.person(email)
    ok, recovered = False, False
    if person is not None and not person.disabled and person.enrolled:
        if recovering:
            ok = recovered = runtime.store.use_recovery_code(email, code)
        else:
            ok = runtime.store.check_code(email, code)
    if runtime.config.passwords:
        # Checked every time and whatever came before, so a wrong code and a wrong password take the same time.
        ok = runtime.store.check_password(email, body.password) and ok
    if not ok or person is None:
        _count_failure(runtime, request, key, email, "wrong_code")
        return _refuse(401, _WRONG_BOTH if runtime.config.passwords else _WRONG)
    runtime.store.succeeded(key)
    if runtime.config.require_passkey and not recovered and person.passkeys:
        return _refuse(
            403,
            "Sign in with your passkey: this server signs people in with passkeys.",
            passkey_only=True,
        )
    refused = _admin_needs_sso(runtime, email)
    if refused is not None:
        return refused
    if recovered:
        runtime.access.record(
            "recovery_code_used", who=email, method="email", address=_address(request)
        )
    return _signed_in(
        runtime,
        request,
        person.email,
        "email",
        extra={"recovery_codes_left": runtime.store.recovery_codes_left(email)}
        if recovered
        else None,
    )


def _code(request: Request, uri: str) -> Optional[str]:
    """The QR code an authenticator app scans, in the dashboard's own look: its logo or mark in the middle, its colours."""
    brand = getattr(request.app.state, "brand", None)
    look = (
        brand.code_look()
        if brand is not None and hasattr(brand, "code_look")
        else Brand().code_look()
    )
    return qr.svg(uri, **look)


@router.post("/auth/email/enrol/begin", tags=["auth"])
async def enrol_begin(request: Request, body: TokenRequest) -> Any:
    """Spend the emailed link, and hand back what either way of signing in needs.

    A POST from a button on the page, not the link itself: mail scanners open
    every link in a message, and a link that worked by being opened would be
    spent before the person saw it. The reply carries an authenticator secret
    to scan and a ticket that finishes either an authenticator or a passkey.
    """
    runtime = _email_on(request)
    email = runtime.store.use_link(body.token, "enrol")
    person = runtime.store.person(email) if email else None
    if email is None or person is None or person.disabled or person.enrolled:
        return _refuse(
            400, "This link has been used or has expired. Ask for a new one from the sign-in page."
        )
    # Before anything is made, so no recovery code is handed out to be lost with a refused sign-in.
    refused = _sso_first(runtime, request, person, "email")
    if refused is not None:
        return refused
    if runtime.config.require_passkey:
        return {
            "ok": True,
            "data": {
                "email": email,
                "ticket": runtime.store.new_link(email, "confirm"),
                "passkey_only": True,
                "passwords": False,
            },
        }
    secret = runtime.store.begin_enrolment(email)
    uri = totp.provisioning_uri(secret, email, issuer=runtime.product)
    return {
        "ok": True,
        "data": {
            "email": email,
            "secret": secret,
            "uri": uri,
            "qr": _code(request, uri),
            "ticket": runtime.store.new_link(email, "confirm"),
            "passwords": runtime.config.passwords,
        },
    }


def _fresh_ticket(runtime: SignInRuntime, email: str, status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=refusal_content(message, data={"ticket": runtime.store.new_link(email, "confirm")}),
    )


@router.post("/auth/email/enrol/confirm", tags=["auth"])
async def enrol_confirm(request: Request, body: ConfirmRequest) -> Any:
    runtime = _email_on(request)
    if runtime.config.require_passkey:
        return _refuse(
            403, "This server signs people in with passkeys. Make one to finish setting up."
        )
    # A ticket works once, like everything else here. A mistyped code gets a
    # fresh ticket back with the refusal, so the person can try again, and
    # the tries are counted against the address, not the ticket.
    email = runtime.store.use_link(body.ticket, "confirm")
    if email is None:
        return _refuse(400, "This set-up has expired. Ask for a new link from the sign-in page.")
    attempts = f"confirm:{email}"
    if runtime.store.locked(attempts):
        return _refuse(
            429,
            f"Too many wrong codes. Wait {runtime.store.lock_minutes(attempts)} minutes, then ask for a new link from the sign-in page.",
        )
    person = runtime.store.person(email)
    if runtime.config.passwords:
        problem = passwords.problem_with(body.password or "", email)
        if problem:
            return _fresh_ticket(runtime, email, 400, problem)
    if (
        person is None
        or person.disabled
        or not runtime.store.check_code(email, body.code, confirming=True)
    ):
        runtime.store.failed(attempts)
        return _fresh_ticket(runtime, email, 401, _WRONG)
    runtime.store.succeeded(attempts)
    if runtime.config.passwords:
        runtime.store.set_password(email, body.password or "")
    codes = runtime.store.new_recovery_codes(email)
    runtime.access.record("enrolled", who=email, method="email", address=_address(request))
    return _signed_in(runtime, request, person.email, "email", extra={"recovery_codes": codes})


@router.post("/auth/signout", tags=["auth"])
async def signout(request: Request) -> Response:
    runtime = _need(request)
    caller = caller_of(request)
    runtime.store.close_session(_current_sid(runtime, request))
    if caller is not None:
        runtime.access.record(
            "signout", who=caller.who, method=caller.method, address=_address(request)
        )
    response = JSONResponse({"ok": True, "data": None})
    _clear_cookies(runtime, response)
    return response


# ============================================================================
# ROUTES: passkeys
# ============================================================================
#
# INPUT   the browser's credential, made or asserted; the ticket the emailed
#         link gave
# OUTPUT  options for the browser; the passkey registered as the first way in;
#         a sign-in with no address asked, the device offering what it holds
#         for this site; the address a passkey belongs to
#
# The challenge the browser signed is read out of its reply, so the right one
# is looked up and spent once.


class CredentialRequest(BaseModel):
    credential: dict
    ticket: Optional[str] = None
    name: Optional[str] = None


def _challenge_in(credential: Any) -> bytes:
    """The challenge a browser signed, read out of its reply so the right one can be looked up."""
    try:
        client = json.loads(pk.unb64u(credential["response"]["clientDataJSON"]).decode("utf-8"))
        return pk.unb64u(client["challenge"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise pk.PasskeyRefused() from exc


def _credential_id(credential: Any) -> str:
    raw = credential.get("rawId") or credential.get("id") if isinstance(credential, dict) else None
    return pk.b64u(pk.unb64u(raw))


def _creation(runtime: SignInRuntime, email: str, purpose: str) -> dict:
    challenge = runtime.store.new_challenge(purpose, email)
    return pk.creation_options(
        rp_id=runtime.rp_id,
        rp_name=runtime.product,
        user_id=runtime.store.user_handle(email),
        user_name=email,
        display_name=email,
        challenge=challenge,
        exclude=runtime.store.passkey_ids(email),
    )


def _registered(
    runtime: SignInRuntime, request: Request, email: str, purpose: str, body: CredentialRequest
) -> dict:
    challenge = _challenge_in(body.credential)
    if runtime.store.use_challenge(challenge, purpose) != email:
        raise pk.PasskeyRefused("That passkey answered a request that has run out. Try again.")
    made = pk.verify_registration(
        body.credential, challenge=challenge, origin=runtime.origin, rp_id=runtime.rp_id
    )
    name = (body.name or "").strip() or device_of(request.headers.get("user-agent"))
    added = runtime.store.add_passkey(
        email, made.credential_id, made.public_key, made.alg, made.sign_count, made.transports, name
    )
    runtime.access.record("passkey_added", who=email, method="passkey", address=_address(request))
    return added


@router.post("/auth/passkey/enrol/begin", tags=["auth"])
async def passkey_enrol_begin(request: Request, body: TokenRequest) -> Any:
    """The first way in, as a passkey: options for the browser, against the ticket the emailed link gave."""
    runtime = _email_on(request)
    _passkeys_on(runtime)
    email = runtime.store.peek_link(body.token, "confirm")
    person = runtime.store.person(email) if email else None
    if email is None or person is None or person.disabled or person.enrolled:
        return _refuse(400, "This set-up has expired. Ask for a new link from the sign-in page.")
    refused = _sso_first(runtime, request, person, "passkey")
    if refused is not None:
        return refused
    return {"ok": True, "data": {"options": _creation(runtime, email, "passkey-enrol")}}


@router.post("/auth/passkey/enrol/finish", tags=["auth"])
async def passkey_enrol_finish(request: Request, body: CredentialRequest) -> Any:
    runtime = _email_on(request)
    _passkeys_on(runtime)
    email = runtime.store.use_link(body.ticket or "", "confirm")
    person = runtime.store.person(email) if email else None
    if email is None or person is None or person.disabled:
        return _refuse(400, "This set-up has expired. Ask for a new link from the sign-in page.")
    if runtime.store.locked(f"confirm:{email}"):
        return _refuse(
            429, "Too many tries. Wait a while, then ask for a new link from the sign-in page."
        )
    try:
        _registered(runtime, request, email, "passkey-enrol", body)
    except pk.PasskeyRefused as exc:
        runtime.store.failed(f"confirm:{email}")
        return _fresh_ticket(runtime, email, 400, exc.reason)
    runtime.store.forget_unconfirmed(email)
    codes = runtime.store.new_recovery_codes(email)
    runtime.access.record("enrolled", who=email, method="passkey", address=_address(request))
    return _signed_in(runtime, request, email, "passkey", extra={"recovery_codes": codes})


@router.post("/auth/passkey/begin", tags=["auth"])
async def passkey_begin(request: Request) -> Any:
    """Sign in with a passkey. No address is asked for: the device offers what it holds for this site."""
    runtime = _email_on(request)
    _passkeys_on(runtime)
    # Nothing to guess here, only a table to fill: a generous limit per address.
    key = f"passkey-from:{_address(request)}"
    if runtime.store.locked(key):
        return _refuse(429, "Too many tries from here. Wait a few minutes and try again.")
    runtime.store.failed(key, limit=120, escalate=False)
    challenge = runtime.store.new_challenge("passkey-signin")
    return {
        "ok": True,
        "data": {"options": pk.request_options(rp_id=runtime.rp_id, challenge=challenge)},
    }


def _asserted(
    runtime: SignInRuntime, credential: Any, purpose: str, email: Optional[str] = None
) -> str:
    """Check a passkey sign-in. The address it belongs to."""
    challenge = _challenge_in(credential)
    issued_for = runtime.store.use_challenge(challenge, purpose)
    if issued_for is None or (email is not None and issued_for != email):
        raise pk.PasskeyRefused("That passkey answered a request that has run out. Try again.")
    credential_id = _credential_id(credential)
    stored = runtime.store.passkey(credential_id)
    if stored is None or (email is not None and stored["email"] != email):
        raise pk.PasskeyRefused("That passkey is not registered here.")
    handle = credential.get("response", {}).get("userHandle")
    if handle and pk.unb64u(handle) != runtime.store.user_handle(stored["email"]):
        raise pk.PasskeyRefused()
    count = pk.verify_assertion(
        credential,
        challenge=challenge,
        origin=runtime.origin,
        rp_id=runtime.rp_id,
        public_key=stored["public_key"],
        sign_count=stored["sign_count"],
    )
    runtime.store.touch_passkey(credential_id, count)
    return str(stored["email"])


@router.post("/auth/passkey/finish", tags=["auth"])
async def passkey_finish(request: Request, body: CredentialRequest) -> Any:
    runtime = _email_on(request)
    _passkeys_on(runtime)
    try:
        email = _asserted(runtime, body.credential, "passkey-signin")
    except pk.PasskeyRefused as exc:
        runtime.access.record(
            "signin_failed", method="passkey", reason="passkey", address=_address(request)
        )
        return _refuse(401, exc.reason)
    person = runtime.store.person(email)
    if person is None or person.disabled:
        return _refuse(401, "That passkey is not registered here.")
    refused = _admin_needs_sso(runtime, email)
    if refused is not None:
        return refused
    return _signed_in(runtime, request, email, "passkey")


# ============================================================================
# ROUTES: passwords
# ============================================================================
#
# INPUT   an address; a reset token and a new password
# OUTPUT  a link to choose a new password, the same answer for everybody; the
#         password reset, with the authenticator code when both are on
#
# The link alone opens nothing.


class PasswordRequest(BaseModel):
    password: str


class ResetRequest(BaseModel):
    token: str
    code: str
    password: str


def _passwords_on(request: Request) -> SignInRuntime:
    runtime = _email_on(request)
    if not runtime.config.passwords:
        raise HTTPException(status_code=404, detail="passwords are not turned on for this server")
    return runtime


@router.post("/auth/password/forgot", tags=["auth"])
async def password_forgot(request: Request, body: BeginRequest) -> Any:
    """A link to choose a new password. The same answer for everybody, and the link alone opens nothing."""
    runtime = _passwords_on(request)
    email = normalise_email(body.email)
    if _asking(runtime, request, email, "forgot"):
        person = runtime.store.person(email)
        if person is not None and not person.disabled and person.authenticator:
            token = runtime.store.new_link(email, "password")
            link = f"{runtime.address('/dashboard/')}#/password?token={token}"
            try:
                runtime.sender(email, *password_email(link, LINK_MINUTES, runtime.product))
            except Exception as exc:  # noqa: BLE001
                logger.error("the password email to %s could not be sent: %s", email, exc)
    return {"ok": True, "data": {"note": "reset_link"}}


@router.post("/auth/password/reset", tags=["auth"])
async def password_reset(request: Request, body: ResetRequest) -> Any:
    runtime = _passwords_on(request)
    email = runtime.store.use_link(body.token, "password")
    if email is None:
        return _refuse(
            400, "This link has been used or has expired. Ask for a new one from the sign-in page."
        )
    key = f"code:{email}"
    held = _locked_out(runtime, request, key)
    if held is not None:
        return held

    def again(status: int, message: str) -> JSONResponse:
        token = runtime.store.new_link(email, "password")
        return JSONResponse(
            status_code=status, content=refusal_content(message, data={"token": token})
        )

    problem = passwords.problem_with(body.password, email)
    if problem:
        return again(400, problem)
    person = runtime.store.person(email)
    if person is None or person.disabled or not runtime.store.check_code(email, body.code):
        _count_failure(runtime, request, key, email, "wrong_code")
        return again(401, _WRONG)
    runtime.store.succeeded(key)
    runtime.store.set_password(email, body.password)
    runtime.access.record("password_set", who=email, reason="reset", address=_address(request))
    refused = _admin_needs_sso(runtime, email)
    return refused if refused is not None else _signed_in(runtime, request, email, "email")


# ============================================================================
# ROUTES: your own ways in
# ============================================================================
#
# INPUT   the signed-in person's session
# OUTPUT  their ways in; their sessions, one or all others ended; a passkey
#         added or removed; the authenticator replaced; new recovery codes; a
#         password set
#
# These need the person themselves, signed in at the dashboard: an app acting
# for them cannot make them, and the only way in is never removed. Somebody
# on the People list who came in with single sign-on keeps ways of their own
# too, and may remove the last of them, because single sign-on still lets
# them in.


def _listed_sso(runtime: SignInRuntime, caller: Optional[Caller]) -> bool:
    """Signed in with single sign-on, on the People list, and the list's own ways are on here."""
    if (
        caller is None
        or caller.method != "oidc"
        or not caller.listed
        or not caller.email
        or "email" not in runtime.config.methods
    ):
        return False
    person = runtime.store.person(caller.email)
    return person is not None and not person.disabled


def _local_caller(request: Request) -> tuple[SignInRuntime, Caller]:
    runtime = _need(request)
    caller = caller_of(request)
    if caller is None or not caller.email or not (caller.local or _listed_sso(runtime, caller)):
        raise HTTPException(
            status_code=400,
            detail="This is for somebody on this server's People list, signed in at the dashboard, not a key",
        )
    return runtime, caller


def _sso_backs(runtime: SignInRuntime, person: Any) -> bool:
    """Single sign-on is on and has let this person in before, so they are not left with no way in."""
    return runtime.oidc is not None and person.last_sso_at is not None


def _session_caller(request: Request) -> tuple[SignInRuntime, Caller]:
    runtime = _need(request)
    caller = caller_of(request)
    if caller is None or caller.method in ("key", "guest") or not caller.subject:
        raise HTTPException(
            status_code=400, detail="This is for somebody signed in to the dashboard"
        )
    return runtime, caller


@router.get("/auth/me/ways", tags=["auth"])
async def my_ways(request: Request) -> Any:
    runtime, caller = _session_caller(request)
    listed = _listed_sso(runtime, caller)
    ways = runtime.store.ways(caller.email) if (caller.local or listed) and caller.email else {}
    if listed:
        # When single sign-on last let them in, which a passkey or a code may be checked against.
        held = runtime.store.person(caller.email or "")
        ways["sso_at"] = held.last_sso_at if held is not None else None
    return {
        "ok": True,
        "data": {
            "local": caller.local,
            "listed": listed,
            "method": caller.method,
            "passwords": runtime.config.passwords,
            "require_passkey": runtime.config.require_passkey,
            "own_passkeys": runtime.config.own_passkeys,
            **ways,
        },
    }


@router.get("/auth/me/sessions", tags=["auth"])
async def my_sessions(request: Request) -> Any:
    runtime, caller = _session_caller(request)
    rows = runtime.store.sessions_of(caller.subject or "")
    for row in rows:
        row["device"] = device_of(row.pop("user_agent"))
        row["current"] = row["id"] == caller.session_key
    return {"ok": True, "data": {"sessions": rows}}


@router.delete("/auth/me/sessions/{session_id}", tags=["auth"])
async def end_my_session(request: Request, session_id: str) -> Any:
    runtime, caller = _session_caller(request)
    if session_id == caller.session_key:
        return _refuse(400, "That is this browser. Sign out instead.")
    if not runtime.store.close_sessions_of(caller.subject or "", only=session_id):
        raise HTTPException(status_code=404, detail="no such session")
    runtime.access.record(
        "sessions_ended",
        who=caller.who,
        method=caller.method,
        reason="one",
        address=_address(request),
    )
    return {"ok": True, "data": None}


@router.post("/auth/me/sessions/end-others", tags=["auth"])
async def end_my_other_sessions(request: Request) -> Any:
    runtime, caller = _session_caller(request)
    ended = runtime.store.close_sessions_of(caller.subject or "", keep=caller.session_key)
    runtime.access.record(
        "sessions_ended",
        who=caller.who,
        method=caller.method,
        reason=f"{ended} others",
        address=_address(request),
    )
    return {"ok": True, "data": {"ended": ended}}


@router.post("/auth/me/passkeys/begin", tags=["auth"])
async def add_passkey_begin(request: Request) -> Any:
    runtime, caller = _local_caller(request)
    _passkeys_on(runtime)
    return {"ok": True, "data": {"options": _creation(runtime, caller.email or "", "passkey-add")}}


@router.post("/auth/me/passkeys/finish", tags=["auth"])
async def add_passkey_finish(request: Request, body: CredentialRequest) -> Any:
    runtime, caller = _local_caller(request)
    _passkeys_on(runtime)
    try:
        added = _registered(runtime, request, caller.email or "", "passkey-add", body)
    except pk.PasskeyRefused as exc:
        return _refuse(400, exc.reason)
    return {"ok": True, "data": added}


@router.delete("/auth/me/passkeys/{credential_id}", tags=["auth"])
async def remove_my_passkey(request: Request, credential_id: str) -> Any:
    runtime, caller = _local_caller(request)
    person = runtime.store.person(caller.email or "")
    if person is None:
        raise HTTPException(status_code=404, detail="nobody by that address")
    if (
        person.passkeys <= 1
        and (runtime.config.require_passkey or not person.authenticator)
        and not _sso_backs(runtime, person)
    ):
        if runtime.config.require_passkey:
            return _refuse(
                409,
                "This is your only passkey, and this server signs people in with passkeys. Add another first.",
            )
        return _refuse(
            409, "This is your only way in. Add another passkey or an authenticator app first."
        )
    if not runtime.store.remove_passkey(person.email, credential_id):
        raise HTTPException(status_code=404, detail="no such passkey")
    runtime.access.record(
        "passkey_removed", who=person.email, method=caller.method, address=_address(request)
    )
    return {"ok": True, "data": None}


@router.post("/auth/me/authenticator/begin", tags=["auth"])
async def replace_authenticator_begin(request: Request) -> Any:
    runtime, caller = _local_caller(request)
    if runtime.config.require_passkey:
        return _refuse(403, "This server signs people in with passkeys. Add a passkey instead.")
    secret = runtime.store.begin_replacement(caller.email or "")
    uri = totp.provisioning_uri(secret, caller.email or "", issuer=runtime.product)
    return {"ok": True, "data": {"secret": secret, "uri": uri, "qr": _code(request, uri)}}


class CodeRequest(BaseModel):
    code: str


@router.delete("/auth/me/authenticator", tags=["auth"])
async def remove_my_authenticator(request: Request) -> Any:
    """Remove your authenticator app. Not your only way in, unless single sign-on still lets you in."""
    runtime, caller = _local_caller(request)
    person = runtime.store.person(caller.email or "")
    if person is None:
        raise HTTPException(status_code=404, detail="nobody by that address")
    if not person.authenticator:
        raise HTTPException(status_code=404, detail="no authenticator app is set up")
    if not person.passkeys and not _sso_backs(runtime, person):
        return _refuse(409, "This is your only way in. Add a passkey first.")
    runtime.store.remove_authenticator(person.email)
    runtime.access.record(
        "authenticator_removed", who=person.email, method=caller.method, address=_address(request)
    )
    return {"ok": True, "data": None}


@router.post("/auth/me/authenticator/confirm", tags=["auth"])
async def replace_authenticator_confirm(request: Request, body: CodeRequest) -> Any:
    runtime, caller = _local_caller(request)
    if runtime.config.require_passkey:
        return _refuse(403, "This server signs people in with passkeys. Add a passkey instead.")
    email = caller.email or ""
    key = f"confirm:{email}"
    if runtime.store.locked(key):
        return _refuse(
            429,
            f"Too many wrong codes. Wait {runtime.store.lock_minutes(key)} minutes and try again.",
        )
    if not runtime.store.confirm_replacement(email, body.code):
        runtime.store.failed(key)
        return _refuse(401, _WRONG)
    runtime.store.succeeded(key)
    runtime.access.record(
        "authenticator_replaced", who=email, method=caller.method, address=_address(request)
    )
    return {"ok": True, "data": None}


@router.post("/auth/me/recovery-codes", tags=["auth"])
async def make_recovery_codes(request: Request) -> Any:
    runtime, caller = _local_caller(request)
    codes = runtime.store.new_recovery_codes(caller.email or "")
    runtime.access.record(
        "recovery_codes_made", who=caller.email, method=caller.method, address=_address(request)
    )
    return {"ok": True, "data": {"recovery_codes": codes}}


@router.post("/auth/me/password", tags=["auth"])
async def set_my_password(request: Request, body: PasswordRequest) -> Any:
    runtime, caller = _local_caller(request)
    if not runtime.config.passwords:
        raise HTTPException(status_code=404, detail="passwords are not turned on for this server")
    problem = passwords.problem_with(body.password, caller.email)
    if problem:
        return _refuse(400, problem)
    runtime.store.set_password(caller.email or "", body.password)
    runtime.access.record(
        "password_set", who=caller.email, method=caller.method, address=_address(request)
    )
    return {"ok": True, "data": None}


# ============================================================================
# ROUTES: proving it is still you
# ============================================================================
#
# INPUT   an authenticator code, or a passkey
# OUTPUT  a fresh check, before a change that matters
#
# Step-up: the session stays, the check is recent.


class StepUpRequest(BaseModel):
    code: str = Field("", max_length=40)
    #: Emergency sign-in and Developer Access have no code: the password is asked again instead.
    password: Optional[str] = Field(None, max_length=1000)


@router.post("/auth/step-up", tags=["auth"])
async def step_up(request: Request, body: StepUpRequest) -> Any:
    """Prove it is still you, with the authenticator code, before a change that matters."""
    runtime, caller = _session_caller(request)
    if caller.method == DEVELOPER:
        access = runtime.config.developer
        if access is None or not runtime.on_this_machine(request):
            return _refuse(401, "Developer Access is off. Sign in again.")
        key = f"developer:{(caller.name or '')[:64]}"
        held = _locked_out(runtime, request, key)
        if held is not None:
            return held
        if not _same_text(body.password or "", access.password):
            runtime.store.failed(key)
            runtime.access.record(
                "signin_failed",
                who=caller.name,
                method=DEVELOPER,
                reason="step_up",
                address=_address(request),
            )
            return _refuse(401, "That password was not accepted.")
        runtime.store.succeeded(key)
        runtime.store.mark_verified(_current_sid(runtime, request))
        runtime.access.record(
            "step_up", who=caller.name, method=DEVELOPER, address=_address(request)
        )
        return {"ok": True, "data": None}
    if caller.method == "oidc" and "code" not in runtime.step_up_ways(caller):
        return _refuse(400, "Confirm with single sign-on", sso=True)
    if caller.method == BREAK_GLASS:
        glass = runtime.break_glass
        if glass is None:
            return _refuse(401, "Emergency sign-in is off. Sign in again.")
        key = f"emergency:{glass.admin}"
        held = _locked_out(runtime, request, key)
        if held is not None:
            return held
        if not passwords.check(body.password or "", glass.password_hash):
            runtime.store.failed(key)
            runtime.access.record(
                "signin_failed",
                who=glass.admin,
                method=BREAK_GLASS,
                reason="step_up",
                address=_address(request),
            )
            return _refuse(401, "That password was not accepted.")
        runtime.store.succeeded(key)
        runtime.store.mark_verified(_current_sid(runtime, request))
        runtime.access.record(
            "step_up", who=glass.admin, method=BREAK_GLASS, address=_address(request)
        )
        return {"ok": True, "data": None}
    email = caller.email or ""
    code = body.code.strip()
    recovering = any(ch.isalpha() for ch in code)
    key = f"{'recovery' if recovering else 'code'}:{email}"
    held = _locked_out(runtime, request, key)
    if held is not None:
        return held
    ok = (
        runtime.store.use_recovery_code(email, code)
        if recovering
        else runtime.store.check_code(email, code)
    )
    if not ok:
        _count_failure(runtime, request, key, email, "step_up")
        return _refuse(401, _WRONG)
    runtime.store.succeeded(key)
    runtime.store.mark_verified(_current_sid(runtime, request))
    runtime.access.record("step_up", who=email, method=caller.method, address=_address(request))
    return {"ok": True, "data": None}


@router.post("/auth/step-up/passkey/begin", tags=["auth"])
async def step_up_passkey_begin(request: Request) -> Any:
    runtime, caller = _local_caller(request)
    _passkeys_on(runtime)
    email = caller.email or ""
    challenge = runtime.store.new_challenge("passkey-stepup", email)
    return {
        "ok": True,
        "data": {
            "options": pk.request_options(
                rp_id=runtime.rp_id, challenge=challenge, allow=runtime.store.passkey_ids(email)
            )
        },
    }


@router.post("/auth/step-up/passkey/finish", tags=["auth"])
async def step_up_passkey_finish(request: Request, body: CredentialRequest) -> Any:
    runtime, caller = _local_caller(request)
    _passkeys_on(runtime)
    try:
        _asserted(runtime, body.credential, "passkey-stepup", email=caller.email)
    except pk.PasskeyRefused as exc:
        return _refuse(401, exc.reason)
    runtime.store.mark_verified(_current_sid(runtime, request))
    runtime.access.record("step_up", who=caller.email, method="passkey", address=_address(request))
    return {"ok": True, "data": None}


# ============================================================================
# ROUTES: the people on the server's own list
# ============================================================================
#
# INPUT   a find and a page; an address, a role and grants
# OUTPUT  everybody, or a page of them; a person added or changed, removed, or
#         reset to start again from a new link; a refusal to leave the server
#         with no admin
#
# A change to a person is written to the access log under who made it.


class PersonRequest(BaseModel):
    email: str
    role: str
    principal: Optional[dict] = None
    grants: Optional[list] = None


def _people_on(request: Request) -> SignInRuntime:
    """The People list says who may sign in, whichever way: there with the list's own ways, with single sign-on, or both."""
    runtime = _need(request)
    if "email" not in runtime.config.methods and runtime.oidc is None:
        raise HTTPException(status_code=404, detail="there is no People list on this server")
    return runtime


@router.get("/auth/people", tags=["auth"])
async def people(request: Request, q: Optional[str] = None, limit: int = 0, offset: int = 0) -> Any:
    """Everybody on the server's own list, or a page of them: ``q`` finds by address or role, ``limit`` 0 is everybody."""
    runtime = _people_on(request)
    everybody = [p.public() for p in runtime.store.people()]
    listed, total = _page_of(everybody, q, limit, offset, keys=("email", "role"))
    return {
        "ok": True,
        "data": {
            "people": listed,
            "total": total,
            "offset": max(0, offset),
            "roles": list(roles.ROLES),
            "grants": roles.table(),
            "grantable": sorted(roles.GRANTABLE),
        },
    }


def _page_of(
    rows: list, q: Optional[str], limit: int, offset: int, *, keys: tuple
) -> tuple[list, int]:
    """The rows that hold ``q`` in one of ``keys``, then the page ``offset`` and ``limit`` cut: ``limit`` 0 is all of them."""
    needle = (q or "").strip().lower()
    if needle:
        rows = [r for r in rows if needle in " ".join(str(r.get(k) or "") for k in keys).lower()]
    start = max(0, int(offset))
    page = rows[start:] if int(limit) <= 0 else rows[start : start + int(limit)]
    return page, len(rows)


def _would_leave_no_admin(runtime: SignInRuntime, email: str, new_role: Optional[str]) -> bool:
    person = runtime.store.person(email)
    return (
        person is not None
        and person.role == roles.ADMIN
        and not person.disabled
        and new_role != roles.ADMIN
        and runtime.store.admins() <= 1
    )


@router.post("/auth/people", tags=["auth"])
async def put_person(request: Request, body: PersonRequest) -> Any:
    runtime = _people_on(request)
    caller = caller_of(request)
    if _would_leave_no_admin(runtime, body.email, body.role):
        return _refuse(409, "This is the only admin. Make somebody else an admin first.")
    existed = runtime.store.person(body.email) is not None
    try:
        person = runtime.store.put_person(body.email, body.role, body.principal, body.grants)
    except Exception as exc:  # noqa: BLE001 - a ConfigurationError, said back as it is
        return _refuse(400, str(exc))
    runtime.access.record(
        "person_changed" if existed else "person_added",
        who=person.email,
        role=person.role,
        by=caller.who if caller else None,
    )
    return {"ok": True, "data": person.public()}


@router.delete("/auth/people/{email}", tags=["auth"])
async def remove_person(request: Request, email: str) -> Any:
    runtime = _people_on(request)
    caller = caller_of(request)
    if _would_leave_no_admin(runtime, email, None):
        return _refuse(409, "This is the only admin. Make somebody else an admin first.")
    if not runtime.store.remove_person(email):
        raise HTTPException(status_code=404, detail="nobody by that address")
    runtime.access.record(
        "person_removed", who=normalise_email(email), by=caller.who if caller else None
    )
    return {"ok": True, "data": None}


@router.post("/auth/people/{email}/reset", tags=["auth"])
async def reset_person(request: Request, email: str) -> Any:
    """Forget every way somebody signs in. Their next sign-in starts with a new link by email."""
    runtime = _email_on(request)
    caller = caller_of(request)
    if runtime.store.person(email) is None:
        raise HTTPException(status_code=404, detail="nobody by that address")
    runtime.store.reset_authenticator(email)
    runtime.access.record(
        "authenticator_reset", who=normalise_email(email), by=caller.who if caller else None
    )
    return {"ok": True, "data": None}


# ============================================================================
# ROUTES: keys, the access check, and the access log
# ============================================================================
#
# INPUT   a key's name, role, collections and expiry; a collection and
#         somebody; days, a page and a find
# OUTPUT  keys listed, made once and shown once, revoked; whether somebody may
#         retrieve from a collection and why, nothing searched; searches,
#         sign-ins and refusals a day, counts only; who read most, a page at a
#         time; the log newest first, a page at a time, with a find and the
#         total
#
# The log names people, actions and collections. It never holds a query, a
# chunk's text or what a search returned.

#: What each kind of key may do, in the words the page shows.
KEY_ROLE_WORDS = {
    roles.READER: "Read only",
    roles.SEARCHER: "Read and search",
    roles.OPERATOR: "Read, search and write",
}


class KeyRequest(BaseModel):
    name: str
    role: str
    collections: Optional[List[str]] = Field(
        default=None,
        description="The collections this key may reach. Left out, every collection its role allows.",
    )
    expires_in_days: Optional[int] = Field(
        default=None,
        gt=0,
        le=3650,
        description="How long the key works for. Left out, until it is revoked.",
    )
    requests_per_minute: Optional[int] = Field(
        default=None,
        gt=0,
        le=100000,
        description="How many requests a minute the key may make. Left out, the server's VECTRIXDB_KEY_REQUESTS_PER_MINUTE, and with that unset, no limit.",
    )


@router.get("/api/v1/keys", tags=["auth"])
async def list_keys(request: Request) -> Any:
    runtime = _need(request)
    return {
        "ok": True,
        "data": {
            "keys": [k.public() for k in runtime.store.keys()],
            "roles": [{"role": r, "means": KEY_ROLE_WORDS[r]} for r in roles.KEY_ROLES],
        },
    }


@router.post("/api/v1/keys", tags=["auth"])
async def create_key(request: Request, body: KeyRequest) -> Any:
    runtime = _need(request)
    caller = caller_of(request)
    expires_at = time.time() + body.expires_in_days * 86400 if body.expires_in_days else None
    try:
        record, key = runtime.store.create_key(
            body.name,
            body.role,
            caller.who if caller else None,
            collections=body.collections,
            expires_at=expires_at,
            per_minute=body.requests_per_minute,
        )
    except Exception as exc:  # noqa: BLE001 - a ConfigurationError, said back as it is
        return _refuse(400, str(exc))
    # What the key may reach is part of what was given out, so the line says it.
    scope = ", ".join(record.collections) if record.collections else "every collection"
    until = (
        time.strftime("%Y-%m-%d", time.gmtime(record.expires_at))
        if record.expires_at
        else "revoked"
    )
    runtime.access.record(
        "key_created",
        who=record.name,
        role=record.role,
        by=caller.who if caller else None,
        address=_address(request),
        reason=f"{scope}, until {until}",
    )
    # Shown once. What is kept is its hash.
    return {"ok": True, "data": {**record.public(), "key": key}}


@router.delete("/api/v1/keys/{key_id}", tags=["auth"])
async def revoke_key(request: Request, key_id: str) -> Any:
    runtime = _need(request)
    caller = caller_of(request)
    name = runtime.store.revoke_key(key_id)
    if name is None:
        raise HTTPException(status_code=404, detail="no such key")
    runtime.access.record(
        "key_revoked", who=name, by=caller.who if caller else None, address=_address(request)
    )
    return {"ok": True, "data": None}


class AccessCheckRequest(BaseModel):
    collection: str
    email: Optional[str] = Field(
        None, description="The address to try, as their sign-in would carry it"
    )
    subject: Optional[str] = Field(
        None, description="Their subject at the identity provider, when there is no address"
    )
    groups: Optional[List[str]] = Field(
        None,
        description="The security group ids to try them with, standing in for the token. A store policy reads the address alone",
    )


@router.post("/api/v1/access/check", tags=["inspect"])
async def access_check(request: Request, body: AccessCheckRequest) -> Any:
    """Whether somebody may retrieve from a collection, and why. Nothing is searched.

    The same decision the server makes before a search, given an address and
    the groups to try, so a policy is tested before anybody signs in. The
    groups stand in for the token; a store policy reads the address alone.
    Every check is written to the access log.
    """
    records = getattr(request.app.state, "collection_store", None)
    if records is None:
        return refusal(
            404,
            "Nothing is gated on this server: set VECTRIXDB_COLLECTION_STORE to where each collection's record is kept",
        )
    name = body.collection.strip()
    try:
        record = records.get(name)
        policy = record.policy_object() if record is not None else None
        decision = decide(
            policy,
            collection=name,
            method="session",
            email=body.email,
            subject=body.subject,
            groups=body.groups or [],
        )
    except CollectionStoreUnavailable as exc:
        return refusal(503, str(exc))
    except ConfigurationError as exc:
        return refusal(500, str(exc))
    runtime, caller = runtime_of(request), caller_of(request)
    if runtime is not None:
        runtime.access.record(
            "access_checked",
            who=caller.who if caller else None,
            role=caller.role if caller else None,
            method=caller.method if caller else None,
            collection=name,
            subject=body.email or body.subject,
            reason=decision.code,
            address=_address(request),
        )
    return {"ok": True, "data": {"collection": name, **decision.to_dict()}}


@router.get("/api/v1/access/daily", tags=["inspect"])
async def access_daily(request: Request, days: int = 14) -> Any:
    """Searches a day and how long they took, for everyone; sign-ins, refusals and policy denials too, for whoever may read the access log.

    Counts only: no names, no collections. Searches and their times say how
    the server is used, and a guest's overview draws them; who signed in and
    who was turned away is about people, so those three counts are left out
    unless the caller holds ``access.read``.
    """
    runtime = _need(request)
    counted = runtime.access.daily(max(1, min(days, 90)))
    if counted is None:
        return {
            "ok": True,
            "data": {
                "available": False,
                "reason": "the access log goes to the server's output, where it cannot be read back",
            },
        }
    caller = caller_of(request)
    if caller is not None and not caller.can("access.read"):
        for about_people in ("signins", "refused", "denied"):
            counted.pop(about_people, None)
    return {"ok": True, "data": {"available": True, **counted}}


@router.get("/api/v1/access/readers", tags=["inspect"])
async def access_readers(
    request: Request, days: int = 14, top: int = 10, offset: int = 0, q: Optional[str] = None
) -> Any:
    """Who searched and read most in the last days, the busiest first, a page at a time.

    ``top`` is the page, ``offset`` where it starts, ``q`` a find on the
    name, and ``total`` how many read at all. Names and counts, as the log
    itself holds.
    """
    runtime = _need(request)
    everybody = runtime.access.readers(max(1, min(days, 90)), 1_000_000)
    if everybody is None:
        return {
            "ok": True,
            "data": {
                "available": False,
                "reason": "the access log goes to the server's output, where it cannot be read back",
            },
        }
    listed, total = _page_of(everybody, q, max(1, min(top, 100)), offset, keys=("who",))
    return {
        "ok": True,
        "data": {
            "available": True,
            "days": max(1, min(days, 90)),
            "readers": listed,
            "total": total,
            "offset": max(0, offset),
        },
    }


@router.get("/api/v1/access", tags=["inspect"])
async def access_log(
    request: Request,
    limit: int = 200,
    offset: int = 0,
    q: Optional[str] = None,
    event: Optional[str] = None,
    who: Optional[str] = None,
) -> Any:
    """The access log, newest first, a page at a time: ``q`` finds in anything a line says, ``total`` is how many match."""
    runtime = _need(request)
    records, total = runtime.access.listing(
        max(1, min(limit, 1000)), max(0, offset), q=q, event=event, who=who
    )
    return {
        "ok": True,
        "data": {
            "records": records,
            "total": total,
            "offset": max(0, offset),
            "events": list(EVENTS),
            "grants": roles.table(),
            # Where the log is kept: stdout, file or blob. A log sent to the server's output cannot be listed here.
            "where": runtime.access.kind,
        },
    }
