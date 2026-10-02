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
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set
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


def _number(report: _Report, env: Mapping[str, str], name: str, kind: Callable[[str], Any], low: float = 0) -> None:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return
    try:
        value = kind(raw)
    except ValueError:
        report.error("Settings", f"{name} is {raw!r}, which is not a number")
        return
    if value <= low:
        report.error("Settings", f"{name} is {raw}. It has to be more than {low:g}")


def _names(report: _Report, env: Mapping[str, str]) -> None:
    for name, near in unknown(env):
        report.warn("Settings", f"{name} is not a setting VectrixDB reads" + (f". Did you mean {near}?" if near else ", so it does nothing"))
    for setting in SETTINGS:
        if not setting.file_twin:
            continue
        inline, file_name = str(env.get(setting.name, "") or "").strip(), str(env.get(setting.name + "_FILE", "") or "").strip()
        if inline and file_name:
            report.error("Settings", f"{setting.name} and {setting.name}_FILE are both set. Keep one, so there is no doubt which is meant")
            report.said.add(setting.name + "_FILE")
        elif file_name and not Path(file_name).is_file():
            report.error("Settings", f"{setting.name}_FILE names {file_name}, which is not a file here")
            report.said.add(setting.name + "_FILE")
    for name in ("VECTRIXDB_SESSION_HOURS", "VECTRIXDB_SESSION_IDLE_MINUTES", "VECTRIXDB_EXTRACTOR_TIMEOUT", "VECTRIXDB_MAX_MEMORY_PERCENT"):
        _number(report, env, name, float)
    for name in ("VECTRIXDB_MAX_UPLOAD_BYTES", "VECTRIXDB_LAKEBASE_PORT", "VECTRIXDB_REDIS_PORT"):
        _number(report, env, name, int)


def _storage(report: _Report, env: Mapping[str, str], path: Path) -> None:
    backend = str(env.get("VECTRIXDB_STORAGE_BACKEND", "") or "sqlite").strip().lower()
    if backend not in _BACKENDS:
        report.error("Storage", f"VECTRIXDB_STORAGE_BACKEND is {backend!r}. It can be {', '.join(_BACKENDS[:-1])} or {_BACKENDS[-1]}")
        return
    needs: Dict[str, List[List[str]]] = {
        "cosmosdb": [["VECTRIXDB_COSMOS_ENDPOINT"], ["VECTRIXDB_COSMOS_KEY"]],
        "lakebase": [["VECTRIXDB_LAKEBASE_HOST"], ["VECTRIXDB_LAKEBASE_TOKEN", "VECTRIXDB_LAKEBASE_PASSWORD"]],
        "delta_lake": [["VECTRIXDB_DELTA_WORKSPACE_URL"], ["VECTRIXDB_DELTA_TOKEN"], ["VECTRIXDB_DELTA_WAREHOUSE_ID", "VECTRIXDB_DELTA_HTTP_PATH"]],
        # The endpoint is the one setting it has no default for; the key may be
        # left out, and then it signs in as the machine.
        "azure_search": [["VECTRIXDB_AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_ENDPOINT"]],
    }
    missing = [" or ".join(any_of) for any_of in needs.get(backend, []) if not any(str(env.get(n, "") or "").strip() for n in any_of)]
    if missing:
        report.error("Storage", f"{backend} needs {', and '.join(missing)}")
    elif backend == "memory":
        report.warn("Storage", "In memory: nothing the server holds survives a restart")
    elif backend == "sqlite":
        if path.exists():
            (report.ok if _writable(path) else report.error)("Storage", f"{path} {'can' if _writable(path) else 'cannot'} be written")
        else:
            (report.ok if _writable(path) else report.error)("Storage", f"{path} does not exist yet, and {'will be made at the first start' if _writable(path) else 'cannot be made here'}")
    else:
        report.ok("Storage", f"{backend}, with the settings it needs")
    _chunk_store(report, env)
    cache = str(env.get("VECTRIXDB_CACHE_BACKEND", "") or "memory").strip().lower()
    if cache not in _CACHES:
        report.error("Cache", f"VECTRIXDB_CACHE_BACKEND is {cache!r}. It can be {', '.join(_CACHES[:-1])} or {_CACHES[-1]}")
    strategy = str(env.get("VECTRIXDB_SCALING_STRATEGY", "") or "none").strip().lower()
    if strategy != "none":
        from .core.scaling import ScalingStrategy

        allowed = [s.value for s in ScalingStrategy]
        if strategy not in allowed:
            report.error("Cache", f"VECTRIXDB_SCALING_STRATEGY is {strategy!r}. It can be {', '.join(allowed)}")


def _chunk_store(report: _Report, env: Mapping[str, str]) -> None:
    """The address the collection pages read every chunk from, by its shape. Nothing is opened."""
    where = str(env.get("VECTRIXDB_CHUNK_STORE", "") or "").strip()
    if not where:
        return
    parts = urlsplit(where) if "://" in where else None
    names = [name for name in parts.path.split("/") if name] if parts is not None else []
    if parts is None or parts.scheme.lower() != "cosmos" or not parts.hostname or len(names) != 2:
        report.error("Storage", "VECTRIXDB_CHUNK_STORE is not cosmos://<account>.documents.azure.com/<database>/<container>")
        return
    import importlib.util

    try:
        installed = importlib.util.find_spec("azure.cosmos") is not None
    except ImportError:  # no azure package at all
        installed = False
    if not installed:
        report.error("Storage", "VECTRIXDB_CHUNK_STORE needs azure-cosmos: pip install 'vectrixdb[azure]'")
        return
    report.ok("Storage", f"The collection pages read every chunk from Cosmos DB {parts.hostname}/{names[0]}/{names[1]}")


def _collection_store(report: _Report, env: Mapping[str, str]) -> None:
    """The records store a server may read, by its shape and the extra it needs. Nothing is opened."""
    _records_store(report, env, "VECTRIXDB_COLLECTION_STORE", "Every collection's record, who may retrieve from it, who may see it and its masking, is read from")


def _records_store(report: _Report, env: Mapping[str, str], setting: str, what: str) -> None:
    from .signin.keys import env_secret

    where = str(env_secret(env, setting) or "").strip()
    if not where:
        return
    scheme = where.split("://", 1)[0].lower() if "://" in where else "file"
    needs = {"cosmos": ("azure.cosmos", "azure"), "dynamodb": ("boto3", "aws"), "postgres": ("psycopg2", "postgres"), "postgresql": ("psycopg2", "postgres")}
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


def _signin(report: _Report, env: Mapping[str, str], path: Path, store_opener: Optional[Callable[[Any], Any]]) -> Any:
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
            report.ok("Access", "API keys, and no sign-in: scripts only, and nobody in particular")
        elif str(env.get("VECTRIXDB_ALLOW_OPEN", "") or "").strip().lower() in ("1", "true", "yes", "on"):
            report.warn("Access", "No key and no sign-in, and VECTRIXDB_ALLOW_OPEN is set: anybody who reaches the port reads everything. Right only behind a gateway that asks")
        else:
            report.ok("Access", "No key and no sign-in: serve listens on this machine only. Set a key or turn sign-in on to listen on any other address")
        return config
    report.ok("Sign-in", f"{' and '.join(config.methods)}, for {config.public_url}")
    if config.sso_pending:
        report.warn(
            "Sign-in",
            "Single sign-on is asked for and not set up: VECTRIXDB_OIDC_ISSUER and VECTRIXDB_OIDC_CLIENT_ID are empty. Until they are set, "
            "people on the People list sign in with a code by email" + ("" if config.own_passkeys else ", and nobody keeps a passkey"),
        )
    if config.guests:
        report.ok("Sign-in", "Guests may search the collections an admin shares with everyone, 30 searches a minute from one address")
    if "email" in config.methods:
        if config.smtp_url:
            report.ok("Email", f"Sign-in links are sent through {urlsplit(config.smtp_url).hostname or 'the mail server'}")
        elif config.local:
            report.ok("Email", "No mail server: sign-in links go to the server's log, which is fine on this machine")
        else:
            report.warn("Email", "No mail server, so sign-in links are not sent. Make each one on the server: vectrixdb people reset <address>")
    where = config.store_url or config.store_path
    report.ok("Sign-in", f"Sign-in state is kept in {describe_where(where)}")
    # With single sign-on an admin is whoever is in a group mapped to admin, and nobody is added by hand.
    oidc = config.oidc
    if oidc is not None:
        mapped = sorted(group for group, role in oidc.role_map.items() if role == "admin")
        if mapped or oidc.default_role == "admin":
            report.ok("Sign-in", "Single sign-on makes an admin of " + (f"anybody in {', '.join(mapped)}" if mapped else "everybody it lets in, by the default role"))
        elif "email" not in config.methods:
            report.warn("Sign-in", 'No group is mapped to admin, so nobody who signs in can manage people, keys or sharing. Map one: VECTRIXDB_OIDC_ROLE_MAP={"<group id>": "admin"}')
        _who_may_sign_in(report, config, store_opener)
        if oidc.token_role:
            report.ok("Sign-in", f"An app's access token is given the role {oidc.token_role}, whoever it is for")
    if config.sso_recheck_days:
        report.ok("Sign-in", f"A passkey or a code works for somebody who signed in with single sign-on in the last {config.sso_recheck_days} days")
    glass = config.break_glass
    if glass is not None and glass.open():
        report.warn("Sign-in", f"Emergency sign-in is on, for {glass.admin}, until {glass.until_iso}. Turn it off when the usual sign-in is back")
        if store_opener is not None and not (config.store_url is None and config.store_path is not None and not Path(config.store_path).exists()):
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
        accounts = ", ".join(f"{name} ({role})" for name, role in sorted(config.developer.accounts.items()))
        report.ok("Sign-in", f"Developer Access, for this machine only: {accounts}")
    # The email list has admins of its own. VECTRIXDB_SIGNIN_USERS is added to it at every start,
    # for anybody on it the list has not got yet.
    listed = [email for email, role in config.users if role == "admin"]
    local_file = config.store_url is None and config.store_path is not None
    if "email" in config.methods:
        if local_file and not Path(config.store_path).exists():
            if listed:
                report.ok("Sign-in", f"{', '.join(listed)} {'is' if len(listed) == 1 else 'are'} made an admin at the first start, from VECTRIXDB_SIGNIN_USERS")
            else:
                report.warn("Sign-in", "No admin yet, and the sign-in file is made at the first start. Then add the first: vectrixdb people add you@company.com --role admin")
        elif store_opener is not None:
            store = store_opener(config)
            try:
                coming = [email for email in listed if store.person(email) is None]
                if coming and not store.admins():
                    report.ok("Sign-in", f"{', '.join(coming)} {'is' if len(coming) == 1 else 'are'} made an admin at the next start, from VECTRIXDB_SIGNIN_USERS")
                elif not store.admins():
                    report.warn("Sign-in", "No admin yet. Add the first on the server: vectrixdb people add you@company.com --role admin")
            finally:
                store.close()
    access = config.access_log
    from .append_log import is_blob_address

    if str(access).strip().lower() in ("stdout", "-"):
        report.ok("Access log", "Written to the server's output, for the platform to collect")
    elif is_blob_address(access):
        report.ok("Access log", f"Appended to Blob at {str(access).split('?', 1)[0]}, one append blob a day, in a container that must keep what is written")
    elif "://" in str(access):
        report.error("Access log", "VECTRIXDB_ACCESS_LOG is a path, stdout, or https://<account>.blob.core.windows.net/<container>/<prefix>")
    elif _writable(Path(access).parent):
        report.ok("Access log", f"Written to {access}")
    else:
        report.error("Access log", f"{access} cannot be written, and a read that cannot be recorded is refused. Set VECTRIXDB_ACCESS_LOG=stdout on a read-only disk")
    return config


def _who_may_sign_in(report: _Report, config: Any, store_opener: Optional[Callable[[Any], Any]]) -> None:
    """Single sign-on needs somebody named, or the server will not start: the People list, the setting's list, or *."""
    oidc = config.oidc
    if oidc.allowed_emails == ("*",):
        report.warn("Sign-in", "VECTRIXDB_OIDC_ALLOWED_EMAILS is *, so everybody in a mapped group signs in. Not everyone in a group should: put them on the People list")
        return
    on_the_list = len(config.users)
    if not on_the_list and store_opener is not None and not (config.store_url is None and config.store_path is not None and not Path(config.store_path).exists()):
        store = store_opener(config)
        try:
            on_the_list = len(store.people())
        finally:
            store.close()
    if on_the_list or oidc.allowed_emails:
        report.ok(
            "Sign-in",
            "Single sign-on lets in the People list, each with the role on their record"
            + (", and the addresses in VECTRIXDB_OIDC_ALLOWED_EMAILS with their groups' role" if oidc.allowed_emails else ""),
        )
    else:
        report.error(
            "Sign-in",
            "Single sign-on is on and nobody is named who may sign in, so the server will not start. Put them on the People list: "
            "VECTRIXDB_SIGNIN_USERS=you@company.com:admin, or vectrixdb people add you@company.com --role admin. Or name them in VECTRIXDB_OIDC_ALLOWED_EMAILS",
        )


def _web(report: _Report, env: Mapping[str, str], config: Any) -> None:
    from .api.security_headers import frame_ancestors_from_env

    origins = [o.strip() for o in str(env.get("VECTRIXDB_CORS_ORIGINS", "") or "").split(",") if o.strip()]
    if config is not None and config.enabled and origins:
        if "*" in origins:
            report.error("Browsers", "VECTRIXDB_CORS_ORIGINS is * while sign-in is on, which would let any website read this server as whoever is signed in. Name the origins")
        for origin in origins:
            if origin != "*" and not re.fullmatch(r"https?://[^/\s]+", origin):
                report.error("Browsers", f"VECTRIXDB_CORS_ORIGINS names {origin!r}. Name an origin: https://host, with no path")
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
        report.ok("Gateway", f"Every route lives under {gateway.prefix}, and answers with it or without")
    for name, where in sorted(gateway.paths.items()):
        report.ok("Gateway", f"{name} is published under {where}: a caller asks {gateway.visible('/' + name)}")
    if gateway.key_header != DEFAULT_KEY_HEADER:
        report.ok("Gateway", f"An app's key arrives in {gateway.key_header}")
    if gateway.token_header != DEFAULT_TOKEN_HEADER:
        report.ok("Gateway", f"A person's access token arrives in {gateway.token_header}, and Authorization is left to the gateway")
    try:
        proxies = parse_proxies(str(env.get("VECTRIXDB_TRUSTED_PROXIES", "") or ""))
    except ValueError as exc:
        report.error("Gateway", str(exc))
        return
    if proxies:
        report.ok("Gateway", "X-Forwarded-For is believed from " + ", ".join(str(n) for n in proxies))
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


def _masking(report: _Report, env: Mapping[str, str]) -> None:
    """The engine VECTRIXDB_MASKING_ENGINE names, and what it needs. Nothing is loaded."""
    from .masking import ENGINES, languages_from_env

    name = str(env.get("VECTRIXDB_MASKING_ENGINE", "") or "auto").strip().lower()
    if name not in ENGINES:
        report.error("Masking", f"VECTRIXDB_MASKING_ENGINE is {name!r}. It can be {', '.join(ENGINES[:-1])} or {ENGINES[-1]}")
        return
    endpoint = str(env.get("AZURE_LANGUAGE_ENDPOINT", "") or "").strip()
    languages = ", ".join(languages_from_env(env))
    try:
        import presidio_analyzer  # noqa: F401

        presidio = True
    except ImportError:
        presidio = False
    if name == "auto":
        name = "language" if endpoint else "presidio" if presidio else "regex"
    if name == "language":
        if not endpoint:
            report.error("Masking", "VECTRIXDB_MASKING_ENGINE=language needs AZURE_LANGUAGE_ENDPOINT, the Language resource's address")
            return
        if not endpoint.startswith("https://"):
            report.error("Masking", f"AZURE_LANGUAGE_ENDPOINT is {endpoint!r}, and it is https://<name>.cognitiveservices.azure.com")
            return
        key = bool(str(env.get("AZURE_LANGUAGE_KEY", "") or env.get("AZURE_LANGUAGE_KEY_FILE", "") or "").strip())
        report.ok("Masking", f"Azure AI Language at {endpoint}, {'with its key' if key else 'through the managed identity'}, for {languages}; the patterns run after it")
    elif name == "presidio":
        if not presidio:
            report.error("Masking", "VECTRIXDB_MASKING_ENGINE=presidio needs Presidio: pip install 'vectrixdb[masking]', then vectrixdb download-models --type masking")
            return
        report.ok("Masking", f"Presidio in this process, for {languages}; the patterns run after it")
    elif name == "comprehend":
        region = str(env.get("AWS_REGION", "") or env.get("AWS_DEFAULT_REGION", "") or "").strip()
        if not region:
            report.error("Masking", "VECTRIXDB_MASKING_ENGINE=comprehend needs AWS_REGION")
            return
        report.ok("Masking", f"Amazon Comprehend in {region}, English and Spanish; the patterns run after it, and alone for {languages} it does not cover")
    else:
        report.ok("Masking", "The patterns alone: emails, phone numbers, cards, national ids, keys and connection strings by their shape")


def _extractor(report: _Report, env: Mapping[str, str]) -> None:
    url = str(env.get("VECTRIXDB_EXTRACTOR_URL", "") or "").strip()
    routes = str(env.get("VECTRIXDB_EXTRACTOR_ROUTES", "") or "").strip()
    body = str(env.get("VECTRIXDB_EXTRACTOR_BODY", "") or "raw").strip()
    if routes:
        try:
            parsed = json.loads(routes)
        except ValueError as exc:
            report.error("Documents", f"VECTRIXDB_EXTRACTOR_ROUTES is not JSON: {exc}")
        else:
            if not isinstance(parsed, dict):
                report.error("Documents", 'VECTRIXDB_EXTRACTOR_ROUTES is JSON but not a mapping of suffix to route: {".pdf": "/extract/pdf"}')
    if body not in ("raw", "multipart"):
        report.error("Documents", f"VECTRIXDB_EXTRACTOR_BODY is {body!r}. It can be raw or multipart")
    asked = str(env.get("VECTRIXDB_EXTRACTOR_MASK", "") or "").strip().lower()
    if asked and asked not in ("1", "yes", "true", "on", "0", "no", "false", "off", "all"):
        from .masking import types_of

        try:
            types_of(asked)
        except Exception as exc:  # noqa: BLE001 - said as the setting's own error
            report.error("Documents", f"VECTRIXDB_EXTRACTOR_MASK: {exc}")
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            report.error("Documents", f"VECTRIXDB_EXTRACTOR_URL is {url!r}, which is not an http or https address")
        elif parts.scheme == "http" and parts.hostname not in _LOCAL:
            report.warn("Documents", f"Documents go to {parts.hostname} over plain http")
        else:
            report.ok("Documents", f"File types the server does not read go to {parts.hostname}")


def _records(report: _Report, env: Mapping[str, str], path: Path, config: Any) -> None:
    from .audit import audit_where, describe_audit_store

    audit = audit_where(dict(env)) or ""
    if audit:
        named = "VECTRIXDB_AUDIT_STORE" if str(env.get("VECTRIXDB_AUDIT_STORE", "") or env.get("VECTRIXDB_AUDIT_STORE_FILE", "") or "").strip() else "VECTRIXDB_AUDIT_JSONL"
        scheme = audit.split("://", 1)[0].lower() if "://" in audit else ""
        days = str(env.get("VECTRIXDB_AUDIT_RETAIN_DAYS", "") or "").strip()
        from .append_log import is_blob_address

        if named == "VECTRIXDB_AUDIT_STORE" and str(env.get("VECTRIXDB_AUDIT_JSONL", "") or "").strip():
            report.warn("Audit", "VECTRIXDB_AUDIT_STORE and VECTRIXDB_AUDIT_JSONL are both set: decisions go to VECTRIXDB_AUDIT_STORE, and the other is ignored")
        if scheme and scheme not in ("s3", "postgres", "postgresql") and not is_blob_address(audit):
            report.error("Audit", f"{named} is a path, https://<account>.blob.core.windows.net/<container>/<prefix>, s3://<bucket>/<prefix> or postgresql://<user>@<host>/<database>")
        elif scheme == "s3" and not days.isdigit():
            report.error("Audit", "An s3:// audit store keeps each record under Object Lock for VECTRIXDB_AUDIT_RETAIN_DAYS days, which has no default")
        elif not scheme and not _writable(Path(audit).parent):
            report.error("Audit", f"{audit} cannot be written")
        elif not str(env.get("VECTRIXDB_AUDIT_QUERY_KEY", "") or "").strip() and not (config is not None and config.enabled):
            report.warn("Audit", f"{named} is set and nothing is written to it: with sign-in off it needs VECTRIXDB_AUDIT_QUERY_KEY")
        else:
            report.ok("Audit", f"Search decisions under a policy are written to {describe_audit_store(audit)}")
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


def run(path: Optional[str] = None, env: Optional[Mapping[str, str]] = None, *, store_opener: Optional[Callable[[Any], Any]] = _open_store) -> List[Finding]:
    """Every finding for a server started with these settings on this data path, most serious last."""
    env = dict(os.environ if env is None else env)
    data = Path(path or env.get("VECTRIXDB_PATH") or "./vectrixdb_data")
    report = _Report()
    report.guard("Settings", lambda: _names(report, env))
    report.guard("Storage", lambda: _storage(report, env, data))
    report.guard("Storage", lambda: _collection_store(report, env))
    holder: Dict[str, Any] = {}
    report.guard("Sign-in", lambda: holder.update(config=_signin(report, env, data, store_opener)))
    report.guard("Browsers", lambda: _web(report, env, holder.get("config")))
    report.guard("Gateway", lambda: _gateway(report, env))
    report.guard("Brand", lambda: _brand(report, env))
    report.guard("Documents", lambda: _extractor(report, env))
    report.guard("Masking", lambda: _masking(report, env))
    report.guard("Records", lambda: _records(report, env, data, holder.get("config")))
    order = {"ok": 0, "warn": 1, "error": 2}
    return sorted(report.findings, key=lambda f: order[f.level])
