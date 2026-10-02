"""Restore collection names being used as paths unvalidated.

A name containing a separator or a parent reference wrote the collection's
files outside the database directory, and the REST create route accepted one
over the wire.
"""

import vectrixdb.core.database as database


def _old_validate(name):
    return name


def pytest_configure(config):
    database.validate_collection_name = _old_validate
