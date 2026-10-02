"""Signing people in to the server and its dashboard.

Two ways in, and either or both may be on:

``oidc``
    Single sign-on through the company's identity provider. No password is
    kept here, second factors and leavers are the provider's business, and
    the groups it vouches for decide the role.
``email``
    For a team with no identity provider, or for the few people outside it.
    An admin lists addresses and roles. The first sign-in is a one-time link
    by email, where the person chooses a passkey (the PIN they unlock their
    computer with) or an authenticator app. After that it is the passkey, or
    the address and the six digit code. A server may also turn passwords on,
    and then the address takes a password and a code together, never a
    password alone.

Everything is configured from the environment, the way the API keys are. A
secret may instead be read from a file: ``VECTRIXDB_SIGNIN_SECRET_FILE``,
``VECTRIXDB_OIDC_CLIENT_SECRET_FILE``, ``VECTRIXDB_OIDC_CLIENT_KEY_FILE``,
``VECTRIXDB_OIDC_CLIENT_CERT_FILE``, ``VECTRIXDB_SMTP_URL_FILE``,
``VECTRIXDB_SIGNIN_STORE_FILE`` and ``VECTRIXDB_SIGNIN_STORE_KEY_FILE``.

===============================  ==============================================
``VECTRIXDB_SIGNIN``             ``oidc``, ``email`` or ``oidc,email``. Unset: off.
                                 ``oidc`` with no provider named yet signs people
                                 in with a code by email until one is. Passkeys
                                 are kept with ``email`` alone.
``VECTRIXDB_SIGNIN_SECRET``      32 characters or more. Two, comma separated,
                                 while one is being rotated: newest first.
``VECTRIXDB_PUBLIC_URL``         The address people type, ``https://...``. It
                                 is the redirect address and the link in email.
``VECTRIXDB_SESSION_HOURS``      How long a sign-in lasts. 8.
``VECTRIXDB_SESSION_IDLE_MINUTES``  Signed out after this long unused. 120.
``VECTRIXDB_SIGNIN_PASSWORDS``   ``on``: the email way in asks for a password and
                                 a code together. Off unless set.
``VECTRIXDB_GUESTS``             ``on``: people who have not signed in may look
                                 around; searching needs a sign-in.
``VECTRIXDB_ADMINS_USE_SSO``     ``on``: an admin may only sign in with single
                                 sign-on. The server console stays the way back in.
``VECTRIXDB_OIDC_ISSUER``        ``https://login.microsoftonline.com/<tenant>/v2.0``
``VECTRIXDB_OIDC_CLIENT_ID``
``VECTRIXDB_OIDC_CLIENT_SECRET``  Left out for a public client; PKCE is always used.
``VECTRIXDB_OIDC_CLIENT_KEY``    A private key (PEM) the server signs in to the
                                 provider with, in place of a client secret.
``VECTRIXDB_OIDC_CLIENT_CERT``   Its certificate (PEM): Entra ID finds the key by it.
``VECTRIXDB_OIDC_CLIENT_KEY_ID``  The key's id, for Okta and Keycloak.
``VECTRIXDB_OIDC_SCOPES``        ``openid profile email``
``VECTRIXDB_OIDC_GROUPS_CLAIM``  ``groups``
``VECTRIXDB_OIDC_ROLE_MAP``      JSON, group to role: ``{"<group id>": "admin"}``
``VECTRIXDB_OIDC_DEFAULT_ROLE``  The role of somebody in no mapped group. Unset: refused.
``VECTRIXDB_OIDC_PRINCIPAL_CLAIMS``  JSON, policy attribute to claim: ``{"clients": "groups"}``
``VECTRIXDB_OIDC_GRANT_MAP``     JSON, group to what its members are given by name:
                                 ``{"<group id>": ["document.read"]}``
``VECTRIXDB_OIDC_GROUPS_URL``    Where to fetch groups that did not fit in the token.
``VECTRIXDB_OIDC_LABEL``         The words on the button. ``Continue with SSO``.
``VECTRIXDB_OIDC_ALLOWED_EMAILS``  Addresses that may sign in, beside the People
                                 list: ``ama@company.com,@contractors.company.com``.
                                 ``*`` alone: the groups decide on their own.
``VECTRIXDB_OIDC_TOKEN_ROLE``    The role an app's access token is given, whoever
                                 it is for: ``searcher``. Unset, their groups'.
``VECTRIXDB_SSO_RECHECK_DAYS``   With single sign-on on, a passkey or a code works
                                 only for somebody who signed in with it within
                                 this many days, so a leaver's passkey stops too.
``VECTRIXDB_SIGNIN_USERS``       ``ada@example.com:admin,sam@example.com:viewer``.
                                 Adds whoever is missing; changes nobody.
``VECTRIXDB_SMTP_URL``           ``smtp://user:password@host:587`` or ``smtps://host:465``
``VECTRIXDB_MAIL_FROM``          The address sign-in emails come from.
``VECTRIXDB_SIGNIN_REQUIRE``     ``passkey``: people on the list sign in with a
                                 passkey only. A code gets somebody with none as
                                 far as making one.
``VECTRIXDB_COOKIE_SAMESITE``    ``strict`` or ``lax``. Strict, or lax while
                                 single sign-on is on.
``VECTRIXDB_AUTH_PATH``          The folder for ``signin.db`` and ``access.jsonl``.
                                 Default ``<database path>/auth``.
``VECTRIXDB_SIGNIN_STORE``       Where sign-in state is kept: a SQLite file by
                                 default, or ``postgresql://``, ``cosmos://`` or
                                 ``dynamodb://`` (see ``records``).
``VECTRIXDB_SIGNIN_STORE_KEY``   That database's password or account key.
``VECTRIXDB_ACCESS_LOG``         Where reads and sign-ins are recorded. Default
                                 ``<database path>/auth/access.jsonl``.
``VECTRIXDB_DEVELOPER_ACCESS``   ``on``: a username and a password, for trying the
                                 roles on this machine. Refused unless the public
                                 address is this machine's.
``VECTRIXDB_DEVELOPER_USERS``    Its accounts, name:role, comma separated:
                                 ``admin.user:admin,viewer.user:viewer``.
``VECTRIXDB_DEVELOPER_PASSWORD``  Their password. Never a default: set it here.
``VECTRIXDB_BREAK_GLASS``        ``on``: emergency sign-in, for while single sign-on
                                 is down. Off unless set; off, its address answers 404.
``VECTRIXDB_BREAK_GLASS_UNTIL``  When it turns itself off, in UTC:
                                 ``2026-09-28T02:00Z``. Needed while it is on.
``VECTRIXDB_BREAK_GLASS_ADMIN``  The one username it takes. It signs in as an admin.
``VECTRIXDB_BREAK_GLASS_PASSWORD_HASH``  The hash of its password, made with
                                 ``vectrixdb break-glass hash``. The password
                                 itself stays in a key vault, never in a setting.
===============================  ==============================================

For an enterprise both ways are on, ``oidc,email``, from one list: the People
list says who may sign in, whichever way they come, and what their role is.
Somebody signs in with single sign-on the first time, then adds a passkey or
an authenticator app from their account, which they may remove again later,
the last one too, because single sign-on still lets them in.

Developer Access is for one machine: accounts named in the settings, a
password, no second step. The server will not start with it unless its public
address is this machine's, and it answers only a caller on this machine.

Emergency sign-in is one page at a fixed, documented address,
``/dashboard/#/break-glass``. The sign-in page never links to it and nothing
about it is secret but the password and the code: an address is not a lock.
It takes the named admin and the password together, locks after wrong tries
like every other way in, writes ``break_glass_used`` to the access log each
time it lets somebody in, and ends every session it opened when it is turned
off or its time runs out. A password works for one emergency: once the
emergency it was used in is over, the next one needs a new password. Admins
see a banner while it is on, so it is not left on by mistake.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from ..exceptions import ConfigurationError
from . import roles
from .keys import env_secret
from .mail import LogSender, NoMailSender, Sender, SmtpSender
from .oidc import Identity, OidcClient, OidcConfig, SignInRefused
from .passwords import hash_password, problem_with
from .store import Person, Session, SignInStore

__all__ = [
    "BreakGlass",
    "DeveloperAccess",
    "Identity",
    "LogSender",
    "NoMailSender",
    "OidcClient",
    "OidcConfig",
    "Person",
    "Session",
    "SignInConfig",
    "SignInRefused",
    "SignInStore",
    "SmtpSender",
    "break_glass_hash",
    "roles",
]


# ============================================================================
# SETTINGS: the methods, and the local hosts
# ============================================================================
#
# The two ways in, and the addresses that mean this machine.

METHODS = ("oidc", "email")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
#: The emergency admin's password is typed rarely and kept in a vault, so it can afford to be long.
BREAK_GLASS_MIN_PASSWORD = 24
#: A hash as ``passwords.hash_password`` writes it: scrypt, its three costs, the salt and the digest.
_SCRYPT_HASH = re.compile(
    r"^scrypt\$\d{1,9}\$\d{1,3}\$\d{1,3}\$[A-Za-z0-9_-]{16,}\$[A-Za-z0-9_-]{32,}$"
)
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
logger = logging.getLogger("vectrixdb.signin")


# ============================================================================
# READING THE ENVIRONMENT
# ============================================================================
#
# INPUT   the environment and a name
# OUTPUT  a whole number of requests a minute, or none; whether a switch is
#         on; a JSON value
#
# Three readers, so the config below stays declarative.


def _per_minute(env: Mapping[str, str], name: str) -> Optional[int]:
    """A whole number of requests a minute, or None when the setting is empty."""
    given = env.get(name, "").strip()
    if not given:
        return None
    try:
        number = int(given)
    except ValueError:
        number = 0
    if number < 1:
        raise ConfigurationError(
            f"{name} is a whole number of requests a minute, 1 or more. It is {given!r}"
        )
    return number


def _on(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "") or "").strip().lower() in ("1", "on", "true", "yes")


def _json_env(env: Mapping[str, str], name: str) -> dict:
    raw = env.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ConfigurationError(f'{name} must be JSON, for example {{"group": "admin"}}') from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a JSON object")
    return value


# ============================================================================
# EMERGENCY SIGN-IN
# ============================================================================
#
# INPUT   the environment
# OUTPUT  the emergency admin, the hash of its password, and when it turns
#         itself off; none when it is off, or when its time has run out; the
#         hash of a new password, for the settings
#
# Every part is needed at once, and a part that is wrong stops the start: an
# emergency is the worst time to find out the way in was never set up. The
# password itself is never a setting: what is set is its hash, which checks a
# password and cannot be read back as one.


@dataclass(frozen=True)
class BreakGlass:
    admin: str
    password_hash: str = field(repr=False)
    #: When it turns itself off, seconds since the epoch.
    until: float = 0.0

    def open(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) < self.until

    @property
    def until_iso(self) -> str:
        return datetime.fromtimestamp(self.until, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def mark(self) -> str:
        """What the password is known by once it has been used: a hash of its hash, which opens nothing."""
        import hashlib

        return hashlib.sha256(self.password_hash.encode("utf-8")).hexdigest()[:24]


def break_glass_hash(password: str, admin: Optional[str] = None) -> str:
    """The hash of a new emergency password, for VECTRIXDB_BREAK_GLASS_PASSWORD_HASH. A weak one is refused."""
    if len(password) < BREAK_GLASS_MIN_PASSWORD:
        raise ConfigurationError(
            f"That password is {len(password)} characters. Use {BREAK_GLASS_MIN_PASSWORD} or more, made for this and kept in a key vault."
        )
    trouble = problem_with(password, admin)
    if trouble:
        raise ConfigurationError(trouble)
    return hash_password(password)


def _until(given: str) -> float:
    text = given.strip()
    try:
        moment = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.upper().endswith("Z") else text
        )
    except ValueError as exc:
        raise ConfigurationError(
            f"VECTRIXDB_BREAK_GLASS_UNTIL is {given!r}. Write it in UTC, for example 2026-09-28T02:00Z"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _break_glass(env: Mapping[str, str], methods: tuple) -> Optional[BreakGlass]:
    if not _on(env, "VECTRIXDB_BREAK_GLASS"):
        return None
    if (
        str(env.get("VECTRIXDB_BREAK_GLASS_PASSWORD", "") or "").strip()
        or str(env.get("VECTRIXDB_BREAK_GLASS_PASSWORD_FILE", "") or "").strip()
    ):
        raise ConfigurationError(
            "VECTRIXDB_BREAK_GLASS_PASSWORD holds the password itself, and the settings keep only its hash. Make the hash with: "
            "vectrixdb break-glass hash. Set it as VECTRIXDB_BREAK_GLASS_PASSWORD_HASH, keep the password in a key vault, and "
            "take VECTRIXDB_BREAK_GLASS_PASSWORD out"
        )
    if (
        str(env.get("VECTRIXDB_BREAK_GLASS_TOTP", "") or "").strip()
        or str(env.get("VECTRIXDB_BREAK_GLASS_TOTP_FILE", "") or "").strip()
    ):
        raise ConfigurationError(
            "Emergency sign-in takes no authenticator code: take VECTRIXDB_BREAK_GLASS_TOTP out"
        )
    admin = env.get("VECTRIXDB_BREAK_GLASS_ADMIN", "").strip()
    hashed = (env_secret(env, "VECTRIXDB_BREAK_GLASS_PASSWORD_HASH") or "").strip()
    until = env.get("VECTRIXDB_BREAK_GLASS_UNTIL", "").strip()
    missing = [
        name
        for name, value in (
            ("VECTRIXDB_BREAK_GLASS_UNTIL", until),
            ("VECTRIXDB_BREAK_GLASS_ADMIN", admin),
            ("VECTRIXDB_BREAK_GLASS_PASSWORD_HASH", hashed),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            f"VECTRIXDB_BREAK_GLASS is on, and it needs {', '.join(missing)} too"
        )
    if not _USERNAME.match(admin):
        raise ConfigurationError(
            f"VECTRIXDB_BREAK_GLASS_ADMIN is {admin!r}. Use letters, digits, dots, dashes and @, 64 at most"
        )
    if not _SCRYPT_HASH.match(hashed):
        raise ConfigurationError(
            "VECTRIXDB_BREAK_GLASS_PASSWORD_HASH is not a hash this server made. It starts scrypt$ and comes from: vectrixdb break-glass hash"
        )
    glass = BreakGlass(admin=admin, password_hash=hashed, until=_until(until))
    if not glass.open():
        logger.warning(
            "emergency sign-in turned itself off at %s, so it is off. Remove VECTRIXDB_BREAK_GLASS, or set a later VECTRIXDB_BREAK_GLASS_UNTIL",
            glass.until_iso,
        )
    return glass


# ============================================================================
# DEVELOPER ACCESS
# ============================================================================
#
# INPUT   the environment
# OUTPUT  the accounts for trying the roles on this machine and their password;
#         none when it is off
#
# A username and a password with nothing else is fine on the one machine it
# answers, and nowhere else, so a public address that is not this machine's
# stops the start.


@dataclass(frozen=True)
class DeveloperAccess:
    #: Account name to role.
    accounts: Mapping[str, str]
    password: str = field(repr=False)


def _developer_access(
    env: Mapping[str, str], public_url: Optional[str], methods: tuple = ()
) -> Optional[DeveloperAccess]:
    from urllib.parse import urlsplit

    if not _on(env, "VECTRIXDB_DEVELOPER_ACCESS"):
        return None
    if methods and "oidc" not in methods:
        raise ConfigurationError(
            "VECTRIXDB_DEVELOPER_ACCESS stands in for single sign-on on this machine, and single sign-on is not asked for: "
            "VECTRIXDB_SIGNIN=oidc or oidc,email, or turn Developer Access off"
        )
    if (urlsplit(public_url or "").hostname or "") not in _LOCAL_HOSTS:
        raise ConfigurationError(
            "VECTRIXDB_DEVELOPER_ACCESS is for this machine only, and VECTRIXDB_PUBLIC_URL is not this machine's: "
            "use http://localhost:<port>, or turn Developer Access off"
        )
    accounts: dict = {}
    for entry in env.get("VECTRIXDB_DEVELOPER_USERS", "").split(","):
        if not entry.strip():
            continue
        name, _, role = entry.strip().rpartition(":")
        if not _USERNAME.match(name) or role not in roles.ROLES:
            raise ConfigurationError(
                f"VECTRIXDB_DEVELOPER_USERS entry {entry.strip()!r} should read name:role, with a role of {', '.join(roles.ROLES)}"
            )
        if name.lower() in accounts:
            raise ConfigurationError(f"VECTRIXDB_DEVELOPER_USERS names {name} twice")
        accounts[name.lower()] = role
    if not accounts:
        raise ConfigurationError(
            "VECTRIXDB_DEVELOPER_ACCESS is on and VECTRIXDB_DEVELOPER_USERS names nobody: admin.user:admin, say"
        )
    password = env_secret(env, "VECTRIXDB_DEVELOPER_PASSWORD") or ""
    if not password:
        raise ConfigurationError(
            "VECTRIXDB_DEVELOPER_ACCESS is on and VECTRIXDB_DEVELOPER_PASSWORD is not set. There is no default password"
        )
    return DeveloperAccess(accounts=accounts, password=password)


def _recheck_days(env: Mapping[str, str], methods: tuple) -> Optional[int]:
    given = env.get("VECTRIXDB_SSO_RECHECK_DAYS", "").strip()
    if not given:
        return None
    if "oidc" not in methods or "email" not in methods:
        raise ConfigurationError(
            "VECTRIXDB_SSO_RECHECK_DAYS is for a server with single sign-on and the list's own ways both on: VECTRIXDB_SIGNIN=oidc,email"
        )
    try:
        days = int(given)
    except ValueError:
        days = 0
    if days < 1:
        raise ConfigurationError(
            f"VECTRIXDB_SSO_RECHECK_DAYS is a whole number of days, 1 or more. It is {given!r}"
        )
    return days


# ============================================================================
# THE CONFIG
# ============================================================================
#
# INPUT   the environment
# OUTPUT  everything sign-in reads, checked once: which ways in are on, the
#         provider, the secret, the mail server, the rate limits, the public
#         address
#
# A server that cannot sign anyone in safely says so at start, not at the
# first sign-in.


@dataclass
class SignInConfig:
    methods: tuple = ()
    secrets: tuple = ()
    public_url: Optional[str] = None
    session_hours: float = 8.0
    idle_minutes: float = 120.0
    oidc: Optional[OidcConfig] = None
    users: tuple = ()
    smtp_url: Optional[str] = None
    mail_from: Optional[str] = None
    store_path: Any = None
    access_log: Any = None
    sender: Optional[Sender] = field(default=None, repr=False)
    passwords: bool = False
    guests: bool = False
    #: Requests a minute for a named key that has no number of its own. None: no limit.
    key_requests_per_minute: Optional[int] = None
    admins_use_sso: bool = False
    #: Only passkeys for people on this server's own list; codes only while they make one.
    require_passkey: bool = False
    #: ``strict`` or ``lax``; None picks: lax while single sign-on is on, strict otherwise.
    cookie_samesite: Optional[str] = None
    #: Where sign-in state lives when it is not the SQLite file at ``store_path``.
    store_url: Optional[str] = field(default=None, repr=False)
    store_key: Optional[str] = field(default=None, repr=False)
    #: Emergency sign-in, while the usual sign-in is down, whichever that is. None: off.
    break_glass: Optional[BreakGlass] = None
    #: Accounts for trying the roles on this machine. None: off.
    developer: Optional[DeveloperAccess] = None
    #: With single sign-on on, days a passkey or a code keeps working after the last sign-in with it.
    sso_recheck_days: Optional[int] = None
    #: Single sign-on is asked for and the provider is not named yet: the button says so, and a code is the way in.
    sso_pending: bool = False
    #: The words on the single sign-on button, kept for while it is pending.
    sso_label: str = "Continue with SSO"
    #: The email way is here only to stand in for single sign-on alone, until its provider is named.
    email_stands_in: bool = False
    #: Whether people keep passkeys of their own here. Never beside single sign-on, set up yet or not.
    own_passkeys: bool = True

    def __post_init__(self) -> None:
        if "oidc" in self.methods or self.sso_pending:
            self.own_passkeys = False
        if self.require_passkey and not self.own_passkeys:
            raise ConfigurationError(
                "VECTRIXDB_SIGNIN_REQUIRE=passkey is for a server with no single sign-on, where people keep passkeys: VECTRIXDB_SIGNIN=email"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.methods) or self.developer is not None

    @property
    def secure_cookies(self) -> bool:
        return bool(self.public_url and self.public_url.startswith("https://"))

    @property
    def samesite(self) -> Literal["strict", "lax"]:
        """Strict unless the identity provider's redirect needs lax, or somebody said which."""
        chosen = self.cookie_samesite or ("lax" if "oidc" in self.methods else "strict")
        return "lax" if chosen == "lax" else "strict"

    @property
    def local(self) -> bool:
        """Whether the public address is this machine, where a link in the log reaches only its owner."""
        from urllib.parse import urlsplit

        return (urlsplit(self.public_url or "").hostname or "") in _LOCAL_HOSTS

    @classmethod
    def from_env(cls, db_path: Any, env: Optional[Mapping[str, str]] = None) -> "SignInConfig":
        env = os.environ if env is None else env
        methods = tuple(
            m.strip().lower() for m in env.get("VECTRIXDB_SIGNIN", "").split(",") if m.strip()
        )
        if not methods:
            if _on(env, "VECTRIXDB_BREAK_GLASS"):
                raise ConfigurationError(
                    "VECTRIXDB_BREAK_GLASS is on and sign-in is not: it is for while the usual sign-in is down, so set VECTRIXDB_SIGNIN"
                )
            if not _on(env, "VECTRIXDB_DEVELOPER_ACCESS"):
                return cls()
            # Developer Access alone still signs people in, so it needs what any sign-in needs.
        unknown = [m for m in methods if m not in METHODS]
        if unknown:
            raise ConfigurationError(
                f"VECTRIXDB_SIGNIN names {unknown[0]!r}. The ways to sign in are {' and '.join(METHODS)}"
            )
        secrets_ = tuple(
            s.strip()
            for s in (env_secret(env, "VECTRIXDB_SIGNIN_SECRET") or "").split(",")
            if s.strip()
        )
        if not secrets_:
            raise ConfigurationError(
                "sign-in is on, so VECTRIXDB_SIGNIN_SECRET is needed: 32 characters or more, the same on every restart. "
                'Make one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        asked = methods
        # Single sign-on asked for, and neither the provider nor the client named: not set up yet.
        # The page draws its button, says so, and a code by email is the way in until both are given.
        # One without the other is a mistake, and is said as one below.
        pending = (
            "oidc" in methods
            and not env.get("VECTRIXDB_OIDC_ISSUER", "").strip()
            and not env.get("VECTRIXDB_OIDC_CLIENT_ID", "").strip()
        )
        if pending:
            methods = tuple(m for m in methods if m != "oidc")
            if "email" not in methods:
                methods += ("email",)
        if _on(env, "VECTRIXDB_ADMINS_USE_SSO") and "oidc" not in asked:
            raise ConfigurationError(
                "VECTRIXDB_ADMINS_USE_SSO needs single sign-on on: VECTRIXDB_SIGNIN=oidc,email"
            )
        public_url = env.get("VECTRIXDB_PUBLIC_URL", "").strip().rstrip("/") or None
        if public_url and not public_url.startswith(
            ("https://", "http://localhost", "http://127.0.0.1")
        ):
            raise ConfigurationError(
                "VECTRIXDB_PUBLIC_URL must be https, because the session cookie is only sent over https"
            )

        if not public_url:
            raise ConfigurationError(
                "sign-in needs VECTRIXDB_PUBLIC_URL, the address people type to reach this server. The identity provider "
                "sends people back to it and the sign-in email links to it, and neither may be taken from a request, "
                "which can claim to be any address it likes"
            )

        oidc = None
        if "oidc" in methods:
            oidc = OidcConfig(
                issuer=env.get("VECTRIXDB_OIDC_ISSUER", "").strip(),
                client_id=env.get("VECTRIXDB_OIDC_CLIENT_ID", "").strip(),
                client_secret=env_secret(env, "VECTRIXDB_OIDC_CLIENT_SECRET"),
                scopes=env.get("VECTRIXDB_OIDC_SCOPES", "").strip() or "openid profile email",
                groups_claim=env.get("VECTRIXDB_OIDC_GROUPS_CLAIM", "").strip() or "groups",
                role_map=_json_env(env, "VECTRIXDB_OIDC_ROLE_MAP"),
                default_role=env.get("VECTRIXDB_OIDC_DEFAULT_ROLE", "").strip() or None,
                principal_claims=_json_env(env, "VECTRIXDB_OIDC_PRINCIPAL_CLAIMS"),
                groups_url=env.get("VECTRIXDB_OIDC_GROUPS_URL", "").strip() or None,
                label=env.get("VECTRIXDB_OIDC_LABEL", "").strip() or "Continue with SSO",
                grant_map=_json_env(env, "VECTRIXDB_OIDC_GRANT_MAP"),
                allowed_emails=tuple(
                    e
                    for e in env.get("VECTRIXDB_OIDC_ALLOWED_EMAILS", "")
                    .replace(";", ",")
                    .split(",")
                    if e.strip()
                ),
                api_audience=env.get("VECTRIXDB_OIDC_API_AUDIENCE", "").strip() or None,
                token_role=env.get("VECTRIXDB_OIDC_TOKEN_ROLE", "").strip().lower() or None,
                client_key=env_secret(env, "VECTRIXDB_OIDC_CLIENT_KEY"),
                client_cert=env_secret(env, "VECTRIXDB_OIDC_CLIENT_CERT"),
                client_key_id=env.get("VECTRIXDB_OIDC_CLIENT_KEY_ID", "").strip() or None,
            )

        users = []
        for entry in env.get("VECTRIXDB_SIGNIN_USERS", "").split(","):
            if not entry.strip():
                continue
            email, _, role = entry.strip().rpartition(":")
            if not email or role not in roles.ROLES:
                raise ConfigurationError(
                    f"VECTRIXDB_SIGNIN_USERS entry {entry.strip()!r} should read address:role, with a role of {', '.join(roles.ROLES)}"
                )
            users.append((email, role))

        require = env.get("VECTRIXDB_SIGNIN_REQUIRE", "").strip().lower()
        if require not in ("", "passkey"):
            raise ConfigurationError(
                f"VECTRIXDB_SIGNIN_REQUIRE is {require!r}. It can be passkey, or left unset"
            )
        if require and ("email" not in asked or "oidc" in asked):
            raise ConfigurationError(
                "VECTRIXDB_SIGNIN_REQUIRE=passkey is for people on this server's own list: VECTRIXDB_SIGNIN=email"
            )
        if require and _on(env, "VECTRIXDB_SIGNIN_PASSWORDS"):
            raise ConfigurationError(
                "VECTRIXDB_SIGNIN_REQUIRE=passkey and VECTRIXDB_SIGNIN_PASSWORDS=on disagree: a passkey needs no password. Choose one"
            )
        samesite = env.get("VECTRIXDB_COOKIE_SAMESITE", "").strip().lower() or None
        if samesite not in (None, "strict", "lax"):
            raise ConfigurationError(
                f"VECTRIXDB_COOKIE_SAMESITE is {samesite!r}. It can be strict or lax"
            )
        access = env.get("VECTRIXDB_ACCESS_LOG", "").strip()
        store_url = env_secret(env, "VECTRIXDB_SIGNIN_STORE")
        if store_url and store_url.strip().lower() == "sqlite":
            store_url = None

        # The sign-in folder sits beside the collections unless it is put somewhere
        # of its own, which it needs when the collections live in a service and
        # the local disk does not last.
        root = Path(
            env.get("VECTRIXDB_AUTH_PATH", "").strip()
            or Path(db_path or "./vectrixdb_data") / "auth"
        )
        return cls(
            methods=methods,
            secrets=secrets_,
            public_url=public_url,
            session_hours=float(env.get("VECTRIXDB_SESSION_HOURS", "") or 8),
            idle_minutes=float(env.get("VECTRIXDB_SESSION_IDLE_MINUTES", "") or 120),
            oidc=oidc,
            users=tuple(users),
            smtp_url=env_secret(env, "VECTRIXDB_SMTP_URL"),
            mail_from=env.get("VECTRIXDB_MAIL_FROM", "").strip() or None,
            store_path=root / "signin.db",
            # An address is kept as it is written: a path would fold its // into one.
            access_log=access if "://" in access else Path(access or root / "access.jsonl"),
            passwords=_on(env, "VECTRIXDB_SIGNIN_PASSWORDS") and "email" in methods,
            guests=_on(env, "VECTRIXDB_GUESTS"),
            key_requests_per_minute=_per_minute(env, "VECTRIXDB_KEY_REQUESTS_PER_MINUTE"),
            admins_use_sso=_on(env, "VECTRIXDB_ADMINS_USE_SSO") and not pending,
            require_passkey=require == "passkey",
            cookie_samesite=samesite,
            store_url=store_url.strip() if store_url else None,
            store_key=env_secret(env, "VECTRIXDB_SIGNIN_STORE_KEY"),
            break_glass=_break_glass(env, asked),
            developer=_developer_access(env, public_url, asked),
            sso_recheck_days=None if pending else _recheck_days(env, asked),
            sso_pending=pending,
            sso_label=env.get("VECTRIXDB_OIDC_LABEL", "").strip() or "Continue with SSO",
            own_passkeys="oidc" not in asked,
            email_stands_in=pending and "email" not in asked,
        )

    def make_sender(self) -> Sender:
        if self.sender is not None:
            return self.sender
        if self.smtp_url:
            return SmtpSender(self.smtp_url, self.mail_from or "")
        # A link in the log is only safe where the log is on its owner's screen.
        return LogSender() if self.local else NoMailSender()
