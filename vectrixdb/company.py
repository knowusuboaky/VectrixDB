"""A company's defaults for the client and the command line: its server, its sign-in, its network.

A company that wraps VectrixDB for its people, a package of its own on its
own package registry, says once where its server is and how to reach it,
and every ``vectrixdb.connect`` and every ``vectrixdb`` command run there
starts from that:

    # the wrapper's pyproject.toml
    [project.entry-points."vectrixdb.defaults"]
    acme = "acme_vectors.defaults:DEFAULTS"

    # acme_vectors/defaults.py
    DEFAULTS = {
        "server": "https://vectors.acme.com",
        "client_id": "0a1b2c3d-...",
        "ca_file": "system",
        "headers": {"Ocp-Apim-Subscription-Key": "${ACME_APIM_KEY}"},
        "command": "acme-vectors",
    }

Or, with no package, a file an administrator puts on every machine:
``/etc/vectrixdb/defaults.toml``, ``%ProgramData%\\vectrixdb\\defaults.toml``,
``/Library/Application Support/vectrixdb/defaults.toml``, or the file
``VECTRIXDB_DEFAULTS_FILE`` names (TOML, or JSON by its suffix).

What wins, last to first: the package's defaults, the machine's file, the
person's environment (``VECTRIXDB_URL`` and the rest), then what a call or a
command is given. Defaults make the wrapper convenient; they enforce
nothing. Who may do what is the server's to decide.

The headers and the certificate authority are sent only to the company's own
server: a call to any other address gets none of them, so a gateway's
subscription key never leaves for a host it was not meant for. A header's
value may name an environment variable, ``${NAME}``, so the file holds no
secret.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .exceptions import ConfigurationError

__all__ = [
    "ENTRY_POINT_GROUP",
    "FIELDS",
    "Defaults",
    "load",
    "machine_file",
]

# ============================================================================
# SETTINGS: where defaults come from, and what they may say
# ============================================================================

#: The entry point group a wrapper package registers its defaults under.
ENTRY_POINT_GROUP = "vectrixdb.defaults"
#: A defaults file of one's own, instead of the machine's.
FILE_ENV = "VECTRIXDB_DEFAULTS_FILE"
#: With more than one wrapper installed, the one whose defaults apply.
NAME_ENV = "VECTRIXDB_DEFAULTS"

#: Every field a company may set, and what it is.
FIELDS: Dict[str, str] = {
    "server": "The company's server: what a call or a command with no address goes to.",
    "client_id": "The company's client id for the command line, for vectrixdb login.",
    "scope": "The scopes vectrixdb login asks for, when the server's own are not the ones.",
    "ca_file": "The company's certificate authority: a PEM file or folder, or system.",
    "key_header": "The header a key goes in, when a gateway renames it.",
    "headers": "Headers sent with every request to the company's server, such as a gateway's subscription key. ${NAME} reads a variable.",
    "user_agent": "The wrapper's name and version, put before the client's own in User-Agent.",
    "command": "The wrapper's command, so every hint names it: acme-vectors login.",
    "credentials": "Where vectrixdb login keeps a sign-in: keyring or file.",
}

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class Defaults:
    """What the company said, merged, and where each part came from."""

    server: str = ""
    client_id: str = ""
    scope: str = ""
    ca_file: str = ""
    key_header: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    command: str = ""
    credentials: str = ""
    #: Where each field came from: "acme_vectors (package)", "/etc/vectrixdb/defaults.toml".
    came_from: Dict[str, str] = field(default_factory=dict)

    @property
    def program(self) -> str:
        """The command a hint names: the wrapper's, else vectrixdb."""
        return self.command or "vectrixdb"

    def is_home(self, url: str) -> bool:
        """Whether ``url`` is the company's own server: the same scheme, host and port."""
        return bool(self.server) and _origin(url) == _origin(self.server)

    def for_server(self, url: str) -> Dict[str, Any]:
        """The client options that go with a call to ``url``: everything for the company's server, nothing for another."""
        if not self.is_home(url):
            return {}
        options: Dict[str, Any] = {}
        if self.ca_file:
            options["verify"] = self.ca_file
        if self.key_header:
            options["key_header"] = self.key_header
        if self.headers:
            options["headers"] = expand(self.headers)
        if self.user_agent:
            options["user_agent"] = self.user_agent
        return options


def _origin(url: str) -> Tuple[str, str, int]:
    parts = urlsplit(url.strip())
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (parts.scheme.lower(), (parts.hostname or "").lower().rstrip("."), port)


def expand(headers: Mapping[str, str], env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Each header with its ${NAME} read from the environment. A variable that is not set is an error naming it."""
    env = os.environ if env is None else env
    out: Dict[str, str] = {}
    for name, value in headers.items():

        def read(found: Any, header: str = name) -> str:
            variable = found.group(1)
            if variable not in env:
                raise ConfigurationError(
                    f"the header {header} reads ${{{variable}}}, and {variable} is not set"
                )
            return str(env[variable])

        out[str(name)] = _VARIABLE.sub(read, str(value))
    return out


# ============================================================================
# WHERE DEFAULTS COME FROM
# ============================================================================
#
# INPUT   the installed packages' entry points; the machine's file; the environment
# OUTPUT  one Defaults, each field from the last place that set it
#
# A field nobody knows is refused by name, so a typo in a file that every
# machine reads is found the first time it is read.


def machine_file(env: Optional[Mapping[str, str]] = None) -> Path:
    """The defaults file: VECTRIXDB_DEFAULTS_FILE, else the machine's own place."""
    env = os.environ if env is None else env
    if env.get(FILE_ENV, "").strip():
        return Path(env[FILE_ENV].strip()).expanduser()
    if sys.platform == "win32":
        return Path(env.get("PROGRAMDATA") or r"C:\ProgramData") / "vectrixdb" / "defaults.toml"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/vectrixdb/defaults.toml")
    return Path("/etc/vectrixdb/defaults.toml")


def _read_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import tomllib as toml
        except ImportError:  # Python 3.9 and 3.10
            try:
                import tomli as toml  # type: ignore[no-redef,import-not-found,unused-ignore]
            except ImportError as exc:
                raise ConfigurationError(
                    f"{path} is TOML, which this Python reads with the tomli package: "
                    "pip install tomli, or write the file as JSON"
                ) from exc
        data = toml.loads(text)
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path} holds no table of defaults")
    # A file may put them under [vectrixdb], beside a wrapper's own settings.
    inner = data.get("vectrixdb")
    return dict(inner) if isinstance(inner, dict) else data


def _entry_points() -> List[Any]:
    from importlib.metadata import entry_points

    found = entry_points()
    if hasattr(found, "select"):
        return list(found.select(group=ENTRY_POINT_GROUP))
    return list(found.get(ENTRY_POINT_GROUP, []))  # type: ignore[attr-defined,unused-ignore]


def _from_package(env: Mapping[str, str]) -> Tuple[Dict[str, Any], str]:
    points = _entry_points()
    if not points:
        return {}, ""
    chosen = env.get(NAME_ENV, "").strip()
    if chosen:
        points = [p for p in points if p.name == chosen]
        if not points:
            raise ConfigurationError(f"{NAME_ENV}={chosen}, and no installed package registers it")
    elif len(points) > 1:
        names = ", ".join(sorted(p.name for p in points))
        raise ConfigurationError(
            f"more than one package sets VectrixDB defaults ({names}): choose one with {NAME_ENV}"
        )
    point = points[0]
    loaded = point.load()
    data = loaded() if callable(loaded) else loaded
    if not isinstance(data, Mapping):
        raise ConfigurationError(f"the {point.name} package's defaults are not a mapping")
    return dict(data), f"{point.name} (package)"


def _merge(into: Defaults, data: Mapping[str, Any], where: str) -> None:
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise ConfigurationError(
            f"{where} sets {', '.join(unknown)}, which are not defaults. Known: {', '.join(FIELDS)}"
        )
    for name, value in data.items():
        if name == "headers":
            if not isinstance(value, Mapping):
                raise ConfigurationError(f"{where}: headers is a table of name = value")
            # Each place adds its headers to the ones before; a name set twice is the later one.
            into.headers.update({str(k): str(v) for k, v in value.items()})
        else:
            setattr(into, name, str(value).strip())
        into.came_from[name] = where


def load(env: Optional[Mapping[str, str]] = None, *, packages: bool = True) -> Defaults:
    """The company's defaults on this machine: a wrapper package's, then the machine's file."""
    env = os.environ if env is None else env
    found = Defaults()
    if packages:
        data, where = _from_package(env)
        if data:
            _merge(found, data, where)
    path = machine_file(env)
    if path.is_file():
        try:
            data = _read_file(path)
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{path} could not be read: {exc}") from exc
        _merge(found, data, str(path))
    elif env.get(FILE_ENV, "").strip():
        raise ConfigurationError(f"{FILE_ENV} names {path}, which is not there")
    if found.server:
        parts = urlsplit(found.server)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigurationError(
                f"the server in {found.came_from['server']} must start https://: {found.server}"
            )
    return found
