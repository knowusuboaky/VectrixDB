"""Restore a missing filter path being indistinguishable from a null value.

``_get_nested_value`` returned None for a path that did not resolve, so
``$exists`` said a null field was absent, ``$is_null`` said an absent field
was null, and ``$is_empty`` said both were empty.
"""

from vectrixdb.core.types import MISSING, FilterCondition

_fixed = FilterCondition._get_nested_value


def _old_get_nested_value(self, data, field_path):
    value = _fixed(self, data, field_path)
    return None if value is MISSING else value


def pytest_configure(config):
    FilterCondition._get_nested_value = _old_get_nested_value
