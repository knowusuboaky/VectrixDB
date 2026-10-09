"""Fetching an address somebody else chose, the way a security review asks it to be done.

    fetcher = Fetcher()                                   # any public host
    fetcher = Fetcher(allowed_hosts=["*.example.com"])    # these hosts and no others
    reply = fetcher.get("https://www.example.com/feed.xml")
    reply.status, reply.header("content-type"), reply.body

A source is an address, and an address can be anything: a page on the
public web, or the cloud's metadata service at 169.254.169.254, an admin
page on localhost, a database in the private network the host sits in. So
before a byte is sent, and again at every redirect:

* only ``http`` and ``https``, and never an address with a name and a
  password in it;
* the host is resolved once, and every address it resolves to must be
  public. Refused: loopback, the private ranges, link-local (where cloud
  metadata services answer), carrier-grade NAT, multicast, reserved,
  unspecified and documentation addresses, IPv6's unique-local, link-local
  and site-local ranges, IPv4 written as IPv6, and Azure's platform address
  168.63.129.16, public as it is;
* the connection is made to the address that was checked, with the host's
  name kept for TLS and the ``Host`` header, so a name that resolves to
  somewhere else a second time (DNS rebinding) never gets the chance;
* a redirect is checked like the first address, a redirect from https to
  plain http is refused, and five is the most followed;
* a body larger than the cap is refused at the cap, a compressed one is
  refused at the cap once unpacked, and a fetch has a time limit for the
  whole of it, redirects included, which holds against a server that
  answers a byte at a time.

``internal_hosts`` names the intranet hosts that may resolve to private
addresses, and nothing else may. Link-local, multicast and the rest stay
refused even for them, and so do the metadata services inside the ranges
they may have, Alibaba Cloud's at 100.100.100.200 and AWS's IPv6 one at
fd00:ec2::254: no feed lives at a metadata service.

Behind a proxy, ``HTTPS_PROXY`` and ``HTTP_PROXY`` with ``NO_PROXY`` as
every other tool reads them, the proxy makes the connection and resolves
the name itself. The addresses are still resolved and checked here first,
so an address that is private by its own DNS is refused before the proxy is
asked, and a host this server cannot look up is refused too. A name that
the proxy then finds somewhere else, DNS rebinding again, reaches nothing
over https, since nowhere else holds the certificate for the name; plain
http could not tell, so plain http is refused through a proxy, and fetched
directly for a host ``NO_PROXY`` names. Keep the proxy's own rules against
internal addresses all the same.

A request's own headers, a key or a cookie a source of your own sends, go
no further than the site they were sent to: a redirect to another site
takes only ``Accept`` and ``User-Agent`` with it, as a browser does.

And politely: robots.txt is read once per host and obeyed, by the rules of
RFC 9309 (the longest matching rule wins, ``*`` and ``$`` are understood,
a robots.txt that answers 4xx allows everything and one that cannot be read
allows nothing for now). Requests to one host are spaced by a second, or by
the robots.txt ``Crawl-delay`` when that is longer, and a host that answers
429 or 503 with ``Retry-After`` is left alone until then. Every request says
who it is: ``VectrixDB/<version> (+https://github.com/knowusuboaky/VectrixDB)``.
"""

from __future__ import annotations

import base64
import contextlib
import email.utils
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from ._web import bot_check, bot_check_message, redact_message, redact_url, site_of
from .exceptions import ExtractionError

__all__ = [
    "Deferred",
    "Fetcher",
    "Refused",
    "Reply",
    "Request",
    "host_matches",
    "user_agent",
    "why_not_public",
]


# ============================================================================
# SETTINGS: the limits, the ranges refused, and who is asking
# ============================================================================
#
# How much, how long and how often; the address ranges a fetch may not
# connect to, and which of them an intranet host may; the product token
# robots.txt is read for.

#: Redirects followed, at most. RFC 9309 asks at least five of a robots.txt, and no page needs more.
MAX_REDIRECTS = 5
#: The most one reply may weigh. A podcast's sound is fetched with a cap of its own.
MAX_BYTES = 20 * 1024 * 1024
#: Seconds one fetch may take, redirects and all.
TIMEOUT = 30.0
#: Seconds between two requests to one host, at the least.
MIN_INTERVAL = 1.0
#: The longest wait for a host this fetcher sits through. A host that asks for
#: more is left for the next refresh, rather than holding this one up.
MAX_WAIT = 60.0
#: The longest a site may put its sources off, by Retry-After or Crawl-delay:
#: a week. One that asks for more is asked again then.
MAX_DEFER = 7 * 24 * 3600.0
#: How long a robots.txt is believed. RFC 9309 asks no more than a day.
ROBOTS_FOR = 24 * 3600.0
#: How much of a robots.txt is read. RFC 9309 asks at least 500 KiB.
ROBOTS_MAX_BYTES = 512 * 1024
#: The name robots.txt is read for, in lower case, as RFC 9309 matches it.
PRODUCT = "vectrixdb"
HOME = "https://github.com/knowusuboaky/VectrixDB"

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
#: The headers that go on with a redirect to another site. Anything else a
#: caller sent, a key, a cookie, a token, was meant for the first site only.
_TO_ANY_SITE = frozenset({"user-agent", "accept", "accept-encoding", "accept-language"})
#: Every character a request line may carry as it is: printable ASCII, a
#: percent sign among it. A space or a letter outside ASCII is escaped.
_AS_IS = "".join(chr(c) for c in range(0x21, 0x7F))
_net = ipaddress.ip_network
# (range, why, whether a host named in internal_hosts may resolve to it)
_V4: Tuple[Tuple[Any, str, bool], ...] = (
    (_net("0.0.0.0/8"), "an unspecified address", False),
    (_net("10.0.0.0/8"), "a private address", True),
    # Inside ranges an intranet host may have, so named before them.
    (_net("100.100.100.200/32"), "Alibaba Cloud's metadata service", False),
    (_net("100.64.0.0/10"), "a carrier-grade NAT address", True),
    (_net("127.0.0.0/8"), "a loopback address", True),
    (_net("169.254.0.0/16"), "a link-local address, where cloud metadata services answer", False),
    (_net("172.16.0.0/12"), "a private address", True),
    (_net("192.0.0.0/24"), "a reserved address", False),
    (_net("192.0.2.0/24"), "a documentation address", False),
    (_net("192.88.99.0/24"), "a reserved address", False),
    (_net("192.168.0.0/16"), "a private address", True),
    (_net("198.18.0.0/15"), "a benchmarking address", False),
    (_net("198.51.100.0/24"), "a documentation address", False),
    (_net("203.0.113.0/24"), "a documentation address", False),
    (_net("224.0.0.0/4"), "a multicast address", False),
    (_net("240.0.0.0/4"), "a reserved address", False),
    # A public address, but Azure's own on every one of its machines: its
    # host agent and the WireServer answer there.
    (_net("168.63.129.16/32"), "Azure's platform address, where its host agent answers", False),
)
_V6: Tuple[Tuple[Any, str, bool], ...] = (
    (_net("::/128"), "an unspecified address", False),
    (_net("::1/128"), "a loopback address", True),
    (_net("::ffff:0:0/96"), "an IPv4 address written as IPv6", False),
    (_net("::/96"), "an IPv4 address written as IPv6", False),
    (_net("64:ff9b:1::/48"), "a local NAT64 address", True),
    (_net("100::/64"), "a discard address", False),
    (_net("2001::/32"), "a Teredo tunnel address", False),
    (_net("2001:db8::/32"), "a documentation address", False),
    (_net("fd00:ec2::254/128"), "AWS's metadata service over IPv6", False),
    (_net("fc00::/7"), "a unique local address, IPv6's private range", True),
    (_net("fe80::/10"), "a link-local address", False),
    (_net("fec0::/10"), "a site-local address", True),
    (_net("ff00::/8"), "a multicast address", False),
    # The rest of ::/8, IPv4-translated addresses among it.
    (_net("::/8"), "a reserved address", False),
)
#: IPv6 ranges that carry an IPv4 address inside them, judged by that address.
_NAT64 = _net("64:ff9b::/96")
_SIX_TO_FOUR = _net("2002::/16")


def user_agent() -> str:
    """Who every request says it is: the product, its version, and where to read about it."""
    from . import _version

    return f"VectrixDB/{_version()} (+{HOME})"


# ============================================================================
# REFUSED, NOT NOW, AND WHAT CAME BACK
# ============================================================================
#
# INPUT   a reason
# OUTPUT  an address refused, which trying again will not change; a fetch
#         put off to the next refresh; a reply, with its headers in lower case
#
# Refused is an ExtractionError, so whatever catches a failed read catches it.


class Refused(ExtractionError):
    """An address refused before anything was sent to it, or an answer refused after.

    A private address, a redirect out of bounds, too large, too slow, not
    allowed by robots.txt, a bot check. Trying again as it is will not help.
    """


class Deferred(Exception):
    """Not now: the site asked for more time between requests than this fetch waits.

    ``until`` is when it may be asked again, in seconds since the epoch,
    when the site said. The next refresh tries again.
    """

    def __init__(self, reason: str, until: Optional[float] = None) -> None:
        super().__init__(reason)
        self.until = until


@dataclass
class Reply:
    """What an address answered: its status, headers with their names in lower case, and its body.

    ``url`` is the address that answered, after any redirect, and is the
    whole address: show :attr:`shown` instead.
    """

    url: str
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    #: Seconds the site asked to be left alone for, with a 429 or a 503.
    retry_after: Optional[float] = None

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def shown(self) -> str:
        """The address that answered, without its credentials."""
        return redact_url(self.url)


@dataclass
class Request:
    """One request as an opener sends it: the address checked, and where to connect.

    ``addresses`` are the ones the host resolved to, every one checked, to
    connect to in turn; ``proxy`` is the proxy that connects instead, when
    one is set for this host. ``target`` is the path and query.
    """

    method: str
    url: str
    scheme: str
    host: str
    port: int
    addresses: List[str]
    target: str
    headers: Dict[str, str]
    timeout: float
    max_bytes: int
    proxy: Optional[str] = None
    #: Cut a body past ``max_bytes`` short instead of refusing it, as robots.txt is.
    truncate: bool = False


#: Sends a request and answers its status, headers and body, the body read
#: to ``max_bytes`` and one byte more at most. The tests hand one in.
Opener = Callable[[Request], Tuple[int, Mapping[str, str], bytes]]
#: A host and a port to the addresses they resolve to.
Resolver = Callable[[str, int], Sequence[str]]


# ============================================================================
# WHICH ADDRESSES, AND WHICH HOSTS
# ============================================================================
#
# INPUT   an IP address; a host and a list of names
# OUTPUT  why the address may not be connected to, or None when it is
#         public; whether the host is on the list
#
# Every address a name resolves to is judged, not only the first.


def _judge(address: str) -> Tuple[Optional[str], bool]:
    """Why an address is not public, or None; and whether an intranet host may resolve to it."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        inner: Optional[ipaddress.IPv4Address] = None
        if ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip in _SIX_TO_FOUR:
            inner = ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
        if inner is not None:
            why, internal = _judge(str(inner))
            return (f"{why}, inside an IPv6 address" if why else None), internal
    for network, why, internal in _V6 if ip.version == 6 else _V4:
        if ip in network:
            return why, internal
    if not ip.is_global:
        return "not a public address", False
    return None, True


def why_not_public(address: str) -> Optional[str]:
    """Why a fetch may not connect to this address, or None when it is public."""
    return _judge(address)[0]


def host_matches(host: str, patterns: Sequence[str]) -> bool:
    """Whether ``host`` is one of ``patterns``: exact names, ``*.example.com`` for its subdomains, ``*`` for any."""
    host = str(host or "").lower().rstrip(".")
    for raw in patterns or ():
        wanted = str(raw).strip().lower().rstrip(".")
        if not wanted:
            continue
        if wanted == "*":
            return True
        if wanted.startswith("*."):
            if host.endswith(wanted[1:]) and host != wanted[2:]:
                return True
        elif host == wanted:
            return True
    return False


def _resolve(host: str, port: int) -> List[str]:
    """Every address ``host`` resolves to, each once, in the order the resolver gave them."""
    found: List[str] = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = str(info[4][0])
        if address not in found:
            found.append(address)
    return found


def _origin(url: str) -> Tuple[str, str, int]:
    """An address's site, as a browser judges one: its scheme, its host and its port."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ("", url, 0)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), port or (443 if scheme == "https" else 80)


def _split_hosts(raw: Optional[str]) -> List[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _bypassed(host: str, port: int, no_proxy: Optional[str]) -> bool:
    """Whether ``NO_PROXY`` says to connect to ``host`` directly: ``*``, the host, or a domain it is in."""
    for raw in _split_hosts(no_proxy):
        if raw == "*":
            return True
        entry = raw.lower().lstrip("*").lstrip(".")
        name, wanted_port = entry, ""
        if entry.count(":") == 1:
            name, wanted_port = entry.split(":")
        if wanted_port and wanted_port != str(port):
            continue
        name = name.strip("[]")
        if name and (host == name or host.endswith("." + name)):
            return True
    return False


# ============================================================================
# THE CONNECTION: to the address checked, the name kept
# ============================================================================
#
# INPUT   a request, with the addresses its host resolved to, or a proxy
# OUTPUT  the status, headers and body, the body read no further than the
#         cap and no longer than the time left
#
# No second lookup between the check and the connection: http.client is told
# the address to connect to, and keeps the name for TLS and the Host header.
# The time left holds however slowly a server answers: a socket's timeout is
# for one read, and a server that sends a byte a second never trips it, so the
# connection is shut when the time is up.


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(
        self, host: str, port: int, addresses: Sequence[str], timeout: float, deadline: float
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._addresses = list(addresses)
        self._deadline = deadline

    def connect(self) -> None:
        self.sock = _connect_any(self._addresses, self.port, self._deadline)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        addresses: Sequence[str],
        timeout: float,
        context: ssl.SSLContext,
        deadline: float,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._addresses = list(addresses)
        self._tls = context
        self._deadline = deadline

    def connect(self) -> None:
        sock = _connect_any(self._addresses, self.port, self._deadline)
        # The name, not the address: the certificate is checked against it.
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


def _connect_any(addresses: Sequence[str], port: int, deadline: float) -> socket.socket:
    """A connection to the first of ``addresses`` that answers, all of them in the time left."""
    last: Optional[OSError] = None
    for address in addresses:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _TooSlow()
        try:
            return socket.create_connection((address, port), left)
        except OSError as exc:
            last = exc
    raise last or OSError("no address to connect to")


def _read(
    response: http.client.HTTPResponse, sock: Any, cap: int, deadline: float, truncate: bool
) -> bytes:
    """The body, a piece at a time, stopping one byte past the cap or when time runs out."""
    said = response.getheader("content-length")
    if not truncate and said and said.strip().isdigit() and int(said) > cap:
        raise _TooLarge()
    pieces: List[bytes] = []
    size = 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _TooSlow()
        if sock is not None:
            sock.settimeout(left)
        piece = response.read(min(65536, cap + 1 - size))
        if not piece:
            break
        pieces.append(piece)
        size += len(piece)
        if size > cap:
            break
    return b"".join(pieces)


class _TooLarge(Exception):
    pass


class _TooSlow(Exception):
    pass


def _http_open(request: Request) -> Tuple[int, Mapping[str, str], bytes]:
    """The real network: straight to a checked address, or through the proxy the environment names.

    Shut when its time is up, however far it got: connecting, waiting for
    the headers, or reading the body a byte at a time.
    """
    deadline = time.monotonic() + request.timeout
    context = ssl.create_default_context()
    headers = dict(request.headers)
    conn: http.client.HTTPConnection
    path = request.target
    if request.proxy:
        proxy = urlsplit(request.proxy if "://" in request.proxy else f"http://{request.proxy}")
        if proxy.scheme != "http" or not proxy.hostname:
            raise Refused(
                f"the proxy {redact_url(request.proxy)} is not an http:// address, which is the only kind used"
            )
        auth: Dict[str, str] = {}
        if proxy.username is not None:
            pair = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}".encode()
            auth["Proxy-Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
        if request.scheme == "https":
            conn = http.client.HTTPSConnection(
                proxy.hostname, proxy.port or 80, timeout=request.timeout, context=context
            )
            conn.set_tunnel(request.host, request.port, headers=auth)
        else:
            conn = http.client.HTTPConnection(
                proxy.hostname, proxy.port or 80, timeout=request.timeout
            )
            headers.update(auth)
            path = urlunsplit(("http", f"{request.host}:{request.port}", request.target, "", ""))
    elif request.scheme == "https":
        conn = _PinnedHTTPS(
            request.host, request.port, request.addresses, request.timeout, context, deadline
        )
    else:
        conn = _PinnedHTTP(request.host, request.port, request.addresses, request.timeout, deadline)
    late = threading.Event()

    def shut() -> None:
        late.set()
        sock = conn.sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    watchdog = threading.Timer(max(request.timeout, 0.0), shut)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.connect()
        if late.is_set():
            raise _TooSlow()
        conn.request(request.method, path, headers=headers)
        response = conn.getresponse()
        merged: Dict[str, str] = {}
        for name, value in response.getheaders():
            key = name.lower()
            merged[key] = f"{merged[key]}, {value}" if key in merged else value
        body = _read(response, conn.sock, request.max_bytes, deadline, request.truncate)
        # A connection shut mid-body can read as one that ended: not a whole body.
        if late.is_set():
            raise _TooSlow()
        return response.status, merged, body
    except (OSError, http.client.HTTPException):
        if late.is_set():
            raise _TooSlow() from None
        raise
    finally:
        watchdog.cancel()
        conn.close()


# ============================================================================
# ROBOTS.TXT, BY RFC 9309
# ============================================================================
#
# INPUT   a robots.txt, and a path
# OUTPUT  whether VectrixDB may fetch the path, and the Crawl-delay it asks for
#
# urllib.robotparser reads the file. Its own answer takes the first rule that
# matches and knows no wildcards, so the groups it read are judged here the
# way RFC 9309 says: the longest match wins, and an allow wins a tie.


@dataclass
class _Rules:
    rules: List[Tuple[bool, "re.Pattern[str]", int]] = field(default_factory=list)
    delay: Optional[float] = None
    #: Nothing may be fetched: robots.txt could not be read.
    closed: bool = False

    def allows(self, target: str) -> bool:
        if self.closed:
            return False
        best_length, allowed = -1, True
        for allow, pattern, length in self.rules:
            if pattern.match(target) and (
                length > best_length or (length == best_length and allow)
            ):
                best_length, allowed = length, allow
        return allowed


#: What a rule's final $ is turned into before robotparser reads it. Older
#: robotparsers kept the $ quoted as %24 and newer ones drop it, so the anchor
#: is carried through as letters every version keeps.
_END = "VXRULEEND"
_ANCHOR = re.compile(r"(?im)^(\s*(?:dis)?allow\s*:\s*\S*?)\$[ 	]*$")


def _pattern(path: str) -> Optional["re.Pattern[str]"]:
    """A rule's path as robotparser kept it, quoted, as a pattern: ``*`` any run, a final ``$`` the end."""
    if not path:
        return None
    from urllib.parse import quote, unquote

    raw = path.replace("%2A", "*").replace("%2a", "*")
    # A final $ anchors the rule. Older robotparsers kept it quoted as %24,
    # newer ones as it was written; both are the anchor.
    anchored = raw.endswith(_END)
    if anchored:
        raw = raw[: -len(_END)]
    # Quoted the way a target is, whichever way this Python's robotparser kept it.
    pieces = [quote(unquote(piece), safe="/") for piece in raw.split("*")]
    body = ".*".join(re.escape(piece) for piece in pieces)
    return re.compile(body + (r"\Z" if anchored else ""))


def _rules_from(text: str, token: str = PRODUCT) -> _Rules:
    """The rules for ``token``: the groups that name it, else the ``*`` groups, merged."""
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.parse(_ANCHOR.sub(lambda m: m.group(1) + _END, text).splitlines())
    entries = list(getattr(parser, "entries", []) or [])
    default = getattr(parser, "default_entry", None)
    if default is not None:
        entries.append(default)
    mine = [e for e in entries if any(str(a).strip().lower() == token for a in e.useragents)]
    if not mine:
        mine = [e for e in entries if "*" in [str(a).strip() for a in e.useragents]]
    rules: List[Tuple[bool, "re.Pattern[str]", int]] = []
    delays: List[float] = []
    for entry in mine:
        for line in entry.rulelines:
            pattern = _pattern(str(line.path))
            if pattern is not None:
                rules.append((bool(line.allowance), pattern, len(str(line.path))))
        if getattr(entry, "delay", None) is not None:
            try:
                delays.append(min(float(entry.delay), MAX_DEFER))
            except (TypeError, ValueError, OverflowError):  # more digits than a float holds
                delays.append(MAX_DEFER)
    return _Rules(rules, max(delays) if delays else None)


def _robots_target(url: str) -> str:
    """The path and query of ``url`` quoted the way robotparser quotes a rule's path."""
    parts = urlsplit(unquote(url))
    target = urlunsplit(("", "", parts.path, parts.query, ""))
    return quote(target) or "/"


def _retry_after(value: str) -> Optional[float]:
    """Seconds from now that a Retry-After header asks for, a week at the most: a number, or an HTTP date."""
    value = str(value or "").strip()
    if not value:
        return None
    if value.isdigit():
        # float() of four hundred nines is infinity, not an error.
        return min(float(value), MAX_DEFER)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    return min(max(0.0, when.timestamp() - time.time()), MAX_DEFER)


# ============================================================================
# THE FETCHER
# ============================================================================
#
# INPUT   an address, and the headers to send with it
# OUTPUT  what it answered, whatever the status, after every check: the
#         address, each redirect, robots.txt, the pace, the size, the time,
#         and whether it was a bot check
#
# One fetcher for one refresh, so robots.txt is read once per host and the
# pace is kept across every source on the same host.


class Fetcher:
    """Fetches addresses somebody else chose: checked, paced, capped, and obeying robots.txt.

    ``allowed_hosts`` limits every fetch, and every redirect, to those hosts;
    left None, any public host. ``internal_hosts`` are the intranet hosts
    that may resolve to private addresses. ``resolver``, ``opener`` and
    ``proxies`` are where the tests hand in stand-ins: left out, the
    system's resolver, a real connection, and the proxies the environment
    names. A fetcher that would touch the network refuses while
    ``VECTRIXDB_OFFLINE`` is set.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Optional[Sequence[str]] = None,
        internal_hosts: Sequence[str] = (),
        timeout: float = TIMEOUT,
        max_bytes: int = MAX_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        min_interval: float = MIN_INTERVAL,
        max_wait: float = MAX_WAIT,
        robots: bool = True,
        agent: Optional[str] = None,
        resolver: Optional[Resolver] = None,
        opener: Optional[Opener] = None,
        proxies: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.allowed_hosts = list(allowed_hosts) if allowed_hosts is not None else None
        self.internal_hosts = list(internal_hosts or ())
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)
        self.max_redirects = int(max_redirects)
        self.min_interval = float(min_interval)
        self.max_wait = float(max_wait)
        self.robots = bool(robots)
        self.agent = agent or user_agent()
        self._resolver: Resolver = resolver or _resolve
        self._opener: Opener = opener or _http_open
        self._proxies = dict(proxies) if proxies is not None else None
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._robots: Dict[str, Tuple[float, _Rules]] = {}
        self._delays: Dict[str, float] = {}
        self._last: Dict[str, float] = {}
        self._until: Dict[str, float] = {}

    @classmethod
    def from_environment(cls, env: Optional[Mapping[str, str]] = None, **given: Any) -> "Fetcher":
        """A fetcher limited by ``VECTRIXDB_SOURCES_HOSTS`` and ``VECTRIXDB_SOURCES_INTERNAL_HOSTS``.

        ``VECTRIXDB_SOURCES_HOSTS`` unset is any public host; set, those
        hosts and no others, ``*`` for any. ``given`` wins over both.
        """
        import os

        found = os.environ if env is None else env
        hosts = _split_hosts(found.get("VECTRIXDB_SOURCES_HOSTS"))
        given.setdefault("allowed_hosts", hosts or None)
        given.setdefault(
            "internal_hosts", _split_hosts(found.get("VECTRIXDB_SOURCES_INTERNAL_HOSTS"))
        )
        return cls(**given)

    # -- the address

    def check(self, url: str) -> Tuple[str, str, int, List[str], str]:
        """The scheme, host, port, checked addresses and target of ``url``, or :class:`Refused` saying why.

        Nothing is sent. The host is resolved once here, and these are the
        addresses the connection is made to. While ``VECTRIXDB_OFFLINE`` is
        set, a name is not looked up: :class:`Deferred` says so.
        """
        shown = redact_url(url)
        try:
            parts = urlsplit(str(url))
            port = parts.port
        except ValueError as exc:
            raise Refused(f"{shown} is not an address that can be fetched: {exc}") from None
        if parts.scheme not in ("http", "https"):
            raise Refused(f"{shown} is not fetched: only http and https addresses are")
        if "@" in parts.netloc:
            raise Refused(
                f"{shown} is not fetched: an address with a name or password in it would send them "
                "to the site in the clear. Give a source that signs in its own way."
            )
        host = parts.hostname or ""
        if not host:
            raise Refused(f"{shown} names no host")
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise Refused(f"{host} is not a host name that can be looked up") from None
        port = port or (443 if parts.scheme == "https" else 80)
        if self.allowed_hosts is not None and not host_matches(ascii_host, self.allowed_hosts):
            raise Refused(
                f"{host} is not one of the hosts sources may be fetched from: add it to "
                "VECTRIXDB_SOURCES_HOSTS if it should be"
            )
        internal = host_matches(ascii_host, self.internal_hosts)
        try:
            ipaddress.ip_address(ascii_host.split("%", 1)[0])
            addresses = [ascii_host]
        except ValueError:
            if self._resolver is _resolve:
                from ._net import offline

                if offline():
                    raise Deferred(
                        f"VECTRIXDB_OFFLINE is set, so {host} is not looked up"
                    ) from None
            try:
                addresses = [str(a) for a in self._resolver(ascii_host, port)]
            except (OSError, UnicodeError) as exc:
                raise ExtractionError(f"{host} could not be found: {exc}") from None
        if not addresses:
            raise ExtractionError(f"{host} could not be found: it resolves to no address")
        for address in addresses:
            try:
                why, may = _judge(address)
            except ValueError:
                raise Refused(f"{host} resolved to {address!r}, which is not an address") from None
            if why is None or (internal and may):
                continue
            hint = (
                " If it is an intranet host this server should reach, name it in "
                "VECTRIXDB_SOURCES_INTERNAL_HOSTS."
                if may
                else ""
            )
            raise Refused(f"{host} resolves to {address}, {why}, which sources never fetch.{hint}")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        # A feed's link with a space in it, as some are, is sent as a browser sends it.
        return parts.scheme, ascii_host, port, addresses, quote(target, safe=_AS_IS)

    def _proxy_for(self, scheme: str, host: str, port: int) -> Optional[str]:
        """The proxy the environment names for this scheme, unless ``NO_PROXY`` names the host."""
        import urllib.request

        proxies = self._proxies if self._proxies is not None else urllib.request.getproxies()
        chosen = proxies.get(scheme)
        if not chosen or _bypassed(host, port, proxies.get("no")):
            return None
        return str(chosen)

    # -- the pace, and robots.txt

    def _wait_for(self, host: str) -> None:
        """Space requests to one host; a wait longer than ``max_wait`` is left for the next refresh."""
        with self._lock:
            now = self._clock()
            gap = max(self.min_interval, self._delays.get(host, 0.0))
            ready = max(self._last.get(host, now - gap) + gap, self._until.get(host, now))
            wait = ready - now
            if wait > self.max_wait:
                raise Deferred(
                    f"{host} asks for {wait:.0f} seconds before the next request, longer than a "
                    "refresh waits; the next refresh carries on",
                    until=time.time() + wait,
                )
            self._last[host] = max(now, ready)
        if wait > 0:
            self._sleep(wait)

    def _rules_for(self, scheme: str, host: str, port: int) -> _Rules:
        """The robots.txt rules for one origin, read once a day; a bot check in its place is raised."""
        default = 443 if scheme == "https" else 80
        origin = f"{scheme}://{host}{'' if port == default else f':{port}'}"
        with self._lock:
            held = self._robots.get(origin)
            if held is not None and self._clock() - held[0] < ROBOTS_FOR:
                return held[1]
        try:
            reply = self._fetch(
                origin + "/robots.txt",
                {"Accept": "text/plain"},
                ROBOTS_MAX_BYTES,
                robots=False,
                truncate=True,
            )
        except Deferred:
            raise
        except Refused as exc:
            if exc.status is not None:
                raise
            rules = _Rules(closed=True)
        except ExtractionError:
            rules = _Rules(closed=True)
        else:
            if 200 <= reply.status < 300:
                rules = _rules_from(reply.body.decode("utf-8", errors="replace"))
            elif 400 <= reply.status < 500:
                rules = _Rules()
            else:
                rules = _Rules(closed=True)
        with self._lock:
            self._robots[origin] = (self._clock(), rules)
            if rules.delay:
                self._delays[host] = max(self._delays.get(host, 0.0), float(rules.delay))
        return rules

    def _obey(self, scheme: str, host: str, port: int, url: str) -> None:
        if urlsplit(url).path == "/robots.txt":
            return
        rules = self._rules_for(scheme, host, port)
        if rules.closed:
            raise Deferred(
                f"robots.txt at {host} could not be read, which RFC 9309 says to take as no; "
                "the next refresh asks again"
            )
        if not rules.allows(_robots_target(url)):
            raise Refused(
                f"robots.txt at {host} does not let VectrixDB fetch {urlsplit(redact_url(url)).path or '/'}"
            )

    # -- the fetch

    def get(
        self,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        *,
        max_bytes: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Reply:
        """What ``url`` answers, whatever the status, once every check has passed.

        ``max_bytes`` and ``timeout`` raise or lower the fetcher's own caps
        for this one request: a podcast episode is larger and slower than a
        page. Raises :class:`Refused` for an address or an answer that is
        refused, :class:`Deferred` for one that is not to be asked now, and
        :class:`ExtractionError` for a host that could not be reached.
        """
        return self._fetch(
            url,
            headers or {},
            int(max_bytes or self.max_bytes),
            robots=self.robots,
            timeout=timeout,
        )

    def _fetch(
        self,
        url: str,
        headers: Mapping[str, str],
        cap: int,
        *,
        robots: bool,
        truncate: bool = False,
        timeout: Optional[float] = None,
    ) -> Reply:
        if self._opener is _http_open:
            from ._net import offline

            if offline():
                raise Deferred("VECTRIXDB_OFFLINE is set, so nothing is fetched")
        # Time on the wire, redirects included; the polite waits between
        # requests are not held against it.
        budget = float(timeout) if timeout else self.timeout
        left = budget
        current = url
        sent = {"User-Agent": self.agent, "Accept-Encoding": "gzip"}
        sent.update({str(k): str(v) for k, v in headers.items()})
        for _hop in range(self.max_redirects + 1):
            scheme, host, port, addresses, target = self.check(current)
            proxy = self._proxy_for(scheme, host, port)
            if proxy and scheme == "http":
                # A proxy is handed the name and looks it up again, and plain
                # http cannot tell if that reached somewhere else; https can,
                # since nowhere else holds the certificate for the name.
                raise Refused(
                    f"{redact_url(current)} is plain http, and the proxy this server goes through "
                    "would look its name up again itself, so the address checked here may not be "
                    "the one it reaches. Use its https address, or name the host in NO_PROXY so it "
                    "is fetched directly."
                )
            if robots:
                self._obey(scheme, host, port, current)
            self._wait_for(host)
            if left <= 0:
                raise Refused(f"{site_of(url)} took longer than {budget:.0f} seconds")
            request = Request(
                method="GET",
                url=current,
                scheme=scheme,
                host=host,
                port=port,
                addresses=list(addresses),
                target=target,
                headers=dict(sent),
                timeout=left,
                max_bytes=cap,
                proxy=proxy,
                truncate=truncate,
            )
            sent_at = self._clock()
            try:
                status, said, body = self._opener(request)
            except _TooLarge:
                raise Refused(f"{redact_url(current)} is larger than {cap:,} bytes") from None
            except _TooSlow:
                raise Refused(f"{site_of(current)} took longer than {budget:.0f} seconds") from None
            except (Refused, Deferred):
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                raise ExtractionError(
                    f"{redact_url(current)} could not be reached: {type(exc).__name__}: "
                    + redact_message(exc, current)
                ) from None
            left -= self._clock() - sent_at
            reply_headers = {str(k).lower(): str(v) for k, v in (said or {}).items()}
            status = int(status)
            location = reply_headers.get("location", "").strip()
            if status in _REDIRECTS and location:
                following = urljoin(current, location)
                if urlsplit(current).scheme == "https" and urlsplit(following).scheme == "http":
                    raise Refused(
                        f"{redact_url(current)} redirected to plain http, which would send the rest in the clear"
                    )
                if _origin(following) != _origin(current):
                    sent = {k: v for k, v in sent.items() if k.lower() in _TO_ANY_SITE}
                current = following
                continue
            body = bytes(body or b"")
            if len(body) > cap:
                if not truncate:
                    raise Refused(f"{redact_url(current)} is larger than {cap:,} bytes")
                body = body[:cap]
            body = self._unpacked(reply_headers, body, cap, current, truncate)
            reason = bot_check(status, reply_headers, body)
            if reason:
                raise Refused(bot_check_message(current, status, reason), status=status)
            reply = Reply(current, status, reply_headers, body)
            if status in (429, 503):
                reply.retry_after = _retry_after(reply_headers.get("retry-after", ""))
                if reply.retry_after:
                    with self._lock:
                        self._until[host] = self._clock() + reply.retry_after
            return reply
        raise Refused(f"{redact_url(url)} redirected more than {self.max_redirects} times")

    @staticmethod
    def _unpacked(
        headers: Mapping[str, str], body: bytes, cap: int, url: str, truncate: bool = False
    ) -> bytes:
        coding = headers.get("content-encoding", "").strip().lower()
        if not coding or coding == "identity" or not body:
            return body
        if coding in ("gzip", "x-gzip"):
            unpack = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif coding == "deflate":
            unpack = zlib.decompressobj()
        else:
            raise Refused(f"{redact_url(url)} answered in {coding}, which is not read")
        try:
            out = unpack.decompress(body, cap + 1)
        except zlib.error as exc:
            raise Refused(f"{redact_url(url)} sent a body that does not unpack: {exc}") from None
        if len(out) > cap or unpack.unconsumed_tail:
            if truncate:
                return out[:cap]
            raise Refused(f"{redact_url(url)} is larger than {cap:,} bytes once unpacked")
        return out


def is_feed_type(content_type: Union[str, None]) -> bool:
    """Whether a content type says the body is a feed: RSS, Atom, RDF or JSON Feed."""
    kind = str(content_type or "").split(";", 1)[0].strip().lower()
    return kind in (
        "application/rss+xml",
        "application/atom+xml",
        "application/rdf+xml",
        "application/feed+json",
        "application/x-rss+xml",
    )
