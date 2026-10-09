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
  for. ``vectrixdb logout`` removes it. ``VECTRIXDB_CREDENTIALS=keyring`` keeps
  its tokens in the system keychain instead (Windows Credential Manager,
  macOS Keychain, the Secret Service), the file holding only what is not a
  secret.
- ``VECTRIXDB_CA_BUNDLE=system`` trusts what the operating system trusts.

A company's wrapper, or an administrator, presets any of these for every
command: ``VECTRIXDB_*`` lines in a machine-wide file
(``/etc/vectrixdb/defaults.env``, ``%ProgramData%\vectrixdb\defaults.env``,
``/Library/Application Support/vectrixdb/defaults.env``, or the file
``VECTRIXDB_DEFAULTS_FILE`` names), read before every command, a person's own
environment winning. ``VECTRIXDB_COMMAND`` names the wrapper's command in
every hint, and ``VECTRIXDB_USER_AGENT`` its name in every request.
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
import functools
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
    "pinned",
    "poster",
    "server_from",
]

_ON = {"1", "true", "yes", "on"}
#: A token this close to expiring is refreshed before it is used.
REFRESH_EARLY = 120
#: Where a server names the identity provider it trusts (RFC 9728).
WELL_KNOWN = "/.well-known/oauth-protected-resource"
#: How long a browser sign-in is waited for.
BROWSER_WAIT = 300
#: Characters a terminal acts on rather than prints: all controls but tab and newline.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def command() -> str:
    """The command a hint names: a wrapper's, from ``VECTRIXDB_COMMAND``, else ``vectrixdb``."""
    return clean(os.environ.get("VECTRIXDB_COMMAND", "").strip()) or "vectrixdb"


def machine_defaults(env: Optional[Mapping[str, str]] = None) -> Path:
    """The machine-wide presets file: ``VECTRIXDB_DEFAULTS_FILE``, else the platform's own place."""
    values = os.environ if env is None else env
    given = _env("VECTRIXDB_DEFAULTS_FILE", values)
    if given:
        return Path(given).expanduser()
    if os.name == "nt":
        return Path(_env("PROGRAMDATA", values) or r"C:\ProgramData") / "vectrixdb" / "defaults.env"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/vectrixdb/defaults.env")
    return Path("/etc/vectrixdb/defaults.env")


def apply_machine_defaults(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Read the machine-wide presets into the environment, what it already sets winning. Returns what was taken.

    Only ``VECTRIXDB_`` settings are taken, so a presets file cannot change
    ``PATH``, a proxy or anything else outside this command. A file named by
    ``VECTRIXDB_DEFAULTS_FILE`` that is not there is an error; the platform's
    own place may be empty.
    """
    from .settings import read_env_file

    target: Any = os.environ if environ is None else environ
    path = machine_defaults(target)
    if not path.is_file():
        if _env("VECTRIXDB_DEFAULTS_FILE", target):
            raise ConfigurationError(f"VECTRIXDB_DEFAULTS_FILE names {path}, which is not a file")
        return {}
    taken: Dict[str, str] = {}
    for name, value in read_env_file(path).items():
        if not name.startswith("VECTRIXDB_"):
            raise ConfigurationError(
                f"{path} sets {name}. The presets file holds VECTRIXDB_ settings alone"
            )
        if name not in target:
            target[name] = value
            taken[name] = value
    return taken


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
    if _env("VECTRIXDB_USER_AGENT", values):
        options["user_agent"] = _env("VECTRIXDB_USER_AGENT", values)
    bundle = ca_bundle or _env("VECTRIXDB_CA_BUNDLE", values)
    if bundle == "system":
        options["verify"] = "system"
    elif bundle:
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
    post = post or poster(options.get("verify", True))
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


#: The parts of a sign-in that are secrets: in the keychain when it is used.
_SECRETS = ("access_token", "refresh_token")
_KEYCHAIN = "vectrixdb"


def _keychain(env: Mapping[str, str]) -> Any:
    """The keyring module when ``VECTRIXDB_CREDENTIALS=keyring``; None for the file."""
    chosen = _env("VECTRIXDB_CREDENTIALS", env).lower()
    if chosen in ("", "file"):
        return None
    if chosen != "keyring":
        raise ConfigurationError(f"VECTRIXDB_CREDENTIALS is keyring or file, not {chosen!r}")
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        raise ConfigurationError(
            "VECTRIXDB_CREDENTIALS=keyring needs the keyring package: pip install keyring"
        ) from None
    if isinstance(keyring.get_keyring(), fail.Keyring):
        raise ConfigurationError(
            "VECTRIXDB_CREDENTIALS=keyring, and this machine has no keychain keyring can reach. "
            "Leave it unset to keep sign-ins in a file only you can read"
        )
    return keyring


class Logins:
    """The sign-ins saved on this machine, one per server address.

    The file holds every sign-in; with ``VECTRIXDB_CREDENTIALS=keyring`` it
    holds all but the tokens, which go to the system keychain under the
    service ``vectrixdb`` and the server's address.
    """

    def __init__(self, folder: Optional[Path] = None, keychain: Any = None) -> None:
        self.folder = folder or config_dir()
        self.path = self.folder / "logins.json"
        self.keychain = keychain if keychain is not None else _keychain(os.environ)

    def _read(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        text = _private_file(self.path, "The saved sign-ins")
        try:
            found = json.loads(text or "{}")
        except json.JSONDecodeError:
            raise ConfigurationError(
                f"{self.path} is not readable JSON. Remove it and sign in again: {command()} login"
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
        if row.get("in") == "keychain":
            if self.keychain is None:
                raise ConfigurationError(
                    f"The sign-in for {url} is in the system keychain: set VECTRIXDB_CREDENTIALS=keyring"
                )
            secrets_kept = self.keychain.get_password(_KEYCHAIN, url.rstrip("/"))
            if not secrets_kept:
                return None
            row = {**row, **json.loads(secrets_kept)}
        try:
            return Login(**{k: row[k] for k in Login.__dataclass_fields__ if k in row})
        except TypeError:
            return None

    def save(self, login: Login) -> None:
        every = self._read()
        row = asdict(login)
        address = login.url.rstrip("/")
        if self.keychain is not None:
            kept = {name: row.pop(name) for name in _SECRETS}
            self.keychain.set_password(_KEYCHAIN, address, json.dumps(kept))
            row["in"] = "keychain"
        every[address] = row
        self._write(every)

    def remove(self, url: str) -> bool:
        every = self._read()
        row = every.pop(url.rstrip("/"), None)
        if row is None:
            return False
        if isinstance(row, dict) and row.get("in") == "keychain" and self.keychain is not None:
            try:
                self.keychain.delete_password(_KEYCHAIN, url.rstrip("/"))
            except Exception:  # noqa: BLE001 - already gone from the keychain
                pass
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
                f"The sign-in saved for {url} has expired. Sign in again: {command()} login --url {url}"
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
                f"The sign-in saved for {url} could not be refreshed ({exc}). Sign in again: {command()} login --url {url}"
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


def _get_json(url: str, what: str, verify: Any = True) -> Dict[str, Any]:
    httpx = _http()
    try:
        response = httpx.get(url, timeout=20, follow_redirects=False, verify=_verify(verify))
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


def _post_form(url: str, form: Mapping[str, str], verify: Any = True) -> Dict[str, Any]:
    """POST a form to an identity provider; its JSON answer, errors included, as a dict."""
    httpx = _http()
    try:
        response = httpx.post(
            url,
            data={k: v for k, v in form.items() if v},
            headers={"accept": "application/json"},
            timeout=30,
            follow_redirects=False,
            verify=_verify(verify),
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


def _verify(verify: Any) -> Any:
    """What httpx is given to check certificates: always checked, against a company CA when one is named."""
    from .client import _tls

    return _tls(verify, None)


def poster(verify: Any = True) -> Callable[..., Dict[str, Any]]:
    """``_post_form`` checking certificates against ``verify``: a CA bundle's path, or True."""
    return functools.partial(_post_form, verify=verify)


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


def _same(a: str, b: str) -> bool:
    one, two = urlsplit(a.rstrip("/")), urlsplit(b.rstrip("/"))
    return (one.scheme.lower(), (one.netloc or "").lower(), one.path) == (
        two.scheme.lower(),
        (two.netloc or "").lower(),
        two.path,
    )


def discover(
    url: str,
    get: Optional[Callable[[str, str], Dict[str, Any]]] = None,
    *,
    verify: Any = True,
    well_known: str = WELL_KNOWN,
    mcp_path: str = "/mcp",
) -> Provider:
    """The identity provider ``url`` trusts, from its protected-resource document, and that provider's endpoints.

    The document has to name ``url`` itself as the resource (RFC 9728, section
    3.3), so a server cannot pass off another's sign-in as its own, and the
    provider has to call itself what the server named.
    """
    get = get or functools.partial(_get_json, verify=verify)
    base = url.rstrip("/")
    resource = get(base + well_known, "The server")
    named = str(resource.get("resource") or "")
    if not any(_same(named, mine) for mine in (base, base + "/mcp", base + mcp_path)):
        said = named[: -len("/mcp")] if named.endswith("/mcp") else named
        raise ConfigurationError(
            f"{base} says it is {clean(said) or 'nothing'}, so a sign-in got for it would not be its own. "
            f"Sign in with the address it gives: {command()} login --url {clean(said)}. "
            "If that is wrong, its admin sets VECTRIXDB_PUBLIC_URL to the address people use"
            if said
            else f"{base} names no resource in its protected-resource document, so it cannot be signed in to"
        )
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
            f"The identity provider at {issuer} calls itself {clean(found.get('issuer'))!r}; refusing a provider that is not who the server named"
        )
    provider = Provider(
        issuer=issuer,
        scopes=[str(s) for s in resource.get("scopes_supported") or []],
        authorization_endpoint=str(found.get("authorization_endpoint") or ""),
        token_endpoint=_https(str(found.get("token_endpoint") or ""), "Its token endpoint"),
        device_authorization_endpoint=str(found.get("device_authorization_endpoint") or ""),
    )
    return provider


def pinned(provider: Provider, scopes: str) -> bool:
    """Whether the scopes the server asks for are the ones ``scopes`` (VECTRIXDB_LOGIN_SCOPES) allows.

    Refused when the server asks for any other, since a token is good wherever
    its scopes say: a server naming another's would get a token for that one.
    False when nothing is pinned, so the person is asked instead.
    """
    allowed = scopes.replace(",", " ").split()
    if not allowed:
        return False
    other = [s for s in provider.scopes if s not in allowed]
    if other:
        raise ConfigurationError(
            f"The server asks for {', '.join(clean(s) for s in other)}, which VECTRIXDB_LOGIN_SCOPES "
            f"({' '.join(allowed)}) does not allow. Not signing in"
        )
    return True


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
    # A connection that sends nothing cannot hold the one listener up.
    timeout = 5

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
            f"The identity provider names no authorization endpoint. Try: {command()} login --device"
        )
    _https(provider.authorization_endpoint, "Its authorization endpoint")
    verifier, challenge = _verifier()
    state = secrets.token_urlsafe(24)
    handler: Any = type("Callback", (_Callback,), {"got": {}, "expected_state": state})
    # Bound to the loopback address alone, on a port the system picks.
    listener = HTTPServer(("127.0.0.1", 0), handler)
    # 127.0.0.1, not localhost: the name can resolve elsewhere (RFC 8252, 8.3).
    redirect = f"http://127.0.0.1:{listener.server_port}/callback"
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
            f"No sign-in came back in time. On a machine without a browser, try: {command()} login --device"
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
            f"The identity provider does not offer sign-in with a code. Sign in in a browser: {command()} login"
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
        f"The code expired before the sign-in finished. Run {command()} login again"
    )
