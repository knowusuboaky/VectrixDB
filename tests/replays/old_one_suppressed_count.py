"""Restore a single "suppressed" count covering both kinds of withholding.

The obvious shape, and the one that quietly defeats an ethical wall. A record
that reports seven results suppressed does not say whether the principal was
inside the scope and lost documents to a clearance rule, or outside it and
must not learn the documents are there at all. A caller handed one number
will render it, and the first time somebody walled off a client reads "7
results withheld" the wall has confirmed the client is a client.

Under this replay both counts collapse into the disclosable one, which is what
a caller would surface.
"""

from vectrixdb.policy import Policy

_fixed = Policy.classify


def _old_classify(self, principal, items, metadata_of=None):
    get = metadata_of or (lambda item: item)
    allowed = []
    suppressed = 0
    denied = set()
    for item in items:
        decision = self.decide(principal, get(item) or {})
        if decision.allowed:
            allowed.append(item)
        else:
            # One bucket, no distinction. The scope flag exists precisely
            # because this cannot be worked out after the fact.
            suppressed += 1
        denied.update(decision.failed)
    return allowed, suppressed, 0, tuple(sorted(denied))


def pytest_configure(config):
    Policy.classify = _old_classify
