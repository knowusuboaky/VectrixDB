"""The engine that is always there: identifiers found by their shape.

Emails, phone numbers and card numbers are masked where they stand, so a
sentence still reads: an address keeps its first letter and its domain, a
number its last four digits. The rest, a social security or insurance number,
an IBAN, an IP address, a key, a password inside a connection string, an
internal host name, is replaced whole by its type in brackets, ``[SSN]``,
because there is nothing in it a reader should be left.

Each type carries a weight, the harm of one leaking, and a text's risk score
is the heaviest thing found in it, nudged up when there is more than one.
Names, addresses, passports and licences have no shape a pattern can trust,
so they are the other engines' work; this one never guesses at them.
"""

from __future__ import annotations

import re
import dataclasses
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_TYPES",
    "IN_PLACE",
    "MASK",
    "TYPES",
    "WEIGHTS",
    "Found",
    "RegexEngine",
    "apply",
    "score_of",
]


# ============================================================================
# SETTINGS: the mask, the types, the defaults, the weights, and what is masked in place
# ============================================================================
#
# The stand-in text, every type the patterns know, the ones masked unless told
# otherwise, how much each weighs in the score, and the three masked where
# they stand rather than replaced.

#: What stands in for a hidden character.
MASK = "•"

#: Every type an engine may report, in the words the record and the dashboard use.
TYPES: Tuple[str, ...] = (
    "name",
    "email",
    "phone",
    "credit_card",
    "ssn",
    "sin",
    "iban",
    "bank_account",
    "passport",
    "drivers_license",
    "ip_address",
    "address",
    "date_of_birth",
    "date",
    "api_key",
    "connection_string",
    "internal_host",
)
#: What a collection masks when it says "masked" and names no types: identifiers, not people's names.
DEFAULT_TYPES: Tuple[str, ...] = (
    "email",
    "phone",
    "credit_card",
    "ssn",
    "sin",
    "iban",
    "bank_account",
    "api_key",
    "connection_string",
)
#: The harm of one of each leaking, 0 to 1.
WEIGHTS: Dict[str, float] = {
    "name": 0.5,
    "email": 0.6,
    "phone": 0.5,
    "credit_card": 0.9,
    "ssn": 0.95,
    "sin": 0.95,
    "iban": 0.85,
    "bank_account": 0.85,
    "passport": 0.8,
    "drivers_license": 0.7,
    "ip_address": 0.3,
    "address": 0.6,
    "date_of_birth": 0.5,
    "date": 0.1,
    "api_key": 0.95,
    "connection_string": 0.95,
    "internal_host": 0.4,
}
#: Masked where they stand, keeping enough to recognise; everything else is replaced by its type.
IN_PLACE = frozenset({"email", "phone", "credit_card"})


# ============================================================================
# ONE FIND
# ============================================================================
#
# INPUT   a match
# OUTPUT  what it is, where it is, and who found it
#
# The same shape every engine answers in.


@dataclass(frozen=True)
class Found:
    """One identifier in a text: what it is, where it is, and who found it."""

    type: str
    start: int
    end: int
    engine: str = "regex"
    score: float = 1.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "type": self.type,
            "start": self.start,
            "end": self.end,
            "engine": self.engine,
            "score": round(self.score, 3),
        }


# ============================================================================
# THE SHAPES
# ============================================================================
#
# INPUT   the text
# OUTPUT  the pattern for each type, with a Luhn check for cards and SINs and
#         mod 97 for IBANs, and the last four digits kept for cards and phones
#
# The address-password pattern runs before the email one, or a URL's user
# would read as an address.
# An address: the first character of its local part is kept, and the domain,
# which says whose address it is without saying whose mailbox.

_EMAIL = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b"
)
# Thirteen to nineteen digits, spaced or dashed, that pass the Luhn check.
_CARD = re.compile(r"(?<![\w:])(?:\d[ -]?){12,18}\d(?![\w:])")
# Nine to fifteen digits with the separators phone numbers are written with.
# A colon on either side is a time, so it ends a match rather than joining one.
_PHONE = re.compile(r"(?<![\w:])\+?\(?\d[\d ().-]{6,}\d(?![\w:])")
# A US social security number, dashed: the shape people write it in and nothing else has.
_SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
# A Canadian social insurance number, three groups of three, that passes the Luhn check.
_SIN = re.compile(r"(?<![\w-])\d{3}[ -]\d{3}[ -]\d{3}(?![\w-])")
# An IBAN: country, two check digits, then up to thirty letters and digits, checked mod 97.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_IP = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
# Keys by their prefixes, then any "key = value" that names itself a secret.
_KEY_SHAPES = re.compile(
    r"(?<![A-Za-z0-9_-])(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})(?![A-Za-z0-9_-])"
)
_KEY_NAMED = re.compile(
    r"(?i)\b(?:api[_ -]?key|secret(?:[_ -]?key)?|access[_ -]?token|auth[_ -]?token|token|password|passwd|pwd)\s*[:=]\s*['\"]?([^\s'\",;]{8,})"
)
# A password inside a connection string or a database address.
_CONNECTION = re.compile(
    r"(?i)\b(?:AccountKey|SharedAccessSignature|sig|Password|Pwd)=([^;\s]{8,})"
)
_URL_PASSWORD = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s@/]+)@")
# Hosts nobody outside the company can reach, which a reader outside it should not be told of.
_INTERNAL_HOST = re.compile(
    r"(?i)\b(?:[a-z0-9-]+\.)+(?:internal|corp|intranet|lan|local)\b|\bintranet(?:\.[a-z0-9-]+)+\b"
)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    compact = value.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    moved = compact[4:] + compact[:4]
    number = "".join(str(int(ch, 36)) for ch in moved)
    return int(number) % 97 == 1


def _keep_last_four(text: str) -> str:
    """Every digit hidden but the last four, the separators left where they were."""
    digits = sum(ch.isdigit() for ch in text)
    shown, out = 0, []
    for ch in text:
        if ch.isdigit():
            shown += 1
            out.append(ch if shown > digits - 4 else MASK)
        else:
            out.append(ch)
    return "".join(out)


def _is_card(found: str) -> bool:
    digits = re.sub(r"\D", "", found)
    return 13 <= len(digits) <= 19 and _luhn(digits)


def _is_phone(found: str) -> bool:
    digits = re.sub(r"\D", "", found)
    if not 9 <= len(digits) <= 15:
        return False
    # A single dot and nothing else is a decimal number, not a phone.
    if found.count(".") == 1 and not re.search(r"[ ()+-]", found):
        return False
    # A date leads with a four-digit year and a two-digit month.
    return not re.match(r"\d{4}[-./]\d{2}\b", found)


# ============================================================================
# THE ENGINE, THE MASKING, AND THE SCORE
# ============================================================================
#
# INPUT   a text and the types wanted; what was found
# OUTPUT  identifiers by their shape, always available, no model, no service;
#         the text with each find masked, overlaps resolved in favour of the
#         earliest and longest; the risk, the heaviest type nudged up a tenth
#         for each further find, to at most one
#
# The engine that is always there.


class RegexEngine:
    """Identifiers by their shape. Always available, no model, no service."""

    name = "regex"
    #: None: any language, since a shape has none.
    languages: Optional[Tuple[str, ...]] = None

    def covers(self, language: Optional[str]) -> bool:
        return True

    def find(self, text: str, types: Iterable[str], language: Optional[str] = None) -> List[Found]:
        wanted = set(types)
        out: List[Found] = []
        taken: List[Tuple[int, int]] = []

        def take(kind: str, start: int, end: int) -> None:
            if kind in wanted and not any(s < end and start < e for s, e in taken):
                taken.append((start, end))
                out.append(Found(kind, start, end, self.name))

        # Cards first: a card number is long enough to look like a phone number.
        for m in _CARD.finditer(text):
            if _is_card(m.group(0)):
                take("credit_card", m.start(), m.end())
        for m in _SSN.finditer(text):
            take("ssn", m.start(), m.end())
        for m in _SIN.finditer(text):
            if _luhn(re.sub(r"\D", "", m.group(0))):
                take("sin", m.start(), m.end())
        for m in _IBAN.finditer(text):
            if _iban_ok(m.group(0)):
                take("iban", m.start(), m.end())
        for m in _PHONE.finditer(text):
            if _is_phone(m.group(0)):
                take("phone", m.start(), m.end())
        for m in _IP.finditer(text):
            if all(int(part) <= 255 for part in m.group(0).split(".")):
                take("ip_address", m.start(), m.end())
        for m in _KEY_SHAPES.finditer(text):
            take("api_key", m.start(), m.end())
        for m in _KEY_NAMED.finditer(text):
            take("api_key", m.start(1), m.end(1))
        for m in _CONNECTION.finditer(text):
            take("connection_string", m.start(1), m.end(1))
        for m in _URL_PASSWORD.finditer(text):
            take("connection_string", m.start(1), m.end(1))
        for m in _EMAIL.finditer(text):
            take("email", m.start(), m.end())
        for m in _INTERNAL_HOST.finditer(text):
            take("internal_host", m.start(), m.end())
        return sorted(out, key=lambda f: (f.start, -f.end))


def _stand_in(kind: str, piece: str) -> str:
    if kind == "email":
        m = _EMAIL.match(piece)
        return f"{m.group(1)}{MASK * 3}@{m.group(2)}" if m else f"[{kind.upper()}]"
    if kind in ("phone", "credit_card"):
        return _keep_last_four(piece)
    return f"[{kind.upper()}]"


def apply(text: str, found: Sequence[Found]) -> str:
    """``text`` with each thing found masked. Spans that overlap are masked as
    one, under the type of the earliest and longest: an engine's span that
    ran past a pattern's used to be dropped whole, and its tail stayed in
    the clear."""
    kept: List[Found] = []
    for item in sorted(found, key=lambda f: (f.start, -f.end)):
        if kept and item.start < kept[-1].end:
            if item.end > kept[-1].end:
                kept[-1] = dataclasses.replace(kept[-1], end=item.end)
            continue
        kept.append(item)
    out, at = [], 0
    for item in kept:
        out.append(text[at : item.start])
        out.append(_stand_in(item.type, text[item.start : item.end]))
        at = item.end
    out.append(text[at:])
    return "".join(out)


def score_of(found: Sequence[Found]) -> float:
    """The risk of what was found: the heaviest type, nudged up by a tenth for each further find, to at most one."""
    if not found:
        return 0.0
    heaviest = max(WEIGHTS.get(f.type, 0.5) for f in found)
    return round(min(heaviest + 0.1 * (len(found) - 1), 1.0), 2)
