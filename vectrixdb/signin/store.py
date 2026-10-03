"""People, sessions, sign-in links, recovery codes and failed attempts: what sign-in keeps, and how.

What is kept, and in what form:

* a session id, a sign-in link, a recovery code and an API key are kept as
  hashes. They are bearer secrets, and a copy of the store should not be a set
  of keys;
* an authenticator secret has to be read back to check a code, so it cannot
  be a hash. It is sealed with a key derived from the sign-in secret, which
  lives in the environment and not with the store;
* a passkey is its public key, which opens nothing;
* a password, only on a server that turned them on, is a salted scrypt hash.

The sign-in secret can be rotated. With two set, newest first, every
authenticator secret sealed under the old one is sealed again under the new
one when the store is opened, so the old one can then be taken away. If one
cannot be opened with any secret configured, the server does not start: a
server that started would have locked everybody out without saying why.

Where it is all kept is ``records.py``'s business: by default a SQLite file
beside the collections, at ``<database path>/auth/signin.db``, and with
``VECTRIXDB_SIGNIN_STORE`` PostgreSQL, Cosmos DB or DynamoDB, so several
servers share one sign-in and a redeploy signs nobody out. Anything that must
happen once (a code spent, a link used, a challenge answered) is a
compare-and-swap, so it happens once however many servers are asked at the
same moment.

A file from before kept each kind in a table of its own. The first time it is
opened it is copied into records, a copy of it as it was is kept beside it
(``signin.v1.db``), and its old tables are dropped.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple

from ..exceptions import ConfigurationError, DependencyError, VectrixError
from . import passwords, roles, totp
from .keys import derive
from .passkeys import PasskeyRefused
from .records import Record, Records, open_records

__all__ = [
    "LINK_MINUTES",
    "LOCK_MINUTES",
    "LOCK_STEPS",
    "MAX_ATTEMPTS",
    "STEP_UP_SECONDS",
    "ApiKey",
    "Person",
    "Session",
    "SignInStore",
    "normalise_email",
]


# ============================================================================
# SETTINGS: the logger, the minutes, the attempts, and the old tables
# ============================================================================
#
# How long a link lasts, how many failures lock, for how long and how it
# grows, how long a proof stays recent, how many times a change is retried,
# and the tables a file from before kept.

logger = logging.getLogger(__name__)

LINK_MINUTES = 15
#: Failed attempts allowed before the door stays shut for a while.
MAX_ATTEMPTS = 5
LOCK_MINUTES = 15
#: How long the door stays shut: longer each time, until a real sign-in resets it.
LOCK_STEPS = (15 * 60, 60 * 60, 4 * 3600, 24 * 3600)
#: How recently somebody must have proved it is them before a change that matters.
STEP_UP_SECONDS = 600
CHALLENGE_SECONDS = 300
#: Methods that belong to somebody on this server's own list, as opposed to single sign-on.
LOCAL_METHODS = ("email", "passkey")

# The kinds of record.
PERSON, SESSION, LINK, ATTEMPT, PASSKEY, CHALLENGE, APIKEY, RATE, EMERGENCY = (
    "person",
    "session",
    "link",
    "attempt",
    "passkey",
    "challenge",
    "apikey",
    "rate",
    "emergency",
)
#: How many times a change is tried again when another server changed the same record first.
_TRIES = 8
#: The tables a file from before records kept.
#: "visibility" is dropped without being read: who may search a collection is its record's policy now.
_OLD_TABLES = (
    "people",
    "sessions",
    "links",
    "recovery",
    "attempts",
    "passkeys",
    "challenges",
    "api_keys",
    "visibility",
)


# ============================================================================
# HASHES, ENCODINGS, AND AN ADDRESS
# ============================================================================
#
# INPUT   a value; an email
# OUTPUT  its SHA-256; base64 and back; the address normalised
#
# Everything bearer is kept as its hash.


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def normalise_email(email: str) -> str:
    return str(email or "").strip().lower()


# ============================================================================
# A PERSON, A SESSION, AND A KEY
# ============================================================================
#
# INPUT   what sign-in keeps about each
# OUTPUT  the three records; a key's collections tidied; a new person made;
#         the sign-in secret's one rule, kept here so the server and vectrixdb
#         check apply the same
#
# The shapes the routes read.


@dataclass
class Person:
    email: str
    role: str
    principal: dict = field(default_factory=dict)
    grants: list = field(default_factory=list)
    enrolled: bool = False
    disabled: bool = False
    created_at: float = 0.0
    last_sign_in: Optional[float] = None
    authenticator: bool = False
    passkeys: int = 0
    password: bool = False
    #: When they last signed in with single sign-on, which a passkey or a code is checked against.
    last_sso_at: Optional[float] = None

    def public(self) -> dict:
        return {
            "email": self.email,
            "role": self.role,
            "principal": self.principal,
            "grants": list(self.grants),
            "enrolled": self.enrolled,
            "disabled": self.disabled,
            "last_sign_in": self.last_sign_in,
            "authenticator": self.authenticator,
            "passkeys": self.passkeys,
            "last_sso_at": self.last_sso_at,
        }


@dataclass
class Session:
    subject: str
    email: Optional[str]
    name: Optional[str]
    role: str
    principal: dict
    method: str
    csrf: str
    created_at: float
    last_seen: float
    expires_at: float
    grants: list = field(default_factory=list)
    verified_at: float = 0.0
    key: str = ""
    #: The security groups they are in, as their sign-in said: what a collection's policy reads.
    groups: list = field(default_factory=list)
    #: Came in with single sign-on and is on the People list, so the record gives the role and they may keep ways of their own.
    listed: bool = False

    def public(self) -> dict:
        return {
            "subject": self.subject,
            "email": self.email,
            "name": self.name or self.email or self.subject,
            "role": self.role,
            "method": self.method,
            "expires_at": self.expires_at,
        }


@dataclass
class ApiKey:
    key_id: str
    name: str
    role: str
    prefix: str
    created_by: Optional[str]
    created_at: float
    last_used: Optional[float]
    #: The collections this key may reach. Empty is every collection its role
    #: allows, which is what a key made before this was, and still is.
    collections: Tuple[str, ...] = ()
    #: When it stops working. None is never.
    expires_at: Optional[float] = None
    #: How many requests a minute it may make. None is the server's setting,
    #: and with that unset, no limit, as a key has always had.
    per_minute: Optional[int] = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at

    def may_reach(self, collection: Optional[str]) -> bool:
        """Whether a request about this collection is this key's to make."""
        if not self.collections or collection is None:
            return True
        return collection in self.collections

    def public(self) -> dict:
        return {
            "id": self.key_id,
            "name": self.name,
            "role": self.role,
            "prefix": self.prefix,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "collections": list(self.collections),
            "expires_at": self.expires_at,
            "expired": self.expired,
            "requests_per_minute": self.per_minute,
        }


def _collections_for_a_key(collections: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """The collections a key is scoped to: tidied, in order, without repeats."""
    if not collections:
        return ()
    out: list = []
    for raw in collections:
        name = str(raw or "").strip()
        if not name:
            continue
        if len(name) > 128:
            raise ConfigurationError(f"{name[:40]!r} is too long to be a collection name")
        if name not in out:
            out.append(name)
    if len(out) > 64:
        raise ConfigurationError(
            "A key is scoped to at most 64 collections. Leave it unscoped instead"
        )
    return tuple(out)


def _new_person(role: str, principal: Optional[dict], grants: Optional[list]) -> dict:
    return {
        "role": role,
        "principal": dict(principal or {}),
        "grants": list(grants or []),
        "created_at": time.time(),
        "last_sign_in": None,
        "disabled": False,
        "totp_secret": None,
        "totp_confirmed": False,
        "last_step": None,
        "totp_pending": None,
        "totp_set_at": None,
        "totp_used_at": None,
        "password_hash": None,
        "password_set_at": None,
        "user_handle": None,
        "passkeys": [],
        "recovery": {},
    }


def check_secrets(secrets_: Sequence[str]) -> None:
    """The sign-in secret's one rule, kept here so the server and ``vectrixdb check`` apply the same."""
    if not secrets_ or any(len(s) < 32 for s in secrets_):
        raise ConfigurationError(
            "the sign-in secret must be at least 32 characters. "
            'Make one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )


# ============================================================================
# THE STORE
# ============================================================================
#
# INPUT   every question the sign-in routes ask
# OUTPUT  people, sessions, links, recovery codes, passkeys, authenticator
#         secrets, API keys and failed attempts, kept as hashes or sealed,
#         over whichever records the server was pointed at
#
# The sign-in secret can be rotated: with two set, newest first, everything is
# read with either and written with the newest.


class SignInStore:
    """Every question the sign-in routes ask of the state they keep.

    ``where`` is a path to a SQLite file (the default), an address naming
    another database (``records.open_records``), or records already open.
    ``key`` is that database's password or account key, when it needs one.
    """

    def __init__(self, where: Any, secrets_: Sequence[str], *, key: Optional[str] = None) -> None:
        check_secrets(secrets_)
        self._secrets = list(secrets_)
        # One key per job, never the secret itself: see keys.derive.
        self._signing = [derive(s, "cookies") for s in self._secrets]
        self._fernet: Any = None
        self._primary: Any = None
        # Where each collection's record lives, once use_collection_store() names it.
        self._collections: Any = None
        self._records: Records = open_records(where, key=key)
        try:
            self._bring_over_old_tables()
            self._reseal()
        except BaseException:
            self._records.close()
            raise

    @property
    def where(self) -> str:
        """Where this is kept, in words fit for a log."""
        return self._records.describe()

    def close(self) -> None:
        self._records.close()

    def _change(self, kind: str, key: str, change: Callable[[dict], Any]) -> Any:
        """Read a record, change it, and write it back only if nobody wrote it in between; again if somebody did.

        ``change`` edits the data in place and returns False to write nothing.
        Returned: what ``change`` returned on the try that was written (True
        for None), False when it declined, None when there is no such record.
        """
        for _ in range(_TRIES):
            record = self._records.get(kind, key)
            if record is None:
                return None
            outcome = change(record.data)
            if outcome is False:
                return False
            if self._records.replace(record):
                return True if outcome is None else outcome
        raise VectrixError(
            f"another server kept changing the same sign-in {kind} record. Try again"
        )

    # ------------------------------------------------------------ signing ---

    def sign(self, value: str) -> str:
        """``value.signature``, signed with the newest secret's cookie key."""
        mac = hmac.new(self._signing[0], value.encode(), hashlib.sha256).digest()
        return value + "." + base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def unsign(self, signed: Optional[str]) -> Optional[str]:
        """The value, if any current secret signed it. Two secrets is how one is rotated."""
        if not signed or "." not in signed:
            return None
        value, _, given = signed.rpartition(".")
        good = False
        for key in self._signing:
            mac = hmac.new(key, value.encode(), hashlib.sha256).digest()
            if hmac.compare_digest(base64.urlsafe_b64encode(mac).decode().rstrip("="), given):
                good = True
        return value if good else None

    def _cipher(self) -> Any:
        if self._fernet is None:
            try:
                from cryptography.fernet import Fernet, MultiFernet
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise DependencyError("cryptography", "signin") from exc
            current = [
                Fernet(base64.urlsafe_b64encode(derive(s, "sealing"))) for s in self._secrets
            ]
            # How secrets were sealed at first, so a store from then still opens.
            legacy = [
                Fernet(
                    base64.urlsafe_b64encode(
                        hashlib.sha256(b"vectrixdb authenticator secrets|" + s.encode()).digest()
                    )
                )
                for s in self._secrets
            ]
            self._primary = current[0]
            self._fernet = MultiFernet(current + legacy)
        return self._fernet

    def _resealed(self, people: Sequence[Record]) -> list[Record]:
        """Those whose authenticator secrets had to be sealed again under the newest secret, sealed again in place.

        Refuses, changing nothing anywhere, when one cannot be opened with any secret configured.
        """
        sealed = [
            (record, column)
            for record in people
            for column in ("totp_secret", "totp_pending")
            if record.data.get(column)
        ]
        if not sealed:
            return []
        cipher = self._cipher()
        from cryptography.fernet import InvalidToken

        unreadable, changed = set(), {}
        for record, column in sealed:
            value = record.data[column].encode()
            try:
                self._primary.decrypt(value)
                continue
            except InvalidToken:
                pass
            try:
                record.data[column] = cipher.rotate(value).decode()
                changed[record.key] = record
            except InvalidToken:
                unreadable.add(record.key)
        if unreadable:
            raise ConfigurationError(
                f"{len(unreadable)} authenticator(s) in {len({r.key for r, _ in sealed})} cannot be opened with the sign-in secret "
                "configured. If the secret was changed, put the old one back as the second value, "
                "VECTRIXDB_SIGNIN_SECRET=new,old, and start again: they are then sealed under the new one, "
                "and the old one can be taken away after that."
            )
        return list(changed.values())

    def _reseal(self) -> None:
        """Seal every authenticator secret under the newest secret, or refuse to start."""
        for record in self._resealed(self._records.query(PERSON)):
            if not self._records.replace(record):
                # Another server started at the same moment and got to this one first: look again.
                self._change(PERSON, record.key, self._reseal_data)

    def _reseal_data(self, data: dict) -> Any:
        """One person's data sealed again in place, for ``_change``: False when nothing needed it."""
        return None if self._resealed([Record(PERSON, "", data)]) else False

    def _seal(self, secret: str) -> str:
        return self._cipher().encrypt(secret.encode()).decode()

    def _open(self, sealed: str) -> str:
        return self._cipher().decrypt(sealed.encode()).decode()

    # ------------------------------------------------ a file from before ---

    def _bring_over_old_tables(self) -> None:
        """Copy a SQLite file's tables from before records into records, keeping the file as it was beside it."""
        connection = getattr(self._records, "sqlite_connection", None)
        if connection is None:
            return
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        old = [t for t in _OLD_TABLES if t in tables]
        if not old:
            return
        records = _records_from_tables(connection, tables)
        # Every sealed secret must open before anything is written: a refused start changes nothing.
        self._resealed([r for r in records if r.kind == PERSON])
        path = getattr(self._records, "path", None)
        if isinstance(path, Path):
            kept = path.with_name(path.stem + ".v1" + path.suffix)
            copy = sqlite3.connect(str(kept))
            try:
                connection.backup(copy)
                # Opening the file made the records table, still empty: the copy is the file as it was.
                copy.execute(f'DROP TABLE IF EXISTS "{getattr(self._records, "table", "records")}"')
                copy.commit()
            finally:
                copy.close()
            try:
                kept.chmod(0o600)
            except OSError:  # pragma: no cover - a file system without modes
                pass
            logger.warning(
                "The sign-in file was copied into its new form. The file as it was is kept at %s: delete it once people can sign in.",
                kept,
            )
        self._records.load(records, drop=old)  # type: ignore[attr-defined]

    # ------------------------------------------------------------- people ---

    def _person(self, record: Optional[Record]) -> Optional[Person]:
        if record is None:
            return None
        data = record.data
        authenticator = bool(data.get("totp_confirmed"))
        keys = len(data.get("passkeys") or [])
        return Person(
            email=record.key,
            role=data["role"],
            principal=dict(data.get("principal") or {}),
            grants=roles.clean_grants(data.get("grants") or []),
            enrolled=authenticator or keys > 0,
            disabled=bool(data.get("disabled")),
            created_at=data.get("created_at") or 0.0,
            last_sign_in=data.get("last_sign_in"),
            authenticator=authenticator,
            passkeys=keys,
            password=bool(data.get("password_hash")),
            last_sso_at=data.get("last_sso_at"),
        )

    def person(self, email: str) -> Optional[Person]:
        return self._person(self._records.get(PERSON, normalise_email(email)))

    def people(self) -> list[Person]:
        found = [self._person(r) for r in self._records.query(PERSON)]
        return sorted((p for p in found if p is not None), key=lambda p: p.email)

    def put_person(
        self,
        email: str,
        role: str,
        principal: Optional[dict] = None,
        grants: Optional[Sequence[str]] = None,
    ) -> Person:
        """Add somebody, or change their role or what they were given. Their ways to sign in are left alone."""
        email = normalise_email(email)
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ConfigurationError(f"{email!r} is not an email address")
        if role not in roles.ROLES:
            raise ConfigurationError(f"{role!r} is not a role. Roles are {', '.join(roles.ROLES)}")
        wanted = roles.clean_grants(grants) if grants is not None else None

        def change(data: dict) -> None:
            data["role"] = role
            if principal is not None:
                data["principal"] = dict(principal)
            if wanted is not None:
                data["grants"] = wanted

        if self._change(PERSON, email, change) is None and not self._records.create(
            Record(PERSON, email, _new_person(role, principal, wanted))
        ):
            self._change(PERSON, email, change)  # added by another server at the same moment
        # A change of role ends the sessions that carried the old one, and so does a change in what they were given.
        for record in self._records.query(SESSION, ix1=email):
            data = record.data
            if (data.get("method") in LOCAL_METHODS or data.get("listed")) and (
                data.get("role") != role
                or (wanted is not None and roles.clean_grants(data.get("grants") or []) != wanted)
            ):
                self._records.delete(SESSION, record.key)
        found = self.person(email)
        assert found is not None
        return found

    def _end_sessions_of(self, email: str) -> None:
        for record in self._records.query(SESSION, ix1=email):
            self._records.delete(SESSION, record.key)

    def set_disabled(self, email: str, disabled: bool) -> None:
        email = normalise_email(email)

        def change(data: dict) -> None:
            data["disabled"] = bool(disabled)

        self._change(PERSON, email, change)
        if disabled:
            self._end_sessions_of(email)

    def remove_person(self, email: str) -> bool:
        email = normalise_email(email)
        record = self._records.get(PERSON, email)
        gone = self._records.delete(PERSON, email) if record is not None else False
        for credential_id in (record.data.get("passkeys") if record is not None else None) or []:
            self._records.delete(PASSKEY, credential_id)
        for kind in (SESSION, LINK, PASSKEY):
            for other in self._records.query(kind, ix1=email):
                self._records.delete(kind, other.key)
        return gone

    def admins(self) -> int:
        return sum(
            1
            for r in self._records.query(PERSON)
            if r.data.get("role") == roles.ADMIN and not r.data.get("disabled")
        )

    def seed(self, entries: Sequence[tuple[str, str]]) -> int:
        """People named in configuration who are not here yet. Nobody already here is changed."""
        added = 0
        for email, role in entries:
            if self.person(email) is None:
                self.put_person(email, role)
                added += 1
        return added

    # ------------------------------------------------------ authenticator ---

    def begin_enrolment(self, email: str) -> str:
        """A new secret for this person, unconfirmed until they type a code from it."""
        secret = totp.new_secret()
        sealed = self._seal(secret)

        def change(data: dict) -> None:
            data.update(totp_secret=sealed, totp_confirmed=False, last_step=None)

        self._change(PERSON, normalise_email(email), change)
        return secret

    def _secret_of(self, email: str) -> tuple[Optional[str], Optional[int], bool]:
        record = self._records.get(PERSON, normalise_email(email))
        if record is None or not record.data.get("totp_secret"):
            return None, None, False
        data = record.data
        return (
            self._open(data["totp_secret"]),
            data.get("last_step"),
            bool(data.get("totp_confirmed")),
        )

    def pending_secret(self, email: str) -> Optional[str]:
        secret, _, confirmed = self._secret_of(email)
        return None if confirmed else secret

    def check_code(
        self, email: str, code: str, *, confirming: bool = False, now: Optional[float] = None
    ) -> bool:
        """Whether the code is right, now, and unused. A right code is spent by this call, on every server at once."""

        def spend(data: dict) -> Any:
            if not data.get("totp_secret") or bool(data.get("totp_confirmed")) == confirming:
                return False
            step = totp.verify(
                self._open(data["totp_secret"]), code, last_step=data.get("last_step"), now=now
            )
            if step is None:
                return False
            stamp = time.time()
            data.update(last_step=step, totp_confirmed=True, totp_used_at=stamp)
            # The code that confirms an authenticator also signs the person in, so it counts as a use.
            if confirming:
                data["totp_set_at"] = stamp
            return True

        return self._change(PERSON, normalise_email(email), spend) is True

    def emergency_spent(self, admin: str, mark: str, *, now: Optional[float] = None) -> bool:
        """Whether this emergency password was used in an emergency that is over. Nothing is written."""
        record = self._records.get(EMERGENCY, admin)
        if record is None:
            return False
        return self._spent(record.data, mark, time.time() if now is None else now)

    @staticmethod
    def _spent(data: dict, mark: str, now: float) -> bool:
        if mark in (data.get("spent") or []):
            return True
        return data.get("mark") == mark and (
            bool(data.get("closed")) or float(data.get("until") or 0.0) < now
        )

    def use_emergency(
        self, admin: str, mark: str, until: float, *, now: Optional[float] = None
    ) -> bool:
        """The emergency password has just been given, right. False when it was used in an emergency that is over.

        A password works for one emergency. Its first use is written down with
        when that emergency ends; while it lasts the password goes on working,
        and a later end given to the same emergency is taken. Once it is over,
        by its time or by being turned off, the password is spent on every
        server, and so is one that was replaced by another.
        """
        moment = time.time() if now is None else now
        for _ in range(_TRIES):
            record = self._records.get(EMERGENCY, admin)
            data = dict(record.data) if record is not None else {}
            spent = [str(m) for m in (data.get("spent") or [])]
            allowed = not self._spent(data, mark, moment)
            if (
                data.get("mark")
                and (data.get("mark") != mark or not allowed)
                and data["mark"] not in spent
            ):
                spent.append(str(data["mark"]))
            if allowed:
                data = {
                    "mark": mark,
                    "until": max(
                        float(until),
                        float(data.get("until") or 0.0) if data.get("mark") == mark else 0.0,
                    ),
                    "closed": False,
                    "used_at": moment,
                }
            else:
                data = {"mark": None, "until": 0.0, "closed": True, "used_at": data.get("used_at")}
            # The last fifty are remembered, which is years of emergencies.
            data["spent"] = spent[-50:]
            fresh = Record(EMERGENCY, admin, data)
            if record is None:
                if self._records.create(fresh):
                    return allowed
            else:
                fresh.version = record.version
                if self._records.replace(fresh):
                    return allowed
        return False

    def close_emergencies(self) -> int:
        """Emergency sign-in is off: whatever password was used while it was on is spent. How many were closed."""
        closed = 0
        for record in self._records.query(EMERGENCY):
            if record.data.get("mark") and not record.data.get("closed"):
                data = dict(record.data)
                data["closed"] = True
                fresh = Record(EMERGENCY, record.key, data)
                fresh.version = record.version
                closed += 1 if self._records.replace(fresh) else 0
        return closed

    def forget_unconfirmed(self, email: str) -> None:
        """Drop an authenticator secret that was handed out and never confirmed: the person chose a passkey instead."""

        def change(data: dict) -> Any:
            if data.get("totp_confirmed"):
                return False
            data.update(totp_secret=None, last_step=None)
            return True

        self._change(PERSON, normalise_email(email), change)

    def begin_replacement(self, email: str) -> str:
        """A second authenticator secret, kept aside until a code from it proves it works."""
        secret = totp.new_secret()
        sealed = self._seal(secret)

        def change(data: dict) -> None:
            data["totp_pending"] = sealed

        self._change(PERSON, normalise_email(email), change)
        return secret

    def confirm_replacement(self, email: str, code: str, *, now: Optional[float] = None) -> bool:
        def swap(data: dict) -> Any:
            pending = data.get("totp_pending")
            if not pending:
                return False
            step = totp.verify(self._open(pending), code, now=now)
            if step is None:
                return False
            data.update(
                totp_secret=pending,
                totp_pending=None,
                totp_confirmed=True,
                last_step=step,
                totp_set_at=time.time(),
            )
            return True

        return self._change(PERSON, normalise_email(email), swap) is True

    def remove_authenticator(self, email: str) -> bool:
        """Forget this person's authenticator app, and any replacement half set up. Their other ways stay."""

        def change(data: dict) -> Any:
            if not data.get("totp_confirmed") and not data.get("totp_pending"):
                return False
            data.update(
                totp_secret=None,
                totp_pending=None,
                totp_confirmed=False,
                last_step=None,
                totp_set_at=None,
                totp_used_at=None,
            )
            return True

        return self._change(PERSON, normalise_email(email), change) is True

    def stamp_sso(self, email: str, *, now: Optional[float] = None) -> None:
        """Somebody on the list just signed in with single sign-on: the time a passkey or a code is checked against."""
        stamp = time.time() if now is None else now

        def change(data: dict) -> None:
            data["last_sso_at"] = stamp

        self._change(PERSON, normalise_email(email), change)

    def reset_authenticator(self, email: str) -> None:
        """Forget every way this person signs in, their recovery codes and their sessions. They start over from a link."""
        email = normalise_email(email)
        dropped: list = []

        def change(data: dict) -> None:
            dropped[:] = data.get("passkeys") or []
            data.update(
                totp_secret=None,
                totp_pending=None,
                totp_confirmed=False,
                last_step=None,
                totp_set_at=None,
                totp_used_at=None,
                password_hash=None,
                password_set_at=None,
                passkeys=[],
                recovery={},
            )

        self._change(PERSON, email, change)
        for credential_id in dropped:
            self._records.delete(PASSKEY, credential_id)
        for kind in (SESSION, LINK, PASSKEY):
            for record in self._records.query(kind, ix1=email):
                self._records.delete(kind, record.key)

    def ways(self, email: str) -> dict:
        """How this person signs in, for their own page. Nothing secret."""
        record = self._records.get(PERSON, normalise_email(email))
        if record is None:
            return {}
        data = record.data
        return {
            "passkeys": self._passkeys_of(data),
            "authenticator": {
                "set_up_at": data.get("totp_set_at"),
                "used_at": data.get("totp_used_at"),
            }
            if data.get("totp_confirmed")
            else None,
            "recovery_codes_left": sum(
                1 for used in (data.get("recovery") or {}).values() if used is None
            ),
            "password": {"set_at": data.get("password_set_at")}
            if data.get("password_hash")
            else None,
        }

    # ---------------------------------------------------------- passwords ---

    def set_password(self, email: str, password: str) -> None:
        hashed = passwords.hash_password(password)

        def change(data: dict) -> None:
            data.update(password_hash=hashed, password_set_at=time.time())

        self._change(PERSON, normalise_email(email), change)

    def check_password(self, email: str, password: Optional[str]) -> bool:
        record = self._records.get(PERSON, normalise_email(email))
        return passwords.check(
            password, record.data.get("password_hash") if record is not None else None
        )

    # ------------------------------------------------------------ passkeys ---

    def user_handle(self, email: str) -> bytes:
        """A random handle for this person, the id a passkey carries. Not their address."""
        email = normalise_email(email)
        for _ in range(_TRIES):
            record = self._records.get(PERSON, email)
            if record is None:
                return secrets.token_bytes(16)
            if record.data.get("user_handle"):
                return _unb64(record.data["user_handle"])
            record.data["user_handle"] = _b64(secrets.token_bytes(16))
            if self._records.replace(record):
                return _unb64(record.data["user_handle"])
        raise VectrixError("another server kept changing the same sign-in person record. Try again")

    def add_passkey(
        self,
        email: str,
        credential_id: bytes,
        public_key: bytes,
        alg: int,
        sign_count: int,
        transports: Sequence[str],
        name: str,
    ) -> dict:
        email = normalise_email(email)
        key = _b64(credential_id)
        data = {
            "email": email,
            "public_key": base64.b64encode(public_key).decode(),
            "alg": int(alg),
            "sign_count": int(sign_count),
            "transports": list(transports),
            "name": (str(name or "Passkey").strip() or "Passkey")[:60],
            "created_at": time.time(),
            "last_used": None,
        }
        if not self._records.create(Record(PASSKEY, key, data, ix1=email)):
            raise PasskeyRefused("That passkey is already set up on this server.")

        def listed(person: dict) -> None:
            person["passkeys"] = [k for k in (person.get("passkeys") or []) if k != key] + [key]

        if self._change(PERSON, email, listed) is None:
            self._records.delete(PASSKEY, key)
            raise PasskeyRefused("Nobody on this server's list has that address.")
        return {
            "id": key,
            "name": data["name"],
            "created_at": data["created_at"],
            "last_used": None,
        }

    def _passkeys_of(self, person: dict) -> list[dict]:
        found = []
        for key in person.get("passkeys") or []:
            record = self._records.get(PASSKEY, key)
            if record is not None:
                data = record.data
                found.append(
                    {
                        "id": key,
                        "name": data["name"],
                        "created_at": data["created_at"],
                        "last_used": data.get("last_used"),
                    }
                )
        return sorted(found, key=lambda k: k["created_at"])

    def passkeys(self, email: str) -> list[dict]:
        record = self._records.get(PERSON, normalise_email(email))
        return self._passkeys_of(record.data) if record is not None else []

    def passkey_ids(self, email: str) -> list[bytes]:
        return [_unb64(k["id"]) for k in self.passkeys(email)]

    def passkey(self, credential_id: str) -> Optional[dict]:
        """The stored passkey this id names, with the public key and its owner. Only one its owner's list still has."""
        key = str(credential_id)
        record = self._records.get(PASSKEY, key)
        if record is None:
            return None
        owner = self._records.get(PERSON, record.data["email"])
        if owner is None or key not in (owner.data.get("passkeys") or []):
            return None
        data = record.data
        return {
            "email": data["email"],
            "public_key": base64.b64decode(data["public_key"]),
            "sign_count": data["sign_count"],
            "name": data["name"],
        }

    def touch_passkey(self, credential_id: str, sign_count: int) -> None:
        def change(data: dict) -> None:
            data.update(sign_count=int(sign_count), last_used=time.time())

        self._change(PASSKEY, str(credential_id), change)

    def remove_passkey(self, email: str, credential_id: str) -> bool:
        email, key = normalise_email(email), str(credential_id)
        record = self._records.get(PASSKEY, key)
        if record is None or record.data.get("email") != email:
            return False
        # The passkey first: until it is gone it still opens the door, whatever the list says.
        gone = self._records.delete(PASSKEY, key)

        def unlisted(person: dict) -> Any:
            keys = person.get("passkeys") or []
            if key not in keys:
                return False
            person["passkeys"] = [k for k in keys if k != key]
            return True

        self._change(PERSON, email, unlisted)
        return gone

    def new_challenge(self, purpose: str, email: str = "") -> bytes:
        challenge = secrets.token_bytes(32)
        self._records.purge()
        record = Record(
            CHALLENGE,
            hashlib.sha256(challenge).hexdigest(),
            {"purpose": purpose, "email": normalise_email(email)},
        )
        record.expires = time.time() + CHALLENGE_SECONDS
        self._records.create(record)
        return challenge

    def use_challenge(self, challenge: bytes, purpose: str) -> Optional[str]:
        """The address a challenge was issued for (empty for anybody), once, while it is fresh."""
        key = hashlib.sha256(challenge).hexdigest()
        record = self._records.get(CHALLENGE, key)
        # Whoever deletes it answered it; anybody else was second.
        if record is None or not self._records.delete(CHALLENGE, key, record.version):
            return None
        return str(record.data["email"]) if record.data.get("purpose") == purpose else None

    # ---------------------------------------------------------- recovery ---

    def new_recovery_codes(self, email: str) -> list[str]:
        email = normalise_email(email)
        codes = totp.new_recovery_codes()
        hashed = {_hash(email + "|" + totp.normalise_recovery(code)): None for code in codes}

        def change(data: dict) -> None:
            data["recovery"] = dict(hashed)

        self._change(PERSON, email, change)
        return codes

    def use_recovery_code(self, email: str, code: str) -> bool:
        email = normalise_email(email)
        wanted = _hash(email + "|" + totp.normalise_recovery(code))

        def spend(data: dict) -> Any:
            codes = dict(data.get("recovery") or {})
            if wanted not in codes or codes[wanted] is not None:
                return False
            codes[wanted] = time.time()
            data["recovery"] = codes
            return True

        return self._change(PERSON, email, spend) is True

    def recovery_codes_left(self, email: str) -> int:
        record = self._records.get(PERSON, normalise_email(email))
        return (
            0
            if record is None
            else sum(1 for used in (record.data.get("recovery") or {}).values() if used is None)
        )

    # --------------------------------------------------------------- links ---

    def new_link(self, email: str, purpose: str = "enrol", minutes: int = LINK_MINUTES) -> str:
        email = normalise_email(email)
        token = secrets.token_urlsafe(32)
        for record in self._records.query(LINK, ix1=email):
            if record.data.get("purpose") == purpose:
                self._records.delete(LINK, record.key)
        link = Record(
            LINK, _hash(token), {"email": email, "purpose": purpose, "used_at": None}, ix1=email
        )
        link.expires = time.time() + minutes * 60
        self._records.create(link)
        return token

    def peek_link(self, token: str, purpose: str) -> Optional[str]:
        """The email a live link belongs to, without spending it."""
        record = self._records.get(LINK, _hash(str(token)))
        if record is None or record.data.get("purpose") != purpose or record.data.get("used_at"):
            return None
        return str(record.data["email"])

    def use_link(self, token: str, purpose: str = "enrol") -> Optional[str]:
        """The email this link was sent to, once. A second use, or a late one, gets nothing."""

        def spend(data: dict) -> Any:
            if data.get("purpose") != purpose or data.get("used_at"):
                return False
            data["used_at"] = time.time()
            return str(data["email"])

        outcome = self._change(LINK, _hash(str(token)), spend)
        return outcome if isinstance(outcome, str) else None

    # ------------------------------------------------------------ attempts ---

    def _lock_of(self, key: str) -> Optional[float]:
        record = self._records.get(ATTEMPT, key)
        return record.data.get("locked_until") if record is not None else None

    def locked(self, key: str) -> bool:
        until = self._lock_of(key)
        return bool(until and until > time.time())

    def failed(self, key: str, *, limit: int = MAX_ATTEMPTS, escalate: bool = True) -> bool:
        """Count a failure. True when this one shut the door.

        Each time the door shuts it stays shut longer: fifteen minutes, an hour,
        four hours, a day. Only a real sign-in, or a day with no lock, starts
        the count again. A lock that simply ran out used to start it again,
        which let a patient guesser try five codes every fifteen minutes for ever.
        """
        for _ in range(_TRIES):
            now = time.time()
            record = self._records.get(ATTEMPT, key)
            data = record.data if record is not None else {}
            count, first_at, level = (
                data.get("count", 0),
                data.get("first_at", now),
                data.get("level", 0),
            )
            last_lock = data.get("locked_until")
            if last_lock and now - last_lock > LOCK_STEPS[-1]:
                level = 0
            if now - first_at > LOCK_MINUTES * 60:
                count, first_at = 0, now
            count += 1
            shut = count >= limit
            if shut:
                last_lock = now + (
                    LOCK_STEPS[min(level, len(LOCK_STEPS) - 1)] if escalate else LOCK_STEPS[0]
                )
                level = level + 1 if escalate else level
                count, first_at = 0, now
            fresh = Record(
                ATTEMPT,
                key,
                {"count": count, "first_at": first_at, "locked_until": last_lock, "level": level},
            )
            # Forgotten a day after the last lock ends, which is when it would start again anyway.
            fresh.expires = max(now, last_lock or 0.0) + LOCK_STEPS[-1] + 60
            if record is None:
                if self._records.create(fresh):
                    return shut
            else:
                fresh.version = record.version
                if self._records.replace(fresh):
                    return shut
        # Counted by others this fast, the door is treated as shut.
        return True

    def lock_minutes(self, key: str) -> int:
        """Whole minutes until the door opens again, at least one."""
        until = self._lock_of(key)
        left = (until - time.time()) if until else 0
        return max(1, int(left // 60) + (1 if left % 60 else 0))

    def succeeded(self, key: str) -> None:
        self._records.delete(ATTEMPT, key)

    # ------------------------------------------------------------ sessions ---

    def open_session(
        self,
        *,
        subject: str,
        email: Optional[str],
        name: Optional[str],
        role: str,
        principal: Optional[dict],
        method: str,
        hours: float,
        grants: Optional[Sequence[str]] = None,
        user_agent: Optional[str] = None,
        address: Optional[str] = None,
        groups: Optional[Sequence[str]] = None,
        listed: bool = False,
    ) -> tuple[str, Session]:
        """A new session and its id. The id is returned once and kept only as a hash.

        ``listed`` is for somebody who came in with single sign-on and is on the
        People list: their session is checked against the record, as a local
        one is, so turning them off or changing their role ends it.
        """
        if role not in roles.ROLES:
            raise ConfigurationError(f"{role!r} is not a role")
        sid = secrets.token_urlsafe(32)
        now = time.time()
        session = Session(
            subject=subject,
            email=normalise_email(email) if email else None,
            name=name,
            role=role,
            principal=dict(principal or {}),
            method=method,
            csrf=secrets.token_urlsafe(24),
            created_at=now,
            last_seen=now,
            expires_at=now + hours * 3600,
            grants=roles.clean_grants(grants),
            verified_at=now,
            key=_hash(sid)[:16],
            groups=[str(g) for g in (groups or [])],
            listed=bool(listed),
        )
        self._records.purge()
        record = Record(
            SESSION,
            _hash(sid),
            {
                "subject": session.subject,
                "email": session.email,
                "name": session.name,
                "role": session.role,
                "principal": session.principal,
                "method": session.method,
                "csrf": session.csrf,
                "created_at": now,
                "last_seen": now,
                "expires_at": session.expires_at,
                "grants": session.grants,
                "verified_at": now,
                "user_agent": (user_agent or "")[:300] or None,
                "address": address,
                "groups": session.groups,
                "listed": bool(listed),
            },
            expires=session.expires_at,
            ix1=session.email,
            ix2=subject,
        )
        if not self._records.create(record):  # pragma: no cover - 256 random bits do not collide
            raise VectrixError("a new session id was already taken")
        if session.email:

            def stamp(data: dict) -> None:
                data["last_sign_in"] = now

            self._change(PERSON, session.email, stamp)
        return sid, session

    def _still_theirs(self, data: dict) -> bool:
        """A session of somebody on the list holds while they are, with the role and grants it was opened with.

        Their sessions are ended when that changes; asking again here means one
        opened on another server at the same moment is not missed.
        """
        if (data.get("method") not in LOCAL_METHODS and not data.get("listed")) or not data.get(
            "email"
        ):
            return True
        person = self._records.get(PERSON, data["email"])
        if (
            person is None
            or person.data.get("disabled")
            or person.data.get("role") != data.get("role")
        ):
            return False
        return roles.clean_grants(person.data.get("grants") or []) == roles.clean_grants(
            data.get("grants") or []
        )

    def session(self, sid: Optional[str], *, idle_minutes: float = 120) -> Optional[Session]:
        """The session this id names, if it is still good. Looking it up counts as activity."""
        if not sid:
            return None
        key = _hash(sid)
        now = time.time()
        record = self._records.get(SESSION, key)
        if record is None:
            return None
        data = record.data
        if now - data["last_seen"] > idle_minutes * 60 or not self._still_theirs(data):
            self._records.delete(SESSION, key)
            return None
        if now - data["last_seen"] > 60:
            data["last_seen"] = now
            self._records.replace(
                record
            )  # losing this race only means another request stamped it first
        return Session(
            subject=data["subject"],
            email=data.get("email"),
            name=data.get("name"),
            role=data["role"],
            principal=dict(data.get("principal") or {}),
            method=data["method"],
            csrf=data["csrf"],
            created_at=data["created_at"],
            last_seen=now,
            expires_at=data["expires_at"],
            grants=roles.clean_grants(data.get("grants") or []),
            verified_at=data.get("verified_at") or 0.0,
            key=key[:16],
            groups=[str(g) for g in (data.get("groups") or [])],
            listed=bool(data.get("listed")),
        )

    def mark_verified(self, sid: Optional[str]) -> None:
        """This person just proved it is them again."""
        if not sid:
            return

        def change(data: dict) -> None:
            data["verified_at"] = time.time()

        self._change(SESSION, _hash(sid), change)

    def close_session(self, sid: Optional[str]) -> None:
        if sid:
            self._records.delete(SESSION, _hash(sid))

    def sessions_of(self, subject: str) -> list[dict]:
        """Where this person is signed in, newest first. Nothing that could be replayed."""
        found = sorted(
            self._records.query(SESSION, ix2=subject),
            key=lambda r: r.data.get("last_seen") or 0.0,
            reverse=True,
        )
        return [
            {
                "id": r.key[:16],
                "method": r.data["method"],
                "created_at": r.data["created_at"],
                "last_seen": r.data["last_seen"],
                "user_agent": r.data.get("user_agent"),
                "address": r.data.get("address"),
            }
            for r in found
        ]

    def close_sessions_of(
        self, subject: str, *, only: Optional[str] = None, keep: Optional[str] = None
    ) -> int:
        """End one of this person's sessions by its listed id, or all of them but ``keep``."""
        doomed = [
            r.key
            for r in self._records.query(SESSION, ix2=subject)
            if (only is None or r.key[:16] == only) and r.key[:16] != keep
        ]
        for key in doomed:
            self._records.delete(SESSION, key)
        return len(doomed)

    # ------------------------------------------------------------ api keys ---

    def create_key(
        self,
        name: str,
        role: str,
        created_by: Optional[str],
        collections: Optional[Sequence[str]] = None,
        expires_at: Optional[float] = None,
        per_minute: Optional[int] = None,
    ) -> tuple[ApiKey, str]:
        """A new named key. The key itself is returned once and kept only as a hash.

        ``collections`` narrows it to those collections, which is what an app
        built by somebody else should be given: a key that reads the one
        collection it was made for rather than everything its role allows.
        Empty is every collection, as a key has always been. ``expires_at``
        is when it stops working, so a key handed out for a piece of work
        does not outlive it.
        """
        if role not in roles.KEY_ROLES:
            raise ConfigurationError(
                f"{role!r} is not a role a key can have. Keys can be {', '.join(roles.KEY_ROLES)}"
            )
        name = str(name or "").strip()[:60]
        if not name:
            raise ConfigurationError("Give the key a name, so it is clear later what uses it")
        scope = _collections_for_a_key(collections)
        if expires_at is not None:
            expires_at = float(expires_at)
            if expires_at <= time.time():
                raise ConfigurationError(
                    "A key that has already expired is no use. Give a time in the future, or none at all"
                )
        if per_minute is not None:
            per_minute = int(per_minute)
            if per_minute < 1:
                raise ConfigurationError(
                    "A key that may make no requests is no key. Give a number of requests a minute, or none at all"
                )
        key_id = secrets.token_hex(4)
        key = f"vx_{key_id}_{secrets.token_urlsafe(32)}"
        made = ApiKey(
            key_id=key_id,
            name=name,
            role=role,
            prefix=key[:11],
            created_by=created_by,
            created_at=time.time(),
            last_used=None,
            collections=scope,
            expires_at=expires_at,
            per_minute=per_minute,
        )
        self._records.create(
            Record(
                APIKEY,
                _hash(key),
                {
                    "key_id": key_id,
                    "name": name,
                    "role": role,
                    "prefix": made.prefix,
                    "created_by": created_by,
                    "created_at": made.created_at,
                    "last_used": None,
                    "revoked_at": None,
                    "collections": list(scope),
                    "expires_at": expires_at,
                    "per_minute": per_minute,
                },
            )
        )
        return made, key

    @staticmethod
    def _key(data: dict, last_used: Optional[float]) -> ApiKey:
        # A record written before keys could be scoped has neither field, and
        # reads as what it was: every collection, and no expiry.
        return ApiKey(
            data["key_id"],
            data["name"],
            data["role"],
            data["prefix"],
            data.get("created_by"),
            data["created_at"],
            last_used,
            tuple(data.get("collections") or ()),
            data.get("expires_at"),
            data.get("per_minute"),
        )

    # ---------------------------------------------------------- rate limit ---

    def within_rate(self, bucket: str, per_minute: int) -> tuple[bool, int]:
        """Count one event against ``bucket``'s allowance for this minute.

        Returned: whether it is within the allowance, and how many seconds are
        left of the minute, which is what a 429's ``Retry-After`` says. The
        count lives in the store and not in the process, so it is one count
        however many servers are asked: kept in memory, three servers behind
        a load balancer gave every caller three allowances, and a restart
        forgave everybody. It is a fixed minute rather than a sliding one,
        because a fixed minute is one record that expires on its own.

        Too many servers changing the one record at the same moment reads as
        over the allowance, since that is only ever a burst from one caller.
        """
        now = time.time()
        minute = int(now // 60)
        left = max(1, int(60 - now % 60))
        key = f"{_hash(bucket)[:32]}:{minute}"
        if self._records.create(Record(RATE, key, {"n": 1}, expires=(minute + 2) * 60.0)):
            return True, left

        def one_more(data: dict) -> Any:
            if int(data.get("n", 0)) >= per_minute:
                return False
            data["n"] = int(data.get("n", 0)) + 1
            return None

        try:
            counted = self._change(RATE, key, one_more)
        except VectrixError:
            return False, left
        return counted is not False, left

    def api_key(self, given: Optional[str]) -> Optional[ApiKey]:
        """The live named key a caller presented, if it is one."""
        if not given or not given.startswith("vx_"):
            return None
        record = self._records.get(APIKEY, _hash(given))
        if record is None or record.data.get("revoked_at"):
            return None
        expires_at = record.data.get("expires_at")
        if expires_at is not None and time.time() >= float(expires_at):
            return None  # an expired key is no key, and reads as one that is not ours
        last_used = record.data.get("last_used")
        now = time.time()
        if not last_used or now - last_used > 60:
            record.data["last_used"] = now
            self._records.replace(record)
        return self._key(record.data, last_used)

    def keys(self) -> list[ApiKey]:
        live = [r.data for r in self._records.query(APIKEY) if not r.data.get("revoked_at")]
        return [
            self._key(d, d.get("last_used"))
            for d in sorted(live, key=lambda d: d["created_at"], reverse=True)
        ]

    def revoke_key(self, key_id: str) -> Optional[str]:
        """Revoke a key. Its name, or None when there was no such key."""
        for record in self._records.query(APIKEY):
            if record.data.get("key_id") != str(key_id) or record.data.get("revoked_at"):
                continue

            def revoke(data: dict) -> Any:
                if data.get("revoked_at"):
                    return False
                data["revoked_at"] = time.time()
                return True

            return (
                str(record.data["name"])
                if self._change(APIKEY, record.key, revoke) is True
                else None
            )
        return None

    # ---------------------------------------------------- the collection store ---

    def use_collection_store(self, store: Any) -> None:
        """Name the store every collection's record is kept in, so a collection that goes takes its record with it.

        One record answers who may search a collection, read by every server
        alike. None means no records are kept. See vectrixdb.collection_records.
        """
        self._collections = store

    def forget_collection(self, collection: str) -> None:
        """A collection is gone: its record goes with it, so one made again under the name starts with nobody."""
        if self._collections is not None:
            self._collections.delete(collection)


# ============================================================================
# A FILE FROM BEFORE
# ============================================================================
#
# INPUT   a SQLite file an older version kept tables in
# OUTPUT  every row as the records it becomes; anything past its time stays
#         behind
#
# Read once, on the first start after an upgrade.


def _rows(connection: sqlite3.Connection, table: str) -> list[dict]:
    cursor = connection.execute(f'SELECT * FROM "{table}"')  # noqa: S608 - names from _OLD_TABLES
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _records_from_tables(connection: sqlite3.Connection, tables: set) -> list[Record]:
    """Every row a file from before kept, as the records it becomes. Anything past its time stays behind."""

    def rows(table: str) -> list[dict]:
        return _rows(connection, table) if table in tables else []

    now = time.time()
    passkeys = rows("passkeys")
    recovery = rows("recovery")
    out: list[Record] = []
    for p in rows("people"):
        email = p["email"]
        data = _new_person(
            p["role"], json.loads(p.get("principal") or "{}"), json.loads(p.get("grants") or "[]")
        )
        data.update(
            created_at=p.get("created_at") or 0.0,
            last_sign_in=p.get("last_sign_in"),
            disabled=bool(p.get("disabled")),
            totp_secret=p.get("totp_secret"),
            totp_confirmed=bool(p.get("totp_confirmed")),
            last_step=p.get("last_step"),
            totp_pending=p.get("totp_pending"),
            totp_set_at=p.get("totp_set_at"),
            totp_used_at=p.get("totp_used_at"),
            password_hash=p.get("password_hash"),
            password_set_at=p.get("password_set_at"),
            user_handle=p.get("user_handle"),
            passkeys=[
                k["credential_id"]
                for k in sorted(passkeys, key=lambda k: k["created_at"])
                if k["email"] == email
            ],
            recovery={r["code_hash"]: r.get("used_at") for r in recovery if r["email"] == email},
        )
        out.append(Record(PERSON, email, data))
    for s in rows("sessions"):
        if s["expires_at"] > now:
            data = {
                k: s.get(k)
                for k in (
                    "subject",
                    "email",
                    "name",
                    "role",
                    "method",
                    "csrf",
                    "created_at",
                    "last_seen",
                    "expires_at",
                    "verified_at",
                    "user_agent",
                    "address",
                )
            }
            data.update(
                principal=json.loads(s.get("principal") or "{}"),
                grants=json.loads(s.get("grants") or "[]"),
            )
            data["verified_at"] = data["verified_at"] or 0.0
            out.append(
                Record(
                    SESSION,
                    s["sid_hash"],
                    data,
                    expires=s["expires_at"],
                    ix1=s.get("email"),
                    ix2=s["subject"],
                )
            )
    for link in rows("links"):
        if link["expires_at"] > now:
            out.append(
                Record(
                    LINK,
                    link["token_hash"],
                    {
                        "email": link["email"],
                        "purpose": link["purpose"],
                        "used_at": link.get("used_at"),
                    },
                    expires=link["expires_at"],
                    ix1=link["email"],
                )
            )
    for a in rows("attempts"):
        lock = a.get("locked_until")
        out.append(
            Record(
                ATTEMPT,
                a["key"],
                {
                    "count": a["count"],
                    "first_at": a["first_at"],
                    "locked_until": lock,
                    "level": a.get("level") or 0,
                },
                expires=max(now, lock or 0.0) + LOCK_STEPS[-1] + 60,
            )
        )
    for k in passkeys:
        out.append(
            Record(
                PASSKEY,
                k["credential_id"],
                {
                    "email": k["email"],
                    "public_key": k["public_key"],
                    "alg": k["alg"],
                    "sign_count": k.get("sign_count") or 0,
                    "transports": json.loads(k.get("transports") or "[]"),
                    "name": k["name"],
                    "created_at": k["created_at"],
                    "last_used": k.get("last_used"),
                },
                ix1=k["email"],
            )
        )
    for c in rows("challenges"):
        if c["expires_at"] > now:
            out.append(
                Record(
                    CHALLENGE,
                    c["challenge_hash"],
                    {"purpose": c["purpose"], "email": c["email"]},
                    expires=c["expires_at"],
                )
            )
    for k in rows("api_keys"):
        out.append(
            Record(
                APIKEY,
                k["key_hash"],
                {
                    key: k.get(key)
                    for key in (
                        "key_id",
                        "name",
                        "role",
                        "prefix",
                        "created_by",
                        "created_at",
                        "last_used",
                        "revoked_at",
                    )
                },
            )
        )
    return out
