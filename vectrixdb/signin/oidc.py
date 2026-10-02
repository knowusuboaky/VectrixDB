"""Sign-in through a company's identity provider, by OpenID Connect.

One integration for Entra ID, Okta, Google Workspace, Auth0, Keycloak and
anything else that publishes ``/.well-known/openid-configuration``. It is the
authorization code flow with PKCE, which is the only one worth having: the
browser carries a code that is useless without a verifier it never sees, and
the tokens arrive over a connection from this server to the provider.

The identity token is verified before a single claim is read out of it.
Reading claims from an unverified token is the classic mistake, because
base64 looks like a signature to nobody but the person in a hurry. So:
the signature against the provider's published keys, with an allowlist of
algorithms that leaves out ``none`` and the symmetric ones; the issuer,
exactly; the audience, this client; the expiry with a minute of tolerance;
an age of ten minutes at most, because it was minted for this exchange; the
nonce sent with the request; and ``azp`` when there is more than one
audience. After that the token is thrown away. It is not a session.

What the person may do is decided here, once, at sign-in, from the groups
the provider vouches for, and written to the session record. A request never
names its own role.

Groups that do not fit in the token. Entra ID leaves the groups out of a
token when there are too many and says so with ``_claim_names`` or
``hasgroups``. The way to fetch them is a call to Microsoft Graph whose
address, scope and paging are Microsoft's to define, so they are
configuration here and not a guess in source: ``groups_url`` is called with
the access token, and the ``id`` of each entry in ``value`` is a group,
following ``@odata.nextLink``. With no ``groups_url`` a sign-in that hits the
limit is refused with a message that says why, because signing somebody in
with an empty group list would quietly give them the wrong role.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlencode, urlsplit

from ..exceptions import ConfigurationError, DependencyError, VectrixError
from . import roles

__all__ = [
    "ALGORITHMS",
    "Identity",
    "OidcClient",
    "OidcConfig",
    "SignInRefused",
    "Transport",
    "challenge_of",
    "new_verifier",
]


# ============================================================================
# SETTINGS: the transport, the logger, the algorithms, and the token's limits
# ============================================================================
#
# The call shape a transport has, the signature algorithms accepted, how old a
# token may be, the clock leeway, and how many pages of groups are fetched.

#: ``(method, url, headers, body, timeout)`` to ``(status, headers, body)``, the shape the extractors use.
Transport = Callable[[str, str, dict, bytes, float], tuple]

logger = logging.getLogger("vectrixdb.signin")

ALGORITHMS = ("RS256", "ES256", "PS256", "RS384", "ES384", "RS512")
MAX_TOKEN_AGE = 600
LEEWAY = 60
_MAX_GROUP_PAGES = 20


# ============================================================================
# REFUSED, THE CONFIG, AND AN IDENTITY
# ============================================================================
#
# INPUT   a provider's settings; a verified token
# OUTPUT  a refusal with a reason fit to show the person; the configuration;
#         who signed in
#
# The reason shown is never the reason logged.


class SignInRefused(VectrixError):
    """A sign-in that did not succeed, with a reason fit to show the person."""

    def __init__(self, reason: str, *, code: str = "refused") -> None:
        self.reason = reason
        self.code = code
        super().__init__(reason)


@dataclass
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: Optional[str] = None
    scopes: str = "openid profile email"
    groups_claim: str = "groups"
    #: group id or name to role. The highest role among a person's groups wins.
    role_map: Mapping[str, str] = field(default_factory=dict)
    #: the role of somebody in none of the mapped groups. None refuses them.
    default_role: Optional[str] = None
    #: principal attribute to claim name, for entitlement policies: {"clients": "groups"}.
    principal_claims: Mapping[str, str] = field(default_factory=dict)
    groups_url: Optional[str] = None
    label: str = "Continue with SSO"
    #: group to the actions its members are given by name: {"<group>": ["document.read"]}.
    grant_map: Mapping[str, Sequence[str]] = field(default_factory=dict)
    #: Only these addresses sign in, beside the People list, and ``@company.com`` means anybody
    #: at that domain. A second lock beside the security group: in the group and on a list, or
    #: not at all. ``*`` alone says the groups decide alone, on purpose.
    allowed_emails: Sequence[str] = ()
    #: The role somebody calling the API with their own access token is given, whoever they
    #: are: ``searcher`` reads and searches, and each collection's policy says where. Unset,
    #: their groups give it, as a sign-in's do. The platform's sign-in list is not asked:
    #: it says who runs the platform, and a token is somebody using a collection.
    token_role: Optional[str] = None
    #: A private key (PEM) the server proves itself with, in place of a client secret: it
    #: signs a short-lived assertion and the provider checks it against the certificate
    #: registered for the application. No secret travels, so none can leak.
    client_key: Optional[str] = field(default=None, repr=False)
    #: The certificate that goes with the key (PEM). Entra ID finds the key by its thumbprint.
    client_cert: Optional[str] = field(default=None, repr=False)
    #: The key's id as registered, for providers that look keys up by ``kid`` (Okta, Keycloak).
    client_key_id: Optional[str] = None
    #: The audience of an access token this server takes as a Bearer token, ``api://vectrixdb``.
    #: Unset, no token is taken: an app reaches the server with a key, as nobody in particular.
    api_audience: Optional[str] = None
    _signer: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.issuer or not self.client_id:
            raise ConfigurationError("single sign-on needs VECTRIXDB_OIDC_ISSUER and VECTRIXDB_OIDC_CLIENT_ID")
        if self.client_key and self.client_secret:
            raise ConfigurationError(
                "VECTRIXDB_OIDC_CLIENT_KEY and VECTRIXDB_OIDC_CLIENT_SECRET are both set. Choose one: the key, so no secret exists to leak"
            )
        if self.client_cert and not self.client_key:
            raise ConfigurationError("VECTRIXDB_OIDC_CLIENT_CERT is set without VECTRIXDB_OIDC_CLIENT_KEY, the private key it belongs to")
        if self.client_key:
            self._signer = _ClientSigner(self.client_key, self.client_cert, self.client_key_id)
        self.allowed_emails = tuple(str(e).strip().lower() for e in self.allowed_emails if str(e).strip())
        if "*" in self.allowed_emails and len(self.allowed_emails) > 1:
            raise ConfigurationError("VECTRIXDB_OIDC_ALLOWED_EMAILS is * alone, the groups deciding on their own, or a list of addresses: not both")
        for entry in self.allowed_emails:
            if entry != "*" and ("@" not in entry or entry.endswith("@") or " " in entry):
                raise ConfigurationError(
                    f"VECTRIXDB_OIDC_ALLOWED_EMAILS has {entry!r}. Each entry is an address, ama@company.com, or a domain, @company.com"
                )
        if self.token_role is not None and (self.token_role not in roles.GRANTS or self.token_role in (roles.ADMIN, roles.GUEST)):
            raise ConfigurationError(
                f"VECTRIXDB_OIDC_TOKEN_ROLE is {self.token_role!r}. It is one of reader, searcher, viewer or operator: never an admin, since a token is an app"
            )
        if not self.issuer.startswith("https://") and not self.issuer.startswith("http://localhost"):
            raise ConfigurationError("the identity provider's issuer must be an https address")
        for group, role in self.role_map.items():
            if role not in roles.ROLES:
                raise ConfigurationError(f"group {group!r} maps to {role!r}, which is not a role")
        for group, given in self.grant_map.items():
            unknown = [g for g in given if g not in roles.GRANTABLE]
            if unknown or isinstance(given, str):
                raise ConfigurationError(
                    f"group {group!r} is given {unknown[0] if unknown else given!r}, which cannot be given. What can: {', '.join(sorted(roles.GRANTABLE))}"
                )
        if self.default_role is not None and self.default_role not in roles.ROLES:
            raise ConfigurationError(f"the default role {self.default_role!r} is not a role")
        if not self.role_map and self.default_role is None:
            raise ConfigurationError(
                "single sign-on needs VECTRIXDB_OIDC_ROLE_MAP, VECTRIXDB_OIDC_DEFAULT_ROLE, or both: "
                "without them nobody who signs in has a role"
            )


@dataclass
class Identity:
    subject: str
    email: Optional[str]
    name: Optional[str]
    role: str
    groups: list
    principal: dict
    grants: list = field(default_factory=list)
    #: On the People list: their role and grants are the record's, and the session is checked against it.
    listed: bool = False


# ============================================================================
# THE CLIENT ASSERTION
# ============================================================================
#
# INPUT   a private key from the environment
# OUTPUT  a signed assertion the server proves itself with at the token
#         address, RFC 7523
#
# For a provider that takes a key instead of a client secret.


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_SECONDS = 300


def _pem(value: str) -> bytes:
    """A PEM from an environment variable. Some platforms hand newlines over as ``\\n``."""
    value = str(value).strip()
    if "\n" not in value and "\\n" in value:
        value = value.replace("\\n", "\n")
    return value.encode()


class _ClientSigner:
    """Signs the assertion a server proves itself with at the token address (RFC 7523).

    RSA keys sign RS256 and EC keys ES256 or ES384. With the certificate, the
    header carries its SHA-1 and SHA-256 thumbprints, which is how Entra ID
    finds the key; with a key id, ``kid``. A certificate that has run out
    stops the server at start, and one that runs out within a month says so.
    """

    def __init__(self, key_pem: str, cert_pem: Optional[str], key_id: Optional[str]) -> None:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec, rsa
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise DependencyError("cryptography", "signin") from exc
        try:
            key = serialization.load_pem_private_key(_pem(key_pem), password=None)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(
                "VECTRIXDB_OIDC_CLIENT_KEY is not a private key in PEM without a passphrase: -----BEGIN PRIVATE KEY----- ..."
            ) from exc
        if isinstance(key, rsa.RSAPrivateKey):
            if key.key_size < 2048:
                raise ConfigurationError(f"VECTRIXDB_OIDC_CLIENT_KEY is a {key.key_size} bit RSA key. Use 2048 bits or more")
            self.algorithm = "RS256"
        elif isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name in ("secp256r1", "secp384r1"):
            self.algorithm = "ES256" if key.curve.name == "secp256r1" else "ES384"
        else:
            raise ConfigurationError("VECTRIXDB_OIDC_CLIENT_KEY must be an RSA key, or an EC key on P-256 or P-384")
        self.key = key
        self.header: dict = {"typ": "JWT"}
        if key_id:
            self.header["kid"] = str(key_id).strip()
        if cert_pem:
            try:
                cert = x509.load_pem_x509_certificate(_pem(cert_pem))
            except ValueError as exc:
                raise ConfigurationError("VECTRIXDB_OIDC_CLIENT_CERT is not a certificate in PEM: -----BEGIN CERTIFICATE----- ...") from exc
            mine = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            theirs = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            if mine != theirs:
                raise ConfigurationError("VECTRIXDB_OIDC_CLIENT_CERT is not the certificate of VECTRIXDB_OIDC_CLIENT_KEY: their public keys differ")
            ends = cert.not_valid_after_utc.timestamp()
            if ends < time.time():
                raise ConfigurationError("the certificate in VECTRIXDB_OIDC_CLIENT_CERT has expired. Register a new one with the provider")
            if ends - time.time() < 30 * 86400:
                logger.warning("the single sign-on certificate expires in %d days. Register its successor with the provider", int((ends - time.time()) // 86400))
            der = cert.public_bytes(serialization.Encoding.DER)
            self.header["x5t"] = _b64(hashlib.sha1(der).digest())  # noqa: S324 - the thumbprint Entra ID matches on, not a security hash
            self.header["x5t#S256"] = _b64(hashlib.sha256(der).digest())

    def assertion(self, client_id: str, audience: str) -> str:
        import jwt

        now = int(time.time())
        claims = {"iss": client_id, "sub": client_id, "aud": audience, "jti": secrets.token_urlsafe(16), "iat": now, "nbf": now, "exp": now + ASSERTION_SECONDS}
        return jwt.encode(claims, self.key, algorithm=self.algorithm, headers=self.header)


# ============================================================================
# PKCE, AND THE CLIENT
# ============================================================================
#
# INPUT   a provider that publishes its configuration
# OUTPUT  a verifier and its challenge; the authorization code flow with PKCE,
#         the identity token verified before a claim is read, and the groups
#         paged
#
# The browser carries a code that is useless without a verifier it never sees.


def new_verifier() -> str:
    return _b64(secrets.token_bytes(48))


def challenge_of(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


class OidcClient:
    def __init__(self, config: OidcConfig, *, transport: Optional[Transport] = None, timeout: float = 10.0) -> None:
        self.config = config
        self.timeout = timeout
        if transport is None:
            from ..extract import _urllib_transport

            transport = _urllib_transport
        self._transport: Transport = transport
        self._discovery: Optional[dict] = None
        self._jwks: Optional[dict] = None
        self._jwks_at = 0.0
        #: Somebody on the server's People list, by address, or None: set by the server, so
        #: one list says who signs in whichever way they come. Left None, only the setting's.
        self.listed: Callable[[str], Any] = lambda email: None

    # ----------------------------------------------------------- provider ---

    def _json(self, method: str, url: str, headers: Optional[dict] = None, body: bytes = b"") -> dict:
        try:
            status, _, reply = self._transport(method, url, {"Accept": "application/json", **(headers or {})}, body, self.timeout)
        except OSError as exc:
            raise SignInRefused("The identity provider could not be reached. Try again in a moment.", code="provider_unreachable") from exc
        try:
            parsed = json.loads(reply.decode("utf-8") if isinstance(reply, (bytes, bytearray)) else reply)
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        if status >= 400 or not isinstance(parsed, dict):
            # The provider's own words stay in the log. The person gets a sentence.
            raise SignInRefused("The identity provider refused the sign-in.", code=str(parsed.get("error", status)) if isinstance(parsed, dict) else str(status))
        return parsed

    def discovery(self) -> dict:
        if self._discovery is None:
            found = self._json("GET", self.config.issuer.rstrip("/") + "/.well-known/openid-configuration")
            # The document must be the one for the issuer that was asked for.
            if str(found.get("issuer", "")).rstrip("/") != self.config.issuer.rstrip("/"):
                raise SignInRefused("The identity provider described a different issuer from the one configured.", code="issuer_mismatch")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not str(found.get(key, "")).startswith(("https://", "http://localhost")):
                    raise SignInRefused("The identity provider's configuration is incomplete.", code="discovery_incomplete")
            self._discovery = found
        return self._discovery

    def _keys(self, refresh: bool = False) -> dict:
        if self._jwks is None or refresh or time.time() - self._jwks_at > 3600:
            self._jwks = self._json("GET", self.discovery()["jwks_uri"])
            self._jwks_at = time.time()
        return self._jwks

    # --------------------------------------------------------------- flow ---

    def authorization_url(self, *, redirect_uri: str, state: str, nonce: str, verifier: str, prompt: Optional[str] = None) -> str:
        """Where to send the person. ``prompt`` is ``select_account`` to let them pick another account, or ``login`` to ask again."""
        query = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.config.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge_of(verifier),
            "code_challenge_method": "S256",
        }
        if prompt in ("select_account", "login"):
            query["prompt"] = prompt
        return self.discovery()["authorization_endpoint"] + "?" + urlencode(query)

    def redeem(self, *, code: str, redirect_uri: str, verifier: str, nonce: str) -> Identity:
        """Exchange the code, verify the identity token, and decide the role."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": verifier,
        }
        endpoint = self.discovery()["token_endpoint"]
        if self.config._signer is not None:
            # Proof by signature: a fresh assertion, for this token address, minutes long.
            form["client_assertion_type"] = ASSERTION_TYPE
            form["client_assertion"] = self.config._signer.assertion(self.config.client_id, endpoint)
        elif self.config.client_secret:
            form["client_secret"] = self.config.client_secret
        tokens = self._json(
            "POST",
            endpoint,
            {"Content-Type": "application/x-www-form-urlencoded"},
            urlencode(form).encode(),
        )
        id_token = tokens.get("id_token")
        if not id_token:
            raise SignInRefused("The identity provider sent no identity token.", code="no_id_token")
        claims = self.verify(str(id_token), nonce=nonce)
        groups = self._groups(claims, tokens.get("access_token"))
        return self.identity(claims, groups)

    def verify(self, id_token: str, *, nonce: str) -> dict:
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise DependencyError("pyjwt", "signin") from exc

        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise SignInRefused("The identity token could not be read.", code="token_unreadable") from exc
        if header.get("alg") not in ALGORITHMS:
            raise SignInRefused("The identity token is signed in a way this server does not accept.", code="algorithm")
        key = self._signing_key(header.get("kid")) or self._signing_key(header.get("kid"), refresh=True)
        if key is None:
            raise SignInRefused("The identity token was signed with a key the provider does not publish.", code="unknown_key")
        try:
            claims = jwt.decode(
                id_token,
                key=jwt.PyJWK(key).key,
                algorithms=list(ALGORITHMS),
                audience=self.config.client_id,
                issuer=self.discovery()["issuer"],
                leeway=LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise SignInRefused("The identity token did not verify.", code=type(exc).__name__) from exc
        if time.time() - float(claims["iat"]) > MAX_TOKEN_AGE + LEEWAY:
            raise SignInRefused("The identity token is older than a sign-in should be.", code="token_age")
        given = str(claims.get("nonce", ""))
        if not given or not secrets.compare_digest(given, nonce):
            raise SignInRefused("The identity token belongs to a different sign-in.", code="nonce")
        if claims.get("azp") and claims["azp"] != self.config.client_id:
            raise SignInRefused("The identity token was issued to a different application.", code="azp")
        return dict(claims)

    def verify_api_token(self, token: str) -> dict:
        """The claims of an access token an app sent for the person using it.

        The same checks an identity token gets, less the ones that belong to a
        sign-in in a browser: the algorithm is on the list, the key is one the
        provider publishes, the issuer is the provider, it has not expired, and
        its audience is this server's, ``api_audience``, and never the sign-in
        client's. That last one is the point. An identity token is issued to
        the dashboard and says who signed in there; taken as an API token, any
        application the person ever signed in to with this provider could read
        their collections with it. There is no nonce, because no browser asked.
        """
        audience = self.config.api_audience
        if not audience:
            raise SignInRefused("This server does not take tokens.", code="tokens_off")
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise DependencyError("pyjwt", "signin") from exc

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise SignInRefused("The token could not be read.", code="token_unreadable") from exc
        if header.get("alg") not in ALGORITHMS:
            raise SignInRefused("The token is signed in a way this server does not accept.", code="algorithm")
        key = self._signing_key(header.get("kid")) or self._signing_key(header.get("kid"), refresh=True)
        if key is None:
            raise SignInRefused("The token was signed with a key the provider does not publish.", code="unknown_key")
        try:
            claims = jwt.decode(
                token,
                key=jwt.PyJWK(key).key,
                algorithms=list(ALGORITHMS),
                audience=audience,
                issuer=self.discovery()["issuer"],
                leeway=LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise SignInRefused("The token did not verify.", code=type(exc).__name__) from exc
        return dict(claims)

    def token_identity(self, token: str) -> Identity:
        """Who an access token is, with the role their groups give them here.

        Everything a sign-in decides is decided the same way: the role map, the
        list of addresses, the principal a policy reads. Groups that did not
        fit in the token are a refusal, since looking them up needs a token
        for the directory, and this one is for this server.
        """
        claims = self.verify_api_token(token)
        return self.identity(claims, self._groups(claims, None), signing_in=False)

    def _signing_key(self, kid: Optional[str], refresh: bool = False) -> Optional[dict]:
        keys = [k for k in self._keys(refresh).get("keys", []) if k.get("use", "sig") == "sig"]
        if kid is None:
            return keys[0] if len(keys) == 1 else None
        return next((k for k in keys if k.get("kid") == kid), None)

    # ------------------------------------------------------------- groups ---

    def _groups(self, claims: Mapping[str, Any], access_token: Optional[str]) -> list:
        name = self.config.groups_claim
        found = claims.get(name)
        if isinstance(found, str):
            return [found]
        if isinstance(found, Sequence):
            return [str(g) for g in found]
        overflowed = bool(claims.get("hasgroups")) or name in (claims.get("_claim_names") or {})
        if not overflowed:
            return []
        if not self.config.groups_url or not access_token:
            raise SignInRefused(
                "You are in more groups than fit in a sign-in token, and this server has not been told where to look them up. "
                "Ask whoever runs it to set VECTRIXDB_OIDC_GROUPS_URL.",
                code="groups_overflow",
            )
        groups: list = []
        url: Optional[str] = self.config.groups_url
        for _ in range(_MAX_GROUP_PAGES):
            if not url:
                break
            page = self._json("GET", url, {"Authorization": f"Bearer {access_token}"})
            groups.extend(str(entry["id"]) for entry in page.get("value", []) if isinstance(entry, dict) and entry.get("id"))
            url = page.get("@odata.nextLink")
            # a next page is only ever followed on the host that was configured
            if url and urlsplit(str(url))[:2] != urlsplit(self.config.groups_url)[:2]:
                break
        return groups

    @property
    def groups_alone(self) -> bool:
        """``*``: the groups decide who signs in, with no list beside them, on purpose."""
        return self.config.allowed_emails == ("*",)

    def allows(self, email: Optional[str]) -> bool:
        """Whether an address is on the setting's list: ``*`` lets in everybody the groups do."""
        if self.groups_alone:
            return True
        email = str(email or "").strip().lower()
        if "@" not in email or not self.config.allowed_emails:
            return False
        domain = "@" + email.rsplit("@", 1)[1]
        return email in self.config.allowed_emails or domain in self.config.allowed_emails

    def identity(self, claims: Mapping[str, Any], groups: Sequence[str], *, signing_in: bool = True) -> Identity:
        """Who this is, with their role. Signing in, on a list as well as in a group; with a token, the group alone.

        Signing in to the platform is for the people who run it, so the list is
        asked: the People list first, where somebody's role and grants are the
        record's and a record that is turned off is a refusal, then the
        setting's list. A token is somebody using a collection through an app,
        which the platform's list has nothing to say about: the role is
        ``token_role`` when set, their groups' otherwise, and each collection's
        policy decides what they read.
        """
        order = {role: i for i, role in enumerate(roles.ROLES)}
        held = [self.config.role_map[g] for g in groups if g in self.config.role_map]
        role = max(held, key=lambda r: order[r]) if held else self.config.default_role
        grants = roles.clean_grants(g for group in groups for g in self.config.grant_map.get(group, ()))
        email = claims.get("email") or claims.get("preferred_username")
        # An address the provider says it has not checked is on no list: anybody could have typed it.
        checked = claims.get("email_verified") is not False
        listed = False
        if not signing_in and self.config.token_role is not None:
            role = self.config.token_role
        elif role is None:
            raise SignInRefused("You signed in, but you are not in a group that has access here.", code="no_role")
        if signing_in:
            # Both locks, or none: in the security group and on a list.
            person = self.listed(str(email)) if email and checked else None
            if person is not None:
                if person.disabled:
                    raise SignInRefused("You signed in, but your access here is turned off.", code="turned_off")
                role, grants, listed = person.role, list(person.grants), True
            elif not (self.groups_alone or (checked and self.allows(email))):
                raise SignInRefused("You signed in, but your address is not on the list for this server.", code="not_listed")
        principal: dict = {}
        for attribute, claim in self.config.principal_claims.items():
            value = list(groups) if claim == self.config.groups_claim else claims.get(claim)
            if value is not None:
                principal[attribute] = value
        return Identity(
            subject=str(claims["sub"]),
            email=str(email) if email else None,
            name=str(claims["name"]) if claims.get("name") else None,
            role=role,
            groups=list(groups),
            principal=principal,
            grants=grants,
            listed=listed,
        )
