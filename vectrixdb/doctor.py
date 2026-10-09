"""Try every part of an install and say what works: what ``vectrixdb doctor`` runs.

``vectrixdb check`` reads the settings and opens nothing, so it can say what a
start would refuse, never whether the identity provider answers or the model
loads. This is the other half. It loads the models and embeds a word, reads a
small document of each built-in kind (which readers are installed is
``vectrixdb check``'s, and comes with it), and reaches every service
the settings name: the extraction service, the identity provider and its
keys, the mail server, the stores, the chat models and the trace collector.
Each answer is ``ok``, ``warn`` or ``error``, with what to do about it.

It changes nothing anywhere. A service is asked a GET, or for a connection
and nothing sent; a key is never printed, and an address is shown by its host
alone, since a database address can hold a password. ``offline=True``, and
``VECTRIXDB_OFFLINE=1``, leave the network out and say which checks were
skipped.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .check import Finding

__all__ = ["Diagnosis", "Reach", "Connect", "LEVELS", "run", "summary"]


# ============================================================================
# SETTINGS: the readers, the stores and how long a service is waited for
# ============================================================================
#
# What each kind of file needs installed, which extra installs it, and the
# addresses each store setting names.

#: (url, headers) -> (status, body). Swapped for a stand-in in tests.
Reach = Callable[[str, Mapping[str, str]], Tuple[int, bytes]]
#: (host, port) -> None, or raises OSError. Swapped for a stand-in in tests.
Connect = Callable[[str, int], None]

_TIMEOUT = 8.0
#: The levels a diagnosis has, in the order they are listed: most serious last.
LEVELS = ("ok", "skip", "warn", "error")
_TRUE = ("1", "true", "yes", "on")

#: The packages each part of the server needs, when its setting turns it on.
_SERVER_PARTS: Sequence[Tuple[str, str, str]] = (
    ("The REST API and dashboard", "fastapi", "api"),
    ("The web server", "uvicorn", "api"),
)


# ============================================================================
# A DIAGNOSIS, AND THE REPORT
# ============================================================================
#
# INPUT   what a probe found
# OUTPUT  one diagnosis: a finding with what to do about it
#
# A finding as ``vectrixdb check`` makes one, so the two print the same way.


@dataclass
class Diagnosis(Finding):
    """A finding, and what to do about it when it is not ok."""

    fix: str = ""


class _Report:
    def __init__(self) -> None:
        self.found: List[Diagnosis] = []

    def ok(self, area: str, text: str) -> None:
        self.found.append(Diagnosis("ok", area, text))

    def warn(self, area: str, text: str, fix: str = "") -> None:
        self.found.append(Diagnosis("warn", area, text, fix))

    def error(self, area: str, text: str, fix: str = "") -> None:
        self.found.append(Diagnosis("error", area, text, fix))

    def skip(self, area: str, text: str) -> None:
        self.found.append(Diagnosis("skip", area, text))

    def guard(self, area: str, step: Callable[[], None]) -> None:
        """Run one probe; one that breaks says so, and the rest still run."""
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - every failure is a finding
            self.error(area, f"The check itself failed: {type(exc).__name__}: {exc}")


# ============================================================================
# REACHING A SERVICE: a GET, or a connection with nothing sent
# ============================================================================
#
# INPUT   an address
# OUTPUT  its status and body; or that a connection opened
#
# Any answer from a service means it is there: a 401 from an extraction
# service is a key problem, which the service will say at the first document.


def _reach(url: str, headers: Mapping[str, str]) -> Tuple[int, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "vectrixdb-doctor", **headers}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as reply:  # noqa: S310 - the settings' own address
            return reply.status, reply.read(1_000_000)
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read(1_000_000)


def _connect(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=_TIMEOUT):
        pass


def _setting(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(env.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _ms(started: float) -> str:
    return f"{(time.perf_counter() - started) * 1000:.0f} ms"


def _http_service(
    report: _Report,
    area: str,
    what: str,
    url: str,
    reach: Reach,
    headers: Optional[Mapping[str, str]] = None,
    setting: str = "",
) -> None:
    """A service at an http(s) address: there, or not, and why."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        report.error(area, f"{what}: {setting or 'the address'} is not an http or https address")
        return
    started = time.perf_counter()
    try:
        status, _ = reach(url, dict(headers or {}))
    except (OSError, ValueError) as exc:
        report.error(
            area,
            f"{what} at {parts.hostname} cannot be reached: {_reason(exc)}",
            f"Check {setting or 'the address'}, and that this machine can reach {parts.hostname} "
            "(a firewall, a private endpoint, a proxy).",
        )
        return
    if status >= 500:
        report.error(
            area,
            f"{what} at {parts.hostname} answers {status}: it is there and failing",
            "Look at that service's own logs.",
        )
    elif status in (401, 403):
        report.ok(area, f"{what} at {parts.hostname} answers, {_ms(started)} (it asks for a key)")
    else:
        report.ok(area, f"{what} at {parts.hostname} answers, {_ms(started)}")


def _tcp_service(
    report: _Report, area: str, what: str, host: str, port: int, connect: Connect, setting: str
) -> None:
    started = time.perf_counter()
    try:
        connect(host, port)
    except OSError as exc:
        report.error(
            area,
            f"{what} at {host}:{port} cannot be reached: {_reason(exc)}",
            f"Check {setting}, and that this machine can reach {host} on port {port}.",
        )
        return
    report.ok(area, f"{what} at {host}:{port} takes a connection, {_ms(started)}")


def _reason(exc: BaseException) -> str:
    inner = getattr(exc, "reason", None)
    if isinstance(inner, BaseException):
        exc = inner
    if isinstance(exc, socket.gaierror):
        return "the name does not resolve"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"no answer in {_TIMEOUT:g} seconds"
    if isinstance(exc, ConnectionRefusedError):
        return "the connection was refused"
    return str(exc) or type(exc).__name__


# ============================================================================
# THIS MACHINE: Python, the data folder, the models and the readers
# ============================================================================
#
# INPUT   the data path and the settings
# OUTPUT  whether the folder takes a file, whether each model loads and
#         answers, and which kinds of file this install reads
#
# Done for real: a file written and removed, a word embedded, a pair scored,
# a small document of each built-in kind read.


def _python(report: _Report) -> None:
    from . import __version__

    version = ".".join(str(n) for n in sys.version_info[:3])
    report.ok("Install", f"VectrixDB {__version__} on Python {version}")


def _data(report: _Report, data: Path) -> None:
    if not data.exists():
        report.ok("Install", f"{data} does not exist yet; the first collection makes it")
        return
    try:
        with tempfile.NamedTemporaryFile(dir=data, prefix=".vx-doctor-", delete=True) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        report.error(
            "Install",
            f"{data} cannot be written: {_reason(exc)}",
            "Give the user this runs as write access, or point VECTRIXDB_PATH somewhere it has.",
        )
        return
    free = shutil.disk_usage(data).free / 1e9
    if free < 1:
        report.warn(
            "Install",
            f"{data} can be written, with {free:.1f} GB free",
            "Collections grow with every document; free some space.",
        )
    else:
        report.ok("Install", f"{data} can be written, {free:.0f} GB free")


def _models(report: _Report, env: Mapping[str, str], quick: bool) -> None:
    from .models.embedded import get_models_dir, is_models_installed

    where = get_models_dir()
    if quick:
        report.skip("Models", "Not loaded (quick)")
    else:
        from .models.embedded import DenseEmbedder, RerankerEmbedder

        started = time.perf_counter()
        try:
            vector = DenseEmbedder(language="en").embed(["doctor"])
        except Exception as exc:  # noqa: BLE001 - the finding is the failure
            report.error(
                "Models",
                f"The default embedding model does not load: {exc}",
                "pip install --force-reinstall vectrixdb, or vectrixdb download-models --type dense",
            )
        else:
            report.ok(
                "Models",
                f"Embedding model loads and embeds, {vector.shape[1]} dimensions, {_ms(started)}",
            )
        started = time.perf_counter()
        try:
            RerankerEmbedder(language="en").score("refunds", ["Refunds are paid in ten days."])
        except Exception as exc:  # noqa: BLE001
            report.warn(
                "Models",
                f"The reranker does not load: {exc}",
                "vectrixdb download-models --type reranker; search without rerank still works.",
            )
        else:
            report.ok("Models", f"Reranker loads and scores, {_ms(started)}")
    for kind, what, command in (
        ("sparse", "Sparse model (hybrid's word weights)", "sparse"),
        ("late_interaction", "Multilingual late-interaction model", "colbert"),
        ("rebel", "Graph extraction model", "graphrag"),
    ):
        if is_models_installed(kind, exact=kind == "late_interaction"):
            report.ok("Models", f"{what} is installed")
        else:
            report.skip(
                "Models",
                f"{what} is not installed; vectrixdb download-models --type {command} when you need it",
            )
    if _setting(env, "VECTRIXDB_OFFLINE").lower() in _TRUE:
        report.ok("Models", f"Offline: only the models in {where} are used")


def _readers(report: _Report) -> None:
    from .ingest import load_bytes

    samples = (
        ("Markdown", "doctor.md", b"# Refunds\n\nRefunds are paid within ten working days.\n"),
        (
            "HTML",
            "doctor.html",
            b"<html><body><h1>Refunds</h1><p>Refunds are paid within ten working days.</p></body></html>",
        ),
        ("Plain text", "doctor.txt", b"Refunds are paid within ten working days.\n"),
    )
    built_in = []
    for label, name, data in samples:
        try:
            doc = load_bytes(data, name)
        except Exception as exc:  # noqa: BLE001
            report.error("Readers", f"{label} cannot be read: {exc}", "Reinstall vectrixdb.")
            continue
        if "ten working days" in doc.text:
            built_in.append(label)
        else:
            report.error("Readers", f"{label} reads as {doc.text[:60]!r}", "Reinstall vectrixdb.")
    if built_in:
        report.ok("Readers", f"{', '.join(built_in)}: read")


def _server_parts(report: _Report, env: Mapping[str, str]) -> None:
    for what, module, extra in _SERVER_PARTS:
        if not _installed(module):
            report.skip("Server", f"{what}: pip install 'vectrixdb[{extra}]' to serve")
            return
    report.ok("Server", "The REST API, the dashboard and the web server are installed")
    signin = _setting(env, "VECTRIXDB_SIGNIN")
    if signin and not _installed("jwt"):
        report.error(
            "Sign-in",
            f"VECTRIXDB_SIGNIN is {signin}, and the sign-in packages are not installed",
            "pip install 'vectrixdb[signin]'",
        )
    if _setting(env, "VECTRIXDB_MCP").lower() in _TRUE:
        if _installed("mcp.server.mcpserver"):
            report.ok("MCP", "On, and the mcp package is installed: /mcp answers")
        else:
            report.error(
                "MCP",
                "VECTRIXDB_MCP is on and the mcp package, version 2 or later, is not installed",
                "pip install -U 'vectrixdb[mcp]'",
            )
    if _tracing_on(env):
        if _installed("opentelemetry.sdk"):
            report.ok("Tracing", "On, and the OpenTelemetry SDK is installed")
        else:
            report.error(
                "Tracing",
                "Tracing is on and the OpenTelemetry SDK is not installed",
                "pip install 'vectrixdb[tracing]'",
            )


def _tracing_on(env: Mapping[str, str]) -> bool:
    """On as the library turns it on: VECTRIXDB_TRACING=1, or an OTLP endpoint set and tracing not turned off."""
    said = _setting(env, "VECTRIXDB_TRACING").lower()
    if said in ("0", "false", "no", "off"):
        return False
    return (
        said in _TRUE
        or said in ("otlp", "console")
        or bool(_setting(env, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"))
    )


# ============================================================================
# THE SERVICES THE SETTINGS NAME
# ============================================================================
#
# INPUT   the settings
# OUTPUT  whether each service they name answers: the extraction service, the
#         identity provider and its keys, the mail server, the stores, the
#         chat models, the masking service and the trace collector
#
# Asked and never written to: a GET, or a connection with nothing sent.


def _extractor(report: _Report, env: Mapping[str, str], reach: Reach) -> None:
    from .signin.keys import env_secret

    url = _setting(env, "VECTRIXDB_EXTRACTOR_URL")
    if not url:
        return
    headers = {}
    key = env_secret(env, "VECTRIXDB_EXTRACTOR_KEY")
    if key:
        headers[_setting(env, "VECTRIXDB_EXTRACTOR_KEY_HEADER") or "x-api-key"] = key
    health = url.rstrip("/") + "/health"
    _http_service(
        report,
        "Documents",
        "The extraction service",
        health,
        reach,
        headers,
        "VECTRIXDB_EXTRACTOR_URL",
    )


def _identity_provider(report: _Report, env: Mapping[str, str], reach: Reach) -> None:
    issuer = _setting(env, "VECTRIXDB_OIDC_ISSUER")
    if not issuer:
        return
    host = urlsplit(issuer).hostname or issuer
    discovery = issuer.rstrip("/") + "/.well-known/openid-configuration"
    started = time.perf_counter()
    try:
        status, body = reach(discovery, {})
    except (OSError, ValueError) as exc:
        report.error(
            "Sign-in",
            f"The identity provider at {host} cannot be reached: {_reason(exc)}",
            "Check VECTRIXDB_OIDC_ISSUER, and that this machine can reach it.",
        )
        return
    try:
        document = json.loads(body) if status == 200 else None
    except ValueError:
        document = None
    if not isinstance(document, dict):
        report.error(
            "Sign-in",
            f"{host} answers {status} where its sign-in description should be",
            "VECTRIXDB_OIDC_ISSUER is the issuer itself, such as "
            "https://login.microsoftonline.com/<tenant id>/v2.0, with nothing after it.",
        )
        return
    said = str(document.get("issuer", "")).rstrip("/")
    if said != issuer.rstrip("/"):
        report.error(
            "Sign-in",
            f"The provider calls itself {said or 'nothing'}, not VECTRIXDB_OIDC_ISSUER",
            # Entra's common and organizations addresses describe every tenant at once.
            "common and organizations are shared addresses, not an issuer: use your tenant's, "
            "https://login.microsoftonline.com/<tenant id>/v2.0."
            if "{tenantid}" in said
            else "Every token would be refused: set VECTRIXDB_OIDC_ISSUER to what it says.",
        )
        return
    report.ok("Sign-in", f"The identity provider at {host} answers, {_ms(started)}")
    keys_at = str(document.get("jwks_uri") or "")
    if not keys_at:
        report.error("Sign-in", f"{host} publishes no signing keys (jwks_uri)")
        return
    try:
        status, body = reach(keys_at, {})
        keys = json.loads(body).get("keys", []) if status == 200 else None
    except (OSError, ValueError, AttributeError) as exc:
        report.error(
            "Sign-in",
            f"The provider's signing keys cannot be read: {_reason(exc)}",
            "Tokens cannot be checked without them; this machine has to reach "
            f"{urlsplit(keys_at).hostname}.",
        )
        return
    if not keys:
        report.error("Sign-in", "The provider's key list is empty: no token could be checked")
    else:
        report.ok("Sign-in", f"Its {len(keys)} signing key{'s' if len(keys) != 1 else ''} read")
    if not _setting(env, "VECTRIXDB_OIDC_CLIENT_ID"):
        report.error(
            "Sign-in",
            "VECTRIXDB_OIDC_CLIENT_ID is not set",
            "The application's id at the provider: people cannot be signed in without it.",
        )


def _mail(report: _Report, env: Mapping[str, str], connect: Connect) -> None:
    from .signin.keys import env_secret

    url = env_secret(env, "VECTRIXDB_SMTP_URL") or ""
    if not url:
        return
    parts = urlsplit(url)
    if not parts.hostname:
        report.error("Email", "VECTRIXDB_SMTP_URL has no host", "smtp://user:password@host:587")
        return
    port = parts.port or (465 if parts.scheme == "smtps" else 587)
    _tcp_service(
        report, "Email", "The mail server", parts.hostname, port, connect, "VECTRIXDB_SMTP_URL"
    )


def _stores(report: _Report, env: Mapping[str, str], reach: Reach, connect: Connect) -> None:
    from .signin.keys import env_secret

    backend = (_setting(env, "VECTRIXDB_STORAGE_BACKEND") or "sqlite").lower()
    if backend == "cosmosdb":
        endpoint = _setting(env, "VECTRIXDB_COSMOS_ENDPOINT")
        if endpoint:
            _http_service(
                report, "Storage", "Cosmos DB", endpoint, reach, setting="VECTRIXDB_COSMOS_ENDPOINT"
            )
    elif backend == "azure_search":
        endpoint = _setting(env, "VECTRIXDB_AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_ENDPOINT")
        if endpoint:
            _http_service(
                report,
                "Storage",
                "Azure AI Search",
                endpoint,
                reach,
                setting="VECTRIXDB_AZURE_SEARCH_ENDPOINT",
            )
    elif backend == "lakebase":
        host = _setting(env, "VECTRIXDB_LAKEBASE_HOST")
        if host:
            port = int(_setting(env, "VECTRIXDB_LAKEBASE_PORT") or 5432)
            _tcp_service(
                report, "Storage", "Lakebase", host, port, connect, "VECTRIXDB_LAKEBASE_HOST"
            )
    elif backend == "delta_lake":
        workspace = _setting(env, "VECTRIXDB_DELTA_WORKSPACE_URL")
        if workspace:
            _http_service(
                report,
                "Storage",
                "The Databricks workspace",
                workspace,
                reach,
                setting="VECTRIXDB_DELTA_WORKSPACE_URL",
            )
    for setting, what in (
        ("VECTRIXDB_SIGNIN_STORE", "The sign-in store"),
        ("VECTRIXDB_AUDIT_STORE", "The audit store"),
        ("VECTRIXDB_EVALUATIONS", "The evaluation runs"),
        ("VECTRIXDB_GRAPH_STORE", "The graph store"),
    ):
        try:
            where = env_secret(env, setting) or ""
        except Exception:  # noqa: BLE001 - vectrixdb check reports the setting itself
            continue
        parts = urlsplit(where)
        scheme = parts.scheme.lower()
        if scheme in ("postgres", "postgresql") and parts.hostname:
            _tcp_service(
                report, "Storage", what, parts.hostname, parts.port or 5432, connect, setting
            )
        elif scheme == "cosmos" and parts.hostname:
            _http_service(
                report, "Storage", what, f"https://{parts.hostname}/", reach, setting=setting
            )
        elif scheme == "https" and parts.hostname:
            _http_service(
                report, "Storage", what, f"https://{parts.hostname}/", reach, setting=setting
            )


def _chat_models(report: _Report, env: Mapping[str, str], reach: Reach) -> None:
    for setting, what in (
        ("VECTRIXDB_WRITER_URL", "The model that drafts golden questions"),
        ("VECTRIXDB_DESCRIBER_URL", "The model that describes pictures"),
        ("AZURE_OPENAI_ENDPOINT", "Azure OpenAI"),
        ("AZURE_LANGUAGE_ENDPOINT", "Azure AI Language, for masking"),
    ):
        url = _setting(env, setting)
        if url:
            parts = urlsplit(url)
            base = f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}/"
            _http_service(report, "Services", what, base, reach, setting=setting)


def _collector(report: _Report, env: Mapping[str, str], connect: Connect) -> None:
    if not _tracing_on(env) or _setting(env, "VECTRIXDB_TRACING").lower() == "console":
        return
    endpoint = _setting(env, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
    parts = urlsplit(endpoint or "http://localhost:4318")
    if not parts.hostname:
        report.error("Tracing", "OTEL_EXPORTER_OTLP_ENDPOINT has no host")
        return
    port = parts.port or (443 if parts.scheme == "https" else 4318)
    _tcp_service(
        report,
        "Tracing",
        "The trace collector",
        parts.hostname,
        port,
        connect,
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    )


# ============================================================================
# THE RUN
# ============================================================================
#
# INPUT   a data path, the settings, and how to reach a service
# OUTPUT  every diagnosis, most serious last
#
# The settings check first, its warnings and errors only, then each probe.


def run(
    path: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    *,
    offline: Optional[bool] = None,
    quick: bool = False,
    reach: Optional[Reach] = None,
    connect: Optional[Connect] = None,
    settings_check: bool = True,
) -> List[Diagnosis]:
    """Every diagnosis for this install with these settings, most serious last.

    ``offline`` leaves out every check that would use the network; left as
    None it follows ``VECTRIXDB_OFFLINE``. ``quick`` leaves the models
    unloaded. ``reach`` and ``connect`` are how a service is asked, swapped
    for stand-ins in tests.
    """
    env = dict(os.environ if env is None else env)
    data = Path(path or env.get("VECTRIXDB_PATH") or "./vectrixdb_data")
    if offline is None:
        offline = _setting(env, "VECTRIXDB_OFFLINE").lower() in _TRUE
    reach = reach or _reach
    connect = connect or _connect
    report = _Report()
    if settings_check:
        from .check import run as check_settings

        for finding in check_settings(str(data), env):
            if finding.level != "ok":
                report.found.append(
                    Diagnosis(
                        finding.level,
                        finding.area,
                        finding.text,
                        # The check's own words already say what to do.
                        "",
                    )
                )
    report.guard("Install", lambda: _python(report))
    report.guard("Install", lambda: _data(report, data))
    report.guard("Models", lambda: _models(report, env, quick))
    report.guard("Readers", lambda: _readers(report))
    report.guard("Server", lambda: _server_parts(report, env))
    network: Sequence[Tuple[str, Callable[[], None]]] = (
        ("Documents", lambda: _extractor(report, env, reach)),
        ("Sign-in", lambda: _identity_provider(report, env, reach)),
        ("Email", lambda: _mail(report, env, connect)),
        ("Storage", lambda: _stores(report, env, reach, connect)),
        ("Services", lambda: _chat_models(report, env, reach)),
        ("Tracing", lambda: _collector(report, env, connect)),
    )
    if offline:
        report.skip("Network", "Offline: no service the settings name was asked")
    else:
        for area, step in network:
            report.guard(area, step)
    return sorted(report.found, key=lambda d: LEVELS.index(d.level))


def summary(found: Sequence[Diagnosis]) -> Dict[str, Any]:
    """The counts, and every diagnosis as a dictionary, for ``--json``."""
    counts = {level: sum(1 for d in found if d.level == level) for level in LEVELS}
    return {
        "counts": counts,
        "healthy": counts["error"] == 0,
        "diagnoses": [
            {"level": d.level, "area": d.area, "text": d.text, "fix": d.fix} for d in found
        ],
    }
