"""Restore a document with no entitlement metadata being visible to everyone.

`enterprise_search` treated a document with no `_acl` as unrestricted, so a
chunk that missed its stamp at ingestion was readable by every caller. The
same shape, one level down: if an absent field is read as "no value to test,
so nothing to fail", a policy naming that field stops applying to exactly the
documents whose metadata is broken.

This is the half of fail-closed that the filter engine provides rather than
the policy module, which is why it is worth holding down from here.
"""

from vectrixdb.core.types import MISSING, FilterCondition

_fixed = FilterCondition.matches


def _old_matches(self, metadata):
    field_value = self._get_nested_value(metadata, self.field)
    if field_value is MISSING and self.operator not in ("exists", "is_null", "is_empty"):
        # The old reading: nothing there to contradict the filter.
        return True
    return _fixed(self, metadata)


def pytest_configure(config):
    FilterCondition.matches = _old_matches
