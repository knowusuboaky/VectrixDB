"""Restore ``$in`` and ``$nin`` testing the whole field against the operand.

``field_value in self.value`` is false for every list-valued field, so $in
matched nothing and $nin, its negation, excluded nothing.
"""

from vectrixdb.core.types import FilterCondition


def _old_is_member(self, field_value):
    try:
        return field_value in self.value
    except TypeError:
        return False


def pytest_configure(config):
    FilterCondition._is_member = _old_is_member
