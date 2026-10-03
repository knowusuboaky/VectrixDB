"""Timezone-aware time helpers.

``datetime.utcnow()`` is deprecated from Python 3.12 and scheduled for removal.
Worse than the deprecation, it returns a *naive* datetime that claims no timezone
while actually holding UTC, which makes it compare incorrectly against local-time
values and raise ``TypeError`` against aware ones.

Everything in VectrixDB stores and compares UTC-aware datetimes. :func:`parse_iso`
accepts the naive strings written by VectrixDB <= 2.1.7 and coerces them to UTC,
so upgrading does not invalidate data already on disk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

__all__ = ["utcnow", "utcnow_iso", "parse_iso", "ensure_aware"]


# ============================================================================
# AWARE TIME
# ============================================================================
#
# INPUT   nothing, a naive datetime, or an ISO string
# OUTPUT  now as an aware UTC datetime, or as an ISO string with its offset; a
#         naive datetime with UTC attached; an ISO timestamp parsed to UTC
#
# datetime.utcnow() is deprecated and returns a naive value that claims no
# timezone while holding UTC, which compares wrongly against aware values;
# everything here is aware.


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string including the ``+00:00`` offset."""
    return utcnow().isoformat()


def ensure_aware(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; return aware datetimes unchanged.

    Naive values are assumed to be UTC, which is what every VectrixDB version has
    actually written even when the type did not say so.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into a UTC-aware datetime.

    Tolerates the three forms VectrixDB has written across versions:
    a trailing ``Z``, an explicit ``+00:00`` offset, and bare naive timestamps
    from releases <= 2.1.7. Returns ``None`` for ``None`` or an empty string so
    callers can pass optional columns straight through.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return ensure_aware(datetime.fromisoformat(text))
