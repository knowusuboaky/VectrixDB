"""The command line, pointed at a server: where it is, who you are, and a saved sign-in.

    vectrixdb query "refunds" --url https://vectors.company.com --name handbook
    VECTRIXDB_URL=https://vectors.company.com VECTRIXDB_KEY=... vectrixdb list
    vectrixdb login --url https://vectors.company.com

Every command that works on a folder works on a server too, given ``--url``
or ``VECTRIXDB_URL``, through the Python client (``vectrixdb.client``). The
server decides everything it decides for any caller: who you are, what your
role allows, which collections a key reaches, each collection's policy, and
the audit trail.

Who you are, the first of these that is there:

    --key-file PATH            a file holding an API key, readable by you alone
    VECTRIXDB_KEY              an API key, from the environment or a secrets store
    VECTRIXDB_KEY_FILE         a file holding one, as a mounted secret
    VECTRIXDB_TOKEN            a company sign-in token your own tooling fetched
    vectrixdb login            a sign-in saved for this address, refreshed as needed

A key is never an option on the command line, where it would be kept in the
shell's history and shown to anyone listing processes. Two of them at once is
refused rather than one picked.

Safe by default, so it can be pointed anywhere:

- A key or token goes over plain HTTP only to this machine;
  ``VECTRIXDB_ALLOW_HTTP=1`` says otherwise for a network you trust.
- No redirect is followed, so a key goes only to the address it was given for.
- Certificates are always checked. ``--ca-bundle`` or ``VECTRIXDB_CA_BUNDLE``
  trusts a company's own CA; ``SSL_CERT_FILE`` and ``HTTPS_PROXY`` /
  ``NO_PROXY`` are honoured as everywhere else.
- A gateway's client certificate: ``VECTRIXDB_CLIENT_CERT`` and
  ``VECTRIXDB_CLIENT_CERT_KEY``.
- A gateway's own key header and paths: ``--key-header``, ``--prefix`` and
  ``--gateway-paths``, or the server's settings of the same names.
- A saved sign-in lives in a file only you can read, under
  ``VECTRIXDB_CONFIG_DIR`` or the platform's config folder. The command refuses
  to read it when others can, and it is used only for the address it was made
  for. ``vectrixdb logout`` removes it.
- What a server sends back is printed with terminal control characters taken
  out, so a document cannot rewrite the screen.

``vectrixdb login`` signs a person in with the company's identity provider,
the one the server names at ``/.well-known/oauth-protected-resource``: in a
browser on this machine (authorization code with PKCE, answered on
127.0.0.1), or with ``--device`` from any terminal, SSH and containers
included (the device code flow). It needs a client id the company registered
for the command line, as a public client: ``--client-id`` or
``VECTRIXDB_LOGIN_CLIENT_ID``.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

from .exceptions import ConfigurationError

__all__ = [
    "Login",
    "Logins",
    "Server",
    "clean",
    "login_with_browser",
    "login_with_device",
    "server_from",
]

_ON = {"1", "true", "yes", "on"}
#: A token this close to expiring is refreshed before it is used.
REFRESH_EARLY = 120
#: How long a browser sign-in is waited for.
BROWSER_WAIT = 300
#: Characters a terminal acts on rather than prints: all controls but tab and newline.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text: Any) -> str:
    """``text`` with terminal control characters taken out, so what a server sends cannot move the cursor or recolour the screen."""
    return _CONTROL.sub("", str(text))


# ============================================================================
# WHERE THE SERVER IS, AND WHO YOU ARE
# ============================================================================
#
# INPUT   the command's --url, --key-file and gateway options; the environment
# OUTPUT  a Server: the address, the credential and the client's options; or
#         None when the command is for a folder
#
# The options a command is given win over the environment, and the
# environment over a saved sign-in.


def _env(name: str, env: Mapping[str, str]) -> str:
    return str(env.get(name, "") or "").strip()


def _private_file(path: Path, what: str) -> str:
    """The text of ``path``, refused when others may read it: the way ssh treats a key."""
    try:
        info = path.stat()
    except OSError as exc:
        raise ConfigurationError(f"{what} {path} cannot be read: {exc.strerror or exc}") from None
    if os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigurationError(
            f"{what} {path} can be read by others ({stat.filemode(info.st_mode)}). "
            f"Make it yours alone first: chmod 600 {path}"
        )
    return path.read_text(encoding="utf-8").strip()


@dataclass
class Server:
    """A server to talk to: the address, the credential, and how to reach it."""

    url: str
    key: Optional[str] = field(default=None, repr=False)
    token: Optional[str] = field(default=None, repr=False)
    #: Where the credential came from, in words: "VECTRIXDB_KEY", "the sign-in saved for it".
    who: str = "no key"
    options: Dict[str, Any] = field(default_factory=dict)

    def client(self) -> Any:
        from .client import VectrixClient

        return VectrixClient(self.url, key=self.key, token=self.token, **self.options)


def server_from(
    url: Optional[str],
    *,
    key_file: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    key_header: Optional[str] = None,
    prefix: Optional[str] = None,
    gateway_paths: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    logins: Optional["Logins"] = None,
    post: Optional[Callable[..., Dict[str, Any]]] = None,
    use_saved: bool = True,
) -> Optional[Server]:
    """The server a command talks to, or None when it is for the folder on this machine."""
    values = os.environ if env is None else env
    address = (url or _env("VECTRIXDB_URL", values)).strip().rstrip("/")
    if not address:
        if key_file:
            raise ConfigurationError("--key-file is for a server: give --url or VECTRIXDB_URL too")
        return None
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigurationError(
            f"{address!r} is not an address. Give the one people use: https://vectors.company.com"
        )
    if parts.username or parts.password:
        raise ConfigurationError(
            "Put the key in VECTRIXDB_KEY or a --key-file, not in the address, where logs and history keep it"
        )

    given: List[Tuple[str, str, Optional[str], Optional[str]]] = []
    if key_file:
        given.append(("--key-file", "key", _private_file(Path(key_file), "The key file"), None))
    if _env("VECTRIXDB_KEY", values):
        given.append(("VECTRIXDB_KEY", "key", _env("VECTRIXDB_KEY", values), None))
    if _env("VECTRIXDB_KEY_FILE", values):
        path = Path(_env("VECTRIXDB_KEY_FILE", values))
        given.append(("VECTRIXDB_KEY_FILE", "key", _private_file(path, "The key file"), None))
    if _env("VECTRIXDB_TOKEN", values):
        given.append(("VECTRIXDB_TOKEN", "token", None, _env("VECTRIXDB_TOKEN", values)))
    if len(given) > 1:
        raise ConfigurationError(
            f"{' and '.join(g[0] for g in given)} are both set. Keep one, so it is clear who the server sees"
        )

    options: Dict[str, Any] = {
        "allow_http": _env("VECTRIXDB_ALLOW_HTTP", values).lower() in _ON,
        "key_header": key_header or _env("VECTRIXDB_KEY_HEADER", values) or "api-key",
        "token_header": _env("VECTRIXDB_TOKEN_HEADER", values) or "authorization",
        "prefix": prefix if prefix is not None else _env("VECTRIXDB_PREFIX", values),
        "gateway_paths": gateway_paths
        if gateway_paths is not None
        else _env("VECTRIXDB_GATEWAY_PATHS", values),
    }
    bundle = ca_bundle or _env("VECTRIXDB_CA_BUNDLE", values)
    if bundle:
        if not Path(bundle).is_file():
            raise ConfigurationError(f"The CA bundle {bundle} is not a file")
        options["verify"] = bundle
    cert = _env("VECTRIXDB_CLIENT_CERT", values)
    if cert:
        cert_key = _env("VECTRIXDB_CLIENT_CERT_KEY", values)
        if cert_key:
            _private_file(Path(cert_key), "The client certificate's key")
        options["cert"] = (cert, cert_key) if cert_key else cert

    if given:
        source, kind, key, token = given[0]
        return _checked(Server(address, key=key, token=token, who=source, options=options))
    saved = (logins or Logins()).fresh(address, post=post) if use_saved else None
    if saved is not None:
        return _checked(
            Server(
                address,
                token=saved.access_token,
                who=f"the sign-in saved for it ({saved.who or 'you'})",
                options=options,
            )
        )
    return Server(address, options=options)


def _checked(server: Server) -> Server:
    """``server``, refused when its key or token would cross the network over plain HTTP."""
    parts = urlsplit(server.url)
    if (
        parts.scheme == "http"
        and not server.options.get("allow_http")
        and not _is_local(parts.hostname)
    ):
        raise ConfigurationError(
            f"{server.url} is plain HTTP, and {server.who} would cross the network readable. "
            "Use its https:// address, or set VECTRIXDB_ALLOW_HTTP=1 on a network you trust"
        )
    return server


# ============================================================================
# SAVED SIGN-INS
# ============================================================================
#
# INPUT   an address; a sign-in to keep; the environment
# OUTPUT  the sign-in for that address, refreshed when it is close to expiring;
#         the file rewritten readable by its owner alone
#
# One JSON file, keyed by the exact address, so a token is only ever sent to
# the server it was got for. It is written to a temporary file made private
# before anything is in it, then moved into place.


@dataclass
class Login:
    """A person's sign-in at one server: the tokens, and where to refresh them."""

    url: str
    issuer: str
    client_id: str
    token_endpoint: str
    access_token: str = field(repr=False)
    expires_at: float = 0.0
    refresh_token: str = field(default="", repr=False)
    scope: str = ""
    who: str = ""

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at - REFRESH_EARLY


def config_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """``VECTRIXDB_CONFIG_DIR``, else the platform's: ``%APPDATA%\\vectrixdb``, ``~/Library/Application Support/vectrixdb``, ``$XDG_CONFIG_HOME/vectrixdb``."""
    values = os.environ if env is None else env
    given = _env("VECTRIXDB_CONFIG_DIR", values)
    if given:
        return Path(given).expanduser()
    if os.name == "nt" and _env("APPDATA", values):
        return Path(_env("APPDATA", values)) / "vectrixdb"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "vectrixdb"
    return Path(_env("XDG_CONFIG_HOME", values) or Path.home() / ".config") / "vectrixdb"


class Logins:
    """The sign-ins saved on this machine, one per server address."""

    def __init__(self, folder: Optional[Path] = None) -> None:
        self.folder = folder or config_dir()
        self.path = self.folder / "logins.json"

    def _read(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        text = _private_file(self.path, "The saved sign-ins")
        try:
            found = json.loads(text or "{}")
        except json.JSONDecodeError:
            raise ConfigurationError(
                f"{self.path} is not readable JSON. Remove it and sign in again: vectrixdb login"
            ) from None
        return found if isinstance(found, dict) else {}

    def _write(self, every: Dict[str, Dict[str, Any]]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.folder, 0o700)
        temporary = self.path.with_name(f".logins-{secrets.token_hex(4)}.tmp")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(every, out, indent=2)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def get(self, url: str) -> Optional[Login]:
        row = self._read().get(url.rstrip("/"))
        if not row:
            return None
        try:
            return Login(**{k: row[k] for k in Login.__dataclass_fields__ if k in row})
        except TypeError:
            return None

    def save(self, login: Login) -> None:
        every = self._read()
        every[login.url.rstrip("/")] = asdict(login)
        self._write(every)

    def remove(self, url: str) -> bool:
        every = self._read()
        if every.pop(url.rstrip("/"), None) is None:
            return False
        self._write(every)
        return True

    def urls(self) -> List[str]:
        return sorted(self._read())

    def fresh(
        self, url: str, *, post: Optional[Callable[..., Dict[str, Any]]] = None
    ) -> Optional[Login]:
        """The sign-in saved for ``url``, refreshed first when it is close to expiring."""
        login = self.get(url)
        if login is None or not login.expired:
            return login
        if not login.refresh_token:
            raise ConfigurationError(
                f"The sign-in saved for {url} has expired. Sign in again: vectrixdb login --url {url}"
            )
        try:
            answer = (post or _post_form)(
                login.token_endpoint,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": login.refresh_token,
                    "client_id": login.client_id,
                    "scope": login.scope,
                },
            )
        except ConfigurationError as exc:
            raise ConfigurationError(
                f"The sign-in saved for {url} could not be refreshed ({exc}). Sign in again: vectrixdb login --url {url}"
            ) from None
        _take_tokens(login, answer)
        self.save(login)
        return login


# ============================================================================
# SIGNING IN
# ============================================================================
#
# INPUT   the server's address and a client id
# OUTPUT  a Login: the identity provider the server names, asked for a token
#         in a browser on this machine or with a code typed on any device
#
# The server says which identity provider it trusts and which scopes to ask
# for (RFC 9728); the provider says where to send people (OpenID discovery).
# Nothing about the provider is a setting here, so a change there needs no
# change on anyone's machine.


def _http() -> Any:
    from .client import _httpx

    return _httpx()


def _get_json(url: str, what: str) -> Dict[str, Any]:
    httpx = _http()
    try:
        response = httpx.get(url, timeout=20, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise ConfigurationError(f"{what} at {url} could not be reached: {exc}") from None
    if response.status_code != 200:
        raise ConfigurationError(f"{what} at {url} answered {response.status_code}")
    try:
        found = response.json()
    except ValueError:
        raise ConfigurationError(f"{what} at {url} did not answer with JSON") from None
    if not isinstance(found, dict):
        raise ConfigurationError(f"{what} at {url} did not answer with a JSON object")
    return found


def _post_form(url: str, form: Mapping[str, str]) -> Dict[str, Any]:
    """POST a form to an identity provider; its JSON answer, errors included, as a dict."""
    httpx = _http()
    try:
        response = httpx.post(
            url,
            data={k: v for k, v in form.items() if v},
            headers={"accept": "application/json"},
            timeout=30,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise ConfigurationError(f"{url} could not be reached: {exc}") from None
    try:
        found = response.json()
    except ValueError:
        raise ConfigurationError(f"{url} answered {response.status_code}, not JSON") from None
    if not isinstance(found, dict):
        raise ConfigurationError(f"{url} answered {response.status_code}, not a JSON object")
    found.setdefault("_status", response.status_code)
    return found


def _https(url: str, what: str) -> str:
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" and not (parts.scheme == "http" and _is_local(parts.hostname)):
        raise ConfigurationError(f"{what} is {url!r}; a sign-in goes only to an https:// address")
    return str(url)


def _is_local(host: Optional[str]) -> bool:
    from .client import _loopback

    return bool(host) and _loopback(str(host))


@dataclass
class Provider:
    """What the server and its identity provider say a sign-in needs."""

    issuer: str
    scopes: List[str]
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    device_authorization_endpoint: str = ""


def discover(url: str, get: Callable[[str, str], Dict[str, Any]] = _get_json) -> Provider:
    """The identity provider ``url`` trusts, from its protected-resource document, and that provider's endpoints."""
    base = url.rstrip("/")
    resource = get(base + "/.well-known/oauth-protected-resource", "The server")
    issuers = [str(i) for i in resource.get("authorization_servers") or [] if i]
    if not issuers:
        raise ConfigurationError(
            f"{base} takes no company sign-in tokens (it has no VECTRIXDB_OIDC_API_AUDIENCE). "
            "Use an API key instead: VECTRIXDB_KEY, or --key-file"
        )
    issuer = _https(issuers[0], "The identity provider")
    found = get(issuer.rstrip("/") + "/.well-known/openid-configuration", "The identity provider")
    if str(found.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
        raise ConfigurationError(
            f"The identity provider at {issuer} calls itself {found.get('issuer')!r}; refusing a provider that is not who the server named"
        )
    provider = Provider(
        issuer=issuer,
        scopes=[str(s) for s in resource.get("scopes_supported") or []],
        authorization_endpoint=str(found.get("authorization_endpoint") or ""),
        token_endpoint=_https(str(found.get("token_endpoint") or ""), "Its token endpoint"),
        device_authorization_endpoint=str(found.get("device_authorization_endpoint") or ""),
    )
    return provider


def _scope(provider: Provider) -> str:
    # offline_access asks for a refresh token, so a sign-in lasts longer than one token.
    return " ".join(dict.fromkeys(["openid", "profile", "offline_access", *provider.scopes]))


def _take_tokens(login: Login, answer: Mapping[str, Any]) -> None:
    if answer.get("error") or not answer.get("access_token"):
        said = answer.get("error_description") or answer.get("error") or "no access token"
        raise ConfigurationError(f"The identity provider said: {clean(said)}")
    login.access_token = str(answer["access_token"])
    login.refresh_token = str(answer.get("refresh_token") or login.refresh_token or "")
    try:
        login.expires_at = time.time() + float(answer.get("expires_in") or 3600)
    except (TypeError, ValueError):
        login.expires_at = time.time() + 3600


def _new_login(url: str, provider: Provider, client_id: str, answer: Mapping[str, Any]) -> Login:
    login = Login(
        url=url.rstrip("/"),
        issuer=provider.issuer,
        client_id=client_id,
        token_endpoint=provider.token_endpoint,
        access_token="",
        scope=_scope(provider),
    )
    _take_tokens(login, answer)
    return login


def _verifier() -> Tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


class _Callback(BaseHTTPRequestHandler):
    """The one request the identity provider sends the browser back with."""

    got: Dict[str, str] = {}
    expected_state = ""

    def do_GET(self) -> None:  # noqa: N802 - the http.server name
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        if urlsplit(self.path).path != "/callback" or query.get("state") != self.expected_state:
            self.send_response(400)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not the sign-in this command started.")
            return
        type(self).got = query
        failed = query.get("error")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.end_headers()
        said = (
            f"Sign-in refused: {html.escape(query.get('error_description') or failed)}"
            if failed
            else "Signed in. You can close this tab and go back to the terminal."
        )
        self.wfile.write(f"<!doctype html><title>VectrixDB</title><p>{said}</p>".encode())

    def log_message(self, *_args: Any) -> None:  # the terminal is the command's
        return


def login_with_browser(
    url: str,
    client_id: str,
    *,
    provider: Optional[Provider] = None,
    open_browser: Optional[Callable[[str], bool]] = None,
    say: Callable[[str], None] = print,
    post: Callable[..., Dict[str, Any]] = _post_form,
    wait: float = BROWSER_WAIT,
) -> Login:
    """Sign in in a browser on this machine: authorization code with PKCE, answered on 127.0.0.1 (RFC 8252)."""
    provider = provider or discover(url)
    if not provider.authorization_endpoint:
        raise ConfigurationError(
            "The identity provider names no authorization endpoint. Try: vectrixdb login --device"
        )
    _https(provider.authorization_endpoint, "Its authorization endpoint")
    verifier, challenge = _verifier()
    state = secrets.token_urlsafe(24)
    handler: Any = type("Callback", (_Callback,), {"got": {}, "expected_state": state})
    # Bound to the loopback address alone, on a port the system picks.
    listener = HTTPServer(("127.0.0.1", 0), handler)
    redirect = f"http://localhost:{listener.server_port}/callback"
    address = (
        provider.authorization_endpoint
        + ("&" if "?" in provider.authorization_endpoint else "?")
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "scope": _scope(provider),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    thread = threading.Thread(target=_serve_until_answered, args=(listener, wait), daemon=True)
    thread.start()
    opened = False
    if open_browser is None:
        import webbrowser

        open_browser = webbrowser.open
    try:
        opened = bool(open_browser(address))
    except Exception:  # noqa: BLE001 - no browser here; the address is printed below
        opened = False
    say(
        (
            "A browser opened to sign you in. If it did not, open this address:\n  "
            if opened
            else "Open this address in a browser on this machine to sign in:\n  "
        )
        + address
    )
    thread.join(wait + 5)
    listener.server_close()
    got = handler.got
    if not got:
        raise ConfigurationError(
            "No sign-in came back in time. On a machine without a browser, try: vectrixdb login --device"
        )
    if got.get("error"):
        raise ConfigurationError(
            f"The identity provider refused: {clean(got.get('error_description') or got['error'])}"
        )
    answer = post(
        provider.token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": got.get("code", ""),
            "redirect_uri": redirect,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    return _new_login(url, provider, client_id, answer)


def _serve_until_answered(listener: HTTPServer, wait: float) -> None:
    listener.timeout = 1
    ends = time.monotonic() + wait
    handler: Any = listener.RequestHandlerClass
    while time.monotonic() < ends and not getattr(handler, "got", None):
        listener.handle_request()


def login_with_device(
    url: str,
    client_id: str,
    *,
    provider: Optional[Provider] = None,
    say: Callable[[str], None] = print,
    post: Callable[..., Dict[str, Any]] = _post_form,
    sleep: Callable[[float], None] = time.sleep,
) -> Login:
    """Sign in from any terminal: a code to type on any device with a browser (RFC 8628)."""
    provider = provider or discover(url)
    if not provider.device_authorization_endpoint:
        raise ConfigurationError(
            "The identity provider does not offer sign-in with a code. Sign in in a browser: vectrixdb login"
        )
    _https(provider.device_authorization_endpoint, "Its device endpoint")
    started = post(
        provider.device_authorization_endpoint, {"client_id": client_id, "scope": _scope(provider)}
    )
    if started.get("error") or not started.get("device_code"):
        said = started.get("error_description") or started.get("error") or "no device code"
        raise ConfigurationError(f"The identity provider said: {clean(said)}")
    where = clean(started.get("verification_uri") or started.get("verification_url") or "")
    say(
        f"To sign in, open {where} on any device and enter the code {clean(started.get('user_code', ''))}"
    )
    interval = float(started.get("interval") or 5)
    ends = time.monotonic() + float(started.get("expires_in") or 900)
    while time.monotonic() < ends:
        sleep(interval)
        answer = post(
            provider.token_endpoint,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": str(started["device_code"]),
                "client_id": client_id,
            },
        )
        error = answer.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        return _new_login(url, provider, client_id, answer)
    raise ConfigurationError(
        "The code expired before the sign-in finished. Run vectrixdb login again"
    )
