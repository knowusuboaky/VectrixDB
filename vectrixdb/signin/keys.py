"""Where secrets come from, and the keys made from the sign-in secret.

**From a file as well as the environment.** Every secret setting has a twin
ending ``_FILE`` that names a file to read it from:
``VECTRIXDB_SIGNIN_SECRET_FILE`` beside ``VECTRIXDB_SIGNIN_SECRET``, and the
same for the identity provider's client secret, the mail server's address and
the API keys. An environment variable shows up in ``docker inspect``, an ECS
task definition and a crash report. A file mounted from a secrets manager does
not. Setting both is refused, because nobody can say which one was meant.

**One key per job.** The sign-in secret is never used as a key itself. Each
job gets its own, derived from it with HKDF (RFC 5869, SHA-256): one signs
cookies, one seals authenticator secrets. A value made for one job is then
useless for the other, and a secret can be rotated for all of them at once.

**An API key can be given as its hash.** Callers present an API key, so the
server only has to recognise it, and ``VECTRIXDB_API_KEY_SHA256`` holds the
SHA-256 of the key instead of the key. A key is long and random, so a fast
hash is the right one: there is nothing to guess.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Mapping, Optional

from ..exceptions import ConfigurationError

__all__ = [
    "derive",
    "env_secret",
    "hash_key",
    "key_matches",
]


# ============================================================================
# SETTINGS: the salt
# ============================================================================
#
# One fixed salt for the derivation; the purpose is what tells the keys apart.

_SALT = b"vectrixdb sign-in"


# ============================================================================
# SECRETS, AND THE KEYS MADE FROM THEM
# ============================================================================
#
# INPUT   the environment, or a file a _FILE twin names; the sign-in secret
#         and a purpose
# OUTPUT  the secret; one key per job, by HKDF with SHA-256; an API key's
#         SHA-256; whether a key matches, given as itself or as its hash
#
# The sign-in secret is never used as a key itself. Setting both a variable
# and its _FILE twin is refused, because nobody can say which one was meant.


def env_secret(env: Mapping[str, str], name: str) -> Optional[str]:
    """``name`` from the environment, or read from the file ``name_FILE`` names. Neither: None."""
    inline = str(env.get(name, "") or "").strip()
    file_name = str(env.get(name + "_FILE", "") or "").strip()
    if inline and file_name:
        raise ConfigurationError(
            f"{name} and {name}_FILE are both set. Keep one, so there is no doubt which is meant"
        )
    if not file_name:
        return inline or None
    try:
        value = Path(file_name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(
            f"{name}_FILE names {file_name}, which cannot be read: {exc}"
        ) from exc
    if not value:
        raise ConfigurationError(f"{name}_FILE names {file_name}, which is empty")
    return value


def derive(secret: str, purpose: str, length: int = 32) -> bytes:
    """A key for one job, from the sign-in secret. HKDF with SHA-256, the purpose as its info."""
    prk = hmac.new(_SALT, secret.encode("utf-8"), hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    info = b"vectrixdb|" + purpose.encode("utf-8")
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def hash_key(key: str) -> str:
    """The SHA-256 of an API key, as hex: what ``VECTRIXDB_API_KEY_SHA256`` holds."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_matches(given: Optional[str], plain: Optional[str], hashed: Optional[str]) -> bool:
    """Whether the key a caller sent is the configured one, given as itself or as its hash."""
    if not given:
        return False
    if plain and hmac.compare_digest(given.encode("utf-8"), plain.encode("utf-8")):
        return True
    if hashed:
        return hmac.compare_digest(hash_key(given), hashed.strip().lower())
    return False
