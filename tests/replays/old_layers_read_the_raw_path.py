"""Restore the layers around the routes reading the path as it arrived.

Behind a gateway that serves the app under a path, that path carries the
prefix. The role table could not place it, and a route nothing places is
admin only, so every operator, viewer and guest was refused; the guest rules
and the masking layer missed for the same reason, and identifiers went out
unmasked. None of it failed loudly.
"""

from vectrixdb.api import rootpath


def _raw_path(request):
    return request.url.path


def pytest_configure(config):
    rootpath.route_path = _raw_path
    import vectrixdb.api.masked as masked
    import vectrixdb.api.signin as signin

    masked.route_path = _raw_path
    signin.route_path = _raw_path
