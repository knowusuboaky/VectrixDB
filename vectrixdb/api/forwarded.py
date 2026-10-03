"""Who is calling, when a gateway is in the way.

Behind APIM, an API gateway or any reverse proxy, every request arrives from
the proxy, so ``request.client.host`` is the proxy for everybody. Three things
key on the caller's address: the sign-in lockout, the guest rate limit and the
access log. Left alone, one person's wrong codes lock out everyone behind the
gateway and the whole organisation shares one guest quota.

``X-Forwarded-For`` carries the original address, and anybody can put anything
in it, so it is believed only from a peer the operator named in
``VECTRIXDB_TRUSTED_PROXIES``. With nothing named, the header is ignored and
the peer is the caller, which is what a server on its own should do.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any, Iterable, List, Optional, Sequence, Tuple

__all__ = ["ENV", "caller", "client_address", "parse_proxies", "trusted_from_env"]


# ============================================================================
# SETTINGS: the variable that names the trusted proxies
# ============================================================================
#
# Unset, no header is trusted and the peer is the caller.

#: Comma or space separated addresses and networks, as the setting takes them.
ENV = "VECTRIXDB_TRUSTED_PROXIES"


# ============================================================================
# THE CALLER'S ADDRESS
# ============================================================================
#
# INPUT   the peer, the forwarding header, and the trusted networks
# OUTPUT  the networks in the setting, as objects; the address to hold
#         responsible, from the header only when the peer is a trusted proxy
#
# Behind APIM, an API gateway or any reverse proxy every request arrives from
# the proxy, so request.client.host is the proxy for everybody, and the rate
# limit, the lockout and the access log would all key on one address. The
# header is trusted only where told.


def parse_proxies(value: Optional[str]) -> Tuple[Any, ...]:
    """The networks in the setting, as objects.

    A bare address is its own network, so ``10.0.0.7`` and ``10.0.0.0/8`` are
    written the same way. A value that is neither raises, because a proxy list
    nobody can parse would silently mean "trust nothing" and the lockout would
    go on counting the gateway.
    """
    out: List[Any] = []
    for raw in (value or "").replace(",", " ").split():
        try:
            out.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:  # noqa: PERF203 - the message names the offender
            raise ValueError(
                f"{ENV}: {raw!r} is not an address or a network, such as 10.0.0.0/8"
            ) from exc
    return tuple(out)


def trusted_from_env(env: Optional[dict] = None) -> Tuple[Any, ...]:
    """The trusted networks from the environment, empty when unset."""
    source = os.environ if env is None else env
    return parse_proxies(source.get(ENV))


def _trusted(address: str, networks: Sequence[Any]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def caller(peer: Optional[str], forwarded: Optional[str], networks: Iterable[Any]) -> Optional[str]:
    """The address to hold responsible, given the peer and the header.

    The header is a chain, oldest first, each proxy appending the address it
    saw. Trusted entries are dropped from the right; the first address that is
    not one of ours is the caller. When every hop is trusted the oldest entry
    is the caller, and when the peer is not trusted the header is ignored, so
    a caller cannot name their own address.
    """
    networks = tuple(networks)
    if not networks or peer is None or not _trusted(peer, networks):
        return peer
    chain = [part.strip() for part in (forwarded or "").split(",") if part.strip()]
    if not chain:
        return peer
    for address in reversed(chain):
        if not _trusted(address, networks):
            return address
    return chain[0]


def client_address(request: Any, networks: Optional[Iterable[Any]] = None) -> Optional[str]:
    """The caller's address for a request, trusting the header only where told."""
    peer = request.client.host if request.client else None
    if networks is None:
        networks = trusted_from_env()
    return caller(peer, request.headers.get("x-forwarded-for"), networks)
