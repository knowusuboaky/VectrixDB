"""Text as the patterns expect it, before anything is matched.

A card number typed with Cyrillic digits, an address with a zero-width space
inside it, a phone number in fullwidth digits: each walks past a pattern that
reads the text as typed. So every engine is handed the text folded first:
compatibility forms folded (NFKC, which turns fullwidth into ASCII and ligatures
into letters), invisible characters taken out, and the letters and digits that
look like Latin ones but are not, mapped to the Latin ones they imitate.

The folded text is what is masked and returned. It reads the same to a person;
it is not byte for byte what came in, and a caller that needs the original
keeps it.
"""

from __future__ import annotations

import unicodedata

__all__ = ["normalize"]


# ============================================================================
# SETTINGS: the invisible characters, and the look-alikes
# ============================================================================
#
# Zero-width characters that hide inside an identifier, and the letters and
# digits from other scripts that look like ours.

#: Characters that take no space and carry no meaning a reader would miss.
_INVISIBLE = frozenset(
    "​‌‍‎‏⁠⁡⁢⁣⁤﻿­᠎  ͏"
)

#: Letters and digits that look like Latin ones. Cyrillic and Greek mostly, the
#: ones a copy-and-paste or a deliberate evasion puts inside an identifier.
_LOOKALIKES = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ј": "j", "һ": "h", "ѕ": "s", "А": "A", "В": "B", "Е": "E",
    "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "Ѕ": "S", "І": "I", "Ј": "J", "ο": "o", "α": "a", "Α": "A",
    "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
}


# ============================================================================
# THE FOLD
# ============================================================================
#
# INPUT   a text
# OUTPUT  the text folded so every pattern sees what a reader sees
#
# A card number typed with Cyrillic digits, an address with a zero-width space
# inside it, a phone number in fullwidth digits: each reads as an identifier
# to a person and as nothing to a pattern, unless folded first.


def normalize(text: str) -> str:
    """``text`` folded so every pattern sees what a reader sees."""
    if not text:
        return text
    folded = unicodedata.normalize("NFKC", text)
    if not any(ch in _INVISIBLE or ch in _LOOKALIKES for ch in folded):
        return folded
    return "".join(_LOOKALIKES.get(ch, ch) for ch in folded if ch not in _INVISIBLE)
