"""Test a server's settings before it starts: what ``vectrixdb check`` runs.

Each check reads the settings through the code the server itself uses, so
what it reports is what a start would find: the sign-in settings through
``SignInConfig.from_env``, the brand through ``Brand.from_env``, the frame
setting through the security headers' own reader. It changes nothing. A
folder is asked whether it can be written, never written to, and the
sign-in store is opened only when it already exists, to see whether an admin
has been added.

A finding is ``ok``, ``warn`` or ``error``. An error is something a start
would refuse, or something that would fail the first request that needs it;
a warning is a setting that works and is probably not what was meant.

The Extraction findings say who reads each kind of file a server with these
settings is sent, from documents to YouTube addresses, and how to have one
read that nothing reads. A missing reader stops nothing at a start; the
first file that needs it is refused, so it is said here, as a warning. The
machine is asked the same way, changing nothing: a package is imported,
ffmpeg is run with ``-version`` and a short timeout, never with a file, and
a speech model is looked for in its cache, never fetched::

    from vectrixdb.check import run

    for finding in run("./vectrixdb_data"):
        print(finding.level, finding.area, finding.text)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from .exceptions import ConfigurationError
from .settings import SETTINGS, unknown

__all__ = ["Finding", "run"]


# ============================================================================
# SETTINGS: the local shapes, the backends and the caches a setting may name
# ============================================================================
#
# What a storage, backend or cache setting may say, so a check can tell a typo
# from a choice.

_LOCAL = ("localhost", "127.0.0.1", "::1")
_BACKENDS = ("sqlite", "memory", "cosmosdb", "azure_search", "lakebase", "delta_lake")
_CACHES = ("memory", "redis", "hybrid", "none")


# ============================================================================
# A FINDING, AND THE REPORT
# ============================================================================
#
# INPUT   what a check found
# OUTPUT  one finding: ok, warn or error, the part of the server, and what it
#         is; the report they collect in
#
# Most serious last, so the last line on the screen is the one to act on.


@dataclass
class Finding:
    """One thing the check found: ``ok``, ``warn`` or ``error``, the part of the server, and what it is."""

    level: str
    area: str
    text: str


class _Report:
    def __init__(self) -> None:
        self.findings: List[Finding] = []
        # _FILE settings already reported as wrong. A later check that reads one fails for the
        # same reason, and one cause is one error.
        self.said: Set[str] = set()

    def ok(self, area: str, text: str) -> None:
        self.findings.append(Finding("ok", area, text))

    def warn(self, area: str, text: str) -> None:
        self.findings.append(Finding("warn", area, text))

    def error(self, area: str, text: str) -> None:
        self.findings.append(Finding("error", area, text))

    def guard(self, area: str, step: Callable[[], None]) -> None:
        """Run one check; a ConfigurationError is the finding, anything else is reported, never raised."""
        try:
            step()
        except ConfigurationError as exc:
            if not any(name in str(exc) for name in self.said):
                self.error(area, str(exc))
        except Exception as exc:  # a check that breaks says so and the rest still run
            self.error(area, f"{type(exc).__name__}: {exc}")


# ============================================================================
# THE CHECKS: one a part of the server
# ============================================================================
#
# INPUT   the settings, read through the code the server itself uses
# OUTPUT  a finding a part: the numbers, the names, storage, the chunk and
#         collection stores, the keys, sign-in, the web, a gateway, the brand,
#         masking, the extractor, and the records
#
# Nothing is opened or loaded: what it reports is what a start would find,
# without the start.


def _writable(folder: Path) -> bool:
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return os.access(probe, os.W_OK)


def _number(
    report: _Report, env: Mapping[str, str], name: str, kind: Callable[[str], Any], low: float = 0
) -> None:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return
    if name.endswith("_PORT") and raw.lower().startswith(("tcp://", "udp://", "sctp://")):
        service = name[len("VECTRIXDB_") : -len("_PORT")].lower().replace("_", "-")
        report.error(
            "Settings",
            f"{name} is {raw}, which Kubernetes sets for a Service named vectrixdb-{service}. "
            "Set enableServiceLinks: false on the pod, or give the Service another name",
        )
        return
    try:
        value = kind(raw)
    except ValueError:
        report.error("Settings", f"{name} is {raw!r}, which is not a number")
        return
    if value <= low:
        least = "0 or more" if low == -1 else f"more than {low:g}"
        report.error("Settings", f"{name} is {raw}. It has to be {least}")


def _names(report: _Report, env: Mapping[str, str]) -> None:
    for name, near in unknown(env):
        report.warn(
            "Settings",
            f"{name} is not a setting VectrixDB reads"
            + (f". Did you mean {near}?" if near else ", so it does nothing"),
        )
    for setting in SETTINGS:
        if not setting.file_twin:
            continue
        inline, file_name = (
            str(env.get(setting.name, "") or "").strip(),
            str(env.get(setting.name + "_FILE", "") or "").strip(),
        )
        if inline and file_name:
            report.error(
                "Settings",
                f"{setting.name} and {setting.name}_FILE are both set. Keep one, so there is no doubt which is meant",
            )
            report.said.add(setting.name + "_FILE")
        elif file_name and not Path(file_name).is_file():
            report.error(
                "Settings", f"{setting.name}_FILE names {file_name}, which is not a file here"
            )
            report.said.add(setting.name + "_FILE")
    for name in (
        "VECTRIXDB_SESSION_HOURS",
        "VECTRIXDB_SESSION_IDLE_MINUTES",
        "VECTRIXDB_EXTRACTOR_TIMEOUT",
        "VECTRIXDB_MAX_MEMORY_PERCENT",
    ):
        _number(report, env, name, float)
    for name in (
        "VECTRIXDB_MAX_UPLOAD_BYTES",
        "VECTRIXDB_LAKEBASE_PORT",
        "VECTRIXDB_REDIS_PORT",
        "VECTRIXDB_LISTEN_PORT",
        "VECTRIXDB_EXTRACT_LISTEN_PORT",
    ):
        _number(report, env, name, int)
    # Counts that may be 0 but must be numbers: anything else in the retries
    # fails every upload to an extraction service, and in the other two stops
    # the service as it starts.
    for name in (
        "VECTRIXDB_EXTRACTOR_RETRIES",
        "VECTRIXDB_SPEECH_SPEAKERS",
        "VECTRIXDB_VIDEO_FRAMES",
    ):
        _number(report, env, name, int, low=-1)


def _storage(report: _Report, env: Mapping[str, str], path: Path) -> None:
    backend = str(env.get("VECTRIXDB_STORAGE_BACKEND", "") or "sqlite").strip().lower()
    if backend not in _BACKENDS:
        report.error(
            "Storage",
            f"VECTRIXDB_STORAGE_BACKEND is {backend!r}. It can be {', '.join(_BACKENDS[:-1])} or {_BACKENDS[-1]}",
        )
        return
    needs: Dict[str, List[List[str]]] = {
        "cosmosdb": [["VECTRIXDB_COSMOS_ENDPOINT"], ["VECTRIXDB_COSMOS_KEY"]],
        "lakebase": [
            ["VECTRIXDB_LAKEBASE_HOST"],
            ["VECTRIXDB_LAKEBASE_TOKEN", "VECTRIXDB_LAKEBASE_PASSWORD"],
        ],
        "delta_lake": [
            ["VECTRIXDB_DELTA_WORKSPACE_URL"],
            ["VECTRIXDB_DELTA_TOKEN"],
            ["VECTRIXDB_DELTA_WAREHOUSE_ID", "VECTRIXDB_DELTA_HTTP_PATH"],
        ],
        # The endpoint is the one setting it has no default for; the key may be
        # left out, and then it signs in as the machine.
        "azure_search": [["VECTRIXDB_AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_ENDPOINT"]],
    }
    missing = [
        " or ".join(any_of)
        for any_of in needs.get(backend, [])
        if not any(str(env.get(n, "") or "").strip() for n in any_of)
    ]
    if missing:
        report.error("Storage", f"{backend} needs {', and '.join(missing)}")
    elif backend == "memory":
        report.warn("Storage", "In memory: nothing the server holds survives a restart")
    elif backend == "sqlite":
        if path.exists():
            (report.ok if _writable(path) else report.error)(
                "Storage", f"{path} {'can' if _writable(path) else 'cannot'} be written"
            )
        else:
            (report.ok if _writable(path) else report.error)(
                "Storage",
                f"{path} does not exist yet, and {'will be made at the first start' if _writable(path) else 'cannot be made here'}",
            )
    else:
        report.ok("Storage", f"{backend}, with the settings it needs")
    _chunk_store(report, env)
    cache = str(env.get("VECTRIXDB_CACHE_BACKEND", "") or "memory").strip().lower()
    if cache not in _CACHES:
        report.error(
            "Cache",
            f"VECTRIXDB_CACHE_BACKEND is {cache!r}. It can be {', '.join(_CACHES[:-1])} or {_CACHES[-1]}",
        )
    strategy = str(env.get("VECTRIXDB_SCALING_STRATEGY", "") or "none").strip().lower()
    if strategy != "none":
        from .core.scaling import ScalingStrategy

        allowed = [s.value for s in ScalingStrategy]
        if strategy not in allowed:
            report.error(
                "Cache",
                f"VECTRIXDB_SCALING_STRATEGY is {strategy!r}. It can be {', '.join(allowed)}",
            )


def _chunk_store(report: _Report, env: Mapping[str, str]) -> None:
    """The address the collection pages read every chunk from, by its shape. Nothing is opened."""
    where = str(env.get("VECTRIXDB_CHUNK_STORE", "") or "").strip()
    if not where:
        return
    parts = urlsplit(where) if "://" in where else None
    names = [name for name in parts.path.split("/") if name] if parts is not None else []
    if parts is None or parts.scheme.lower() != "cosmos" or not parts.hostname or len(names) != 2:
        report.error(
            "Storage",
            "VECTRIXDB_CHUNK_STORE is not cosmos://<account>.documents.azure.com/<database>/<container>",
        )
        return
    import importlib.util

    try:
        installed = importlib.util.find_spec("azure.cosmos") is not None
    except ImportError:  # no azure package at all
        installed = False
    if not installed:
        report.error(
            "Storage", "VECTRIXDB_CHUNK_STORE needs azure-cosmos: pip install 'vectrixdb[azure]'"
        )
        return
    report.ok(
        "Storage",
        f"The collection pages read every chunk from Cosmos DB {parts.hostname}/{names[0]}/{names[1]}",
    )


def _collection_store(report: _Report, env: Mapping[str, str]) -> None:
    """The records store a server may read, by its shape and the extra it needs. Nothing is opened."""
    _records_store(
        report,
        env,
        "VECTRIXDB_COLLECTION_STORE",
        "Every collection's record, who may retrieve from it, who may see it and its masking, is read from",
    )


def _records_store(report: _Report, env: Mapping[str, str], setting: str, what: str) -> None:
    from .signin.keys import env_secret

    where = str(env_secret(env, setting) or "").strip()
    if not where:
        return
    scheme = where.split("://", 1)[0].lower() if "://" in where else "file"
    needs = {
        "cosmos": ("azure.cosmos", "azure"),
        "dynamodb": ("boto3", "aws"),
        "postgres": ("psycopg2", "postgres"),
        "postgresql": ("psycopg2", "postgres"),
    }
    if scheme not in ("file", "sqlite", *needs):
        report.error(
            "Storage",
            f"{setting} is a path, sqlite:///<path>, postgresql://<user>@<host>/<database>, "
            "cosmos://<account>.documents.azure.com/<database>/<container> or dynamodb://<table>?region=<region>",
        )
        return
    if scheme in needs:
        import importlib.util

        module, extra = needs[scheme]
        try:
            installed = importlib.util.find_spec(module) is not None
        except ImportError:
            installed = False
        if not installed:
            report.error("Storage", f"{setting} needs {module}: pip install 'vectrixdb[{extra}]'")
            return
    report.ok("Storage", f"{what} {where.split('?', 1)[0]}")


def _key(report: _Report, env: Mapping[str, str], name: str) -> bool:
    plain = str(env.get(name, "") or "").strip() or str(env.get(name + "_FILE", "") or "").strip()
    hashed = str(env.get(name + "_SHA256", "") or "").strip()
    if hashed and not re.fullmatch(r"[0-9a-fA-F]{64}", hashed):
        report.error("Keys", f"{name}_SHA256 is not a SHA-256: 64 hexadecimal characters")
    return bool(plain or hashed)


def _signin(
    report: _Report,
    env: Mapping[str, str],
    path: Path,
    store_opener: Optional[Callable[[Any], Any]],
) -> Any:
    from .signin import SignInConfig
    from .signin.records import describe_where
    from .signin.store import check_secrets

    config = SignInConfig.from_env(path, env)
    if config.enabled:
        check_secrets(config.secrets)
    full = _key(report, env, "VECTRIXDB_API_KEY")
    read_only = _key(report, env, "VECTRIXDB_READ_ONLY_API_KEY")
    if not config.enabled:
        if full or read_only:
            open_reads = str(env.get("VECTRIXDB_OPEN_READS", "") or "").strip().lower() not in (
                "0",
                "false",
                "no",
                "off",
            )
            if open_reads:
                report.warn(
                    "Access",
                    "API keys, and no sign-in, and a read needs no key: anybody who reaches the port "
                    "searches and reads every collection. Set VECTRIXDB_OPEN_READS=0 unless that is meant",
                )
            else:
                report.ok(
                    "Access", "API keys, and no sign-in: every call needs a key, scripts only"
                )
        elif str(env.get("VECTRIXDB_ALLOW_OPEN", "") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            report.warn(
                "Access",
                "No key and no sign-in, and VECTRIXDB_ALLOW_OPEN is set: anybody who reaches the port reads everything. Right only behind a gateway that asks",
            )
        else:
            report.ok(
                "Access",
                "No key and no sign-in: serve listens on this machine only. Set a key or turn sign-in on to listen on any other address",
            )
        return config
    report.ok("Sign-in", f"{' and '.join(config.methods)}, for {config.public_url}")
    if config.sso_pending:
        report.warn(
            "Sign-in",
            "Single sign-on is asked for and not set up: VECTRIXDB_OIDC_ISSUER and VECTRIXDB_OIDC_CLIENT_ID are empty. Until they are set, "
            "people on the People list sign in with a code by email"
            + ("" if config.own_passkeys else ", and nobody keeps a passkey"),
        )
    if config.guests:
        report.ok(
            "Sign-in",
            "Guests may search the collections an admin shares with everyone, 30 searches a minute from one address",
        )
    if "email" in config.methods:
        if config.smtp_url:
            report.ok(
                "Email",
                f"Sign-in links are sent through {urlsplit(config.smtp_url).hostname or 'the mail server'}",
            )
        elif config.local:
            report.ok(
                "Email",
                "No mail server: sign-in links go to the server's log, which is fine on this machine",
            )
        else:
            report.warn(
                "Email",
                "No mail server, so sign-in links are not sent. Make each one on the server: vectrixdb people reset <address>",
            )
    where = config.store_url or config.store_path
    report.ok("Sign-in", f"Sign-in state is kept in {describe_where(where)}")
    # With single sign-on an admin is whoever is in a group mapped to admin, and nobody is added by hand.
    oidc = config.oidc
    if oidc is not None:
        mapped = sorted(group for group, role in oidc.role_map.items() if role == "admin")
        if mapped or oidc.default_role == "admin":
            report.ok(
                "Sign-in",
                "Single sign-on makes an admin of "
                + (
                    f"anybody in {', '.join(mapped)}"
                    if mapped
                    else "everybody it lets in, by the default role"
                ),
            )
        elif "email" not in config.methods:
            report.warn(
                "Sign-in",
                'No group is mapped to admin, so nobody who signs in can manage people, keys or sharing. Map one: VECTRIXDB_OIDC_ROLE_MAP={"<group id>": "admin"}',
            )
        _who_may_sign_in(report, config, store_opener)
        if oidc.token_role:
            report.ok(
                "Sign-in",
                f"An app's access token is given the role {oidc.token_role}, whoever it is for",
            )
    if config.sso_recheck_days:
        report.ok(
            "Sign-in",
            f"A passkey or a code works for somebody who signed in with single sign-on in the last {config.sso_recheck_days} days",
        )
    glass = config.break_glass
    if glass is not None and glass.open():
        report.warn(
            "Sign-in",
            f"Emergency sign-in is on, for {glass.admin}, until {glass.until_iso}. Turn it off when the usual sign-in is back",
        )
        if store_opener is not None and not (
            config.store_url is None
            and config.store_path is not None
            and not Path(config.store_path).exists()
        ):
            store = store_opener(config)
            try:
                spent = store.emergency_spent(glass.admin, glass.mark)
            finally:
                store.close()
            if spent:
                report.error(
                    "Sign-in",
                    "The emergency password was used in an earlier emergency, and a password works for one, so the server will not start. "
                    "Make a new one and set its hash: vectrixdb break-glass hash",
                )
    if config.developer is not None:
        accounts = ", ".join(
            f"{name} ({role})" for name, role in sorted(config.developer.accounts.items())
        )
        report.ok("Sign-in", f"Developer Access, for this machine only: {accounts}")
    # The email list has admins of its own. VECTRIXDB_SIGNIN_USERS is added to it at every start,
    # for anybody on it the list has not got yet.
    listed = [email for email, role in config.users if role == "admin"]
    local_file = config.store_url is None and config.store_path is not None
    if "email" in config.methods:
        if local_file and not Path(config.store_path).exists():
            if listed:
                report.ok(
                    "Sign-in",
                    f"{', '.join(listed)} {'is' if len(listed) == 1 else 'are'} made an admin at the first start, from VECTRIXDB_SIGNIN_USERS",
                )
            else:
                report.warn(
                    "Sign-in",
                    "No admin yet, and the sign-in file is made at the first start. Then add the first: vectrixdb people add you@company.com --role admin",
                )
        elif store_opener is not None:
            store = store_opener(config)
            try:
                coming = [email for email in listed if store.person(email) is None]
                if coming and not store.admins():
                    report.ok(
                        "Sign-in",
                        f"{', '.join(coming)} {'is' if len(coming) == 1 else 'are'} made an admin at the next start, from VECTRIXDB_SIGNIN_USERS",
                    )
                elif not store.admins():
                    report.warn(
                        "Sign-in",
                        "No admin yet. Add the first on the server: vectrixdb people add you@company.com --role admin",
                    )
            finally:
                store.close()
    access = config.access_log
    from .append_log import is_blob_address

    if str(access).strip().lower() in ("stdout", "-"):
        report.ok("Access log", "Written to the server's output, for the platform to collect")
    elif is_blob_address(access):
        report.ok(
            "Access log",
            f"Appended to Blob at {str(access).split('?', 1)[0]}, one append blob a day, in a container that must keep what is written",
        )
    elif "://" in str(access):
        report.error(
            "Access log",
            "VECTRIXDB_ACCESS_LOG is a path, stdout, or https://<account>.blob.core.windows.net/<container>/<prefix>",
        )
    elif _writable(Path(access).parent):
        report.ok("Access log", f"Written to {access}")
    else:
        report.error(
            "Access log",
            f"{access} cannot be written, and a read that cannot be recorded is refused. Set VECTRIXDB_ACCESS_LOG=stdout on a read-only disk",
        )
    return config


def _who_may_sign_in(
    report: _Report, config: Any, store_opener: Optional[Callable[[Any], Any]]
) -> None:
    """Single sign-on needs somebody named, or the server will not start: the People list, the setting's list, or *."""
    oidc = config.oidc
    if oidc.allowed_emails == ("*",):
        report.warn(
            "Sign-in",
            "VECTRIXDB_OIDC_ALLOWED_EMAILS is *, so everybody in a mapped group signs in. Not everyone in a group should: put them on the People list",
        )
        return
    on_the_list = len(config.users)
    if (
        not on_the_list
        and store_opener is not None
        and not (
            config.store_url is None
            and config.store_path is not None
            and not Path(config.store_path).exists()
        )
    ):
        store = store_opener(config)
        try:
            on_the_list = len(store.people())
        finally:
            store.close()
    if on_the_list or oidc.allowed_emails:
        report.ok(
            "Sign-in",
            "Single sign-on lets in the People list, each with the role on their record"
            + (
                ", and the addresses in VECTRIXDB_OIDC_ALLOWED_EMAILS with their groups' role"
                if oidc.allowed_emails
                else ""
            ),
        )
    else:
        report.error(
            "Sign-in",
            "Single sign-on is on and nobody is named who may sign in, so the server will not start. Put them on the People list: "
            "VECTRIXDB_SIGNIN_USERS=you@company.com:admin, or vectrixdb people add you@company.com --role admin. Or name them in VECTRIXDB_OIDC_ALLOWED_EMAILS",
        )


def _web(report: _Report, env: Mapping[str, str], config: Any) -> None:
    from .api.security_headers import frame_ancestors_from_env

    origins = [
        o.strip() for o in str(env.get("VECTRIXDB_CORS_ORIGINS", "") or "").split(",") if o.strip()
    ]
    if config is not None and config.enabled and origins:
        if "*" in origins:
            report.error(
                "Browsers",
                "VECTRIXDB_CORS_ORIGINS is * while sign-in is on, which would let any website read this server as whoever is signed in. Name the origins",
            )
        for origin in origins:
            if origin != "*" and not re.fullmatch(r"https?://[^/\s]+", origin):
                report.error(
                    "Browsers",
                    f"VECTRIXDB_CORS_ORIGINS names {origin!r}. Name an origin: https://host, with no path",
                )
    frame = frame_ancestors_from_env(env)
    if frame != "'none'":
        report.ok("Browsers", f"The dashboard may be shown in a frame by {frame}")


def _gateway(report: _Report, env: Mapping[str, str]) -> None:
    """Behind APIM, an API gateway or any reverse proxy.

    Two things decide whether a deployment behind one behaves: the path the
    gateway serves the app under, and whose ``X-Forwarded-For`` may be
    believed. Both are quiet when wrong, so they are said here.
    """
    from urllib.parse import urlsplit

    from .api.forwarded import parse_proxies
    from .api.server import root_path_from_env

    root = root_path_from_env(dict(env))
    public_path = urlsplit(str(env.get("VECTRIXDB_PUBLIC_URL", "") or "").strip()).path.rstrip("/")
    named = str(env.get("VECTRIXDB_ROOT_PATH", "") or "").strip()
    if root:
        report.ok("Gateway", f"Served under {root}, so links and the OpenAPI document carry it")
        if named and public_path and public_path != root:
            report.error(
                "Gateway",
                f"VECTRIXDB_ROOT_PATH is {root} and VECTRIXDB_PUBLIC_URL's path is {public_path}. "
                "They are the same path seen twice, so one of them sends people somewhere the app is not",
            )
    from .api.gateway import DEFAULT_KEY_HEADER, DEFAULT_TOKEN_HEADER, Gateway
    from .api.server import served_paths
    from .exceptions import ConfigurationError

    try:
        gateway = Gateway.from_env(dict(env), root=root)
        gateway.check(served_paths())
    except ConfigurationError as exc:
        report.error("Gateway", str(exc))
        return
    if gateway.prefix:
        report.ok(
            "Gateway", f"Every route lives under {gateway.prefix}, and answers with it or without"
        )
    for name, where in sorted(gateway.paths.items()):
        report.ok(
            "Gateway",
            f"{name} is published under {where}: a caller asks {gateway.visible('/' + name)}",
        )
    if gateway.key_header != DEFAULT_KEY_HEADER:
        report.ok("Gateway", f"An app's key arrives in {gateway.key_header}")
    if gateway.token_header != DEFAULT_TOKEN_HEADER:
        report.ok(
            "Gateway",
            f"A person's access token arrives in {gateway.token_header}, and Authorization is left to the gateway",
        )
    try:
        proxies = parse_proxies(str(env.get("VECTRIXDB_TRUSTED_PROXIES", "") or ""))
    except ValueError as exc:
        report.error("Gateway", str(exc))
        return
    if proxies:
        report.ok(
            "Gateway", "X-Forwarded-For is believed from " + ", ".join(str(n) for n in proxies)
        )
    elif root or public_path or gateway.shaped:
        report.warn(
            "Gateway",
            "No VECTRIXDB_TRUSTED_PROXIES, so every request counts as coming from the gateway: "
            "one person's failed sign-ins lock out everybody, and guests share one rate limit. "
            "Name the proxy's addresses, such as 10.0.0.0/8",
        )


def _brand(report: _Report, env: Mapping[str, str]) -> None:
    from .brand import Brand

    brand = Brand.from_env(env)
    banner = brand.banner()
    if banner:
        report.ok("Brand", banner)


def _masking(report: _Report, env: Mapping[str, str], machine: Any) -> None:
    """The engine VECTRIXDB_MASKING_ENGINE names, chosen as ``engine_from_env`` chooses it, and what its first document needs.

    A start checks the engine's name and settings; what the engine needs to
    run, a spaCy model, azure-identity, boto3, is found missing only by the
    first document masked, so it is said here. Nothing is loaded.
    """
    from .masking import ENGINES, languages_from_env
    from .masking.engines import SPACY_MODELS

    name = str(env.get("VECTRIXDB_MASKING_ENGINE", "") or "auto").strip().lower()
    if name not in ENGINES:
        report.error(
            "Masking",
            f"VECTRIXDB_MASKING_ENGINE is {name!r}. It can be {', '.join(ENGINES[:-1])} or {ENGINES[-1]}",
        )
        return
    endpoint = str(env.get("AZURE_LANGUAGE_ENDPOINT", "") or "").strip()
    named = languages_from_env(env)
    languages = ", ".join(named)
    presidio = machine.imports("presidio_analyzer")[0] == "ok"
    # Presidio reads each language with its spaCy model, found and never loaded here.
    models = {lang: SPACY_MODELS.get(lang, f"{lang}_core_news_lg") for lang in named}
    unready = [lang for lang, model in models.items() if not machine.installed(model)]
    fetch = "vectrixdb download-models --type masking"
    if name == "auto":
        # Presidio only for the languages whose model is here: auto never sends anyone to download one.
        ready = [lang for lang in named if lang not in unready]
        if endpoint:
            name = "language"
        elif presidio and ready and unready:
            report.warn(
                "Masking",
                f"Presidio in this process, for {', '.join(ready)}. {', '.join(unready)} "
                f"{'has' if len(unready) == 1 else 'have'} no spaCy model here, so the patterns alone mask "
                f"{'it' if len(unready) == 1 else 'them'}: {fetch}",
            )
            return
        elif presidio and ready:
            name = "presidio"
        elif presidio:
            report.warn(
                "Masking",
                f"Presidio is installed and no spaCy model for {languages} is, so the patterns alone mask: {fetch}",
            )
            return
        else:
            name = "regex"
    if name == "language":
        if not endpoint:
            report.error(
                "Masking",
                "VECTRIXDB_MASKING_ENGINE=language needs AZURE_LANGUAGE_ENDPOINT, the Language resource's address",
            )
            return
        if not endpoint.startswith("https://"):
            report.error(
                "Masking",
                f"AZURE_LANGUAGE_ENDPOINT is {endpoint!r}, and it is https://<name>.cognitiveservices.azure.com",
            )
            return
        key = bool(
            str(
                env.get("AZURE_LANGUAGE_KEY", "") or env.get("AZURE_LANGUAGE_KEY_FILE", "") or ""
            ).strip()
        )
        if not key and machine.imports("azure.identity")[0] != "ok":
            report.error(
                "Masking",
                "AZURE_LANGUAGE_KEY is empty, so Azure AI Language is called as the managed identity, which needs "
                "azure-identity: pip install 'vectrixdb[azure]'. Until then the first document masked fails",
            )
            return
        report.ok(
            "Masking",
            f"Azure AI Language at {endpoint}, {'with its key' if key else 'through the managed identity'}, for {languages}; the patterns run after it",
        )
    elif name == "presidio":
        if not presidio:
            report.error(
                "Masking",
                "VECTRIXDB_MASKING_ENGINE=presidio needs Presidio: pip install 'vectrixdb[masking]', then vectrixdb download-models --type masking",
            )
            return
        missing = [models[lang] for lang in unready]
        if missing:
            report.error(
                "Masking",
                f"VECTRIXDB_MASKING_ENGINE=presidio, and the spaCy model{'s' if len(missing) > 1 else ''} "
                f"{', '.join(missing)} {'are' if len(missing) > 1 else 'is'} not installed, so the first document "
                f"masked fails: {fetch}, or set VECTRIXDB_MASKING_LANGUAGES to the languages whose models are",
            )
            return
        report.ok(
            "Masking", f"Presidio in this process, for {languages}; the patterns run after it"
        )
    elif name == "comprehend":
        region = str(env.get("AWS_REGION", "") or env.get("AWS_DEFAULT_REGION", "") or "").strip()
        if not region:
            report.error("Masking", "VECTRIXDB_MASKING_ENGINE=comprehend needs AWS_REGION")
            return
        if machine.imports("boto3")[0] != "ok":
            report.error(
                "Masking",
                "Amazon Comprehend is called through boto3, which does not import here: pip install 'vectrixdb[aws]'. "
                "Until then the first document masked fails",
            )
            return
        report.ok(
            "Masking",
            f"Amazon Comprehend in {region}, English and Spanish; the patterns run after it, and alone for {languages} it does not cover",
        )
    else:
        report.ok(
            "Masking",
            "The patterns alone: emails, phone numbers, cards, national ids, keys and connection strings by their shape",
        )


def _extractor(report: _Report, env: Mapping[str, str]) -> None:
    """The shape of the settings that send files to an extraction service. What goes there is said below."""
    url = str(env.get("VECTRIXDB_EXTRACTOR_URL", "") or "").strip()
    routes = str(env.get("VECTRIXDB_EXTRACTOR_ROUTES", "") or "").strip()
    body = str(env.get("VECTRIXDB_EXTRACTOR_BODY", "") or "raw").strip()
    if routes:
        try:
            parsed = json.loads(routes)
        except ValueError as exc:
            report.error("Extraction", f"VECTRIXDB_EXTRACTOR_ROUTES is not JSON: {exc}")
        else:
            if not isinstance(parsed, dict):
                report.error(
                    "Extraction",
                    'VECTRIXDB_EXTRACTOR_ROUTES is JSON but not a mapping of suffix to route: {".pdf": "/extract/pdf"}',
                )
    if body not in ("raw", "multipart"):
        report.error(
            "Extraction", f"VECTRIXDB_EXTRACTOR_BODY is {body!r}. It can be raw or multipart"
        )
    asked = str(env.get("VECTRIXDB_EXTRACTOR_MASK", "") or "").strip().lower()
    if asked and asked not in ("1", "yes", "true", "on", "0", "no", "false", "off", "all"):
        from .masking import types_of

        try:
            types_of(asked)
        except Exception as exc:  # noqa: BLE001 - said as the setting's own error
            report.error("Extraction", f"VECTRIXDB_EXTRACTOR_MASK: {exc}")
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            report.error(
                "Extraction",
                f"VECTRIXDB_EXTRACTOR_URL is {url!r}, which is not an http or https address",
            )
        elif parts.scheme == "http" and parts.hostname not in _LOCAL:
            report.warn("Extraction", f"Documents go to {parts.hostname} over plain http")


def _records(report: _Report, env: Mapping[str, str], path: Path, config: Any) -> None:
    from .audit import audit_where, describe_audit_store

    audit = audit_where(dict(env)) or ""
    if audit:
        named = (
            "VECTRIXDB_AUDIT_STORE"
            if str(
                env.get("VECTRIXDB_AUDIT_STORE", "")
                or env.get("VECTRIXDB_AUDIT_STORE_FILE", "")
                or ""
            ).strip()
            else "VECTRIXDB_AUDIT_JSONL"
        )
        scheme = audit.split("://", 1)[0].lower() if "://" in audit else ""
        days = str(env.get("VECTRIXDB_AUDIT_RETAIN_DAYS", "") or "").strip()
        from .append_log import is_blob_address

        if (
            named == "VECTRIXDB_AUDIT_STORE"
            and str(env.get("VECTRIXDB_AUDIT_JSONL", "") or "").strip()
        ):
            report.warn(
                "Audit",
                "VECTRIXDB_AUDIT_STORE and VECTRIXDB_AUDIT_JSONL are both set: decisions go to VECTRIXDB_AUDIT_STORE, and the other is ignored",
            )
        if scheme and scheme not in ("s3", "postgres", "postgresql") and not is_blob_address(audit):
            report.error(
                "Audit",
                f"{named} is a path, https://<account>.blob.core.windows.net/<container>/<prefix>, s3://<bucket>/<prefix> or postgresql://<user>@<host>/<database>",
            )
        elif scheme == "s3" and not days.isdigit():
            report.error(
                "Audit",
                "An s3:// audit store keeps each record under Object Lock for VECTRIXDB_AUDIT_RETAIN_DAYS days, which has no default",
            )
        elif not scheme and not _writable(Path(audit).parent):
            report.error("Audit", f"{audit} cannot be written")
        elif not str(env.get("VECTRIXDB_AUDIT_QUERY_KEY", "") or "").strip() and not (
            config is not None and config.enabled
        ):
            report.warn(
                "Audit",
                f"{named} is set and nothing is written to it: with sign-in off it needs VECTRIXDB_AUDIT_QUERY_KEY",
            )
        else:
            report.ok(
                "Audit",
                f"Search decisions under a policy are written to {describe_audit_store(audit)}",
            )
    where = str(env.get("VECTRIXDB_EVALUATIONS", "") or "").strip()
    if where:
        from .evaluation import report_store

        try:
            report_store(where)
        except ImportError as exc:
            report.error("Evaluation", str(exc))
        else:
            report.ok("Evaluation", f"Runs are read from {where}")


# ============================================================================
# THE MACHINE: what is installed here, asked without changing anything
# ============================================================================
#
# INPUT   a module's name, a program and its version flag, a speech model
# OUTPUT  whether the module is there and imports, and why not; a
#         distribution's version; the ffmpeg binaries imageio-ffmpeg tries; what
#         a program answers; whether a speech model is already downloaded
#
# A reader that is missing is found out at the first file, not at a start, so
# the machine itself is asked. A package is imported, the one way to know a
# binary wheel works here; a program is run with its version flag and a short
# timeout; a model is looked for in its cache and never fetched. A test hands
# in a machine of its own.

#: Where imageio-ffmpeg finds each ffmpeg it tries, in the words a finding uses.
FFMPEG_OWN = "the one imageio-ffmpeg ships"
FFMPEG_CONDA = "conda's"
FFMPEG_ON_PATH = "the one on PATH"
FFMPEG_NAMED = "the one IMAGEIO_FFMPEG_EXE names"


class _Machine:
    """What this machine has. Nothing is written, fetched or loaded to answer."""

    def installed(self, module: str) -> bool:
        """Is ``module`` there to import? Found, not imported."""
        import importlib.util

        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            return False

    def imports(self, module: str) -> Tuple[str, str]:
        """``("ok", "")``, ``("missing", "")``, or ``("broken", why)`` for one installed that does not import."""
        import importlib

        if not self.installed(module):
            return "missing", ""
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - whatever stopped the import is the reason
            return "broken", f"{type(exc).__name__}: {exc}"
        return "ok", ""

    def version(self, distribution: str) -> str:
        """A distribution's installed version, or an empty string."""
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(distribution)
        except PackageNotFoundError:
            return ""

    def ffmpeg_binaries(self) -> List[Tuple[str, str]]:
        """The ffmpeg binaries imageio-ffmpeg tries, in its order, each with where it is from.

        Its own first, then conda's, then the one on PATH; it takes the first
        that answers ``-version``. IMAGEIO_FFMPEG_EXE comes before all three
        and is taken without being tried.
        """
        found: List[Tuple[str, str]] = []
        try:
            from importlib.resources import files

            own = [
                entry
                for entry in files("imageio_ffmpeg.binaries").iterdir()
                if entry.name.startswith("ffmpeg") and entry.is_file()
            ]
        except Exception:  # noqa: BLE001 - not installed, or no binary for this platform
            own = []
        if own:
            found.append((str(own[0]), FFMPEG_OWN))
        conda = (
            Path(sys.prefix, "Library", "bin", "ffmpeg.exe")
            if os.name == "nt"
            else Path(sys.prefix, "bin", "ffmpeg")
        )
        if conda.is_file():
            found.append((str(conda), FFMPEG_CONDA))
        on_path = shutil.which("ffmpeg")
        if on_path:
            found.append((on_path, FFMPEG_ON_PATH))
        return found

    def answers(self, argv: Sequence[str], timeout: float = 10.0) -> Tuple[bool, str]:
        """Did the program exit 0 within ``timeout`` seconds? With its first line, or why not."""
        try:
            done = subprocess.run(  # noqa: S603 - a fixed argv, a version flag, no shell
                list(argv), capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return False, f"it did not answer within {timeout:g} seconds"
        except OSError as exc:
            return False, exc.strerror or type(exc).__name__
        if done.returncode != 0:
            said = (done.stderr or done.stdout).decode("utf-8", errors="replace").split("\n")
            last = [line.strip() for line in said if line.strip()][-1:]
            return False, f"it exited with {done.returncode}" + (f": {last[0]}" if last else "")
        return True, done.stdout.decode("utf-8", errors="replace").strip().split("\n")[0].strip()

    def whisper_model_here(self, model: str) -> bool:
        """Is faster-whisper's model already downloaded? The Hugging Face cache is read; nothing is fetched."""
        from faster_whisper import download_model

        try:
            download_model(model, local_files_only=True)
        except Exception:  # noqa: BLE001 - not in the cache, however this huggingface_hub says so
            return False
        return True


# ============================================================================
# EXTRACTION: who reads each kind of file, and what each reader needs
# ============================================================================
#
# INPUT   the settings, and the machine: the packages that import, an ffmpeg
#         that runs, a speech model already downloaded
# OUTPUT  a finding a kind of input, saying who reads it on a server with
#         these settings or what would have it read: documents with a text
#         layer, scanned pages, pictures, recordings, videos, YouTube
#         addresses; then what an extraction service started with these
#         settings reads with, and what that needs
#
# The server reads a file by its suffix. A suffix the extractor routes name
# goes to the extraction service, and the rest are read here by the readers
# load_bytes picks. A reader that is missing stops nothing at a start: the
# first file that needs it is refused. So it is said here, with what fixes it.

_AREA = "Extraction"
_YES = ("1", "true", "yes", "on")
_SERVICE = "an extraction service started with these settings"

#: The name a package installs by, where it differs from the name it imports by.
_PACKAGES = {"docx": "python-docx", "fitz": "PyMuPDF"}

#: What each built-in reader imports: a PDF is read by PDFium, else pypdf, else PyMuPDF.
_DOCUMENT_READERS = {
    "pdf": ("pypdfium2", "pypdf", "fitz"),
    "docx": ("docx",),
    "doc": ("olefile",),
    "xlsx": ("openpyxl",),
    "xls": ("xlrd",),
}


def _listed(items: Sequence[str]) -> str:
    """``a, b and c``."""
    items = list(items)
    return f"{', '.join(items[:-1])} and {items[-1]}" if len(items) > 1 else "".join(items)


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _host(address: str) -> str:
    return urlsplit(address).hostname or address


def _routes(report: _Report, env: Mapping[str, str]) -> Tuple[str, Optional[Dict[str, str]]]:
    """Where the server sends files to be read, as its documents route builds it.

    ``(host, routes)``; ``("", {})`` when it sends none anywhere; ``("", None)``
    when the settings fail every upload, which is then said once.
    """
    if not str(env.get("VECTRIXDB_EXTRACTOR_URL", "") or "").strip():
        return "", {}
    from .api.documents import DEFAULT_ROUTES
    from .extract import HttpExtractor

    # The settings whose own findings, when wrong, already say why every upload fails.
    named = (
        "VECTRIXDB_EXTRACTOR_URL",
        "VECTRIXDB_EXTRACTOR_ROUTES",
        "VECTRIXDB_EXTRACTOR_BODY",
        "VECTRIXDB_EXTRACTOR_MASK",
        "VECTRIXDB_EXTRACTOR_RETRIES",
        "VECTRIXDB_EXTRACTOR_TIMEOUT",
    )
    try:
        service = HttpExtractor.from_environment(env, routes=DEFAULT_ROUTES)
    except ConfigurationError as exc:
        if not any(f.level == "error" and any(n in f.text for n in named) for f in report.findings):
            report.error(
                _AREA, f"{exc}. Every file sent to the server is refused until it is fixed"
            )
        return "", None
    if service is None:  # pragma: no cover - the address was there a line ago
        return "", {}
    return _host(service.base_url), dict(service.routes)


def _ffmpeg(
    env: Mapping[str, str], machine: Any
) -> Tuple[str, str, str, List[Tuple[str, str, str]]]:
    """The ffmpeg imageio-ffmpeg hands the video reader, tried as it tries them.

    ``(exe, where it is from, its version, the ones before it that did not
    run)``, with an empty ``exe`` when none runs.
    """
    named = str(env.get("IMAGEIO_FFMPEG_EXE", "") or "").strip()
    tried = [(named, FFMPEG_NAMED)] if named else machine.ffmpeg_binaries()
    failed: List[Tuple[str, str, str]] = []
    for exe, where in tried:
        ran, said = machine.answers([exe, "-version"])
        if ran:
            found = re.search(r"ffmpeg version (\S+)", said)
            return exe, where, found.group(1) if found else said, failed
        failed.append((exe, where, said))
    return "", "", "", failed


def _whisper(env: Mapping[str, str], machine: Any, model: str) -> Tuple[str, str]:
    """Whether faster-whisper reads a recording here, and why not.

    ``ok``; ``download``, its model fetched from Hugging Face at the first
    recording; ``offline``, refused, since the model is not here and
    VECTRIXDB_OFFLINE refuses the download; ``missing``; or ``broken``.
    """
    state, why = machine.imports("faster_whisper")
    if state != "ok":
        return state, why
    if machine.whisper_model_here(model):
        return "ok", ""
    offline = str(env.get("VECTRIXDB_OFFLINE", "") or "").strip().lower() in _YES
    return ("offline" if offline else "download"), ""


def _readers(report: _Report, env: Mapping[str, str], machine: Any) -> None:
    """Who reads each kind of file a server with these settings is sent, or what would have it read."""
    from .extract.engines import Whisper, _whisper_fetch
    from .ingest import SUFFIX_KINDS

    host, routes = _routes(report, env)
    if routes is None:
        return
    sending: Dict[str, str] = routes

    def split(kinds: Sequence[str]) -> Tuple[List[str], List[str]]:
        """The suffixes of these kinds read here, and those sent to the service."""
        suffixes = [suffix for suffix, kind in SUFFIX_KINDS.items() if kind in kinds]
        sent = [s for s in suffixes if s in sending or "*" in sending]
        return [s for s in suffixes if s not in sent], sent

    def opening(label: str, local: List[str], sent: List[str]) -> Tuple[str, str, str]:
        """What goes to the service, what the rest is called, and the rest named when some went."""
        if not sent:
            return "", label, ""
        head = f"{_listed(sent)} {'goes' if len(sent) == 1 else 'go'} to {host}; "
        return head, "the rest", f" ({_listed(local)})"

    def fix(extra: str, service: str) -> str:
        if host:
            return f"pip install 'vectrixdb[{extra}]', or add them to VECTRIXDB_EXTRACTOR_ROUTES"
        return (
            f"pip install 'vectrixdb[{extra}]', or set VECTRIXDB_EXTRACTOR_URL to an extraction "
            f"service that has {service}"
        )

    # Documents with a text layer: the built-in readers, each with the package it imports.
    kinds = [
        k for k in dict.fromkeys(SUFFIX_KINDS.values()) if k not in ("image", "audio", "video")
    ]
    local, sent = split(kinds)
    if not local:
        report.ok(_AREA, f"Documents go to {host}")
    else:
        head, rest, _ = opening(
            "PDFs with a text layer, Word, Excel, PowerPoint, OpenDocument, RTF, HTML, Markdown, CSV and text files",
            local,
            sent,
        )
        refused: List[str] = []
        broken = False
        for kind, modules in _DOCUMENT_READERS.items():
            mine = [s for s in local if SUFFIX_KINDS[s] == kind]
            if not mine:
                continue
            states: List[Tuple[str, str, str]] = []
            for module in modules:  # in the reader's order: the first that imports reads them
                states.append((module, *machine.imports(module)))
                if states[-1][1] == "ok":
                    break
            if states[-1][1] == "ok":
                continue
            failing = [(module, why) for module, state, why in states if state == "broken"]
            if failing:
                broken = True
                module, why = failing[0]
                report.error(
                    _AREA,
                    f"{_PACKAGES.get(module, module)} is installed and does not import, so {_listed(mine)} files fail: {why}",
                )
            else:
                refused += mine
        if refused:
            report.warn(
                _AREA,
                _sentence(
                    f"{head}{_listed(refused)} files are refused here until their readers are installed: "
                    "pip install 'vectrixdb[documents]'"
                    + (", or add them to VECTRIXDB_EXTRACTOR_ROUTES" if host else "")
                ),
            )
        elif not broken:
            report.ok(_AREA, _sentence(f"{head}{rest} are read here by the built-in readers"))

    # Scanned pages: the server's PDF reader is given no OCR, so a page with no text stays unread.
    ocr, why = machine.imports("rapidocr_onnxruntime")
    if ".pdf" in sending or "*" in sending:
        report.ok(
            _AREA,
            f"Scanned PDF pages go to {host} with their PDF. An extraction service reads them only with "
            "AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY",
        )
    else:
        where = (
            "add .pdf to VECTRIXDB_EXTRACTOR_ROUTES, for an extraction service"
            if host
            else "set VECTRIXDB_EXTRACTOR_URL to an extraction service"
        )
        report.warn(
            _AREA,
            f"Nothing reads scanned PDF pages here{', RapidOCR included' if ocr == 'ok' else ''}: "
            "a page with no text is left out, and a PDF of scans alone is refused. "
            f"To read them, {where} that has AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY",
        )

    # Pictures: RapidOCR, whose models come in its wheel.
    local, sent = split(["image"])
    head, rest, named = opening("pictures", local, sent)
    if not local:
        report.ok(_AREA, f"Pictures go to {host}")
    elif ocr == "ok":
        report.ok(_AREA, _sentence(f"{head}{rest} are read here by RapidOCR"))
    elif ocr == "broken":
        report.error(
            _AREA,
            f"rapidocr-onnxruntime is installed and does not import, so every picture read here fails: {why}",
        )
    else:
        report.warn(
            _AREA,
            _sentence(
                f"{head}nothing reads {rest}{named} here. "
                + fix("ocr", "AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY")
            ),
        )

    # Recordings: faster-whisper, whose model comes from Hugging Face the first time.
    model = Whisper().model
    fetch = _whisper_fetch(model)
    hearing, why = _whisper(env, machine, model)
    local, sent = split(["audio"])
    head, rest, named = opening("recordings", local, sent)
    if not local:
        report.ok(_AREA, f"Recordings go to {host}")
    elif hearing == "ok":
        report.ok(
            _AREA,
            _sentence(
                f"{head}{rest} are read here by faster-whisper, whose {model} model is already on this machine"
            ),
        )
    elif hearing == "download":
        report.warn(
            _AREA,
            _sentence(
                f"{head}{rest} are read here by faster-whisper, and the first one downloads its {model} model "
                f"from Hugging Face while its upload waits. Fetch it ahead: {fetch}"
            ),
        )
    elif hearing == "offline":
        report.warn(
            _AREA,
            _sentence(
                f"{head}nothing reads {rest}{named} here: faster-whisper's {model} model is not on this machine, "
                f"and VECTRIXDB_OFFLINE refuses to download it. Fetch it where there is a network, {fetch}, "
                "and copy the Hugging Face cache across"
            ),
        )
    elif hearing == "broken":
        report.error(
            _AREA,
            f"faster-whisper is installed and does not import, so every recording read here fails: {why}",
        )
    else:
        report.warn(
            _AREA,
            _sentence(
                f"{head}nothing reads {rest}{named} here. "
                + fix("asr", "AZURE_SPEECH_ENDPOINT and AZURE_SPEECH_KEY")
            ),
        )

    # Videos: ffmpeg, found through imageio-ffmpeg, takes the sound out, and faster-whisper reads it.
    local, sent = split(["video"])
    head, rest, named = opening("videos", local, sent)
    if not local:
        report.ok(_AREA, f"Videos go to {host}")
        return
    state, why = machine.imports("imageio_ffmpeg")
    if state == "broken":
        report.error(
            _AREA,
            f"imageio-ffmpeg is installed and does not import, so every video read here fails: {why}",
        )
        return
    if state == "missing":
        path = any(where == FFMPEG_ON_PATH for _, where in machine.ffmpeg_binaries())
        report.warn(
            _AREA,
            _sentence(
                f"{head}nothing reads {rest}{named} here: the video reader finds ffmpeg through imageio-ffmpeg, "
                f"which is not installed{', and does not use the ffmpeg on PATH without it' if path else ''}. "
                + fix("video", "the ffmpeg extra, AZURE_SPEECH_ENDPOINT and AZURE_SPEECH_KEY")
            ),
        )
        return
    exe, where, version, failed = _ffmpeg(env, machine)
    if not exe:
        if failed and failed[0][1] == FFMPEG_NAMED:
            report.error(
                _AREA,
                f"IMAGEIO_FFMPEG_EXE names {failed[0][0]}, which imageio-ffmpeg uses without trying it, and it "
                f"does not run: {failed[0][2]}. Every video read here fails until it does",
            )
        elif failed:
            report.error(
                _AREA,
                "imageio-ffmpeg finds no ffmpeg that runs, so every video read here fails: "
                + "; ".join(f"{binary}, {origin}: {said}" for binary, origin, said in failed),
            )
        else:
            report.error(
                _AREA,
                "imageio-ffmpeg ships no ffmpeg for this machine and there is none on PATH, so every video "
                "read here fails. Install ffmpeg, or set IMAGEIO_FFMPEG_EXE to one",
            )
        return
    for binary, origin, said in failed:
        report.warn(
            _AREA,
            f"ffmpeg {binary}, {origin}, does not run here ({said}), so videos go through {exe}, {where}",
        )
    if hearing in ("ok", "download"):
        report.ok(
            _AREA,
            _sentence(
                f"{head}{rest} are read here: ffmpeg {version} takes the sound out, and faster-whisper reads it "
                "as it reads a recording"
            ),
        )
    else:
        report.warn(
            _AREA,
            _sentence(
                f"{head}nothing reads {rest}{named} here: ffmpeg {version} takes the sound out, and nothing "
                "reads the sound, as for recordings"
            ),
        )


def _youtube(report: _Report, machine: Any) -> None:
    """YouTube addresses are read with yt-dlp, by load_youtube and an extraction service."""
    state, why = machine.imports("yt_dlp")
    if state == "ok":
        found = machine.version("yt-dlp")
        report.ok(
            _AREA,
            f"YouTube addresses are read with yt-dlp {found or 'of a version it does not say'}",
        )
    elif state == "broken":
        report.error(
            _AREA, f"yt-dlp is installed and does not import, so every YouTube address fails: {why}"
        )
    else:
        report.warn(_AREA, "Nothing reads YouTube addresses here: pip install 'vectrixdb[youtube]'")


def _on_a_service(report: _Report, env: Mapping[str, str], machine: Any) -> None:
    """What an extraction service started with these settings reads with, as ``ExtractionService.from_environment`` reads them.

    vectrixdb serve reads none of these settings: it sends files to such a
    service with VECTRIXDB_EXTRACTOR_URL. So each is said only when it is
    there, for the service it is meant for. A setting that stops the service
    at its start is an error; one it starts without and that shows at the
    first file is a warning.
    """

    def get(name: str) -> str:
        return str(env.get(name, "") or "").strip()

    service = _sentence(_SERVICE)
    identity = (
        machine.imports("azure.identity")[0] == "ok" if get("AZURE_OPENAI_ENDPOINT") else False
    )

    def both(first: str, second: str, what: str) -> bool:
        """Are both of a pair set? One without the other is said, since the pair is then not used."""
        if get(first) and get(second):
            return True
        if get(first) or get(second):
            given, missing = (first, second) if get(first) else (second, first)
            report.warn(
                _AREA,
                f"{given} is set and {missing} is not, so {_SERVICE} does not use {what}: it needs both",
            )
        return False

    def chat(deployment: str) -> str:
        """The chat model that can see ChatRoute.from_environment makes, in words; empty for none."""
        name, endpoint = get(deployment), get("AZURE_OPENAI_ENDPOINT")
        if name and endpoint and (get("AZURE_OPENAI_KEY") or identity):
            if urlsplit(endpoint).scheme not in ("http", "https"):
                return ""
            return f"the {name} deployment" + (
                "" if get("AZURE_OPENAI_KEY") else ", as the managed identity"
            )
        route = urlsplit(get("VECTRIXDB_DESCRIBER_URL"))
        if route.scheme in ("http", "https") and route.netloc:
            return f"the chat model at {route.hostname}"
        return ""

    # Pictures and scanned pages: Document Intelligence first, else RapidOCR, for pictures alone.
    docintel = both("AZURE_DOCINTEL_ENDPOINT", "AZURE_DOCINTEL_KEY", "Document Intelligence")
    if docintel:
        state, why = machine.imports("azure.ai.documentintelligence")
        if state != "ok":
            report.error(
                _AREA,
                f"{service} stops at its start: Document Intelligence needs azure-ai-documentintelligence"
                + (f", which does not import ({why})" if why else "")
                + ". pip install 'vectrixdb[ocr-azure]'",
            )
        else:
            report.ok(
                _AREA,
                f"{service} reads pictures and scanned PDF pages with Document Intelligence at "
                f"{_host(get('AZURE_DOCINTEL_ENDPOINT'))}",
            )

    # Recordings and the sound of videos: Azure Speech first, else faster-whisper.
    if both("AZURE_SPEECH_ENDPOINT", "AZURE_SPEECH_KEY", "Azure Speech"):
        speech = urlsplit(get("AZURE_SPEECH_ENDPOINT"))
        if speech.scheme != "https" or not speech.netloc:
            report.error(
                _AREA,
                f"{service} stops at its start: AZURE_SPEECH_ENDPOINT is {get('AZURE_SPEECH_ENDPOINT')!r}, "
                "and Azure Speech is called at an https address",
            )
        else:
            report.ok(
                _AREA,
                f"{service} reads recordings, and the sound of videos, with Azure Speech at {speech.hostname}",
            )

    # What a picture shows: a chat model that can see, then Azure Vision, asked in that order.
    deployment, endpoint = get("AZURE_OPENAI_VISION_DEPLOYMENT"), get("AZURE_OPENAI_ENDPOINT")
    if deployment and not endpoint:
        report.warn(
            _AREA,
            "AZURE_OPENAI_VISION_DEPLOYMENT is set and AZURE_OPENAI_ENDPOINT is not, so it describes no picture",
        )
    elif deployment and not get("AZURE_OPENAI_KEY") and not identity:
        report.warn(
            _AREA,
            "AZURE_OPENAI_VISION_DEPLOYMENT has no AZURE_OPENAI_KEY, and calling it as the managed identity "
            "needs azure-identity: pip install 'vectrixdb[azure]'. Until then it describes no picture",
        )
    elif deployment and urlsplit(endpoint).scheme not in ("http", "https"):
        report.error(
            _AREA,
            f"{service} stops at its start: AZURE_OPENAI_ENDPOINT is {endpoint!r}, which is not an http "
            "or https address",
        )
    described = chat("AZURE_OPENAI_VISION_DEPLOYMENT")
    route = get("VECTRIXDB_DESCRIBER_URL")
    if route:
        parts = urlsplit(route)
        if described and not described.startswith("the chat model at"):
            report.warn(
                _AREA,
                "VECTRIXDB_DESCRIBER_URL is set and not used: the AZURE_OPENAI_VISION_DEPLOYMENT deployment "
                "describes pictures instead",
            )
        elif parts.scheme not in ("http", "https") or not parts.netloc:
            report.error(
                _AREA,
                f"{service} stops at its start: VECTRIXDB_DESCRIBER_URL is {route!r}, which is not an http "
                "or https address",
            )
    seeing = [described] if described else []
    if both("AZURE_VISION_ENDPOINT", "AZURE_VISION_KEY", "Azure Vision"):
        seeing.append(f"Azure Vision at {_host(get('AZURE_VISION_ENDPOINT'))}")
    if seeing:
        report.ok(_AREA, f"{service} describes pictures with {', then '.join(seeing)}")

    # How a PDF is read: its own text, a chat model for the hard pages, or Document Intelligence's layout.
    pdf = get("VECTRIXDB_EXTRACT_PDF").lower()
    pages = (
        "AZURE_OPENAI_PAGE_DEPLOYMENT"
        if get("AZURE_OPENAI_PAGE_DEPLOYMENT")
        else "AZURE_OPENAI_VISION_DEPLOYMENT"
    )
    if pdf == "layout" and not (get("AZURE_DOCINTEL_ENDPOINT") and get("AZURE_DOCINTEL_KEY")):
        report.warn(
            _AREA,
            f"VECTRIXDB_EXTRACT_PDF=layout needs AZURE_DOCINTEL_ENDPOINT and AZURE_DOCINTEL_KEY, so {_SERVICE} "
            "reads PDFs from their own text instead",
        )
    elif pdf == "vision" and not chat(pages):
        report.warn(
            _AREA,
            "VECTRIXDB_EXTRACT_PDF=vision needs a chat model that can see: AZURE_OPENAI_VISION_DEPLOYMENT with "
            f"AZURE_OPENAI_ENDPOINT, or VECTRIXDB_DESCRIBER_URL, so {_SERVICE} reads PDFs by the rules alone",
        )
    elif pdf and pdf not in ("text", "vision", "layout"):
        report.warn(
            _AREA,
            f"VECTRIXDB_EXTRACT_PDF is {pdf!r}. It can be text, vision or layout, and anything else reads PDFs as text does",
        )

    # Translation: Azure AI Translator, with the key, and the region a regional resource needs.
    key, region = get("AZURE_TRANSLATOR_KEY"), get("AZURE_TRANSLATOR_REGION")
    if key and not region:
        report.warn(
            _AREA,
            "AZURE_TRANSLATOR_KEY is set without AZURE_TRANSLATOR_REGION: a regional or multi-service Translator "
            "resource refuses the first translation without it. Only a global one does without",
        )
    elif key:
        report.ok(_AREA, f"{service} translates with Azure AI Translator, in {region}")
    elif region or get("AZURE_TRANSLATOR_ENDPOINT"):
        given = "AZURE_TRANSLATOR_REGION" if region else "AZURE_TRANSLATOR_ENDPOINT"
        report.warn(
            _AREA,
            f"{given} is set and AZURE_TRANSLATOR_KEY is not, so {_SERVICE} translates nothing",
        )


# ============================================================================
# RUNNING THEM
# ============================================================================
#
# INPUT   a data path and the environment
# OUTPUT  the sign-in store as the server opens it; every finding for a server
#         started with these settings, most serious last
#
# What vectrixdb check runs.


def _open_store(config: Any) -> Any:
    """The sign-in store as the server opens it."""
    from .signin.store import SignInStore

    return SignInStore(config.store_url or config.store_path, config.secrets, key=config.store_key)


def run(
    path: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    *,
    store_opener: Optional[Callable[[Any], Any]] = _open_store,
    machine: Optional[Any] = None,
) -> List[Finding]:
    """Every finding for a server started with these settings on this data path, most serious last.

    ``machine`` answers what is installed here, which packages import, which
    ffmpeg runs, whether a speech model is downloaded; left out, it is this
    machine. A test hands in one of its own.
    """
    env = dict(os.environ if env is None else env)
    data = Path(path or env.get("VECTRIXDB_PATH") or "./vectrixdb_data")
    here = machine if machine is not None else _Machine()
    report = _Report()
    report.guard("Settings", lambda: _names(report, env))
    report.guard("Storage", lambda: _storage(report, env, data))
    report.guard("Storage", lambda: _collection_store(report, env))
    holder: Dict[str, Any] = {}
    report.guard("Sign-in", lambda: holder.update(config=_signin(report, env, data, store_opener)))
    report.guard("Browsers", lambda: _web(report, env, holder.get("config")))
    report.guard("Gateway", lambda: _gateway(report, env))
    report.guard("Brand", lambda: _brand(report, env))
    report.guard(_AREA, lambda: _extractor(report, env))
    report.guard(_AREA, lambda: _readers(report, env, here))
    report.guard(_AREA, lambda: _youtube(report, here))
    report.guard(_AREA, lambda: _on_a_service(report, env, here))
    report.guard("Masking", lambda: _masking(report, env, here))
    report.guard("Records", lambda: _records(report, env, data, holder.get("config")))
    order = {"ok": 0, "warn": 1, "error": 2}
    return sorted(report.findings, key=lambda f: order[f.level])
