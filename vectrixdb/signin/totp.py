"""Time-based one-time codes (RFC 6238) and recovery codes, from the standard library.

An authenticator app and this module agree on a secret once, at enrolment,
through a QR code. After that a six digit code is the HMAC of the current
thirty second step, and nothing travels that is worth stealing for longer
than a minute.

Three things here are easy to get subtly wrong, so they are written down:

* a code is accepted for the step before and after the current one, because
  phones drift, and no wider;
* a code is accepted once. The step it matched is remembered, and that step
  or any earlier one is refused afterwards, so a code read over a shoulder is
  already spent;
* comparison is constant time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Any, Optional
from urllib.parse import quote, urlencode

from . import qr

__all__ = [
    "DIGITS",
    "PERIOD",
    "RECOVERY_CODES",
    "WINDOW",
    "code_at",
    "new_recovery_codes",
    "new_secret",
    "normalise_recovery",
    "provisioning_uri",
    "qr_svg",
    "step_now",
    "verify",
]


# ============================================================================
# SETTINGS: the period, the digits, the window, and the recovery alphabet
# ============================================================================
#
# Thirty seconds, six digits, one step either way, and an alphabet with no
# letters that look like numbers.

PERIOD = 30
DIGITS = 6
#: Steps either side of now that a code may come from.
WINDOW = 1

_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0 O 1 I, they get misread
RECOVERY_CODES = 10


# ============================================================================
# THE CODES
# ============================================================================
#
# INPUT   a secret and the clock
# OUTPUT  a fresh secret in base32; the code for one step; the step a code
#         belongs to, or none, accepted once and within one step either way;
#         the otpauth address and its QR code; ten recovery codes shown once
#         and kept as hashes
#
# Comparison is constant time, and a code read over a shoulder is already
# spent.


def new_secret() -> str:
    """A fresh 160 bit secret, base32, the form every authenticator app takes."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def _key(secret: str) -> bytes:
    padded = secret.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    """The code for one thirty second step."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10**DIGITS)).zfill(DIGITS)


def step_now(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def verify(secret: str, code: str, *, last_step: Optional[int] = None, now: Optional[float] = None) -> Optional[int]:
    """The step this code belongs to, or None.

    ``last_step`` is the step of the last code this person used. A match at or
    before it is a replay and is refused. The caller stores what comes back.
    """
    given = "".join(ch for ch in str(code) if ch.isdigit())
    if len(given) != DIGITS:
        return None
    current = step_now(now)
    matched: Optional[int] = None
    # Every candidate is compared, match or not, so the time taken says nothing.
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, step), given) and matched is None:
            matched = step
    if matched is None:
        return None
    if last_step is not None and matched <= last_step:
        return None
    return matched


def provisioning_uri(secret: str, account: str, issuer: str = "VectrixDB") -> str:
    """The ``otpauth://`` address a QR code carries to an authenticator app.

    The algorithm, the digits and the period are left out. Without them every
    app assumes SHA1, six digits and thirty seconds, which is what these codes
    are, and each one written would make the code bigger and its dots smaller.
    """
    label = quote(f"{issuer}:{account}", safe="")
    query = urlencode({"secret": secret, "issuer": issuer})
    return f"otpauth://totp/{label}?{query}"


def qr_svg(text: str, **look: Any) -> Optional[str]:
    """The text as a QR code in SVG, or None when no QR library is installed.

    Drawn by :func:`vectrixdb.signin.qr.svg`, which takes the dashboard's look
    as keywords: its ink and tile, and its logo or mark for the middle.
    """
    return qr.svg(text, **look)


def new_recovery_codes(count: int = RECOVERY_CODES) -> list[str]:
    """Ten codes of the form ABCD-EFGH-JKLM, shown once and stored only as hashes."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def normalise_recovery(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())
