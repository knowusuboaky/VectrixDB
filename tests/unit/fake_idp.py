"""An identity provider that lives in a test.

It does what a real one does, as far as the client can tell: publishes a
discovery document and its keys, hands out a code at the authorization
address, and at the token address swaps the code for an identity token it
signs with a real RSA key. It holds the client to PKCE, because a fake that
forgives a missing verifier would let the client stop sending one.

The knobs are the ways a provider, or somebody pretending to be one, can be
wrong: another issuer in the token, another audience, an expired token, a
token signed by a key that is not published, ``alg: none``, groups that did
not fit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.example.test/tenant"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class FakeIdp:
    def __init__(self, client_id: str = "vectrixdb", client_secret: Optional[str] = "s3cret") -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "key-1"
        self.codes: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.person: dict[str, Any] = {"sub": "u-1", "email": "ada@example.com", "name": "Ada Lovelace", "groups": ["g-admins"]}
        # ways to be wrong
        self.token_issuer = ISSUER
        self.token_audience: Any = client_id
        self.expires_in = 300
        self.issued_ago = 0
        self.sign_with_unpublished_key = False
        self.unsigned = False
        self.drop_nonce = False
        self.overflow = False
        self.graph_groups: list[str] = []
        self.graph_pages = 1
        self.down = False
        # A client that proves itself with a key instead of a secret: its public
        # key as registered with the provider, the assertions seen, and their ids.
        self.client_public_key: Any = None
        self.assertions: list[tuple[dict, dict]] = []
        self.seen_jti: set[str] = set()

    # ---------------------------------------------------------------- apps ---

    def access_token(self, audience: str = "api://vectrixdb", **claims: Any) -> str:
        """An access token an app was given for the person, for this audience.

        The same knobs make it wrong as make an identity token wrong, and
        ``claims`` overrides anything: ``exp``, ``iss``, ``groups``.
        """
        now = int(time.time()) - self.issued_ago
        body: dict[str, Any] = {
            "iss": self.token_issuer, "aud": audience, "iat": now, "exp": now + self.expires_in,
            **{k: v for k, v in self.person.items() if k != "groups"},
        }
        if self.overflow:
            body["_claim_names"] = {"groups": "src1"}
        else:
            body["groups"] = self.person.get("groups", [])
        body.update(claims)
        if self.unsigned:
            header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
            return f"{header}.{_b64(json.dumps(body).encode())}.eA"
        key = self.other_key if self.sign_with_unpublished_key else self.key
        return jwt.encode(body, key, algorithm="RS256", headers={"kid": self.kid})

    # ------------------------------------------------------------ browser ---

    def authorize(self, url: str) -> tuple[str, str]:
        """What the provider does with the browser: the person signs in there, and a code and the state come back."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ISSUER + "/authorize"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert query["response_type"] == "code" and query["client_id"] == self.client_id
        assert query["code_challenge_method"] == "S256" and query["code_challenge"]
        assert "openid" in query["scope"].split()
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"challenge": query["code_challenge"], "nonce": query.get("nonce"), "redirect_uri": query["redirect_uri"]}
        return code, query["state"]

    # ---------------------------------------------------------- transport ---

    def transport(self, method: str, url: str, headers: dict, body: bytes, timeout: float) -> tuple:
        self.calls.append((method, url))
        if self.down:
            raise OSError("connection refused")
        if url == ISSUER + "/.well-known/openid-configuration":
            return self._reply(
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/keys",
                }
            )
        if url == ISSUER + "/keys":
            public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return self._reply({"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]})
        if url == ISSUER + "/token":
            return self._token(body)
        if url.startswith("https://graph.example.test/groups"):
            assert headers.get("Authorization", "").startswith("Bearer at-")
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            size = max(1, -(-len(self.graph_groups) // self.graph_pages))
            chunk = self.graph_groups[(page - 1) * size : page * size]
            reply: dict[str, Any] = {"value": [{"id": g} for g in chunk]}
            if page < self.graph_pages:
                reply["@odata.nextLink"] = f"https://graph.example.test/groups?page={page + 1}"
            return self._reply(reply)
        return 404, {}, b"{}"

    @staticmethod
    def _reply(payload: dict, status: int = 200) -> tuple:
        return status, {"Content-Type": "application/json"}, json.dumps(payload).encode()

    def _check_assertion(self, form: dict) -> Optional[str]:
        """What a provider checks of a client that signs a JWT instead of sending a secret (RFC 7523)."""
        if "client_secret" in form:
            return "a secret and an assertion"
        if form.get("client_assertion_type") != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer":
            return "assertion type"
        assertion = form.get("client_assertion", "")
        try:
            header = jwt.get_unverified_header(assertion)
            claims = jwt.decode(
                assertion, self.client_public_key, algorithms=["RS256", "PS256", "ES256", "ES384"], audience=ISSUER + "/token",
                options={"require": ["exp", "iat", "jti", "iss", "sub", "aud"]},
            )
        except jwt.PyJWTError as exc:
            return f"assertion: {exc}"
        if claims["iss"] != self.client_id or claims["sub"] != self.client_id:
            return "issuer and subject are the client"
        if claims["exp"] - claims["iat"] > 600:
            return "an assertion lives minutes"
        if claims["jti"] in self.seen_jti:
            return "assertion replayed"
        self.seen_jti.add(claims["jti"])
        self.assertions.append((header, claims))
        return None

    def _token(self, body: bytes) -> tuple:
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        held = self.codes.pop(form.get("code", ""), None)  # a code works once
        if held is None or form.get("grant_type") != "authorization_code":
            return self._reply({"error": "invalid_grant"}, 400)
        if form.get("redirect_uri") != held["redirect_uri"] or form.get("client_id") != self.client_id:
            return self._reply({"error": "invalid_grant"}, 400)
        if self.client_public_key is not None:
            refused = self._check_assertion(form)
            if refused:
                return self._reply({"error": "invalid_client", "error_description": refused}, 401)
        elif "client_assertion" in form:
            return self._reply({"error": "invalid_client", "error_description": "no key registered"}, 401)
        elif self.client_secret is not None and form.get("client_secret") != self.client_secret:
            return self._reply({"error": "invalid_client"}, 401)
        verifier = form.get("code_verifier", "")
        if _b64(hashlib.sha256(verifier.encode()).digest()) != held["challenge"]:
            return self._reply({"error": "invalid_grant", "error_description": "PKCE"}, 400)

        now = int(time.time()) - self.issued_ago
        claims: dict[str, Any] = {
            "iss": self.token_issuer,
            "aud": self.token_audience,
            "iat": now,
            "exp": now + self.expires_in,
            **{k: v for k, v in self.person.items() if k != "groups"},
        }
        if not self.drop_nonce:
            claims["nonce"] = held["nonce"]
        if self.overflow:
            claims["_claim_names"] = {"groups": "src1"}
            claims["_claim_sources"] = {"src1": {"endpoint": "https://graph.example.test/ignored"}}
        else:
            claims["groups"] = self.person.get("groups", [])
        if self.unsigned:
            header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
            id_token = f"{header}.{_b64(json.dumps(claims).encode())}."
        else:
            key = self.other_key if self.sign_with_unpublished_key else self.key
            id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": self.kid})
        return self._reply({"id_token": id_token, "access_token": "at-" + secrets.token_urlsafe(8), "token_type": "Bearer"})
