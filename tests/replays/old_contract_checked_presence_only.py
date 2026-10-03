"""Restore a metadata contract that only looked for absent fields.

The check caught a document with no classification rank and passed one whose
rank was the string "high". Both are invisible to every principal for ever,
for the same reason: the rule is false whatever the principal holds. The
difference is only that the second one looks fine, which makes it the worse
of the two, and a schema validator will never catch it either because "high"
is a perfectly good string.

Under this replay nothing is undecidable, so only the missing half is found.
"""

from vectrixdb.policy import Policy

_fixed = Policy.undecidable_fields


def _old_undecidable_fields(self, metadata):
    return []


def pytest_configure(config):
    Policy.undecidable_fields = _old_undecidable_fields
