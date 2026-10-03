"""Restore ACL filtering being skipped for an empty principal list.

``enterprise_search`` guarded with ``if user_principals:``, so a user in no
groups was treated as a caller who wanted no ACL filtering at all and saw
every document.
"""

from vectrixdb.core.collection import Collection

_fixed = Collection.enterprise_search


def _old_enterprise_search(self, query, *args, **kwargs):
    if kwargs.get("user_principals") == []:
        kwargs["user_principals"] = None
    return _fixed(self, query, *args, **kwargs)


def pytest_configure(config):
    Collection.enterprise_search = _old_enterprise_search
