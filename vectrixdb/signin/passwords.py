"""Passwords, for a server that turns them on, and never on their own.

Off unless ``VECTRIXDB_SIGNIN_PASSWORDS=on``. When on, the email way in asks
for the password and the code from the authenticator app together, on one
screen, and checks both before saying anything. A guesser therefore cannot
learn that a password was right without also having the code, and a password
that leaked from somewhere else opens nothing by itself.

Stored as scrypt, which Python has built in: slow on purpose, salted, and
compared in constant time. The rules are the ones current guidance settled
on: twelve characters or more, no rules about symbols, no forced changes,
and the passwords everybody tries refused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Optional

__all__ = [
    "MIN_LENGTH",
    "check",
    "hash_password",
    "problem_with",
]


# ============================================================================
# SETTINGS: the lengths, the passwords everybody tries, and scrypt's memory
# ============================================================================
#
# Twelve characters or more, no rules about symbols, and the passwords every
# guesser tries first, refused.

MIN_LENGTH = 12
MAX_LENGTH = 256
#: scrypt's cost. 2**15 takes about a tenth of a second and 32 MB, per try.
_N, _R, _P = 2**15, 8, 1

#: Long enough to pass the length rule and still the first thing anybody tries.
_COMMON = frozenset(
    {
        "password1234",
        "password12345",
        "password123456",
        "passw0rd1234",
        "p@ssw0rd1234",
        "p@ssword1234",
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123456789abc",
        "1234567890ab",
        "123123123123",
        "qwertyuiop12",
        "qwertyuiopas",
        "qwerty123456",
        "1qaz2wsx3edc",
        "1q2w3e4r5t6y",
        "zaq12wsxcde3",
        "iloveyou1234",
        "letmein12345",
        "welcome12345",
        "welcome123456",
        "changeme1234",
        "administrator",
        "admin1234567",
        "adminadmin12",
        "trustno12345",
        "monkey123456",
        "dragon123456",
        "football1234",
        "baseball1234",
        "superman1234",
        "sunshine1234",
        "princess1234",
        "abcdefghijkl",
        "abcd12345678",
        "abc123456789",
        "aaaaaaaaaaaa",
        "111111111111",
        "000000000000",
        "passwordpassword",
        "correcthorsebatterystaple",
        "vectrixdb123",
        "vectrixdb1234",
        "summer202412",
        "winter202412",
        "spring202412",
        "autumn202412",
    }
)


_MAXMEM = 64 * 1024 * 1024


# ============================================================================
# HASH, CHECK, AND THE RULES
# ============================================================================
#
# INPUT   a password
# OUTPUT  a salted scrypt hash, packed; whether a password is the one stored,
#         always doing the work so the time taken says nothing; what is wrong
#         with a new one, in a sentence, or nothing
#
# Only on a server that turned passwords on, and never on their own.


def _pack(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unpack(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32, maxmem=_MAXMEM
    )
    return f"scrypt${_N}${_R}${_P}${_pack(salt)}${_pack(digest)}"


def check(password: Optional[str], stored: Optional[str]) -> bool:
    """Whether this password is the one stored. Always does the work, so the time taken says nothing."""
    if not stored or not stored.startswith("scrypt$"):
        # Nothing stored: hash anyway, so a missing password costs what a wrong one does.
        hashlib.scrypt(b"x", salt=b"0" * 16, n=_N, r=_R, p=_P, dklen=32, maxmem=_MAXMEM)
        return False
    try:
        _, n, r, p, salt, digest = stored.split("$")
        given = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=_unpack(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(given, _unpack(digest)) and bool(password)


def problem_with(password: str, email: Optional[str] = None) -> Optional[str]:
    """What is wrong with a new password, in a sentence, or None."""
    password = str(password or "")
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters. A few ordinary words together are easy to remember and hard to guess."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    folded = password.lower().replace(" ", "")
    if folded in _COMMON or len(set(folded)) <= 2:
        return "That password is one of the first anybody tries. Choose another."
    local = (email or "").split("@")[0].lower()
    if len(local) >= 4 and local in folded:
        return "Leave your email address out of your password."
    if (
        folded in "01234567890123456789"
        or folded in "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz"
    ):
        return "That password is a run of letters or numbers. Choose another."
    return None
