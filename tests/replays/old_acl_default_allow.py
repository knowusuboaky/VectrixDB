"""Restore the ACL default that let unlabelled documents through.

``Collection.enterprise_search`` passed ``default_allow=True`` to the ACL
filter and took no argument for it, so a document with no ``_acl`` was
returned to every caller who searched with principals, while the sibling
``search_with_acl`` refused those documents by default.
"""

from vectrixdb.core.collection import Collection

_fixed_enterprise_search = Collection.enterprise_search


def _old_enterprise_search(self, *args, **kwargs):
    kwargs["default_allow"] = True
    return _fixed_enterprise_search(self, *args, **kwargs)


def pytest_configure(config):
    Collection.enterprise_search = _old_enterprise_search
