"""Timestamps must be timezone-aware, and old naive data must still load.

VectrixDB <= 2.1.7 wrote naive UTC timestamps via the deprecated
``datetime.utcnow()``. Reading one of those back into an aware comparison raises
``TypeError: can't compare offset-naive and offset-aware datetimes``, so the
upgrade path matters as much as the fix.
"""

from datetime import datetime, timedelta, timezone

import pytest

from vectrixdb._time import ensure_aware, parse_iso, utcnow, utcnow_iso


class TestUtcnow:
    def test_is_timezone_aware(self):
        assert utcnow().tzinfo is not None

    def test_is_utc(self):
        assert utcnow().utcoffset() == timedelta(0)

    def test_tracks_real_time(self):
        before = datetime.now(timezone.utc)
        sampled = utcnow()
        after = datetime.now(timezone.utc)
        assert before <= sampled <= after

    def test_iso_carries_an_offset(self):
        text = utcnow_iso()
        assert text.endswith("+00:00")
        assert parse_iso(text).tzinfo is not None

    def test_comparable_with_stdlib_aware_datetimes(self):
        """The bug this replaces: naive utcnow() raised when compared to aware."""
        assert utcnow() > datetime(2020, 1, 1, tzinfo=timezone.utc)


class TestParseIso:
    def test_none_and_empty_pass_through(self):
        assert parse_iso(None) is None
        assert parse_iso("") is None

    def test_parses_offset_form(self):
        parsed = parse_iso("2026-09-08T12:00:00+00:00")
        assert parsed == datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    def test_parses_trailing_z(self):
        assert parse_iso("2026-09-08T12:00:00Z") == datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        assert parse_iso("2026-09-08T12:00:00z") is not None

    def test_naive_legacy_timestamps_are_read_as_utc(self):
        """Data written by <= 2.1.7 has no offset. It was always UTC in practice."""
        parsed = parse_iso("2026-09-08T12:00:00")
        assert parsed.tzinfo is not None
        assert parsed == datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    def test_legacy_and_current_timestamps_compare_without_raising(self):
        """The whole point: mixed-vintage rows must sort together."""
        legacy = parse_iso("2026-09-08T12:00:00")
        current = parse_iso("2026-09-08T13:00:00+00:00")
        assert legacy < current
        assert (current - legacy) == timedelta(hours=1)

    def test_offsets_other_than_utc_are_preserved_as_instants(self):
        assert parse_iso("2026-09-08T14:00:00+02:00") == datetime(
            2026, 9, 8, 12, 0, tzinfo=timezone.utc
        )

    def test_surrounding_whitespace_is_tolerated(self):
        assert parse_iso("  2026-09-08T12:00:00Z  ") is not None

    def test_malformed_input_raises(self):
        with pytest.raises(ValueError):
            parse_iso("not a timestamp")


class TestEnsureAware:
    def test_naive_becomes_utc(self):
        assert ensure_aware(datetime(2026, 1, 1)).tzinfo == timezone.utc

    def test_aware_is_left_alone(self):
        original = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5)))
        assert ensure_aware(original) is original

    def test_round_trip_through_iso_is_stable(self):
        first = utcnow()
        assert parse_iso(first.isoformat()) == first


class TestNoDeprecatedUtcnowRemains:
    def test_package_does_not_call_datetime_utcnow(self):
        """A regression guard: the deprecated call must not creep back in.

        ``datetime.utcnow()`` is removed in a future Python, and it returns a
        naive value that silently misbehaves in comparisons long before that.
        """
        import ast
        import pathlib

        import vectrixdb

        root = pathlib.Path(vectrixdb.__file__).parent
        offenders = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # Match real calls only, so prose about the deprecation is fine.
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "utcnow"
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
        assert not offenders, f"deprecated datetime.utcnow() called at: {offenders}"
