"""The extraction service, a Function App of its own: a utility API for any work.

    python 05_create_extraction_function_app.py
    python 05_create_extraction_function_app.py --settings-only    # after changing a setting
    python 05_create_extraction_function_app.py --code-only        # after rebuilding the wheel

Files, addresses and YouTube videos in, text out, nothing indexed. It is the
library's extraction service, ``create_extraction_app``, deployed as it ships;
every route is in docs/how-to/extraction-service.md.

It stands alone. It is a backend for whatever work needs text out of
something, not a part of the ingest app, and it needs nothing of that app: no
search service, no ingest queue, no Event Grid link. It needs the group and
the storage account from 01, and uses Document Intelligence, Speech, Vision and
Language from 03 when they are there. What each missing one means is said when the
settings are sent. With VX_DETAILED_PICTURES=yes it is given 03's chat model
too, which describes every picture in detail in front of Vision.

Four things happen here, in this order:

1. The app, on Flex Consumption: billed per call, nothing running between
   calls, and an hour for a long job.
2. Its settings. The key its callers present is made on the first run and
   kept in the app's settings, nowhere else, and a second run keeps it. The
   services' keys are read from Azure and sent straight to the app. None is
   written to a file on this machine.
3. The wheel, looked inside before anything is sent. One built before the
   extraction service had what this app imports installs without complaint,
   and then the app loads nothing and answers 404 to everything. The line
   in ``requirements.txt`` that names it is pinned here to the wheel that
   is actually in dist/, so a version bump cannot leave a stale name.
4. The code, with ``func azure functionapp publish``.

Once it is up, ``/health/wiring`` says what it was given, by service, with
no key in it; and when it answers 404 to everything, its live log holds one
message that lists every setting that is wrong.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from typing import Any, Dict

from _common import (
    EXTRACTION_APP,
    Az,
    _no_region,
    regions,
    somewhere,
    arguments,
    begin,
    done,
    finish,
    links,
    note,
    portal,
    remember,
    settings,
    skipped,
    state,
    step,
    stop,
    wants,
    wheel_ready,
)


# ============================================================================
# SETTINGS: what the wheel must hold, what is passed on, and the services
# ============================================================================
#
# What the wheel must hold before the app can be published, the settings
# passed on from settings.env, and the services from 03 the app uses when they
# are there.
#: What the app imports that only a new enough wheel has.
NEEDS = {
    "vectrixdb/api/extraction.py": "class MaskRequest",
    "vectrixdb/masking/__init__.py": "def engine_from_env",
}

#: The settings.env names that pass straight through to the library's own.
PASSED_ON = {
    "VX_EXTRACT_PREFIX": "VECTRIXDB_EXTRACT_PREFIX",
    "VX_EXTRACT_GATEWAY_PATHS": "VECTRIXDB_EXTRACT_GATEWAY_PATHS",
    "VX_EXTRACT_URL_HOSTS": "VECTRIXDB_EXTRACT_URL_HOSTS",
    "VX_EXTRACT_PDF": "VECTRIXDB_EXTRACT_PDF",
    "VX_VISION_PAGES": "VECTRIXDB_VISION_PAGES",
}

#: The services 03 makes that this app uses, what state.json calls each
#: endpoint, and what the app does without it.
SERVICES = (
    (
        "VX_DOCINTEL",
        "docintel_endpoint",
        "AZURE_DOCINTEL",
        "pictures answer 503, and a scanned PDF page comes back without its text",
    ),
    ("VX_SPEECH", "speech_endpoint", "AZURE_SPEECH", "recordings, videos and YouTube answer 503"),
    (
        "VX_VISION_NAME",
        "vision_endpoint",
        "AZURE_VISION",
        "a picture is read for its words and not described",
    ),
    (
        "VX_LANGUAGE_NAME",
        "language_endpoint",
        "AZURE_LANGUAGE",
        "a document is masked by the patterns alone, which know no names or addresses",
    ),
)


# ============================================================================
# OPTIONS AND PATHS
# ============================================================================
#
# INPUT   the command line; the prefix and the gateway list from settings.env
# OUTPUT  --settings-only or --code-only; the prefix as the app will use it;
#         the paths checked the way the app checks them when it starts
#
# settings.env with paths the app would refuse stops here, not on the first
# call.


def options(parser) -> None:
    parser.add_argument(
        "--settings-only", action="store_true", help="only push the settings, make nothing"
    )
    parser.add_argument("--code-only", action="store_true", help="only publish the code")


def route_prefix(config: Dict[str, str]) -> str:
    """The prefix as the app will use it, for the addresses printed at the end."""
    parts = [
        part.strip() for part in config.get("VX_EXTRACT_PREFIX", "").split("/") if part.strip()
    ]
    return "/" + "/".join(parts) if parts else ""


def paths_hold(config: Dict[str, str]) -> None:
    """The prefix and the gateway list, checked here the way the app checks them when it starts.

    A mistake in either stops the app loading at all, and a function app that
    loads nothing answers 404 to everything, which says nothing about why.
    """
    prefix, listed = config.get("VX_EXTRACT_PREFIX", ""), config.get("VX_EXTRACT_GATEWAY_PATHS", "")
    if not (prefix or listed):
        return
    try:
        from vectrixdb.api.extraction import ExtractionService, create_extraction_app
        from vectrixdb.exceptions import ConfigurationError
    except ImportError:
        note("the library is not installed here, so the app checks the paths itself when it starts")
        return
    try:
        create_extraction_app(
            ExtractionService(), prefix=prefix, gateway_paths=listed, allow_open=True
        )
    except ConfigurationError as exc:
        stop(f"settings.env has paths the app would refuse, and it would load nothing:\n  {exc}")
    done("the prefix and the gateway paths are ones the app accepts")


# ============================================================================
# THE SETTINGS THE APP GETS: current, keys, environment
# ============================================================================
#
# INPUT   the app as it runs now; the services 03 made
# OUTPUT  what it runs with, so a second run keeps its key rather than
#         changing it under its callers; the services' keys, read fresh from
#         Azure; every setting the extraction app runs with
#
# The keys are read from Azure and sent straight to the app; none is written
# into this code or into a file. Nothing of the ingest app's is here: this one
# stands alone.


def current_settings(az: Az, app: str, group: str) -> Dict[str, str]:
    """What the app runs with now, so a second run keeps its key rather than changing it under its callers."""
    found = az(
        "functionapp",
        "config",
        "appsettings",
        "list",
        "--name",
        app,
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    return {
        entry["name"]: entry.get("value") or ""
        for entry in (found or [])
        if isinstance(entry, dict) and "name" in entry
    }


def service_keys(az: Az, config: Dict[str, str]) -> Dict[str, str]:
    """The keys of the services 03 made, read fresh from Azure and never written down here."""
    found_keys = {}
    for name, _kept, setting, _what in SERVICES:
        found = az(
            "cognitiveservices",
            "account",
            "keys",
            "list",
            "--name",
            config[name],
            "--resource-group",
            config["VX_RESOURCE_GROUP"],
            reads=True,
            quiet=True,
            allow_fail=True,
        )
        if found:
            found_keys[f"{setting}_KEY"] = found["key1"]
    if wants(config, "VX_DETAILED_PICTURES"):
        found = az(
            "cognitiveservices",
            "account",
            "keys",
            "list",
            "--name",
            config["VX_OPENAI_NAME"],
            "--resource-group",
            config["VX_RESOURCE_GROUP"],
            reads=True,
            quiet=True,
            allow_fail=True,
        )
        if found:
            found_keys["AZURE_OPENAI_KEY"] = found["key1"]
    if wants(config, "VX_TRANSLATOR"):
        found = az(
            "cognitiveservices",
            "account",
            "keys",
            "list",
            "--name",
            config["VX_TRANSLATOR_NAME"],
            "--resource-group",
            config["VX_RESOURCE_GROUP"],
            reads=True,
            quiet=True,
            allow_fail=True,
        )
        if found:
            found_keys["AZURE_TRANSLATOR_KEY"] = found["key1"]
    return found_keys


def extraction_env(
    config: Dict[str, str], kept: Dict[str, Any], keys: Dict[str, str], key: str
) -> Dict[str, str]:
    """Every setting the extraction app runs with. Nothing of the ingest app's: this one stands alone."""
    setting = {
        # The door. The app will not start without one, because every call
        # spends money on a paid service.
        "VECTRIXDB_API_KEY": key,
        # A long video is a job on a queue in the app's own storage account,
        # AzureWebJobsStorage, because Azure ends every request at 230 seconds.
        "VECTRIXDB_EXTRACT_JOBS": "azure",
        # Masking, for ?mask=1 and /mask: Azure AI Language when 03 made it,
        # else the patterns, and the patterns after either. The documents are
        # in English and French unless settings.env says otherwise.
        "VECTRIXDB_MASKING_ENGINE": "auto",
        "VECTRIXDB_MASKING_LANGUAGES": config.get("VX_MASKING_LANGUAGES") or "en,fr",
    }
    for ours, theirs in PASSED_ON.items():
        if config.get(ours):
            setting[theirs] = config[ours]
    for _name, endpoint, name, _what in SERVICES:
        if kept.get(endpoint):
            setting[f"{name}_ENDPOINT"] = kept[endpoint]
    # Pictures in detail: 03's chat model describes each one in front of
    # Vision, when settings.env asks for it and Azure OpenAI is there.
    if wants(config, "VX_DETAILED_PICTURES") and kept.get("openai_endpoint"):
        setting["AZURE_OPENAI_ENDPOINT"] = kept["openai_endpoint"]
        setting["AZURE_OPENAI_VISION_DEPLOYMENT"] = config["AZURE_OPENAI_VISION_DEPLOYMENT"]
        if config.get("AZURE_OPENAI_API_VERSION"):
            setting["AZURE_OPENAI_API_VERSION"] = config["AZURE_OPENAI_API_VERSION"]
    setting.update(keys)
    # Translator is regional: its key is refused without the region it is in.
    # No region from 03 means no Translator to call, so the key stays out too.
    if kept.get("translator_region") and setting.get("AZURE_TRANSLATOR_KEY"):
        setting["AZURE_TRANSLATOR_REGION"] = kept["translator_region"]
    else:
        setting.pop("AZURE_TRANSLATOR_KEY", None)
    return setting


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --settings-only, --code-only
# OUTPUT  the extraction Function App, its settings, and the code published
#         with func
#
# Files, addresses and YouTube videos in, text out, nothing indexed. It needs
# the group and the storage account from 01, and uses Document Intelligence,
# Speech, Vision, Language and Translator from 03 when they are there.


def main() -> int:
    args = arguments(__doc__, options)
    config = settings()
    az = Az(args.dry_run)
    kept = state()
    group, app, account = (
        config["VX_RESOURCE_GROUP"],
        config["VX_EXTRACT_APP"],
        config["VX_STORAGE"],
    )
    host, prefix = f"https://{app}.azurewebsites.net", route_prefix(config)
    begin(
        "05",
        "The extraction service",
        "A utility API of its own: files, addresses and videos in, text out.",
    )

    if not args.dry_run and not az.exists(
        "storage", "account", "show", "--name", account, "--resource-group", group
    ):
        stop(
            f"There is no storage account {account} in {group} yet. Run 01_create_resources.py first."
        )

    if not args.code_only:
        step("The paths it answers at")
        print(f"       {'prefix':<16} {prefix or '(none: the routes are at the root)'}")
        print(f"       {'gateway paths':<16} {config.get('VX_EXTRACT_GATEWAY_PATHS') or '(none)'}")
        paths_hold(config)

    if not args.settings_only and not args.code_only:
        step("The app, on Flex Consumption")
        if az.exists("functionapp", "show", "--name", app, "--resource-group", group):
            skipped(app)
        else:
            tries = regions(config["VX_LOCATION"], config["VX_ELSEWHERE"])
            went = somewhere(
                az,
                tries,
                lambda where: az(
                    "functionapp",
                    "create",
                    "--name",
                    app,
                    "--resource-group",
                    group,
                    "--storage-account",
                    account,
                    "--flexconsumption-location",
                    where,
                    "--runtime",
                    "python",
                    "--runtime-version",
                    "3.11",
                    # 2 GB, the default: it holds no model, the services do the reading.
                    "--instance-memory",
                    "2048",
                    "--output",
                    "none",
                    allow_fail=True,
                ),
                lambda: az.exists("functionapp", "show", "--name", app, "--resource-group", group),
                f"the app {app}",
            )
            if not went:
                stop(_no_region(app, tries))
            done(f"{app}, Python 3.11, 2 GB an instance, an hour a job, in {went}")

    if not args.code_only:
        step("The settings")
        now = {} if args.dry_run else current_settings(az, app, group)
        key = now.get("VECTRIXDB_API_KEY") or secrets.token_urlsafe(32)
        every = extraction_env(config, kept, {} if args.dry_run else service_keys(az, config), key)
        for name, value in sorted(every.items()):
            print(f"       {name:<34} {'...' if name.endswith('_KEY') else value}")
        az(
            "functionapp",
            "config",
            "appsettings",
            "set",
            "--name",
            app,
            "--resource-group",
            group,
            "--settings",
            *[f"{k}={v}" for k, v in every.items()],
            "--output",
            "none",
        )
        kept_key = (
            "the key it already had, so its callers are not cut off"
            if now.get("VECTRIXDB_API_KEY")
            else "a new key"
        )
        done(f"{len(every)} settings, sent straight to the app, with {kept_key}")
        # A path setting taken out of settings.env comes off the app too, or
        # the app would go on answering at the old paths.
        stale = [theirs for theirs in PASSED_ON.values() if theirs in now and theirs not in every]
        if stale:
            az(
                "functionapp",
                "config",
                "appsettings",
                "delete",
                "--name",
                app,
                "--resource-group",
                group,
                "--setting-names",
                *stale,
                "--output",
                "none",
            )
            done(f"took off {', '.join(stale)}, which settings.env no longer sets")
        for _name, _kept, setting, without in SERVICES:
            if not every.get(f"{setting}_ENDPOINT"):
                note(f"no {setting}_ENDPOINT from 03, so {without}")
        if not every.get("AZURE_TRANSLATOR_KEY"):
            note(
                "no Translator from 03, so /translate/text and /translate/detect answer 503 and name the setting they need"
            )
        if not every.get("VECTRIXDB_EXTRACT_URL_HOSTS"):
            note(
                "no VX_EXTRACT_URL_HOSTS, so the routes that fetch an address refuse every one, which is the safe way round"
            )

    if not args.settings_only:
        step("The wheel it installs")
        wheel_ready(EXTRACTION_APP, NEEDS, args.dry_run)

        step("The code")
        if shutil.which("func") is None and not args.dry_run:
            stop(
                "The Azure Functions Core Tools are not on this machine, and they are what publishes the code.\n"
                "  npm install -g azure-functions-core-tools@4 --unsafe-perm true\n"
                "  or see https://aka.ms/functions-core-tools"
            )
        print(f"  $ func azure functionapp publish {app} --python   (in {EXTRACTION_APP})")
        if not args.dry_run:
            done("publishing, which builds the wheels remotely and takes a few minutes")
            finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [shutil.which("func"), "azure", "functionapp", "publish", app, "--python"],
                cwd=str(EXTRACTION_APP),
            )
            if finished.returncode != 0:
                stop(
                    "func could not publish. Its output above says why; the usual cause is being signed in as somebody else."
                )
            done("published")

    remember(extract_app=app, extract_host=host)
    links(
        ("is it alive", f"{host}{prefix}/health"),
        ("what it was given, with no secret in it", f"{host}{prefix}/health/wiring"),
        ("every route, to try", f"{host}{prefix}/docs"),
        ("the function app", portal("function", app, config)),
        ("its live log", portal("function", app, config, page="logStream")),
    )
    note(
        "health, health/wiring and the docs need no key; every other route needs it, in the api-key header"
    )
    note(
        "a 404 on health means the app loaded nothing: its live log then has one message listing every setting that is wrong"
    )
    note("the key is in the app's settings and nowhere else. Read it when you need it:")
    print(
        f"       az functionapp config appsettings list --name {app} --resource-group {group} "
        "--query \"[?name=='VECTRIXDB_API_KEY'].value\" --output tsv"
    )
    note(
        f'then: curl -X POST {host}{prefix}/extract/pdf -H "api-key: $KEY" -H "X-Filename: a.pdf" --data-binary @a.pdf'
    )
    note(
        "YouTube often refuses an address in a cloud; when it does, the route says so rather than failing quietly"
    )
    note("99_delete_everything.py deletes this app with the rest of the group")
    finish(
        f"The extraction service is at {host}{prefix}",
        "06_create_main_function_app.py",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
