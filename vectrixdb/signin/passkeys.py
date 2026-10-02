"""Passkeys (WebAuthn): the device checks the person, and this server keeps only a public key.

What a person does is unlock their device: on a work laptop that is the PIN
they unlock Windows with, on a security key its own PIN. That PIN never leaves
the device. What reaches this server is a signature over a fresh challenge,
made by a key that is bound to this site's address, so a page on any other
address cannot ask for one. What is stored is the public half: a copy of the
sign-in file opens nothing.

Every passkey here is made with user verification required, so the device
always asks for its PIN (or whatever its owner set it to use) and a passkey
never works on a bare touch. Attestation is not asked for: this server trusts
the person who registered the key from a signed-in session or a one-time link,
not the maker of their device, so the attestation statement is not read.

The parsing is small and strict on purpose. WebAuthn's binary parts are CBOR
and COSE; only the subset they use is understood here, lengths are checked
before every read, and anything left over is a refusal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import struct
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from ..exceptions import DependencyError, VectrixError

__all__ = [
    "ALGORITHMS",
    "AuthData",
    "NewPasskey",
    "PasskeyRefused",
    "b64u",
    "cbor_decode",
    "creation_options",
    "origin_of",
    "parse_auth_data",
    "request_options",
    "rp_id_of",
    "unb64u",
    "verify_assertion",
    "verify_registration",
]

ES256, EDDSA, RS256 = -7, -8, -257


# ============================================================================
# SETTINGS: the algorithms, the timeout, and the CBOR limit
# ============================================================================
#
# The COSE algorithms accepted, how long a browser waits, and the largest CBOR
# item that is decoded.

#: What a passkey may sign with, most wanted first. Windows Hello keys are often RS256.
ALGORITHMS = (ES256, EDDSA, RS256)
TIMEOUT_MS = 120_000
_MAX_CBOR = 64 * 1024
_UP, _UV, _AT, _ED = 0x01, 0x04, 0x40, 0x80


# ============================================================================
# REFUSED
# ============================================================================
#
# INPUT   a reply that could not be accepted
# OUTPUT  the refusal, with a reason fit to show the person
#
# Every check below ends in this when it fails.


class PasskeyRefused(VectrixError):
    """A passkey reply that could not be accepted, with a reason fit to show the person."""

    def __init__(self, reason: str = "That passkey could not be checked. Try again.") -> None:
        self.reason = reason
        super().__init__(reason)


# ============================================================================
# ENCODINGS, AND THE SITE
# ============================================================================
#
# INPUT   bytes; the server's public address
# OUTPUT  base64url and back; the relying party id, the host people type; the
#         origin a browser reports
#
# A key is bound to the relying party id, so a page on any other address
# cannot ask for a signature.


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64u(text: Any) -> bytes:
    if not isinstance(text, str) or len(text) > 4 * _MAX_CBOR:
        raise PasskeyRefused()
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise PasskeyRefused() from exc


def rp_id_of(public_url: str) -> str:
    """The relying party id: the host people type, with no scheme or port."""
    host = urlsplit(public_url).hostname
    if not host:
        raise PasskeyRefused("This server's public address has no host name.")
    return host


def origin_of(public_url: str) -> str:
    """The origin a browser reports for pages on this server."""
    parts = urlsplit(public_url)
    default = {"https": 443, "http": 80}.get(parts.scheme)
    port = parts.port
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


# ============================================================================
# CBOR
# ============================================================================
#
# INPUT   the bytes of one item
# OUTPUT  the item decoded: definite lengths only, as WebAuthn uses
#
# Enough CBOR to read an attestation, and no more.


def cbor_decode(data: bytes) -> Any:
    """One CBOR item, the whole of ``data``. Definite lengths only, as WebAuthn uses."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > _MAX_CBOR:
        raise PasskeyRefused()
    value, end = _item(bytes(data), 0, 0)
    if end != len(data):
        raise PasskeyRefused()
    return value


def _need(data: bytes, at: int, size: int) -> None:
    if size < 0 or at + size > len(data):
        raise PasskeyRefused()


def _item(data: bytes, at: int, depth: int) -> tuple[Any, int]:
    if depth > 16:
        raise PasskeyRefused()
    _need(data, at, 1)
    head = data[at]
    major, info = head >> 5, head & 0x1F
    at += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        _need(data, at, size)
        arg = int.from_bytes(data[at : at + size], "big")
        at += size
    else:
        raise PasskeyRefused()  # indefinite lengths and reserved values
    if major == 0:
        return arg, at
    if major == 1:
        return -1 - arg, at
    if major in (2, 3):
        _need(data, at, arg)
        raw = data[at : at + arg]
        at += arg
        if major == 2:
            return raw, at
        try:
            return raw.decode("utf-8"), at
        except UnicodeDecodeError as exc:
            raise PasskeyRefused() from exc
    if major == 4:
        items = []
        for _ in range(arg):
            value, at = _item(data, at, depth + 1)
            items.append(value)
        return items, at
    if major == 5:
        found: dict = {}
        for _ in range(arg):
            key, at = _item(data, at, depth + 1)
            if not isinstance(key, (int, str)) or key in found:
                raise PasskeyRefused()
            found[key], at = _item(data, at, depth + 1)
        return found, at
    if major == 6:
        return _item(data, at, depth + 1)
    # major 7: simple values and floats
    if info == 20:
        return False, at
    if info == 21:
        return True, at
    if info in (22, 23):
        return None, at
    if info in (25, 26, 27):
        size = 1 << (info - 24)
        return struct.unpack({2: ">e", 4: ">f", 8: ">d"}[size], arg.to_bytes(size, "big"))[0], at
    raise PasskeyRefused()


# ============================================================================
# KEYS
# ============================================================================
#
# INPUT   a COSE key map; a signature
# OUTPUT  the algorithm and a usable public key, anything else refused; the
#         signature verified
#
# Only the public half is ever held.


def _need_cryptography() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise DependencyError("cryptography", "signin") from exc


def public_key_from_cose(cose: Any) -> tuple[int, Any]:
    """The algorithm and a usable public key, from a COSE key map. Anything else is refused."""
    _need_cryptography()
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    if not isinstance(cose, dict):
        raise PasskeyRefused()
    kty, alg = cose.get(1), cose.get(3)
    try:
        if alg == ES256 and kty == 2 and cose.get(-1) == 1:
            x, y = cose.get(-2), cose.get(-3)
            if not (
                isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32
            ):
                raise PasskeyRefused()
            numbers = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
            )
            return ES256, numbers.public_key()
        if alg == EDDSA and kty == 1 and cose.get(-1) == 6:
            x = cose.get(-2)
            if not (isinstance(x, bytes) and len(x) == 32):
                raise PasskeyRefused()
            return EDDSA, ed25519.Ed25519PublicKey.from_public_bytes(x)
        if alg == RS256 and kty == 3:
            n, e = cose.get(-1), cose.get(-2)
            if not (isinstance(n, bytes) and isinstance(e, bytes)) or len(n) * 8 < 2048:
                raise PasskeyRefused()
            return RS256, rsa.RSAPublicNumbers(
                int.from_bytes(e, "big"), int.from_bytes(n, "big")
            ).public_key()
    except ValueError as exc:  # a point not on the curve, and the like
        raise PasskeyRefused() from exc
    raise PasskeyRefused("That passkey uses a kind of key this server does not accept.")


def _verify(alg: int, key: Any, signature: bytes, data: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding

    try:
        if alg == ES256:
            key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif alg == RS256:
            key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        elif alg == EDDSA:
            key.verify(signature, data)
        else:
            return False
    except (InvalidSignature, ValueError):
        return False
    return True


# ============================================================================
# AUTHENTICATOR DATA
# ============================================================================
#
# INPUT   the raw bytes
# OUTPUT  the relying party hash, the flags, the counter and the credential,
#         parsed
#
# User verification is required, so the flag is checked, not assumed.


@dataclass
class AuthData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    credential_id: Optional[bytes] = None
    public_key: Optional[bytes] = None

    @property
    def user_present(self) -> bool:
        return bool(self.flags & _UP)

    @property
    def user_verified(self) -> bool:
        return bool(self.flags & _UV)


def parse_auth_data(raw: Any) -> AuthData:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 37 or len(raw) > _MAX_CBOR:
        raise PasskeyRefused()
    raw = bytes(raw)
    flags = raw[32]
    out = AuthData(rp_id_hash=raw[:32], flags=flags, sign_count=struct.unpack(">I", raw[33:37])[0])
    at = 37
    if flags & _AT:
        _need(raw, at, 18)
        length = struct.unpack(">H", raw[at + 16 : at + 18])[0]
        at += 18
        _need(raw, at, length)
        out.credential_id = raw[at : at + length]
        at += length
        start = at
        _, at = _item(raw, at, 0)
        out.public_key = raw[start:at]
    if flags & _ED:
        _, at = _item(raw, at, 0)
    if at != len(raw):
        raise PasskeyRefused()
    return out


# ============================================================================
# THE OPTIONS
# ============================================================================
#
# INPUT   nothing
# OUTPUT  what navigator.credentials.create and .get are given, binary values
#         as base64url; no list of keys, the device offers the ones it holds
#
# The challenge is fresh each time.


def creation_options(
    *,
    rp_id: str,
    rp_name: str,
    user_id: bytes,
    user_name: str,
    display_name: str,
    challenge: bytes,
    exclude: Sequence[bytes] = (),
) -> dict:
    """What ``navigator.credentials.create`` is given, with binary values as base64url."""
    return {
        "rp": {"id": rp_id, "name": rp_name},
        "user": {"id": b64u(user_id), "name": user_name, "displayName": display_name},
        "challenge": b64u(challenge),
        "pubKeyCredParams": [{"type": "public-key", "alg": alg} for alg in ALGORITHMS],
        "timeout": TIMEOUT_MS,
        "attestation": "none",
        "authenticatorSelection": {
            "residentKey": "required",
            "requireResidentKey": True,
            "userVerification": "required",
        },
        "excludeCredentials": [{"type": "public-key", "id": b64u(c)} for c in exclude],
    }


def request_options(*, rp_id: str, challenge: bytes, allow: Sequence[bytes] = ()) -> dict:
    """What ``navigator.credentials.get`` is given. No list of keys: the device offers the ones it holds for this site."""
    return {
        "rpId": rp_id,
        "challenge": b64u(challenge),
        "timeout": TIMEOUT_MS,
        "userVerification": "required",
        "allowCredentials": [{"type": "public-key", "id": b64u(c)} for c in allow],
    }


# ============================================================================
# VERIFICATION
# ============================================================================
#
# INPUT   a registration or an assertion from the browser
# OUTPUT  a new passkey to keep; a sign-in checked against a stored one, with
#         the new signature count to keep
#
# A counter that did not move is a cloned key, and is refused.


@dataclass
class NewPasskey:
    credential_id: bytes
    public_key: bytes
    alg: int
    sign_count: int
    transports: list = field(default_factory=list)


def _response(credential: Any) -> dict:
    if (
        not isinstance(credential, dict)
        or credential.get("type") != "public-key"
        or not isinstance(credential.get("response"), dict)
    ):
        raise PasskeyRefused()
    return credential["response"]


def _client_data(encoded: Any, kind: str, challenge: bytes, origin: str) -> bytes:
    raw = unb64u(encoded)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PasskeyRefused() from exc
    if not isinstance(data, dict) or data.get("type") != kind:
        raise PasskeyRefused()
    if not hmac.compare_digest(unb64u(data.get("challenge")), challenge):
        raise PasskeyRefused("That passkey answered a different request. Try again.")
    if data.get("origin") != origin or data.get("crossOrigin") is True:
        raise PasskeyRefused("That passkey was used from a different address.")
    return raw


def _checked(auth: AuthData, rp_id: str) -> None:
    if not hmac.compare_digest(auth.rp_id_hash, hashlib.sha256(rp_id.encode("utf-8")).digest()):
        raise PasskeyRefused("That passkey belongs to a different site.")
    if not auth.user_present or not auth.user_verified:
        raise PasskeyRefused(
            "Your device did not check that it was you. Use a passkey that asks for a PIN."
        )


def verify_registration(
    credential: Any, *, challenge: bytes, origin: str, rp_id: str
) -> NewPasskey:
    response = _response(credential)
    _client_data(response.get("clientDataJSON"), "webauthn.create", challenge, origin)
    attestation = cbor_decode(unb64u(response.get("attestationObject")))
    if (
        not isinstance(attestation, dict)
        or not isinstance(attestation.get("authData"), bytes)
        or not isinstance(attestation.get("fmt"), str)
    ):
        raise PasskeyRefused()
    auth = parse_auth_data(attestation["authData"])
    _checked(auth, rp_id)
    if not auth.credential_id or not auth.public_key:
        raise PasskeyRefused()
    if (
        credential.get("rawId") is not None
        and unb64u(credential.get("rawId")) != auth.credential_id
    ):
        raise PasskeyRefused()
    alg, _ = public_key_from_cose(cbor_decode(auth.public_key))
    transports = [
        t for t in response.get("transports") or [] if isinstance(t, str) and len(t) < 20
    ][:8]
    return NewPasskey(
        credential_id=auth.credential_id,
        public_key=auth.public_key,
        alg=alg,
        sign_count=auth.sign_count,
        transports=transports,
    )


def verify_assertion(
    credential: Any,
    *,
    challenge: bytes,
    origin: str,
    rp_id: str,
    public_key: bytes,
    sign_count: int,
) -> int:
    """Check a sign-in with a stored passkey. Returns the new signature count to keep."""
    response = _response(credential)
    client_raw = _client_data(response.get("clientDataJSON"), "webauthn.get", challenge, origin)
    auth_raw = unb64u(response.get("authenticatorData"))
    auth = parse_auth_data(auth_raw)
    _checked(auth, rp_id)
    alg, key = public_key_from_cose(cbor_decode(public_key))
    signed = auth_raw + hashlib.sha256(client_raw).digest()
    if not _verify(alg, key, unb64u(response.get("signature")), signed):
        raise PasskeyRefused()
    # A counter that did not go up means two devices hold one key: a copy.
    if (auth.sign_count or sign_count) and auth.sign_count <= sign_count:
        raise PasskeyRefused("That passkey looks like a copy of another, so it was refused.")
    return auth.sign_count
