"""Restore the scope rule written the open way round.

The natural way to write it is: if the path names a collection, check the
name; otherwise let it through. That is how it was written first, and it is
wrong. ``/api/v1/documents`` lists every document on the server and
``/api/v1/visibility`` names every collection, and neither carries a
collection in its path, so a key made for one collection was handed both.
So would any route added afterwards, which nobody would have noticed.
"""

from vectrixdb.api import signin


def _open_rule(path, method, scope):
    asked = signin._COLLECTION.match(path)
    if asked is None:
        return None  # no collection named, so nothing to check: the bug
    from urllib.parse import unquote

    wanted = unquote(asked.group(1))
    if wanted in scope:
        return None
    return signin._refuse(404, f"No collection named {wanted!r}")


def pytest_configure(config):
    signin.outside_the_scope = _open_rule
