"""Restore the caller's address being whoever the server's peer was.

Behind APIM, an API gateway or any reverse proxy that is the gateway for
every request, so the sign-in lockout counted everyone's failures together,
the guest rate limit was one quota for the whole organisation, and the access
log named the gateway for every read.
"""

from vectrixdb.api import signin


def _old_address(request):
    return request.client.host if request.client else None


def pytest_configure(config):
    signin._address = _old_address
