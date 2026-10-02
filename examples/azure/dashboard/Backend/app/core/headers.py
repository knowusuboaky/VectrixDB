"""Which headers travel each way, and the one this service adds.

The rule both ways is to pass what the two ends sent and to change nothing
else: the retrieval service decides what a caller may do, and its refusals,
its cache rules and its security headers are already right. Rewriting any of
them here is how two layers come to disagree about who somebody is.

Two things are deliberate.

**The caller is passed through.** Their cookie and their Authorization header
go up untouched, so the service resolves the person, checks who may retrieve
from the collection as them, and writes their name into the audit record. A
forwarder that swapped everyone for one service key would make every
collection either refused to all or served to all as one identity.

**The key is added, not exposed.** ``UPSTREAM_KEY`` is held by this process
and put on the request here. A caller who sent their own key keeps it: theirs
may be scoped where ours is not.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

from typing import Any, Iterable, List, Tuple
from urllib.parse import urlsplit

#: Headers that describe one connection and mean nothing to the next one.
HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"trailers",
        b"transfer-encoding",
        b"upgrade",
    }
)

#: Also dropped on the way up: httpx sets the host and the length itself, and
#: a stale value for either is a request the service reads wrongly or refuses.
NOT_UPWARD = HOP_BY_HOP | {b"host", b"content-length"}


def upward(
    raw: Iterable[Tuple[bytes, bytes]], *, key: str = "", key_header: str = "api-key"
) -> List[Tuple[bytes, bytes]]:
    """The caller's headers as the retrieval service should see them, with our key when it has one."""
    sent = [(name, value) for name, value in raw if name.lower() not in NOT_UPWARD]
    header = key_header.encode("latin-1").lower()
    if key and not any(name.lower() == header for name, _ in sent):
        sent.append((header, key.encode("latin-1")))
    return sent


def downward(raw: Iterable[Tuple[bytes, bytes]]) -> List[Tuple[bytes, bytes]]:
    """The service's headers as the browser should see them.

    A list, not a mapping, because a sign-in answer sets more than one cookie
    and a mapping keeps one of them.
    """
    return [(name, value) for name, value in raw if name.lower() not in HOP_BY_HOP]


def from_another_site(headers: Any) -> bool:
    """Whether a browser says the call came from a page on another site.

    ``UPSTREAM_KEY`` goes on every call that carries no key of its own, so a
    page on any site the operator has open could post here and act with it:
    a form or a fetch with no preflight is enough. A browser names the page's
    origin on such a call; one that is not this host's is not forwarded. A
    call with no Origin, from curl or a script, is not a browser's and passes.
    """
    origin = headers.get("origin")
    if not origin:
        return False
    if origin == "null":
        return True
    ours = {headers.get("host", "")} | {
        h.strip() for h in headers.get("x-forwarded-host", "").split(",") if h.strip()
    }
    return urlsplit(origin).netloc not in ours
