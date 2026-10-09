"""Addresses as they are kept and shown, and answers that are a bot check instead of a page.

    redact_url("https://acct.blob.core.windows.net/raw/q3.pdf?sv=2024-01-01&se=2026-12-31&sig=SECRET")
    # 'https://acct.blob.core.windows.net/raw/q3.pdf'
    bot_check(403, {"cf-mitigated": "challenge"}, b"<html>...</html>")
    # 'a Cloudflare challenge (cf-mitigated: challenge)'

An address is fetched as it was given and kept without what lets anybody in.
A signed link carries its credential in the address itself: an Azure SAS
signature, an S3 or Cloud Storage signature, a token, a key, a name and
password before the host. An address kept as a document's source is copied
onto every chunk, into every citation and every answer that quotes one, so
what is kept and shown is :func:`redact_url`'s version, and only the fetch
itself ever sees the whole.

Credentials are dropped, not replaced with a placeholder, for three reasons.
What is left still opens the object for anybody allowed to open it another
way. It is the same address however many times the link is signed again, so
a document keeps one identity instead of becoming a new one each time its
link is renewed. And there is no ``sig=REDACTED`` for a tool to mistake for
a value. A signature takes the rest of its signed link with it, the expiry
and permissions and key id of a SAS, every ``X-Amz-`` field of an S3 link,
since none of them means anything without it and all of them change each
time the link is signed.

What it does not find: a credential that is a segment of the path itself,
which some webhook addresses carry. An address like that should not be a
source as it is; a source reads that segment from the environment instead.

A bot check is a page a site's firewall sends in place of the one asked for:
"Just a moment...", a CAPTCHA, an access denied page with a reference
number. Read as a document it would be indexed and cited as though it were
the page. :func:`bot_check` says when an answer is one, from its status, its
headers and the start of its body, and only when it is sure: a page with
more to read than a bot check ever says is a page, so an article about
Cloudflare that quotes "Just a moment..." is not refused.
"""

from __future__ import annotations

import html
import re
from typing import Callable, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import unquote_plus, urlsplit, urlunsplit

__all__ = [
    "bot_check",
    "bot_check_message",
    "redact_message",
    "redact_url",
    "site_of",
    "status_message",
]


# ============================================================================
# SETTINGS: what a credential is called, and what a bot check looks like
# ============================================================================
#
# The names whose values are credentials, the fields a signed link signs
# with, and how much of a body is looked at.

#: Names whose value is a credential in any address, compared without case.
_SECRET_NAMES = frozenset(
    {
        "sig",
        "signature",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "code",
        "key",
        "apikey",
        "api_key",
        "api-key",
        "x-api-key",
        "subscription-key",
        "password",
        "passwd",
        "pwd",
        "pass",
        "secret",
        "client_secret",
        "credential",
        "credentials",
        "access_key",
        "accesskey",
        "secret_key",
        "secretkey",
        "private_key",
        "auth",
        "authorization",
        "jwt",
        "sas",
        "sid",
        "sessionid",
        "session_id",
        "phpsessid",
        "jsessionid",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
        # Akamai's signed links: hdnts, and __token__ once its underscores go.
        "hdnts",
        "hmac",
        # API keys by other names: 3scale's user_key, a CDN's auth_key.
        "auth_key",
        "authkey",
        "user_key",
        "app_key",
        "appkey",
        "client_key",
        "license_key",
    }
)
#: Endings that make any name a credential: auth_token, client_secret, oauth_signature.
_SECRET_ENDINGS = (
    "token",
    "secret",
    "password",
    "passwd",
    "signature",
    "apikey",
    "api_key",
    "api-key",
    "accesskey",
    "access_key",
    "secretkey",
    "secret_key",
    "subscription-key",
    "credential",
    "credentials",
    "sessionid",
    "session_id",
)
#: Every field of an Azure SAS. They go with its signature, sig: without it
#: they mean nothing, and the expiry and start change each time it is signed.
_AZURE_SAS = frozenset(
    {
        "sv",
        "ss",
        "srt",
        "sp",
        "se",
        "st",
        "spr",
        "sip",
        "sr",
        "si",
        "sig",
        "ses",
        "sdd",
        "skoid",
        "sktid",
        "skt",
        "ske",
        "sks",
        "skv",
        "skdutid",
        "saoid",
        "suoid",
        "scid",
        "sduoid",
        "rscc",
        "rscd",
        "rsce",
        "rscl",
        "rsct",
    }
)
#: What the older S3 and Cloud Storage links and CloudFront's sign with, gone with their Signature.
_SIGNED_WITH = frozenset({"awsaccesskeyid", "googleaccessid", "expires", "policy", "key-pair-id"})
#: A JSON Web Token in a value, whatever its name: two JSON objects and a signature, base64url.
_JWT = re.compile(r"^eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*$")

#: How much of a body is looked at. A bot check is a page or two of markup;
#: a body larger than this is not one, whatever it says.
_LOOK = 256 * 1024
#: The most visible text a bot check has. Its page says a sentence or two and
#: a reference number; anything with more to read than this is a page.
_SHORT = 1500

_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_HIDDEN = re.compile(r"<(script|style|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"<[^>]*>")


# ============================================================================
# THE ADDRESS AS IT IS KEPT
# ============================================================================
#
# INPUT   an address, signed or not
# OUTPUT  the same address without its credentials: no name and password
#         before the host, no signed-link fields, no token, key or password
#         in the query or the fragment; the host, for a message
#
# Dropped rather than replaced, so what is kept is still an address, and the
# same one each time a link is signed again.


def _name(piece: str) -> str:
    """A piece's name, compared without case and without the underscores round it: ``__token__`` is ``token``."""
    return unquote_plus(piece.split("=", 1)[0]).strip().strip("_").lower()


def _value(piece: str) -> str:
    return unquote_plus(piece.split("=", 1)[1]).strip() if "=" in piece else ""


def _is_secret(name: str) -> bool:
    return name in _SECRET_NAMES or name.endswith(_SECRET_ENDINGS)


def _without_secrets(part: str) -> Tuple[str, List[str]]:
    """A query or a fragment without the pieces that are credentials, and the pieces that went.

    Pieces are split on ``&`` and ``;``, and what is kept is joined back with
    the separator that stood before it, so a value with a ``;`` in it reads
    as it did.
    """
    split = re.split(r"([&;])", part)
    pieces, separators = split[0::2], split[1::2]
    names = [_name(p) for p in pieces]
    present: Set[str] = set(names)
    family: Set[str] = set()
    if "sig" in present:
        family |= _AZURE_SAS
    if "x-amz-signature" in present:
        family |= {n for n in present if n.startswith("x-amz-")}
    if "x-goog-signature" in present:
        family |= {n for n in present if n.startswith("x-goog-")}
    if "signature" in present:
        family |= _SIGNED_WITH
    kept: List[str] = []
    dropped: List[str] = []
    for index, (piece, name) in enumerate(zip(pieces, names)):
        if name and (name in family or _is_secret(name) or _JWT.match(_value(piece))):
            dropped.append(piece)
            continue
        if kept:
            kept.append(separators[index - 1])
        kept.append(piece)
    return "".join(kept), dropped


#: A path parameter: ``;jsessionid=ABC`` at the end of a path's segment.
_PATH_PARAMETER = re.compile(r";([^;/=?#]*)=([^;/?#]*)")


def _path_without_secrets(path: str) -> Tuple[str, List[str]]:
    """A path without the parameters in it that are credentials, a Java session's ``;jsessionid=``; and those that went."""
    dropped: List[str] = []

    def keep(match: "re.Match[str]") -> str:
        if _is_secret(_name(match.group(1))):
            dropped.append(match.group(0)[1:])
            return ""
        return match.group(0)

    return (_PATH_PARAMETER.sub(keep, path) if ";" in path else path), dropped


def _fragment_without_secrets(fragment: str) -> Tuple[str, List[str]]:
    """A fragment without its credentials: a sign-in's ``#access_token=``, or a single-page app's ``#/callback?code=``."""
    route, mark, tail = fragment.rpartition("?")
    if mark:
        kept_tail, dropped = _without_secrets(tail)
        if "=" in route or "&" in route:
            route, more = _without_secrets(route)
            dropped += more
        return route + (f"?{kept_tail}" if kept_tail else ""), dropped
    if "=" in fragment or "&" in fragment:
        return _without_secrets(fragment)
    if _JWT.match(unquote_plus(fragment)):
        return "", [fragment]
    return fragment, []


def _split_secrets(url: str) -> Tuple[str, List[str]]:
    """``url`` without its credentials, and the credentials themselves: a name and password, each value taken out."""
    parts = urlsplit(url)
    taken: List[str] = []
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, netloc = netloc.rpartition("@")
        taken += userinfo.split(":", 1)
    path, dropped = _path_without_secrets(parts.path)
    query, more = _without_secrets(parts.query) if parts.query else ("", [])
    dropped += more
    fragment, more = _fragment_without_secrets(parts.fragment) if parts.fragment else ("", [])
    dropped += more
    # A piece's value, or the piece itself when it has no name.
    taken += [piece.split("=", 1)[1] if "=" in piece else piece for piece in dropped]
    if "@" not in parts.netloc and not dropped:
        return url, []
    return urlunsplit((parts.scheme, netloc, path, query, fragment)), taken


def _rough(url: str) -> str:
    """An address that would not parse: everything after ``?`` or ``#`` goes, and any name and password."""
    cut = re.split(r"[?#]", url, maxsplit=1)[0]
    return re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@", r"\1", cut)


def redact_url(url: str) -> str:
    """``url`` as it may be kept and shown: the same address without its credentials.

    Gone, compared without case: a name and password before the host; an
    Azure SAS (``sig`` and every field it signs); an S3 signed link
    (``X-Amz-Signature`` and every ``X-Amz-`` field), its credential and
    session token; a Cloud Storage one (``X-Goog-``); the older S3, Cloud
    Storage and CloudFront ``Signature`` with what it signs; and any
    ``token``, ``access_token``, ``api_key``, ``apikey``, ``key``,
    ``user_key``, ``auth_key``, ``password``, ``secret``, ``signature``,
    ``code`` or session id, Akamai's ``hdnts`` and ``__token__`` (a name is
    compared without the underscores round it), and any name ending in token,
    secret, password or signature: in the query, in a path parameter such as
    ``;jsessionid=``, or in the fragment, where a sign-in hands back its token
    and a single-page app its ``#/callback?code=``. A value that is a JSON Web
    Token goes whatever it is called. Everything else is kept as it was
    written, and an address with nothing to take out comes back unchanged.
    """
    text = str(url or "")
    if not text:
        return text
    try:
        return _split_secrets(text)[0]
    except ValueError:
        return _rough(text)


def redact_message(text: object, url: str) -> str:
    """A message that may repeat ``url``, whole or in part, as it may be shown: without its credentials.

    An error's own words can carry the address, or only its path and query,
    as http.client's do, or a single value from it. The address is put back
    as :func:`redact_url` keeps it, and every credential it carried, each
    four characters or more, is taken out wherever else it appears.
    """
    said = str(text)
    address = str(url or "")
    if not address or not said:
        return said
    try:
        shown, taken = _split_secrets(address)
    except ValueError:
        shown, taken = _rough(address), [address]
    if shown == address:
        return said
    said = said.replace(address, shown)
    values: Set[str] = set()
    for value in taken:
        if len(value) >= 4:
            values.update((value, unquote_plus(value)))
    for value in sorted(values, key=len, reverse=True):
        said = said.replace(value, "***")
    return said


def site_of(url: str) -> str:
    """The host an address names, for a message that must not repeat the address itself."""
    try:
        host = urlsplit(str(url or "")).hostname
    except ValueError:
        host = None
    return host or "the site"


# ============================================================================
# A BOT CHECK INSTEAD OF A PAGE
# ============================================================================
#
# INPUT   a reply's status, headers and body
# OUTPUT  what kind of bot check it is, or None when it is the page; the
#         message that names the site and the reason
#
# High confidence only: a header that only a challenge carries, or a short
# page with the marks a vendor's challenge leaves. A page with more to read
# than a bot check ever says is a page, whatever it quotes.


def _visible(markup: str) -> str:
    """The words a person would see, scripts and styles left out, as one line."""
    text = _HIDDEN.sub(" ", _COMMENT.sub(" ", markup))
    text = html.unescape(_TAG.sub(" ", text)).replace("’", "'").replace(" ", " ")
    return " ".join(text.split())


# Each rule reads (status, headers, markup, title, visible words), the last
# four in lower case, and answers its reason or None.
_Rule = Callable[[int, Dict[str, str], str, str, str], Optional[str]]


def _cloudflare(
    status: int, headers: Dict[str, str], low: str, title: str, words: str
) -> Optional[str]:
    hint = (
        headers.get("server") == "cloudflare"
        or "cf-ray" in headers
        or "cloudflare" in low
        or "/cdn-cgi/" in low
    )
    marks = sum(
        (
            title in ("just a moment...", "just a moment…", "just a moment"),
            "_cf_chl_opt" in low,
            "/cdn-cgi/challenge-platform/h/" in low,
            "performing security verification" in words,
            "checking your browser before accessing" in words,
            "checking if the site connection is secure" in words,
            "verify you are human by completing the action below" in words,
            "needs to review the security of your connection" in words,
            "enable javascript and cookies to continue" in words,
        )
    )
    if marks and (hint or marks >= 2):
        return "a Cloudflare challenge page"
    if (
        title == "attention required! | cloudflare"
        or (hint and "sorry, you have been blocked" in words)
        or ('id="cf-error-details"' in low and "access denied" in words)
    ):
        return "a Cloudflare block page"
    return None


def _datadome(
    status: int, headers: Dict[str, str], low: str, title: str, words: str
) -> Optional[str]:
    if "captcha-delivery.com" in low and (
        status in (401, 403, 405, 429)
        or any(name.startswith("x-datadome") or name == "x-dd-b" for name in headers)
    ):
        return "a DataDome CAPTCHA page"
    return None


def _perimeterx(
    status: int, headers: Dict[str, str], low: str, title: str, words: str
) -> Optional[str]:
    if ("px-captcha" in low or "captcha.px-cdn.net" in low) and (
        status >= 400 or "press & hold" in words or "access to this page has been denied" in words
    ):
        return "a PerimeterX (HUMAN) CAPTCHA page"
    return None


def _imperva(
    status: int, headers: Dict[str, str], low: str, title: str, words: str
) -> Optional[str]:
    if "incapsula incident id" in words or (
        "_incapsula_resource" in low
        and ('id="main-iframe"' in low or "request unsuccessful" in words)
    ):
        return "an Imperva (Incapsula) block page"
    if title == "pardon our interruption" and (
        "made us think you were a bot" in words or "as you were browsing" in words
    ):
        return "an Imperva bot check (Pardon Our Interruption)"
    return None


def _akamai(
    status: int, headers: Dict[str, str], low: str, title: str, words: str
) -> Optional[str]:
    if (
        title == "access denied"
        and "you don't have permission to access" in words
        and ("reference #" in words or "errors.edgesuite.net" in low)
    ):
        return "an Akamai Access Denied page"
    return None


def _aws_waf(
    status: int, headers: Dict[str, str], low: str, title: str, words: str
) -> Optional[str]:
    if "awswaf" in low and ("gokuprops" in low or "awswafintegration" in low):
        return "an AWS WAF challenge page"
    return None


def _javascript_wall(
    status: int, headers: Dict[str, str], low: str, title: str, words: str
) -> Optional[str]:
    if len(words) < 400 and (
        "enable javascript and cookies to continue" in words
        or "please enable js and disable any ad blocker" in words
    ):
        return "a page that asks for JavaScript and cookies before it shows anything"
    return None


_RULES: Tuple[_Rule, ...] = (
    _cloudflare,
    _datadome,
    _perimeterx,
    _imperva,
    _akamai,
    _aws_waf,
    _javascript_wall,
)


def bot_check(status: int, headers: Optional[Mapping[str, str]], body: bytes) -> Optional[str]:
    """What kind of bot check this answer is, or None when it is the page.

    Two headers are enough on their own, because only a challenge carries
    them: Cloudflare's ``cf-mitigated: challenge`` and AWS WAF's
    ``x-amzn-waf-action``. Otherwise the start of the body is read, and only
    a short one counts: Cloudflare's "Just a moment..." and its block page,
    DataDome's and PerimeterX's CAPTCHAs, Imperva's incident page and its
    "Pardon Our Interruption", Akamai's "Access Denied" with its reference
    number, AWS WAF's challenge, and a page whose only words ask for
    JavaScript and cookies. Whatever the status: a challenge comes as 200 as
    often as 403, 429 or 503.
    """
    said = {str(k).strip().lower(): str(v).strip().lower() for k, v in (headers or {}).items()}
    if said.get("cf-mitigated") == "challenge":
        return "a Cloudflare challenge (cf-mitigated: challenge)"
    waf = said.get("x-amzn-waf-action")
    if waf in ("captcha", "challenge"):
        return "an AWS WAF CAPTCHA" if waf == "captcha" else "an AWS WAF challenge"
    data = bytes(body or b"")
    if not data or len(data) > _LOOK:
        return None
    markup = data.decode("utf-8", errors="replace")
    words = _visible(markup).lower()
    if len(words) > _SHORT:
        return None
    found = _TITLE.search(markup)
    title = " ".join(html.unescape(found.group(1)).split()).lower() if found else ""
    low = markup.lower()
    for rule in _RULES:
        reason = rule(int(status or 0), said, low, title, words)
        if reason:
            return reason
    return None


def bot_check_message(url: str, status: int, reason: str) -> str:
    """The error for a bot check: the site, what it answered, and what to do. Never the address itself."""
    return (
        f"{site_of(url)} answered {int(status)} with {reason}, not the page. VectrixDB does not get past "
        "bot checks: ask the site for a feed or an API, or, if the site is yours, let VectrixDB's "
        "requests through its firewall."
    )


def status_message(shown: str, status: int, headers: Optional[Mapping[str, str]] = None) -> str:
    """What an answer that is not a success means, in words, for an address already fit to show.

    ``shown`` is the address as :func:`redact_url` leaves it. A 401 or 403
    that is not a bot check is the site saying no: a sign-in wanted, or
    requests that are not a browser's turned away. A 429 or a 503 is a site
    asking for time, and says how long when the site said.
    """
    status = int(status)
    if status in (401, 403):
        return (
            f"{shown} answered {status}: the site refused the request. It may want a sign-in, or turn "
            "away requests that do not come from a browser: ask the site for a feed or an API, or, "
            "if the site is yours, let VectrixDB's requests through"
        )
    if status in (429, 503):
        what = "too many requests" if status == 429 else "unavailable for now"
        return f"{shown} answered {status}, {what}{_asked_wait(headers)}"
    if status == 404:
        return f"{shown} answered 404: nothing is at this address"
    if status == 410:
        return f"{shown} answered 410: what was at this address is gone"
    return f"{shown} answered {status}"


def _asked_wait(headers: Optional[Mapping[str, str]]) -> str:
    """How long a Retry-After header asks for, as words to follow a status, or nothing."""
    value = next(
        (str(v).strip() for k, v in (headers or {}).items() if str(k).lower() == "retry-after"),
        "",
    )
    if not value:
        return ""
    if value.isdigit():
        # A week is the most a fetch waits for (vectrixdb._fetch.MAX_DEFER),
        # and a number of a thousand digits is no number of seconds to repeat.
        if len(value) > 7 or int(value) > 7 * 86400:
            return ", and asked for more than a week; it is asked again in a week"
        return f", and asked for {int(value)} seconds"
    return f", and asked to be left until {value[:64]}"
