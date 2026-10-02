"""Who may retrieve from a collection: its policy, and the check that runs before anything is searched.

A collection's policy names the people it answers, one of two ways::

    {"method": "token", "allow": [{"id": "…-0101", "name": "HR"}], "people": [{"email": "ama@company.com"}]}
    {"method": "store", "allow": [{"email": "ama@company.com"}, {"email": "kofi@company.com"}, {"domain": "company.com"}]}

``token`` answers whoever is in one of the listed security groups AND on the
list of people beside them. The groups are matched by object id against the
groups the identity provider signed into the person's token, which is what
Entra ID, Okta and Cognito all do; the name beside an id is for people
reading the list. The list is by work email, and it is never optional: not
everyone in a security group may search a collection, so a group alone is
refused where it is written, and a record written with no list reads as
nobody yet.

``store`` answers the people on a list kept with the collection's record,
where the record is stored: specific people by email, one each, as long a
list as you like, and a domain for everyone at it. It is matched against
the address the person signed in with. Person A, person B, person C: that
is the whole of it.

A collection with no policy answers nobody, and neither does a collection
nobody has made a record for: the check fails closed, because an open door is
the one mistake this cannot afford. There is no exception: that a collection
exists, its name and its size, is for everyone who reaches the server; what
is in it is for the people the policy names.

Nothing here reads a request. The server works out who is asking from their
session or token, and hands the policy what it found; ``decide`` is the whole
of the check, and the same function answers the dashboard's "check someone".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .exceptions import ConfigurationError

__all__ = ["METHODS", "NEEDS_A_LIST", "AccessPolicy", "Decision", "decide"]


# ============================================================================
# SETTINGS: the log, and the two methods
# ============================================================================
#
# token, security groups read from the sign-in token, and store, a list kept
# with the collection's record.

log = logging.getLogger("vectrixdb.collection_access")

#: The ways a policy names who may retrieve: security groups from the token, or a list of people kept with the record.
METHODS = ("token", "store")

#: Why a token policy with no list of people is refused, in the words the Policy tab shows.
NEEDS_A_LIST = "Add the people who may search. A group alone would let everyone in it search."


# ============================================================================
# THE POLICY
# ============================================================================
#
# INPUT   a record's policy: token with security groups by object id, or store
#         with emails and domains
# OUTPUT  the policy, cleaned and lowercased, its group ids, and itself in
#         words a person reads
#
# Old spellings are mapped: a people method is the store, a groups method is
# the token.


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _clean(value).lower()


@dataclass
class AccessPolicy:
    """Who may retrieve from one collection."""

    method: str
    allow: List[Dict[str, str]] = field(default_factory=list)
    #: A token policy's list: the people, by email, who may search when they
    #: are also in one of its groups. A store policy keeps its people in allow.
    people: List[Dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.method = _lower(self.method)
        if self.method in ("people", "everyone", "groups"):
            raise ConfigurationError(
                f"a policy's method is token or store; {self.method!r} is an earlier spelling. "
                "token lists security groups by id; store lists people by email, or a domain for everyone there"
            )
        if self.method not in METHODS:
            raise ConfigurationError(f"a policy's method is {' or '.join(METHODS)}, not {self.method!r}")
        if not isinstance(self.allow, (list, tuple)) or not self.allow:
            raise ConfigurationError(f"a {self.method} policy needs an allow list with at least one entry, or nobody could ever retrieve")
        cleaned: List[Dict[str, str]] = []
        seen: set = set()
        for entry in self.allow:
            if not isinstance(entry, Mapping):
                raise ConfigurationError(f"each allow entry is an object, not {entry!r}")
            if self.method == "token":
                group = _clean(entry.get("id"))
                if not group:
                    raise ConfigurationError("each group needs its id, the group's object id at the identity provider, which is what the token carries; the name is for people")
                row = {"id": group}
                if _clean(entry.get("name")):
                    row["name"] = _clean(entry.get("name"))
                key = group.lower()
            else:
                email, domain = _lower(entry.get("email")), _lower(entry.get("domain")).lstrip("@")
                if bool(email) == bool(domain):
                    raise ConfigurationError("each store entry is an email, or a domain for everyone there, and not both")
                if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
                    raise ConfigurationError(f"{email!r} is not an email address")
                if domain and ("@" in domain or "." not in domain):
                    raise ConfigurationError(f"{domain!r} is not a domain: company.com")
                row = {"email": email} if email else {"domain": domain}
                key = email or f"@{domain}"
            if key in seen:
                continue  # listed twice is listed once
            seen.add(key)
            cleaned.append(row)
        self.allow = cleaned
        self.people = self._listed(self.people)

    def _listed(self, people: Any) -> List[Dict[str, str]]:
        """A token policy's list of people, cleaned; a store policy has none beside its allow list."""
        if not isinstance(people, (list, tuple)):
            raise ConfigurationError("people is a list, each person by email")
        if self.method == "store":
            if people:
                raise ConfigurationError("a store policy lists its people in allow; people is the list beside a token policy's groups")
            return []
        listed: List[Dict[str, str]] = []
        for entry in people:
            if not isinstance(entry, Mapping):
                raise ConfigurationError(f"each person on the list is an object with an email, not {entry!r}")
            if _clean(entry.get("domain")):
                raise ConfigurationError("a group's list names people, each by email; a domain would let everyone in the group at it search")
            email = _lower(entry.get("email"))
            if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
                raise ConfigurationError(f"{email or entry!r} is not an email address")
            if {"email": email} not in listed:  # listed twice is listed once
                listed.append({"email": email})
        if not listed:
            raise ConfigurationError(NEEDS_A_LIST)
        return listed

    # ------------------------------------------------------------- shapes ---

    def to_dict(self) -> Dict[str, Any]:
        """The policy as a person writes it."""
        out: Dict[str, Any] = {"method": self.method, "allow": [dict(entry) for entry in self.allow]}
        if self.method == "token":
            out["people"] = [dict(entry) for entry in self.people]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AccessPolicy":
        """A policy as written. ``people``, the earlier spelling of a list of people, reads as ``store``; ``groups`` from the token as ``token``."""
        if not isinstance(data, Mapping):
            raise ConfigurationError("a policy is an object with a method")
        method = _lower(data.get("method"))
        if method == "people":
            method = "store"
        elif method == "groups" and _lower(data.get("source")) != "store":
            method = "token"
        people = data.get("people") if method == "token" else None
        return cls(method=method, allow=list(data.get("allow") or []), people=list(people or []))

    # ------------------------------------------------------------ reading ---

    def group_ids(self) -> Tuple[str, ...]:
        """The security group ids a token policy reads; nothing for a store policy."""
        return tuple(entry["id"] for entry in self.allow) if self.method == "token" else ()

    def describe(self) -> str:
        """The policy in words, for a log, a page or a spinner."""
        if self.method == "token":
            named = ", ".join(entry.get("name") or entry["id"] for entry in self.allow)
            count = len(self.people)
            return f"security group{'s' if len(self.allow) > 1 else ''} {named}, narrowed to {count} {'person' if count == 1 else 'people'} on the list"
        people = [entry["email"] for entry in self.allow if "email" in entry]
        domains = [f"everyone at {entry['domain']}" for entry in self.allow if "domain" in entry]
        return ", ".join(people + domains)


# ============================================================================
# THE DECISION
# ============================================================================
#
# INPUT   the policy, the collection, and who is asking: an email, a subject,
#         groups, a host key or a scoped key
# OUTPUT  allowed or not, with a code and why, in words a person can act on
#
# Runs before anything is searched. A host key passes; a scoped key passes
# only for its collections; a collection with no policy answers nobody.


@dataclass
class Decision:
    """What the check decided, and why, in words a person can act on."""

    allowed: bool
    code: str
    because: str
    method: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "because": self.because, "method": self.method}


def decide(
    policy: Optional[AccessPolicy],
    *,
    collection: str,
    method: str,
    email: Optional[str] = None,
    subject: Optional[str] = None,
    groups: Iterable[str] = (),
    host_key: bool = False,
    key_scope: Iterable[str] = (),
) -> Decision:
    """Whether this caller may retrieve from ``collection``, under its ``policy``.

    ``method`` is how the caller came in: ``key`` for an API key, ``guest``
    for nobody, and anything else for a person who signed in, by session or
    by token. A key is nobody in
    particular, so a policy cannot judge it: the server's own key,
    ``host_key``, passes, as the host's own hand, and a key an admin made
    for named collections passes those, ``key_scope``. Any other key is
    refused.

    A ``token`` policy reads ``groups``, the ids the caller's token carries,
    and then ``email``, which must be on its list: in a group and on the list.
    A ``store`` policy reads ``email``, the address they signed in with.
    """
    if policy is None:
        return Decision(False, "no_policy", f"{collection} is unavailable to anyone yet: an admin sets its policy")
    about = policy.method
    if method == "key":
        if host_key:
            return Decision(True, "host_key", "the server's own key, which is the host's own hand", about)
        if collection in set(key_scope):
            return Decision(True, "key_scoped", f"a key an admin made for {collection}", about)
        return Decision(False, "key_not_scoped", f"a key is nobody in particular, and this one was not made for {collection}", about)
    if method == "guest" or not (email or subject):
        return Decision(False, "not_signed_in", f"nobody signed in, and {collection} answers only people who did", about)
    who = email or subject or "the caller"
    if policy.method == "store":
        address = _lower(email)
        if not address:
            return Decision(False, "not_on_list", f"{who} signed in with no email address, and {collection} answers the addresses on its list", about)
        for entry in policy.allow:
            if "email" in entry and entry["email"] == address:
                return Decision(True, "on_list", f"{address} is on the list for {collection}", about)
            if "domain" in entry and address.endswith("@" + entry["domain"]):
                return Decision(True, "on_list", f"{address} is at {entry['domain']}, and everyone there may retrieve from {collection}", about)
        return Decision(False, "not_on_list", f"{address} is not on the list for {collection}", about)
    held = [str(g) for g in groups]
    wanted = {g.lower() for g in policy.group_ids()}
    matched = next((g for g in held if g.lower() in wanted), None)
    if matched is None:
        if not held:
            return Decision(False, "not_in_token", f"the token names no groups for {who}, and {collection} answers only its groups", about)
        return Decision(False, "not_in_token", f"{who} is in none of the groups that may retrieve from {collection}", about)
    name = next((entry.get("name") or entry["id"] for entry in policy.allow if entry["id"].lower() == matched.lower()), matched)
    address = _lower(email)
    if address and {"email": address} in policy.people:
        return Decision(True, "in_token", f"{who} is in {name} and on the list, which may retrieve from {collection}", about)
    # In the group is not enough: not everyone in a security group may search.
    return Decision(False, "not_on_list", f"{who} is in {name}, but not on the list for {collection}", about)
