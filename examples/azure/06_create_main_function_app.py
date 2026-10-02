"""The main Function App, deployed twice, its settings, the Event Grid link, and the code.

    python 06_create_main_function_app.py
    python 06_create_main_function_app.py --settings-only    # after changing a key
    python 06_create_main_function_app.py --code-only        # after changing the code
    python 06_create_main_function_app.py --new-break-glass  # a new emergency password, into the key vault

It runs after 05_create_extraction_function_app.py, because the main app reads
every file through the extraction app: it sends each one to the route its
suffix names, and keeps and indexes the Markdown that comes back. Nothing is
read here any more.

Four things happen here, in this order, because each needs the one before it:

1. The two apps, on Flex Consumption: the same folder deployed twice. The
   ingest app runs the queue trigger and answers health; the query app serves
   the library's API and the dashboard and never reads a file, its trigger
   disabled by an app setting. Both read the same stores, and scale apart:
   the ingest app on the queue, the query app on requests, so a burst of two
   hundred files never slows a search. ``INGEST_ROLE`` tells each which it is.
   The app itself, on Flex Consumption. Consumption stops a function after ten
   minutes, which an hour of audio would not survive; Flex allows an hour, and
   is still billed per use with nothing running between files. Its identity
   gets the blob and queue roles, and a Cosmos DB data role when 03 made the
   account, because Cosmos keeps roles for its data apart from Azure's. The
   one role covers both databases: what ingestion made, and who may see what.
2. Its settings: where the store is, where the blobs are, and where the
   extraction app is, with its key and the prefix and gateway paths it
   answers at. The key is read from the extraction app's own settings, and
   the others from Azure, here, and sent straight to the app. None is written
   to a file on this machine. The company's brand goes too, a logo and a
   palette inside their settings, checked here by the library first; and how
   people sign in: the query app's own list, or single sign-on narrowed to a
   list, with emergency sign-in for while it is down, whose password is kept
   in a key vault and only its hash set on the app.
3. The Event Grid subscription, from the storage account to the queue. Its
   endpoint is the **queue**, not the function, which is what makes a long
   read safe: Event Grid's only job is to write a message.
4. The code, with ``func azure functionapp publish``.

The subscription is filtered to ingestion/raw/ on purpose. The Markdown and
the chunks the function writes sit beside it in ingestion/markdown/ and
ingestion/chunks/, and an event for either would hand the function its own
output: it would read what it wrote, write it again, and go round for ever.
The filter is the guard that matters; the router ignoring everything outside
raw/ is the second one. Deleted events pass the filter too, because a file
deleted from raw/ takes its Markdown, its chunks and its place in the index
with it.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from _common import (
    Az,
    BREAK_GLASS_SETTINGS,
    CATALOG,
    EVALS,
    INGESTION,
    LOCAL,
    MAIN_FUNCTION_APP,
    OLD_CATALOG,
    QUEUE,
    RAW,
    REPO,
    arguments,
    begin,
    _cosmos_key,
    dashboard_address,
    done,
    env_of,
    finish,
    keep_setting,
    links,
    _no_region,
    note,
    portal,
    regions,
    remember,
    settings,
    single_sign_on,
    skipped,
    somewhere,
    sso_alone,
    state,
    step,
    stop,
    tidy_old_catalog,
    wants,
    wheel_ready,
)


# ============================================================================
# SETTINGS: what the wheel must hold, and the Cosmos role
# ============================================================================
#
# What the wheel must hold before the app can be published, and the Cosmos DB
# role the app's identity is given.
#: What the app imports that only a new enough wheel has: the table it routes
#: files by, the pieces a long file goes in, the settings reader, the chunk
#: store every collection writes and the hosted pages read, step five's
#: chunking comparison with the routes the Chunking tab reads and what step
#: ten is handed as it measured, the way of searching a pin names, a
#: document kept before it is cut, and the collection records every server
#: reads each collection's rules from. A wheel without the chunk store would
#: refuse chunk_store= and open no collection at all, and one without the
#: records would refuse collection_store= the same way; one without the rest
#: would fail each chunking message, or keep no file waiting for its cut.
NEEDS = {
    "vectrixdb/api/extraction.py": "def extraction_routes",
    "vectrixdb/extract/batches.py": "def batched",
    "vectrixdb/extract/__init__.py": "def from_environment",
    "vectrixdb/chunk_store.py": "class CosmosChunks",
    "vectrixdb/api/server.py": "def collection_store_from_env",
    "vectrixdb/_eval_chunking.py": "def handed_over",
    "vectrixdb/_setups.py": "def search_of",
    "vectrixdb/easy.py": "index: bool = True",
    "vectrixdb/api/evaluations.py": "async def list_chunking",
    "vectrixdb/collection_records.py": "class CollectionRecords",
}

#: What the app has to install when it is given the second vector: the
#: deployment is called through the openai package, and without it every
#: collection refuses to open, so no file is read or searched.
SECOND_VECTOR = {"openai": "the second vector is embedded with"}


#: Cosmos DB Built-in Data Contributor: reads and writes items, and makes nothing.
COSMOS_DATA_CONTRIBUTOR = "00000000-0000-0000-0000-000000000002"


# ============================================================================
# COSMOS: the grant
# ============================================================================
#
# INPUT   the app's identity and the Cosmos account
# OUTPUT  the identity may read and write the account's items
#
# Once: asking again is refused, not ignored, so the grant is looked for
# before it is asked for.


def grant_cosmos(az: Az, account: str, group: str, principal_id: str) -> None:
    """The app's identity may read and write the Cosmos account's items. Once: asking again is refused, not ignored.

    Cosmos DB's data has roles of its own, apart from Azure's, and the
    account's Owner reads none of it. Without this one the app is refused the
    parent sections and the chunks the pages count, a write to the chunk
    store fails the file it was part of, and a collection whose rules cannot
    be read is not searched at all.
    """
    held = az(
        "cosmosdb",
        "sql",
        "role",
        "assignment",
        "list",
        "--account-name",
        account,
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    for assignment in held or []:
        if (
            isinstance(assignment, dict)
            and assignment.get("principalId") == principal_id
            and str(assignment.get("roleDefinitionId", "")).endswith(COSMOS_DATA_CONTRIBUTOR)
        ):
            skipped("Cosmos DB Built-in Data Contributor")
            return
    az(
        "cosmosdb",
        "sql",
        "role",
        "assignment",
        "create",
        "--account-name",
        account,
        "--resource-group",
        group,
        "--role-definition-id",
        COSMOS_DATA_CONTRIBUTOR,
        "--principal-id",
        principal_id,
        "--scope",
        "/",
        "--output",
        "none",
        allow_fail=True,
    )
    done("Cosmos DB Built-in Data Contributor")


# ============================================================================
# OPTIONS AND KEYS
# ============================================================================
#
# INPUT   the command line; the app; what 02, 03 and 05 recorded
# OUTPUT  --settings-only or --code-only; every key the app needs, read fresh
#         from Azure; the key every recorded query is fingerprinted with, the
#         app's own or a new one the first time
#
# Keys are read fresh and never written down here.


def options(parser) -> None:
    parser.add_argument(
        "--settings-only", action="store_true", help="only push the settings, make nothing"
    )
    parser.add_argument("--code-only", action="store_true", help="only publish the code")
    parser.add_argument(
        "--add-person",
        metavar="EMAIL",
        help="add somebody to the query app's sign-in, as an admin, and print their one-time link here",
    )
    parser.add_argument(
        "--new-break-glass",
        action="store_true",
        help="make a new emergency password, keep it in the key vault, and write its hash to settings.env; neither is shown",
    )


def keys_for(az: Az, config: dict, kept: dict) -> dict:
    """Every key the app needs, read fresh from Azure and never written down here.

    The search key; the Azure OpenAI key, for the second vector; and the
    extraction app's own key, which this app presents with every file it
    sends there. Document Intelligence, Speech and Vision keys are the
    extraction app's to hold, so this app is not given them.
    """
    group = config["VX_RESOURCE_GROUP"]
    secrets = {}
    found = az(
        "search",
        "admin-key",
        "show",
        "--service-name",
        config["VX_SEARCH"],
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    if found:
        secrets["AZURE_SEARCH_KEY"] = found["primaryKey"]
    secrets["AZURE_SEARCH_SEMANTIC"] = "true" if kept.get("search_tier") == "basic" else "false"
    found = az(
        "cognitiveservices",
        "account",
        "keys",
        "list",
        "--name",
        config["VX_OPENAI_NAME"],
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    if found:
        secrets["AZURE_OPENAI_KEY"] = found["key1"]
    extraction = az(
        "functionapp",
        "config",
        "appsettings",
        "list",
        "--name",
        config["VX_EXTRACT_APP"],
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    for entry in extraction or []:
        if (
            isinstance(entry, dict)
            and entry.get("name") == "VECTRIXDB_API_KEY"
            and entry.get("value")
        ):
            secrets["VECTRIXDB_EXTRACTOR_KEY"] = entry["value"]
    if kept.get("audit_container"):
        secrets["VECTRIXDB_AUDIT_QUERY_KEY"] = query_key(az, config)
    if "VECTRIXDB_SIGNIN" in env_of(config, kept, {}):
        # What the sign-in links and cookies are signed with. A new one would
        # sign everybody out and spoil every link not yet opened.
        secrets["VECTRIXDB_SIGNIN_SECRET"] = kept_or_new(az, config, "VECTRIXDB_SIGNIN_SECRET")
    return secrets


def query_key(az: Az, config: dict) -> str:
    """The key every recorded query is fingerprinted with: the app's own, or a new one the first time.

    A decision records an HMAC of the query rather than the query, so two
    searches for the same thing can be matched without the log saying what
    was asked. The key has to stay the same for as long as the log is kept,
    or last week's fingerprints match nothing this week, so it is read back
    from the app and only made when the app has none. It lives in the app's
    settings and nowhere on this machine.
    """
    return kept_or_new(az, config, "VECTRIXDB_AUDIT_QUERY_KEY")


def kept_or_new(az: Az, config: dict, name: str) -> str:
    """A secret the app must keep for as long as it runs: its own, read back, or a new one the first time."""
    import secrets as randomness

    return app_setting(az, config, name) or randomness.token_hex(32)


def app_setting(az: Az, config: dict, name: str, app: Optional[str] = None) -> Optional[str]:
    """One of an app's settings, read from Azure and never printed: the ingest app's unless another is named."""
    held = az(
        "functionapp",
        "config",
        "appsettings",
        "list",
        "--name",
        app or config["VX_FUNCTION_APP"],
        "--resource-group",
        config["VX_RESOURCE_GROUP"],
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    for entry in held or []:
        if isinstance(entry, dict) and entry.get("name") == name and entry.get("value"):
            return str(entry["value"])
    return None


# ============================================================================
# SOMEBODY WHO MAY SIGN IN
# ============================================================================
#
# INPUT   an email address, and the query app's sign-in as the settings step
#         left it
# OUTPUT  that person in the sign-in store, as an admin, and a one-time link
#         for them, printed in this terminal and nowhere else
#
# The app sends no mail, so this is how the first person gets in. It runs the
# library's own command with the app's settings, which live in that one
# command's environment and are never written down.

#: The query app's sign-in settings the command is given, as the app has them.
SIGNIN_SETTINGS = ("VECTRIXDB_SIGNIN", "VECTRIXDB_PUBLIC_URL", "VECTRIXDB_SIGNIN_STORE")
#: And, when there are any, the provider's and the gateway's: the command reads the same sign-in, and writes its link the way people reach it.
SIGNIN_SETTINGS_TOO = (
    "VECTRIXDB_OIDC_",
    "VECTRIXDB_PREFIX",
    "VECTRIXDB_GATEWAY_PATHS",
    "VECTRIXDB_ROOT_PATH",
)
#: What single sign-on here cannot do without: the provider, this app at it, and who is an admin.
SSO_NEEDS = ("VX_OIDC_ISSUER", "VX_OIDC_CLIENT_ID", "VX_OIDC_ROLE_MAP")
#: How the query app scales, and what each is when settings.env does not say.
#: Flex Consumption gives a Python app one web request an instance unless told, so a page that asks for
#: ten files starts ten instances, each cold: a quarter of a minute for a request that takes milliseconds.
QUERY_SCALE = (("VX_QUERY_AT_ONCE", 16, 1, 1000), ("VX_QUERY_ALWAYS_READY", 0, 0, 1000))
#: What an app's token may be here: never an admin, since a token is an app.
TOKEN_ROLES = ("reader", "searcher", "viewer", "operator")


def shown_as(name: str, value: str) -> str:
    """A setting as the terminal shows it: a secret as dots, a logo or a palette by its kind and size."""
    if name.endswith(("_KEY", "_SECRET", "_PASSWORD", "_TOTP", "_HASH")):
        return "..."
    if value.startswith("data:"):
        return f"{value.split(',', 1)[0]},...({len(value)} characters)"
    return value


def query_scale(config: dict) -> dict:
    """How many web requests an instance of the query app takes at once, and how many instances are kept ready.

    Stops on a value that is not a whole number in range, naming the line to change.
    """
    out = {}
    for name, otherwise, low, high in QUERY_SCALE:
        given = str(config.get(name, "")).strip()
        if given and not (given.isdigit() and low <= int(given) <= high):
            stop(f"{name} is {given!r} in settings.env. It is a whole number from {low} to {high}")
        out[name] = int(given) if given else otherwise
    return out


def send_query_scale(az, config: dict, name: str, group: str) -> None:
    """The query app's scaling, set as settings.env has it. Run again, it sets the same."""
    scale = query_scale(config)
    at_once, ready = scale["VX_QUERY_AT_ONCE"], scale["VX_QUERY_ALWAYS_READY"]
    az(
        "functionapp",
        "scale",
        "config",
        "set",
        "--name",
        name,
        "--resource-group",
        group,
        "--trigger-type",
        "http",
        "--trigger-settings",
        f"perInstanceConcurrency={at_once}",
        "--output",
        "none",
    )
    done(
        f"{at_once} web requests an instance at once, so a page's files are answered by one instance"
    )
    az(
        "functionapp",
        "scale",
        "config",
        "always-ready",
        "set",
        "--name",
        name,
        "--resource-group",
        group,
        "--settings",
        f"http={ready}",
        "--output",
        "none",
    )
    if ready:
        done(
            f"{ready} instance{'' if ready == 1 else 's'} kept ready, paid for by the hour, so no call waits for a start"
        )
    else:
        done("no instance kept ready: the first call after a quiet spell waits for one to start")


def held_before_sent(config: dict, every: dict) -> None:
    """What would stop the query app from starting, stopped here instead, with the line to change.

    Single sign-on here is with a list: a security group alone would let
    everyone in it into the dashboard. Emergency sign-in needs all its parts
    and a time it turns itself off. And the brand is read by the library
    itself, so a logo it would refuse, or colours it cannot read, stop this
    step rather than the app.
    """
    query_scale(config)
    if single_sign_on(config):
        way = str(config.get("VX_SIGNIN", "")).strip()
        # Neither the provider nor the client named: not set up yet, which the app starts with.
        # The sign-in box says so, and the People list signs in with a code by email until both are.
        pending = not any(
            str(config.get(name, "")).strip() for name in ("VX_OIDC_ISSUER", "VX_OIDC_CLIENT_ID")
        )
        missing = (
            ""
            if pending
            else ", ".join(name for name in SSO_NEEDS if not str(config.get(name, "")).strip())
        )
        if missing:
            stop(f"VX_SIGNIN={way} needs {missing} in settings.env")
        if pending and not str(config.get("VX_SIGNIN_USERS", "")).strip():
            stop(
                f"VX_SIGNIN={way} with no VX_OIDC_ISSUER and no VX_OIDC_CLIENT_ID signs people in by email until they are set, "
                "so it needs the People list: VX_SIGNIN_USERS=you@company.com:admin"
            )
        if (
            not str(config.get("VX_SIGNIN_USERS", "")).strip()
            and not str(config.get("VX_OIDC_ALLOWED_EMAILS", "")).strip()
        ):
            stop(
                f"VX_SIGNIN={way} needs somebody named who may sign in, since not everyone in a security group "
                "may: VX_SIGNIN_USERS=you@company.com:admin puts them on the People list, or VX_OIDC_ALLOWED_EMAILS names them"
            )
        role = str(config.get("VX_OIDC_TOKEN_ROLE", "")).strip().lower()
        if role and role not in TOKEN_ROLES:
            stop(
                f"VX_OIDC_TOKEN_ROLE is {role!r}. It is reader, searcher, viewer or operator: never an admin, since a token is an app"
            )
        days = str(config.get("VX_SSO_RECHECK_DAYS", "")).strip()
        if days and (sso_alone(config) or not days.isdigit() or int(days) < 1):
            why = (
                "needs VX_SIGNIN=sso, where people keep a code of their own"
                if sso_alone(config)
                else "is a whole number of days, 1 or more"
            )
            stop(f"VX_SSO_RECHECK_DAYS {why}")
    # Emergency sign-in goes with every way in: it is for while the usual one is down, whichever that is.
    if wants(config, "VX_BREAK_GLASS"):
        if not (wants(config, "VX_SIGNIN") or single_sign_on(config)):
            stop(
                "VX_BREAK_GLASS is for while the usual sign-in is down, and VX_SIGNIN is no. Set VX_BREAK_GLASS=no"
            )
        old = ", ".join(
            name
            for name in ("VX_BREAK_GLASS_PASSWORD", "VX_BREAK_GLASS_TOTP")
            if str(config.get(name, "")).strip()
        )
        if old:
            stop(
                f"Take {old} out of settings.env. Emergency sign-in asks for no code, and its password stays in the key vault: "
                "only its hash is a setting, which python 06_create_main_function_app.py --new-break-glass writes"
            )
        absent = ", ".join(
            ours for ours, _ in BREAK_GLASS_SETTINGS if not str(config.get(ours, "")).strip()
        )
        if absent:
            stop(
                f"VX_BREAK_GLASS=yes needs {absent} in settings.env. The hash is written by: "
                "python 06_create_main_function_app.py --new-break-glass"
            )
    held_gateway(config, every)
    brand = {name: value for name, value in every.items() if name.startswith("VECTRIXDB_BRAND_")}
    if not brand:
        return
    env = {
        name: value for name, value in os.environ.items() if not name.startswith("VECTRIXDB_BRAND_")
    }
    env.update(brand)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO), os.environ.get("PYTHONPATH"))))
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            "from vectrixdb.brand import Brand; print(Brand.from_env().banner() or '')",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        said = [line for line in (probe.stderr or "").strip().splitlines() if line.strip()]
        why = said[-1] if said else "the library could not read it"
        stop(f"the brand would stop the query app from starting: {why}")
    done(f"the brand, as the library reads it: {probe.stdout.strip()}")


def held_gateway(config: dict, every: dict) -> None:
    """A gateway in front, read by the library itself, so a path no route falls under stops here, not the app."""
    given = str(config.get("VX_GATEWAY_URL", "")).strip()
    if given and (not given.startswith("https://") or len(given) <= len("https://")):
        stop(
            f"VX_GATEWAY_URL is {given!r}. It is the gateway's https address, https://gateway.example.com, or blank to call the app directly"
        )
    gateway = {
        name: value
        for name, value in every.items()
        if name
        in (
            "VECTRIXDB_PREFIX",
            "VECTRIXDB_GATEWAY_PATHS",
            "VECTRIXDB_KEY_HEADER",
            "VECTRIXDB_TOKEN_HEADER",
            "VECTRIXDB_TRUSTED_PROXIES",
            "VECTRIXDB_PUBLIC_URL",
        )
    }
    if not any(name != "VECTRIXDB_PUBLIC_URL" for name in gateway):
        return
    env = {name: value for name, value in os.environ.items() if not name.startswith("VECTRIXDB_")}
    env.update(gateway)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO), os.environ.get("PYTHONPATH"))))
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            "from vectrixdb.api.gateway import Gateway; from vectrixdb.api.server import served_paths; from vectrixdb.api.forwarded import trusted_from_env; "
            "g = Gateway.from_env(); g.check(served_paths()); trusted_from_env(); print(g.visible('/dashboard/'))",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        said = [line for line in (probe.stderr or "").strip().splitlines() if line.strip()]
        why = said[-1] if said else "the library could not read them"
        stop(f"the gateway settings would stop the query app from starting: {why}")
    done(f"the gateway, as the library reads it: the dashboard at {probe.stdout.strip()}")


#: The name the emergency password is kept under in the key vault.
VAULT_SECRET = "vx-break-glass"


def new_break_glass(az: Az, config: dict, dry_run: bool = False) -> int:
    """A new emergency password, kept in the key vault, and its hash written to settings.env. Neither is shown.

    The password is made here, 32 characters, and goes to the vault from a
    file that is there only for that one command. Its hash is made by the
    library the apps run, given the password on its input and never as an
    argument. A password works for one emergency, so this is run again
    before the next.
    """
    begin(
        "06",
        "A new emergency password",
        "Made here, kept in the key vault, and only its hash written to settings.env.",
    )
    vault = str(config.get("VX_KEY_VAULT", "")).strip()
    if not vault:
        stop(
            "--new-break-glass needs VX_KEY_VAULT in settings.env: the name of a key vault this az login may write secrets to. "
            "The password is kept there and nowhere else"
        )
    admin = str(config.get("VX_BREAK_GLASS_ADMIN", "")).strip() or "emergency.admin"
    held = LOCAL / "new-break-glass.txt"
    step("The password, into the key vault")
    if dry_run:
        az(
            "keyvault",
            "secret",
            "set",
            "--vault-name",
            vault,
            "--name",
            VAULT_SECRET,
            "--file",
            str(held),
            "--encoding",
            "utf-8",
            "--output",
            "none",
        )
        note("a dry run makes no password and writes nothing")
        return 0
    password = secrets.token_urlsafe(24)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(REPO), os.environ.get("PYTHONPATH")))),
    }
    hashed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            "import sys; from vectrixdb.signin import break_glass_hash; print(break_glass_hash(sys.stdin.read().strip(), sys.argv[1]))",
            admin,
        ],
        input=password,
        env=env,
        capture_output=True,
        text=True,
    )
    made = (hashed.stdout or "").strip()
    if hashed.returncode != 0 or not made.startswith("scrypt$"):
        stop(
            "the library could not make the hash, so nothing was kept. Is the library this walkthrough is part of the one on this machine?"
        )
    LOCAL.mkdir(parents=True, exist_ok=True)
    try:
        held.write_text(password, encoding="utf-8")
        az(
            "keyvault",
            "secret",
            "set",
            "--vault-name",
            vault,
            "--name",
            VAULT_SECRET,
            "--file",
            str(held),
            "--encoding",
            "utf-8",
            "--output",
            "none",
        )
    finally:
        held.unlink(missing_ok=True)
    done(f"{VAULT_SECRET}, in the key vault {vault}")
    step("Its hash, into settings.env")
    keep_setting("VX_BREAK_GLASS_PASSWORD_HASH", made)
    done("VX_BREAK_GLASS_PASSWORD_HASH")
    note(
        "neither was shown here. In the emergency, whoever may read the vault reads the password there"
    )
    note("a password works for one emergency: run this again before the next")
    finish(
        "When the usual sign-in is down: set VX_BREAK_GLASS=yes and VX_BREAK_GLASS_UNTIL in settings.env",
        "then python 06_create_main_function_app.py --settings-only",
    )
    return 0


#: Where the line saying who was added goes: this machine. The audit container
#: takes writes only from a role on its data, and an az login that owns the
#: subscription has none.
CONSOLE_ACCESS = LOCAL / "console-access.jsonl"


def add_person(az: Az, config: dict, kept: dict, email: str, dry_run: bool = False) -> int:
    """Somebody added to the query app's sign-in, as an admin, and their one-time link printed here.

    The command is the library's ``vectrixdb people add``, given what the app
    has: the sign-in settings, the secret its links are signed with, read
    back from the app, and the Cosmos account key, because an az login has no
    role on the account's data. The link works once, for fifteen minutes,
    and opens a page on the query app where they choose a passkey or an
    authenticator app, so the app has to be running. After that, admins add
    people on the dashboard's Access page.
    """
    begin(
        "06",
        "Somebody who may sign in",
        f"{email}, added to the query app's sign-in as an admin, with a one-time link.",
    )
    every = env_of(config, kept, {})
    if "VECTRIXDB_SIGNIN" not in every:
        stop(
            "Sign-in is not on for the query app. It needs VX_SIGNIN=yes or sso in settings.env and the sign-in store "
            "03_create_ai_services.py makes, then: python 06_create_main_function_app.py --settings-only"
        )
    command = [
        sys.executable,
        "-m",
        "vectrixdb.cli",
        "people",
        "add",
        email,
        "--role",
        "admin",
        "--path",
        str(LOCAL / "console"),
    ]
    print(f"  $ vectrixdb people add {email} --role admin")
    print(
        "       with the query app's sign-in settings, its link secret and the Cosmos key in this command's environment only"
    )
    if dry_run:
        return 0
    secret = app_setting(az, config, "VECTRIXDB_SIGNIN_SECRET", app=config["VX_QUERY_APP"])
    if not secret:
        stop(
            "The query app has no sign-in secret yet, so a link made now would not open there. "
            "First: python 06_create_main_function_app.py --settings-only"
        )
    CONSOLE_ACCESS.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        **{name: every[name] for name in SIGNIN_SETTINGS},
        **{name: value for name, value in every.items() if name.startswith(SIGNIN_SETTINGS_TOO)},
        "VECTRIXDB_SIGNIN_SECRET": secret,
        "VECTRIXDB_SIGNIN_STORE_KEY": _cosmos_key(
            az, config["VX_COSMOS_NAME"], config["VX_RESOURCE_GROUP"]
        ),
        "VECTRIXDB_ACCESS_LOG": str(CONSOLE_ACCESS),
        # The library the apps were built from, not whichever one this Python
        # has installed: a release from before sign-in has no people command,
        # and one that has it could write what the app does not read.
        "PYTHONPATH": os.pathsep.join(filter(None, (str(REPO), os.environ.get("PYTHONPATH")))),
    }
    finished = subprocess.run(command, env=env)  # noqa: S603 - fixed argv, no shell
    if finished.returncode != 0:
        stop("vectrixdb people add added nobody. What it printed above says why.")
    note(
        "open the link within fifteen minutes and choose a passkey or an authenticator app; the query app has to be running"
    )
    note(f"the line saying who was added is in {CONSOLE_ACCESS}, not the audit container")
    finish(
        f"Then sign in at {dashboard_address(config)}",
        "anyone else, on the dashboard's Access page",
    )
    return 0


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   state.json from 01 to 05, and settings.env
# OUTPUT  the main Function App on Flex Consumption, its settings, the Event
#         Grid link from raw/ to the queue, and the code published
#
# Four things in this order, because each needs the one before it. It runs
# after 05, because the main app reads every file through the extraction app
# and keeps and indexes the Markdown that comes back.


def main() -> int:
    args = arguments(__doc__, options)
    config = settings()
    az = Az(args.dry_run)
    kept = state()
    group, app, account = (
        config["VX_RESOURCE_GROUP"],
        config["VX_FUNCTION_APP"],
        config["VX_STORAGE"],
    )
    query_app = config["VX_QUERY_APP"]
    if args.add_person:
        return add_person(az, config, kept, args.add_person, args.dry_run)
    if args.new_break_glass:
        return new_break_glass(az, config, args.dry_run)
    # The same folder twice: the ingest app reads files, the query app answers questions.
    apps = ((app, "ingest"), (query_app, "query"))
    begin(
        "06",
        "The main Function App, twice",
        "The ingest app and the query app, their settings, the event link, and the code.",
    )

    if not kept.get("search_endpoint") and not args.dry_run:
        stop("The search service is not known yet. Run 02_create_search.py first.")
    if not kept.get("extract_host") and not args.dry_run:
        stop(
            "The extraction app is not known yet. Run 05_create_extraction_function_app.py first: "
            "this app reads every file through it."
        )

    if not args.settings_only and not args.code_only:
        step("The two apps, on Flex Consumption")
        for name, what in apps:
            if az.exists("functionapp", "show", "--name", name, "--resource-group", group):
                skipped(f"{name}, the {what} app")
                continue
            tries = regions(config["VX_LOCATION"], config["VX_ELSEWHERE"])
            went = somewhere(
                az,
                tries,
                lambda where, name=name: az(
                    "functionapp",
                    "create",
                    "--name",
                    name,
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
                    # 4 GB, not the 2 GB default: the embedding model runs inside
                    # the function, and the wheel carries it.
                    "--instance-memory",
                    "4096",
                    "--output",
                    "none",
                    allow_fail=True,
                ),
                lambda name=name: az.exists(
                    "functionapp", "show", "--name", name, "--resource-group", group
                ),
                f"the {what} app {name}",
            )
            if not went:
                stop(_no_region(name, tries))
            done(
                f"{name}, the {what} app, Python 3.11, 4 GB an instance, an hour a file, in {went}"
            )

        step("Their identities, so they read blobs without a key")
        scope = az(
            "storage",
            "account",
            "show",
            "--name",
            account,
            "--resource-group",
            group,
            reads=True,
            quiet=True,
        )
        for name, what in apps:
            az(
                "functionapp",
                "identity",
                "assign",
                "--name",
                name,
                "--resource-group",
                group,
                "--output",
                "none",
            )
            principal = az(
                "functionapp",
                "identity",
                "show",
                "--name",
                name,
                "--resource-group",
                group,
                reads=True,
                quiet=True,
                allow_fail=True,
            )
            if not (principal and principal.get("principalId")):
                continue
            if scope:
                for role in ("Storage Blob Data Contributor", "Storage Queue Data Contributor"):
                    az(
                        "role",
                        "assignment",
                        "create",
                        "--assignee-object-id",
                        principal["principalId"],
                        "--assignee-principal-type",
                        "ServicePrincipal",
                        "--role",
                        role,
                        "--scope",
                        scope["id"],
                        "--output",
                        "none",
                        allow_fail=True,
                    )
                    done(f"{role}, the {what} app")
            if kept.get("parent_store") or kept.get("chunk_store") or kept.get("collection_store"):
                grant_cosmos(az, config["VX_COSMOS_NAME"], group, principal["principalId"])
        note("a role takes a minute or two to take effect, which is why 07 waits before it worries")

    if not args.code_only:
        step("The settings, the same on both but for the role")
        secrets = keys_for(az, config, kept) if not args.dry_run else {}
        every = env_of(config, kept, secrets)
        held_before_sent(config, every)
        shown = {k: shown_as(k, v) for k, v in every.items()}
        for name, value in sorted(shown.items()):
            print(f"       {name:<34} {value}")
        for name, what in apps:
            own = {**every, "INGEST_ROLE": what}
            if what == "query":
                # The platform's own switch: the query app's host never pulls a message, whatever the code does.
                own["AzureWebJobs.ingest_one.Disabled"] = "true"
            az(
                "functionapp",
                "config",
                "appsettings",
                "set",
                "--name",
                name,
                "--resource-group",
                group,
                "--settings",
                *[f"{k}={v}" for k, v in own.items()],
                "--output",
                "none",
            )
            done(f"{len(own)} settings, keys among them, sent straight to the {what} app")
        note(
            "INGEST_ROLE=ingest on the first, INGEST_ROLE=query and AzureWebJobs.ingest_one.Disabled=true on the second"
        )
        if every.get("VECTRIXDB_EXTRACTOR_URL"):
            note(
                f"every file is read by the extraction app at {every['VECTRIXDB_EXTRACTOR_URL']}, at the route its suffix names"
            )
            note("/health/wiring shows the whole table, suffix by suffix")
        if not args.dry_run and not secrets.get("VECTRIXDB_EXTRACTOR_KEY"):
            note(
                "the extraction app's key was not found in its settings, so it will refuse every file: run 05_create_extraction_function_app.py again"
            )
        step("How the query app scales")
        send_query_scale(az, config, query_app, group)

        # A run from before AZURE_SEARCH_INDEX_PREFIX was set empty left the
        # library's default catalog, vectrix-collections, in the service: an
        # empty index nobody reads, beside the collections index the app
        # does. It is taken away here, where the prefix is known, and only
        # when it is empty: a full one is somebody's and is said so.
        step(f"A leftover {OLD_CATALOG} index, from before the prefix was set to nothing")
        went = tidy_old_catalog(az, config, every.get("AZURE_SEARCH_INDEX_PREFIX", ""))
        if went is None:
            done(f"none: the service holds no {OLD_CATALOG}, and the app reads {CATALOG}")
        elif went == "unknown":
            note(
                f"the service cannot be asked in a dry run; on a real run an empty {OLD_CATALOG} is deleted, and 99 lists every index with its count"
            )

    if not args.settings_only and not args.code_only:
        step("Event Grid, from the blobs to the queue")
        name = f"{app}-ingest"
        source = az(
            "storage",
            "account",
            "show",
            "--name",
            account,
            "--resource-group",
            group,
            reads=True,
            quiet=True,
        )
        if az.exists(
            "eventgrid",
            "event-subscription",
            "show",
            "--name",
            name,
            "--source-resource-id",
            (source or {}).get("id", ""),
        ):
            skipped(name)
        elif source:
            az(
                "eventgrid",
                "event-subscription",
                "create",
                "--name",
                name,
                "--source-resource-id",
                source["id"],
                "--endpoint-type",
                "storagequeue",
                "--endpoint",
                f"{source['id']}/queueServices/default/queues/{QUEUE}",
                "--included-event-types",
                "Microsoft.Storage.BlobCreated",
                "Microsoft.Storage.BlobDeleted",
                # Only raw/. The function writes markdown/ and chunks/ into the
                # same container, and an event for those would feed it its own
                # output.
                "--subject-begins-with",
                f"/blobServices/default/containers/{INGESTION}/blobs/{RAW}/",
                "--output",
                "none",
            )
            done(f"{name}, {INGESTION}/{RAW} only, created and deleted")

    if not args.settings_only:
        step("The wheel it installs")
        second = "AZURE_OPENAI_EMBED_DEPLOYMENT" in env_of(config, kept, {})
        wheel_ready(
            MAIN_FUNCTION_APP, NEEDS, args.dry_run, installs=SECOND_VECTOR if second else None
        )

        step("The code")
        if shutil.which("func") is None and not args.dry_run:
            stop(
                "The Azure Functions Core Tools are not on this machine, and they are what publishes the code.\n"
                "  npm install -g azure-functions-core-tools@4 --unsafe-perm true\n"
                "  or see https://aka.ms/functions-core-tools"
            )
        for name, what in apps:
            print(f"  $ func azure functionapp publish {name} --python   (in {MAIN_FUNCTION_APP})")
            if not args.dry_run:
                done(
                    f"publishing the {what} app, which builds the wheels remotely and takes a few minutes"
                )
                finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [shutil.which("func"), "azure", "functionapp", "publish", name, "--python"],
                    cwd=str(MAIN_FUNCTION_APP),
                )
                if finished.returncode != 0:
                    stop(
                        "func could not publish. Its output above says why; the usual cause is being signed in as somebody else."
                    )
                done(f"published, the {what} app")

    remember(
        function_app=app,
        function_host=f"https://{app}.azurewebsites.net",
        query_app=query_app,
        query_host=f"https://{query_app}.azurewebsites.net",
    )
    links(
        ("is the ingest app alive", f"https://{app}.azurewebsites.net/health"),
        ("what the ingest app is wired to", f"https://{app}.azurewebsites.net/health/wiring"),
        ("is the query app alive", f"https://{query_app}.azurewebsites.net/health"),
        ("what the query app holds", f"https://{query_app}.azurewebsites.net/api/v1/info"),
        ("the dashboard", f"https://{query_app}.azurewebsites.net/"),
        ("the ingest app", portal("function", app, config)),
        ("its live log", portal("function", app, config, page="logStream")),
        ("the query app", portal("function", query_app, config)),
    )
    note(
        "point the dashboard's UPSTREAM, and anyone who searches, at the query app; files still land in the container, whichever app is asked"
    )
    note(
        "open the health address in a browser: it needs no key, and a reply proves the worker indexed the code"
    )
    note(
        "a 404 there means the app loaded no functions at all, which is what one function failing to load looks like"
    )
    note(
        "health says it is up, health/wiring says what it was given, /api/v1/info is the one that proves Azure was reached"
    )
    note(
        "the query app publishes the library's whole API, about sixty routes, and its key layer is the door, not a function key"
    )
    finish(
        f"The ingest app is at https://{app}.azurewebsites.net and the query app at https://{query_app}.azurewebsites.net",
        "07_push_new_files_from local_to_blob_cosmosdb.py",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
