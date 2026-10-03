"""Restore an empty principal value being read as "no restriction wanted".

The truthiness family, in the one place where it is a security bug rather
than a wrong answer. `enterprise_search` gated ACL filtering on
`if user_principals:`, so a user in no groups was read as a user who wanted
no access control, and saw every document.

A policy built the same way would skip any rule whose principal value is
empty, which turns a principal with no roles from "matches nothing" into
"matches everything".
"""

from vectrixdb.policy import Policy

_fixed = Policy.compile


def _old_compile(self, principal):
    clauses = []
    for rule in self.rules:
        if not rule.READS_PRINCIPAL:
            clauses.append(rule.clause(None))
            continue
        value = self._value(principal, rule)
        # The old reading: nothing supplied, so nothing to enforce.
        if not value:
            continue
        clauses.append(rule.clause(value))
    return {"$and": clauses}


def pytest_configure(config):
    Policy.compile = _old_compile
