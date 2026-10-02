"""A passkey that lives in a test.

It does what a device and the browser around it do, as far as the server can
tell. Asked to make a passkey, it makes a key pair, keeps the private half, and
hands back the public half as a COSE key inside CBOR authenticator data, with
attestation "none". Asked to sign in, it signs the authenticator data and a
hash of the client data with that key. What it returns is shaped exactly like
the JSON the dashboard's ``credentialJson`` posts: the ids and every binary
field in base64url with no padding, and nothing for a field the browser left
undefined.

The key is ES256, as most devices make; EdDSA and RS256 (which Windows Hello
often uses) are there too. ``counts=False`` is a device that keeps no signature
count and always sends zero, as synced passkeys do.

The knobs are the ways a device, or somebody pretending to be one, can be
wrong: the flags that say the person was present and verified, the origin, the
relying party, the challenge, the kind of request, the signature count, the
signature and the key that made it, the credential id and the user handle.
Each is a keyword on the call that makes the reply, so a test forges one thing
at a time and leaves the rest right.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from typing import Any, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

#: COSE algorithm numbers.
ES256, EDDSA, RS256 = -7, -8, -257
#: Authenticator data flags: the person was present, the device verified them, a new credential follows.
UP, UV, AT = 0x01, 0x04, 0x40

_SAME = object()


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ------------------------------------------------------------------- CBOR ---


def cbor(value: Any) -> bytes:
    """CBOR for maps, arrays, byte strings, text and integers, which is all WebAuthn uses.

    Maps keep the order they were written in; the ones here are written in
    the canonical order an authenticator uses.
    """
    if isinstance(value, bool):  # before int, which it also is
        return b"\xf5" if value else b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, (bytes, bytearray)):
        return _head(2, len(value)) + bytes(value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(cbor(item) for item in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(cbor(key) + cbor(item) for key, item in value.items())
    raise TypeError(f"no CBOR here for a {type(value).__name__}")


def _head(major: int, argument: int) -> bytes:
    if argument < 24:
        return bytes([major << 5 | argument])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if argument < 1 << (8 * size):
            return bytes([major << 5 | info]) + argument.to_bytes(size, "big")
    raise ValueError("too large for CBOR")


# ------------------------------------------------------------------- keys ---


def new_key(alg: int = ES256, *, rsa_bits: int = 2048) -> Any:
    """A private key of the kind a device makes for this algorithm."""
    if alg == ES256:
        return ec.generate_private_key(ec.SECP256R1())
    if alg == EDDSA:
        return ed25519.Ed25519PrivateKey.generate()
    if alg == RS256:
        return rsa.generate_private_key(public_exponent=65537, key_size=rsa_bits)
    raise ValueError(f"the fake passkey does not make {alg} keys")


def _unsigned(number: int) -> bytes:
    return number.to_bytes((number.bit_length() + 7) // 8, "big")


def cose_key(alg: int, public_key: Any) -> dict:
    """The public half as the COSE key map an authenticator writes into its data."""
    if alg == ES256:
        numbers = public_key.public_numbers()
        return {1: 2, 3: ES256, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
    if alg == EDDSA:
        return {1: 1, 3: EDDSA, -1: 6, -2: public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)}
    if alg == RS256:
        numbers = public_key.public_numbers()
        return {1: 3, 3: RS256, -1: _unsigned(numbers.n), -2: _unsigned(numbers.e)}
    raise ValueError(f"the fake passkey does not write {alg} keys")


def sign(alg: int, key: Any, data: bytes) -> bytes:
    """A signature as a device sends it: DER for ES256, raw for EdDSA, PKCS#1 v1.5 for RS256."""
    if alg == ES256:
        return key.sign(data, ec.ECDSA(hashes.SHA256()))
    if alg == EDDSA:
        return key.sign(data)
    if alg == RS256:
        return key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    raise ValueError(f"the fake passkey does not sign with {alg}")


# ----------------------------------------------------------------- device ---


class FakePasskey:
    """One passkey on one device, made on a page at ``origin``."""

    def __init__(
        self,
        origin: str,
        *,
        alg: int = ES256,
        counts: bool = True,
        credential_id: Optional[bytes] = None,
        key: Any = None,
    ) -> None:
        self.origin = origin
        self.host = urlsplit(origin).hostname or ""
        self.alg = alg
        self.key = key if key is not None else new_key(alg)
        self.credential_id = credential_id if credential_id is not None else os.urandom(16)
        self.counts = counts
        self.sign_count = 0
        #: The user id the server gave when the passkey was made, sent back as the user handle.
        self.user_handle: Optional[bytes] = None
        self.transports = ["internal", "hybrid"]

    def cose_key(self) -> dict:
        return cose_key(self.alg, self.key.public_key())

    def copy(self) -> "FakePasskey":
        """A second device holding the same key, id, handle and count: what a copied passkey is."""
        twin = FakePasskey(self.origin, alg=self.alg, counts=self.counts, credential_id=self.credential_id, key=self.key)
        twin.sign_count, twin.user_handle = self.sign_count, self.user_handle
        return twin

    # ------------------------------------------------------------- replies ---

    def create(
        self,
        options: Optional[dict] = None,
        *,
        challenge: Optional[bytes] = None,
        origin: Optional[str] = None,
        rp_id: Optional[str] = None,
        flags: int = UP | UV | AT,
        sign_count: Optional[int] = None,
        client_type: str = "webauthn.create",
        cross_origin: bool = False,
        raw_id: Optional[bytes] = None,
        transports: Optional[list] = None,
    ) -> dict:
        """What the dashboard posts after ``navigator.credentials.create`` with these options."""
        options = options or {}
        user = options.get("user") or {}
        if "id" in user:
            self.user_handle = unb64u(user["id"])
        auth = self._auth_data(rp_id if rp_id is not None else self._rp((options.get("rp") or {}).get("id")), flags, self._count(sign_count, advance=False))
        if flags & AT:
            auth += bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + cbor(self.cose_key())
        client = self._client_data(client_type, challenge, options, origin, cross_origin)
        raw = self.credential_id if raw_id is None else raw_id
        return {
            "id": b64u(raw),
            "rawId": b64u(raw),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64u(client),
                "attestationObject": b64u(cbor({"fmt": "none", "attStmt": {}, "authData": auth})),
                "transports": list(self.transports if transports is None else transports),
            },
        }

    def get(
        self,
        options: Optional[dict] = None,
        *,
        challenge: Optional[bytes] = None,
        origin: Optional[str] = None,
        rp_id: Optional[str] = None,
        flags: int = UP | UV,
        sign_count: Optional[int] = None,
        client_type: str = "webauthn.get",
        cross_origin: bool = False,
        user_handle: Any = _SAME,
        signature: Optional[bytes] = None,
        key: Any = None,
        raw_id: Optional[bytes] = None,
    ) -> dict:
        """What the dashboard posts after ``navigator.credentials.get`` with these options.

        ``key`` signs with another key than this passkey's; ``signature``
        replaces the signature outright; ``user_handle=None`` leaves it out.
        """
        options = options or {}
        auth = self._auth_data(rp_id if rp_id is not None else self._rp(options.get("rpId")), flags, self._count(sign_count, advance=True))
        client = self._client_data(client_type, challenge, options, origin, cross_origin)
        made = sign(self.alg, key if key is not None else self.key, auth + hashlib.sha256(client).digest())
        handle = self.user_handle if user_handle is _SAME else user_handle
        raw = self.credential_id if raw_id is None else raw_id
        response = {
            "clientDataJSON": b64u(client),
            "authenticatorData": b64u(auth),
            "signature": b64u(made if signature is None else signature),
        }
        if handle is not None:
            response["userHandle"] = b64u(handle)
        # An assertion has no getTransports, so the page sends an empty list.
        response["transports"] = []
        return {"id": b64u(raw), "rawId": b64u(raw), "type": "public-key", "response": response}

    # ------------------------------------------------------------- inside ---

    def _count(self, given: Optional[int], *, advance: bool) -> int:
        """The count this reply carries. A number given is where the device's counter now stands."""
        if given is not None:
            self.sign_count = given
        elif not self.counts:
            self.sign_count = 0
        elif advance:
            self.sign_count += 1
        return self.sign_count

    def _rp(self, named: Optional[str]) -> str:
        """The relying party a browser lets a page on ``origin`` name: its own host, or a domain above it."""
        if named is None:
            return self.host
        if self.host != named and not self.host.endswith("." + named):
            raise AssertionError(f"a browser on {self.origin} refuses to make or use a passkey for {named!r}")
        return named

    @staticmethod
    def _auth_data(rp_id: str, flags: int, count: int) -> bytes:
        return hashlib.sha256(rp_id.encode("utf-8")).digest() + bytes([flags]) + struct.pack(">I", count)

    def _client_data(self, kind: str, challenge: Optional[bytes], options: dict, origin: Optional[str], cross_origin: bool) -> bytes:
        if challenge is None:
            challenge = unb64u(options["challenge"])
        data = {"type": kind, "challenge": b64u(challenge), "origin": self.origin if origin is None else origin, "crossOrigin": cross_origin}
        return json.dumps(data, separators=(",", ":")).encode("utf-8")
