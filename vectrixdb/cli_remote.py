"""The command line, against a VectrixDB server: who calls, how they signed in, and the calls.

    vectrixdb login --server https://vectors.company.com          # sign in with the company, in a browser
    vectrixdb login --server https://vectors.company.com --key-file ~/.vectrixdb-key
    vectrixdb query "how long do refunds take" --name handbook --server https://vectors.company.com
    vectrixdb whoami

``--server``, or ``VECTRIXDB_URL``, sends ``list``, ``create``, ``delete``,
``ingest``, ``query``, ``stats`` and ``sources`` to a server instead of the
data on this machine. The caller is, first to last: ``--key-file``, the
``VECTRIXDB_KEY`` or ``VECTRIXDB_TOKEN`` variable, then what ``vectrixdb
login`` kept for that server. A key is never taken on the command line,
where the shell's history and the process list would keep it.

``login`` keeps a key, or a person's sign-in with the company's identity
provider (the device code flow, RFC 8628: a code to type in a browser, on
this machine or any other). What it keeps goes to the operating system's
keychain when the ``keyring`` package is installed, else to a file only
this user may read. A sign-in is renewed with its refresh token as it runs
out; ``logout`` forgets it.

A company's wrapper, or a defaults file on every machine, names the
server, the client id, the certificate authority and the gateway's headers
once (see :mod:`vectrixdb.company`); the person's own settings still win,
and every hint names the wrapper's command.

Anywhere a company's network sits: ``HTTPS_PROXY`` and ``NO_PROXY`` are
honoured, ``VECTRIXDB_CA_FILE`` names the company's certificate authority
(a file, a folder, or ``system`` for the operating system's own), and a key
never crosses a network over plain ``http://``.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "SERVER_ENV",
    "Credentials",
    "caller",
    "config_dir",
    "connect",
    "login_with_key",
    "login_with_device",
    "logout",
]

# ============================================================================
# SETTINGS: the variables the command line reads for a server
# ============================================================================

#: The server's address, when --server is left out.
SERVER_ENV = "VECTRIXDB_URL"
#: A key, or a person's access token, for scripts and CI.
KEY_ENV = "VECTRIXDB_KEY"
TOKEN_ENV = "VECTRIXDB_TOKEN"
#: The company's certificate authority: a file, a folder, or "system".
CA_ENV = "VECTRIXDB_CA_FILE"
#: Let a key cross a network over plain http://. Off; for a network you trust.
ALLOW_HTTP_ENV = "VECTRIXDB_ALLOW_HTTP"
#: Where login keeps what it keeps: "keyring", "file", or left out for keyring when installed.
STORE_ENV = "VECTRIXDB_CREDENTIALS"
#: The folder for the credentials file; left out, the platform's own place.
CONFIG_ENV = "VECTRIXDB_CONFIG_DIR"
#: The company's client id for the command line, at its identity provider.
CLIENT_ID_ENV = "VECTRIXDB_CLIENT_ID"

_TRUE = ("1", "true", "yes", "on")
#: Renew a sign-in this long before it runs out.
_EARLY = 60.0
_KEYRING_SERVICE = "vectrixdb"


class SignInError(Exception):
    """What went wrong signing in, in words to print."""


def company() -> Any:
    """The company's defaults on this machine: a wrapper package's, then the machine's file."""
    from . import company as _company
    from .exceptions import ConfigurationError

    try:
        return _company.load()
    except ConfigurationError as exc:
        raise SignInError(f"the company's defaults: {exc}") from exc


def program() -> str:
    """The command a hint names: the wrapper's, else vectrixdb. Never fails, since it only words a message."""
    try:
        return str(company().program)
    except Exception:  # noqa: BLE001 - a broken defaults file is reported where it is read
        return "vectrixdb"


# ============================================================================
# WHERE A SIGN-IN IS KEPT
# ============================================================================
#
# INPUT   a server's address; a key, or a person's tokens
# OUTPUT  kept in the keychain, or in a file only this user may read;
#         read back, or forgotten
#
# One entry a server. The file never holds a secret when the keychain does:
# it then holds only that the entry is in the keychain.


def config_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """The folder the command line keeps its settings in, the platform's own place."""
    env = os.environ if env is None else env
    if env.get(CONFIG_ENV):
        return Path(env[CONFIG_ENV]).expanduser()
    if sys.platform == "win32":
        return Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "vectrixdb"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "vectrixdb"
    return Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vectrixdb"


def _server_key(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _keyring() -> Any:
    """The keyring module, when it is installed and has a real keychain behind it."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - a broken backend is no keychain
        return None
    if isinstance(backend, fail.Keyring) or "null" in type(backend).__name__.lower():
        return None
    return keyring


class Credentials:
    """The sign-ins this user kept, one a server."""

    def __init__(self, folder: Optional[Path] = None, store: Optional[str] = None) -> None:
        self.folder = folder or config_dir()
        self.path = self.folder / "credentials.json"
        chosen = (
            (store or os.environ.get(STORE_ENV, "") or company().credentials or "").strip().lower()
        )
        if chosen not in ("", "keyring", "file"):
            raise SignInError(f"{STORE_ENV} is keyring or file, not {chosen!r}")
        self._keyring = None if chosen == "file" else _keyring()
        if chosen == "keyring" and self._keyring is None:
            raise SignInError(
                f"{STORE_ENV}=keyring and no keychain is reachable: pip install keyring"
            )

    @property
    def where(self) -> str:
        return "the system keychain" if self._keyring is not None else str(self.path)

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise SignInError(f"{self.path} could not be read: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _write(self, data: Dict[str, Any]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.folder, 0o700)
        temporary = self.path.with_name(self.path.name + ".tmp")
        # Made 0600 from the start, so the secret is never readable by another user, even briefly.
        handle = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2, sort_keys=True)
        os.replace(temporary, self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def servers(self) -> List[str]:
        return sorted(k for k in self._read() if k != "_last")

    def last(self) -> Optional[str]:
        """The server last signed in to, for whoami and logout with no --server."""
        value = self._read().get("_last")
        return str(value) if value else None

    def get(self, server: str) -> Optional[Dict[str, Any]]:
        entry = self._read().get(_server_key(server))
        if not isinstance(entry, dict):
            return None
        if entry.get("in") == "keyring":
            if self._keyring is None:
                raise SignInError(
                    f"the sign-in for {server} is in the system keychain, which is not reachable now"
                )
            secret = self._keyring.get_password(_KEYRING_SERVICE, _server_key(server))
            if not secret:
                return None
            return {**entry, **json.loads(secret)}
        return entry

    def put(self, server: str, entry: Mapping[str, Any]) -> None:
        data = self._read()
        name = _server_key(server)
        if self._keyring is not None:
            secrets = {k: v for k, v in entry.items() if k in _SECRETS}
            self._keyring.set_password(_KEYRING_SERVICE, name, json.dumps(secrets))
            data[name] = {
                **{k: v for k, v in entry.items() if k not in _SECRETS},
                "in": "keyring",
            }
        else:
            data[name] = dict(entry)
        data["_last"] = server.rstrip("/")
        self._write(data)

    def forget(self, server: str) -> bool:
        data = self._read()
        name = _server_key(server)
        entry = data.pop(name, None)
        if entry is None:
            return False
        if isinstance(entry, dict) and entry.get("in") == "keyring" and self._keyring is not None:
            try:
                self._keyring.delete_password(_KEYRING_SERVICE, name)
            except Exception:  # noqa: BLE001 - already gone from the keychain
                pass
        if _server_key(str(data.get("_last") or "")) == name:
            data.pop("_last", None)
        self._write(data)
        return True


#: The parts of an entry that are secrets; the rest says what kind of sign-in it is.
_SECRETS = frozenset({"key", "access_token", "refresh_token"})


# ============================================================================
# THE CONNECTION: TLS, PROXIES, AND PLAIN HTTP
# ============================================================================


def verify_setting(ca_file: Optional[str] = None, server: Optional[str] = None) -> Any:
    """What to check certificates against: --ca-file, else VECTRIXDB_CA_FILE, else the company's for its server."""
    chosen = (ca_file or os.environ.get(CA_ENV, "")).strip()
    if not chosen and server:
        chosen = str(company().for_server(server).get("verify") or "")
    return chosen or True


def server_options(server: str) -> Dict[str, Any]:
    """The company's headers, key header and name, for its own server; nothing for another."""
    return {k: v for k, v in company().for_server(server).items() if k != "verify"}


def allow_http(flag: bool = False) -> bool:
    return flag or os.environ.get(ALLOW_HTTP_ENV, "").strip().lower() in _TRUE


def _http(verify: Any, timeout: float = 30.0) -> Any:
    from .client import _httpx, tls

    return _httpx().Client(timeout=timeout, verify=tls(verify), follow_redirects=False)


def _must_be_https(url: str, what: str, may_be_http: bool) -> None:
    from urllib.parse import urlsplit

    from .client import _on_this_machine

    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and (may_be_http or _on_this_machine(parts.hostname or "")):
        return
    raise SignInError(f"{what} must be https://, and is {url}")


# ============================================================================
# SIGN IN: A KEY, OR A PERSON WITH THE COMPANY'S IDENTITY PROVIDER
# ============================================================================
#
# INPUT   the server; a key file, or the company's client id for the command line
# OUTPUT  the sign-in kept; who the server says the caller is
#
# The device code flow asks the server's protected-resource document
# (RFC 9728) which identity provider signs its people in, and that
# provider's discovery document where to ask for a code. Nothing about the
# provider is configured on this machine but the client id.


def read_key_file(path: str) -> str:
    """A key from a file: the first line, trimmed. A file anyone may read is refused off Windows."""
    source = Path(path).expanduser()
    if source.as_posix() == "-":
        key = sys.stdin.readline().strip()
    else:
        if not source.is_file():
            raise SignInError(f"no key file at {source}")
        if os.name != "nt" and source.stat().st_mode & 0o077:
            raise SignInError(f"{source} may be read by other users: chmod 600 {source}")
        key = (
            source.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            if source.stat().st_size
            else ""
        )
    if not key:
        raise SignInError(f"{path} holds no key")
    return key


def login_with_key(
    server: str,
    key: str,
    *,
    store: Optional[Credentials] = None,
    verify: Any = True,
    may_be_http: bool = False,
) -> Dict[str, Any]:
    """Keep a key for a server, once the server says it is good. Returns who it is."""
    from .client import Client

    with Client(
        server, key=key, verify=verify, allow_http=may_be_http, **server_options(server)
    ) as client:
        who = client.whoami()
    (store or Credentials()).put(server, {"kind": "key", "key": key})
    return who


def _discover(server: str, http: Any, may_be_http: bool) -> Dict[str, Any]:
    """The identity provider the server names, and where that provider takes a device code."""
    # The gateway's headers go to the server only, never to the identity provider.
    answer = http.get(
        server.rstrip("/") + "/.well-known/oauth-protected-resource",
        headers=server_options(server).get("headers") or {},
    )
    if answer.status_code != 200:
        raise SignInError(f"{server} did not say how to sign in (HTTP {answer.status_code})")
    document = answer.json()
    issuers = document.get("authorization_servers") or []
    if not issuers:
        raise SignInError(
            f"{server} takes keys only, no company sign-in: {program()} login --server {server} --key-file <file>"
        )
    issuer = str(issuers[0]).rstrip("/")
    _must_be_https(issuer, "the identity provider", may_be_http)
    found = http.get(issuer + "/.well-known/openid-configuration")
    if found.status_code != 200:
        raise SignInError(
            f"the identity provider {issuer} has no discovery document (HTTP {found.status_code})"
        )
    provider = found.json()
    device = provider.get("device_authorization_endpoint")
    token = provider.get("token_endpoint")
    if not device or not token:
        raise SignInError(
            f"the identity provider {issuer} does not offer the device code flow. "
            "Sign in with a key, or ask for a token from a script: VECTRIXDB_TOKEN"
        )
    _must_be_https(device, "the device endpoint", may_be_http)
    _must_be_https(token, "the token endpoint", may_be_http)
    return {
        "issuer": issuer,
        "device_endpoint": device,
        "token_endpoint": token,
        "scopes": [str(s) for s in document.get("scopes_supported") or []],
    }


def login_with_device(
    server: str,
    client_id: str,
    *,
    say: Callable[[str], None],
    scope: Optional[str] = None,
    store: Optional[Credentials] = None,
    verify: Any = True,
    may_be_http: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Sign a person in with a code typed in a browser, keep the tokens, and return who the server says they are."""
    from .client import Client

    if not client_id:
        raise SignInError(
            f"the company's client id for the command line is needed: --client-id, or {CLIENT_ID_ENV}"
        )
    http = _http(verify)
    try:
        found = _discover(server, http, may_be_http)
        wanted = scope or " ".join([*found["scopes"], "openid", "offline_access"])
        answer = http.post(found["device_endpoint"], data={"client_id": client_id, "scope": wanted})
        if answer.status_code != 200:
            raise SignInError(f"the identity provider refused a code: {_oauth_said(answer)}")
        code = answer.json()
        link = (
            code.get("verification_uri_complete")
            or code.get("verification_uri")
            or code.get("verification_url")
        )
        say(f"Open {link} and enter the code {code['user_code']}")
        interval = float(code.get("interval", 5))
        deadline = now() + float(code.get("expires_in", 900))
        while True:
            if now() > deadline:
                raise SignInError("the code ran out before it was entered: {program()} login again")
            sleep(interval)
            answer = http.post(
                found["token_endpoint"],
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": code["device_code"],
                    "client_id": client_id,
                },
            )
            if answer.status_code == 200:
                tokens = answer.json()
                break
            error = _oauth_error(answer)
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "access_denied":
                raise SignInError("the sign-in was declined in the browser")
            if error == "expired_token":
                raise SignInError("the code ran out before it was entered: {program()} login again")
            raise SignInError(f"the identity provider refused the sign-in: {_oauth_said(answer)}")
    finally:
        http.close()
    entry = _token_entry(tokens, now(), client_id, found["token_endpoint"], wanted)
    with Client(
        server,
        token=entry["access_token"],
        verify=verify,
        allow_http=may_be_http,
        **server_options(server),
    ) as client:
        who = client.whoami()
    (store or Credentials()).put(server, entry)
    return who


def _token_entry(
    tokens: Mapping[str, Any], at: float, client_id: str, endpoint: str, scope: str
) -> Dict[str, Any]:
    # The access token goes to the server as a bearer token; an ID token is not sent anywhere.
    access = tokens.get("access_token")
    if not access:
        raise SignInError("the identity provider answered without an access token")
    return {
        "kind": "token",
        "access_token": access,
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": at + float(tokens.get("expires_in", 3600)),
        "client_id": client_id,
        "token_endpoint": endpoint,
        "scope": scope,
    }


def _oauth_error(answer: Any) -> str:
    try:
        return str(answer.json().get("error") or "")
    except ValueError:
        return ""


def _oauth_said(answer: Any) -> str:
    try:
        body = answer.json()
    except ValueError:
        return f"HTTP {answer.status_code}"
    return str(body.get("error_description") or body.get("error") or f"HTTP {answer.status_code}")


def logout(server: str, store: Optional[Credentials] = None) -> bool:
    """Forget the sign-in kept for a server. False when there was none."""
    return (store or Credentials()).forget(server)


# ============================================================================
# THE CALLER, AND A CLIENT AS THEM
# ============================================================================


def _token_function(
    server: str,
    entry: Dict[str, Any],
    store: Credentials,
    verify: Any,
    now: Callable[[], float] = time.time,
) -> Callable[[], str]:
    """The access token, renewed with the refresh token as it runs out, the renewal kept."""
    held = dict(entry)

    def token() -> str:
        if now() < float(held.get("expires_at", 0)) - _EARLY:
            return str(held["access_token"])
        if not held.get("refresh_token"):
            raise SignInError(
                f"the sign-in for {server} ran out: {program()} login --server {server}"
            )
        http = _http(verify)
        try:
            answer = http.post(
                held["token_endpoint"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": held["refresh_token"],
                    "client_id": held["client_id"],
                    "scope": held.get("scope") or "",
                },
            )
        finally:
            http.close()
        if answer.status_code != 200:
            raise SignInError(
                f"the sign-in for {server} could not be renewed ({_oauth_said(answer)}): "
                f"{program()} login --server {server}"
            )
        renewed = _token_entry(
            answer.json(), now(), held["client_id"], held["token_endpoint"], held.get("scope") or ""
        )
        # A provider that does not rotate refresh tokens sends none back: keep the one there was.
        renewed["refresh_token"] = renewed["refresh_token"] or held["refresh_token"]
        held.update(renewed)
        store.put(server, held)
        return str(held["access_token"])

    return token


def caller(
    server: str,
    key_file: Optional[str] = None,
    store: Optional[Credentials] = None,
    verify: Any = True,
) -> Dict[str, Any]:
    """Who calls the server: --key-file, VECTRIXDB_KEY, VECTRIXDB_TOKEN, else what login kept."""
    if key_file:
        return {"key": read_key_file(key_file)}
    if os.environ.get(KEY_ENV, "").strip():
        return {"key": os.environ[KEY_ENV].strip()}
    if os.environ.get(TOKEN_ENV, "").strip():
        return {"token": os.environ[TOKEN_ENV].strip()}
    store = store or Credentials()
    entry = store.get(server)
    if not entry:
        return {}
    if entry.get("kind") == "key":
        return {"key": entry["key"]}
    return {"token": _token_function(server, entry, store, verify)}


def connect(
    server: str,
    *,
    key_file: Optional[str] = None,
    ca_file: Optional[str] = None,
    allow_plain_http: bool = False,
    store: Optional[Credentials] = None,
) -> Any:
    """A client for the server, as the caller the command line finds."""
    from .client import Client

    verify = verify_setting(ca_file, server)
    return Client(
        server,
        verify=verify,
        allow_http=allow_http(allow_plain_http),
        **server_options(server),
        **caller(server, key_file, store, verify),
    )


# ============================================================================
# THE COMMANDS, AGAINST A SERVER
# ============================================================================
#
# INPUT   a client, and what the command was given
# OUTPUT  what the local command prints, from the server's answer
#
# Each prints the way its local twin does, so a script reads both alike.


def files_to_send(sources: Sequence[str], glob: str) -> List[Path]:
    files: List[Path] = []
    for source in sources:
        p = Path(source)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob(glob) if f.is_file()))
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(source)
    return files


def whoami_lines(who: Mapping[str, Any], server: str) -> List[str]:
    lines = [f"Server: {server}"]
    if who.get("note"):
        lines.append(str(who["note"]))
    for label, key in (
        ("Signed in as", "who"),
        ("By", "method"),
        ("Role", "role"),
        ("Collections", "collections"),
        ("May", "actions"),
    ):
        value = who.get(key)
        if value in (None, "", [], "none"):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        lines.append(f"{label}: {value}")
    return lines
