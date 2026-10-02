"""What every script here shares: the settings, and a way to run ``az`` that shows its working.

Nothing in this folder talks to Azure through an SDK but one thing. Every step
is an ``az`` command, printed before it runs, so you can read what is about to
happen, stop, or type the same command yourself. ``--dry-run`` prints every
command that would change something and runs none of them; the two pushes
look first, reading only, so their plan says what is already there. The one thing is writing an item into Cosmos DB, which no ``az``
command does: each collection's record goes up through the library's own
``CollectionRecords``, so what is written is exactly what every server reads.

Settings come from ``settings.env`` beside this file, which you make once by
copying ``settings.example.env``. Names are built from ``VX_PREFIX`` so there
is one word to change when a name is taken.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ============================================================================
# SETTINGS: the files, the folders and the providers
# ============================================================================
#
# Where settings.env, state.json and the local mirror of the containers live,
# the resource providers the walkthrough registers, and the names the
# containers, folders and apps are known by.

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "settings.env"
EXAMPLE = HERE / "settings.example.env"
STATE = HERE / "state.json"
#: The cloud on this machine. What is under blob/ is laid out as the
#: storage account is, so a push is a copy and a pull is a copy back.
LOCAL = HERE / ".local"
#: The name shared by both sides of the mirror. Under blob/ it is the account
#: folder, one name whatever the storage account is called, so the tree reads
#: account then container. Under cosmosdb/ it is the one database step 03
#: creates, which holds every container.
DATA = "data_db"
BLOB = LOCAL / "blob" / DATA
#: Held back on purpose: these have not been dropped yet, so they are not
#: in the container, and a mirror that showed them would be lying.
LATER = LOCAL / "later"
SOURCES = LOCAL / "sources.json"
#: The main Function App's code, which 06_create_main_function_app.py publishes.
MAIN_FUNCTION_APP = HERE / "main_function_app"
#: The extraction service's code: a utility API that stands alone, made by 05_create_extraction_function_app.py.
EXTRACTION_APP = HERE / "extraction_app"
#: The library's own repository, whose dist/ holds the wheel both apps install.
REPO = HERE.parents[1]

#: What each script needs switched on before it can make anything. A brand new
#: subscription has every one of them off, and Azure reports the first attempt
#: as "SubscriptionNotFound", which sends you looking in the wrong place
#: entirely. Registering is free, idempotent, and takes a minute or two once.
PROVIDERS = {
    "Microsoft.Storage": "the storage account, the containers and the queue",
    "Microsoft.Search": "Azure AI Search",
    "Microsoft.Web": "the two Function Apps, the extraction service and the main app",
    "Microsoft.CognitiveServices": "Document Intelligence, Speech, Vision and Azure OpenAI",
    "Microsoft.DocumentDB": "Cosmos DB, where the parent sections, the pages' chunks and who may see what live",
    "Microsoft.EventGrid": "the event a new blob raises, and the link 06 makes from it to the queue",
    "Microsoft.Insights": "the Function Apps' logs",
    "Microsoft.OperationalInsights": "where those logs are kept",
}


# ============================================================================
# PROVIDERS: switched on, and waited for
# ============================================================================
#
# INPUT   the providers the walkthrough needs
# OUTPUT  each registered, once, and waited for
#
# A new subscription has almost none registered, and the first create of each
# kind is then refused with SubscriptionNotFound, which sends you looking at
# the subscription when the fault is the provider.


def register(az: Any, wait: float = 240.0) -> None:
    """Switch on every provider this walkthrough needs, and wait for them.

    Azure registers a provider asynchronously, so asking is not enough: a
    create that follows too closely fails the same way it did before. This
    asks for all of them at once, then waits until each says Registered.
    """
    import time

    state_of = {}
    for namespace in PROVIDERS:
        found = az("provider", "show", "--namespace", namespace, reads=True, quiet=True, allow_fail=True)
        state_of[namespace] = (found or {}).get("registrationState", "Unknown")
    missing = [n for n, s in state_of.items() if s != "Registered"]
    if not missing:
        skipped(f"all {len(PROVIDERS)} resource providers are on")
        return

    step(f"Switching on {len(missing)} resource providers, which a new subscription has off")
    for namespace in missing:
        print(f"       {namespace:<32} {PROVIDERS[namespace]}")
        az("provider", "register", "--namespace", namespace, "--output", "none", allow_fail=True)
    if az.pretend:
        return

    began = time.time()
    left = list(missing)
    while left and time.time() - began < wait:
        time.sleep(10)
        for namespace in list(left):
            found = az("provider", "show", "--namespace", namespace, reads=True, quiet=True, allow_fail=True)
            if (found or {}).get("registrationState") == "Registered":
                done(namespace)
                left.remove(namespace)
    if left:
        note(f"still registering: {', '.join(left)}. That is Azure being slow, not a failure.")
        note("  wait a minute and run this again; everything here is safe to run twice")
    else:
        done(f"all {len(missing)} on, which only ever happens once on a subscription")


# ============================================================================
# CONTAINERS, FOLDERS AND NAMES
# ============================================================================
#
# The containers the apps use and the folders inside them: audit with its
# access and decisions, ingestion with raw, markdown and chunks, the queue,
# and the mirror folders on this machine.
#: Containers in the storage account. Originals in, Markdown kept, runs saved.
INGESTION, EVALS = "ingestion", "evals"
#: The third container: what was decided and who looked, a line each, under a
#: retention policy that lets a line be added and never changed. access/ holds
#: every sign-in, read and refusal; decisions/ a line for each decision an
#: entitlement policy makes, empty unless a record carries one.
AUDIT = "audit"
#: The three folders inside the ingestion container. You upload to RAW, and
#: the function writes MARKDOWN, then CHUNKS. Event Grid watches RAW only,
#: because an event for either of the others would hand the function its own
#: output and it would read it again.
RAW, MARKDOWN, CHUNKS = "raw", "markdown", "chunks"
#: The Cosmos containers, all four in the one database, DATA. Two hold what
#: ingestion made, kept as rows where the storage container keeps files: the
#: parent sections a search returns, and a record of every chunk for the
#: collection pages. Not "chunks", which is the folder of chunk files above:
#: one name for two things reads as one thing.
PARENT_SECTIONS, CHUNK_RECORDS = "parent_sections", "chunk_records"
#: And two answer who may see what: collection_records holds each
#: collection's record, its path and who may retrieve from it; signin_records
#: holds the people, their sessions and their keys. Both are the library's
#: records, partitioned by kind, and every instance and the hosted API read
#: the same ones.
COLLECTION_RECORDS, SIGNIN_RECORDS = "collection_records", "signin_records"
QUEUE = "ingest"

#: The mirror's two useful corners, named once: the files that go up, and
#: the golden dataset the evaluation reads.
RAW_FILES = BLOB / INGESTION / RAW
GOLDEN_FILES = BLOB / EVALS / "golden_dataset"
#: Each collection's record, one file each and named for it, laid out as the
#: Cosmos account is: database, then container. Where a policy is written.
RECORD_FILES = LOCAL / "cosmosdb" / DATA / COLLECTION_RECORDS


# ============================================================================
# SETTINGS.ENV: read, checked, and the names derived from it
# ============================================================================
#
# INPUT   settings.env beside this file
# OUTPUT  a mapping with every name a script derives from VX_PREFIX, the
#         collections in order, and yes/no settings read the way a person
#         writes them
#
# One word to change when a name is taken. A missing or malformed setting
# stops here, with what to fix.


#: settings.env names that became the ones the apps and Azure use.
RENAMED = {
    "VX_WRITER_DEPLOYMENT": "AZURE_OPENAI_WRITER_DEPLOYMENT",
    "VX_VISION_DEPLOYMENT": "AZURE_OPENAI_VISION_DEPLOYMENT",
    "VX_EMBED_DEPLOYMENT": "AZURE_OPENAI_EMBED_DEPLOYMENT",
    "VX_OPENAI_API_VERSION": "AZURE_OPENAI_API_VERSION",
    "VX_BATCH_MINUTES": "EXTRACTION_BATCH_MINUTES",
    "VX_BATCH_PAGES": "EXTRACTION_BATCH_PAGES",
}


def settings() -> Dict[str, str]:
    """``settings.env`` as a mapping, with the names every script derives from it.

    A novice edits one file. Everything else is worked out here, so two
    scripts can never disagree about what a resource is called.
    """
    if not SETTINGS.exists():
        stop(
            f"There is no {SETTINGS.name} yet.\n"
            f"  copy {EXAMPLE.name} to {SETTINGS.name} and read it through: it is eleven lines.\n"
            f"  cp {EXAMPLE} {SETTINGS}"
        )
    found: Dict[str, str] = {}
    for line in SETTINGS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        found[name.strip()] = value.strip().strip('"').strip("'")

    # Six names are now the ones the apps and Azure use, so a person sees the
    # same name here, in the portal and in Azure's docs. A file written before
    # says which, rather than being quietly half read.
    old = [f"  {name}  is now  {RENAMED[name]}" for name in RENAMED if name in found]
    if old:
        stop("settings.env has names that changed. Rename these lines and run this again:\n" + "\n".join(old))

    prefix = found.get("VX_PREFIX", "").strip().lower()
    if not prefix.isalnum() or not 4 <= len(prefix) <= 16 or not prefix[0].isalpha():
        stop(
            f"VX_PREFIX is {prefix!r}. It has to be 4 to 16 letters and digits, starting with a letter, "
            "and unique across Azure, because a storage account and a search service are named from it. "
            "Something like vxlive7391 works."
        )

    # Every name in one place. Azure's rules differ per service: a storage
    # account takes no dashes and at most 24 characters, the rest are easier.
    found.setdefault("VX_LOCATION", "canadacentral")
    found.setdefault("VX_SEARCH_TIER", "basic")
    found.setdefault("VX_COLLECTIONS", "financial,media,misc")
    found.setdefault("VX_OPENAI", "yes")
    found.setdefault("VX_VISION", "yes")
    found.setdefault("VX_LANGUAGE", "yes")
    found.setdefault("VX_TRANSLATOR", "yes")
    found.setdefault("VX_COSMOS", "yes")
    found.setdefault("VX_COSMOS_FREE", "yes")
    found.setdefault("VX_COSMOS_LOCATION", "")
    # Where a resource goes when VX_LOCATION will not take it: regions in the
    # order to try them. Cosmos and Azure OpenAI follow it unless they are
    # given an order of their own.
    found.setdefault("VX_ELSEWHERE", "eastus,westus")
    found.setdefault("VX_COSMOS_ELSEWHERE", found["VX_ELSEWHERE"])
    found.setdefault("VX_OPENAI_ELSEWHERE", found["VX_ELSEWHERE"])
    found.setdefault("VX_VISION_LOCATION", "eastus")
    found.setdefault("VX_SIGNIN", "yes")
    found.setdefault("VX_GUESTS", "yes")
    found.setdefault("VX_AUDIT_DAYS", "7")
    found.setdefault("INGEST_CHUNKING", "auto")
    found.setdefault("RETRIEVAL_SETUP", "auto")
    found["VX_RESOURCE_GROUP"] = found.get("VX_RESOURCE_GROUP") or f"{prefix}-rg"
    found["VX_STORAGE"] = found.get("VX_STORAGE") or f"{prefix}store"[:24]
    found["VX_SEARCH"] = found.get("VX_SEARCH") or f"{prefix}-search"
    found["VX_FUNCTION_APP"] = found.get("VX_FUNCTION_APP") or f"{prefix}-fn"
    # The same folder deployed a second time, to answer questions while the first reads files.
    found["VX_QUERY_APP"] = found.get("VX_QUERY_APP") or f"{prefix}-query"
    found["VX_EXTRACT_APP"] = found.get("VX_EXTRACT_APP") or f"{prefix}-extract"
    found["VX_DOCINTEL"] = found.get("VX_DOCINTEL") or f"{prefix}-docintel"
    found["VX_SPEECH"] = found.get("VX_SPEECH") or f"{prefix}-speech"
    found["VX_OPENAI_NAME"] = found.get("VX_OPENAI_NAME") or f"{prefix}-openai"
    found["VX_VISION_NAME"] = found.get("VX_VISION_NAME") or f"{prefix}-vision"
    found["VX_LANGUAGE_NAME"] = found.get("VX_LANGUAGE_NAME") or f"{prefix}-language"
    found["VX_TRANSLATOR_NAME"] = found.get("VX_TRANSLATOR_NAME") or f"{prefix}-translator"
    # Cosmos account names are lower case, letters, digits and hyphens.
    found["VX_COSMOS_NAME"] = found.get("VX_COSMOS_NAME") or f"{prefix}-cosmos"
    found["AZURE_OPENAI_VISION_DEPLOYMENT"] = found.get("AZURE_OPENAI_VISION_DEPLOYMENT") or "gpt-5.4-mini"
    found["AZURE_OPENAI_EMBED_DEPLOYMENT"] = found.get("AZURE_OPENAI_EMBED_DEPLOYMENT") or "text-embedding-3-small"
    found["VX_SECOND_VECTOR"] = found.get("VX_SECOND_VECTOR") or "yes"
    found["AZURE_OPENAI_WRITER_DEPLOYMENT"] = found.get("AZURE_OPENAI_WRITER_DEPLOYMENT") or "gpt-5.4-mini"
    found["VX_DETAILED_PICTURES"] = found.get("VX_DETAILED_PICTURES") or "no"

    if found["VX_SEARCH_TIER"] not in ("free", "basic"):
        stop(f"VX_SEARCH_TIER is free or basic, not {found['VX_SEARCH_TIER']!r}. See the README on what each costs.")
    return found


def collections(config: Dict[str, str]) -> List[str]:
    """The collections, in the order they are written. One index each."""
    named = [n.strip() for n in str(config.get("VX_COLLECTIONS", "")).split(",") if n.strip()]
    if not named:
        stop("VX_COLLECTIONS is empty. It is a comma separated list, financial,media,misc")
    for name in named:
        if not name.replace("-", "").isalnum():
            stop(f"{name!r} is not a collection name: letters, digits and dashes, because it names an index too")
    return named


def wants(config: Dict[str, str], name: str) -> bool:
    """A yes/no setting, read the way a person writes one."""
    return str(config.get(name, "")).strip().lower() in ("yes", "y", "true", "1", "on")


# ============================================================================
# STATE: what earlier scripts found out
# ============================================================================
#
# INPUT   state.json
# OUTPUT  addresses and names, never a key; or a stop naming the script to run
#         first
#
# Each script records what the next will need, so nothing is asked of Azure
# twice.


def state() -> Dict[str, Any]:
    """What earlier scripts found out: addresses and names, never a key.

    Keys are read fresh from ``az`` every time they are needed, so none of
    them is ever written to this machine's disk by these scripts.
    """
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def remember(**values: Any) -> Dict[str, Any]:
    kept = state()
    kept.update({k: v for k, v in values.items() if v is not None})
    STATE.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    return kept


def need(name: str, said_by: str) -> Any:
    """Something an earlier script should have recorded, with which script to run."""
    value = state().get(name)
    if value is None:
        stop(f"{name} is not known yet. Run {said_by} first.")
    return value


# ============================================================================
# AZ: the command line, printed before it runs
# ============================================================================
#
# INPUT   the arguments of one az command
# OUTPUT  what it printed, parsed as JSON when asked; every secret shown as
#         dots
#
# Every step is an az command, printed before it runs, so you can read what is
# about to happen, stop, or type the same command yourself. --dry-run prints
# every command that would change something and runs none; the two pushes
# look first, reading only, so their plan says what is already there.


class Az:
    """The ``az`` command line, printed before it runs.

    ``--dry-run`` on any script sets ``pretend``: every command that would
    change something is printed and none is run, which is how you see the
    whole plan without anything being changed. The two pushes look first,
    with reads that change nothing (``look``), so their plan says what is
    already there instead of listing everything as new.
    """

    def __init__(self, pretend: bool = False) -> None:
        self.pretend = pretend
        #: What the last command that was allowed to fail said when it did,
        #: so a script can give Azure's reason and not its own guess.
        self.said = ""
        self.path = shutil.which("az") or shutil.which("az.cmd")
        if self.path is None and not pretend:
            stop(
                "The Azure command line is not on this machine.\n"
                "  Install it, then open a new terminal: https://aka.ms/installazurecli"
            )

    def __call__(self, *args: str, reads: bool = False, quiet: bool = False, allow_fail: bool = False) -> Any:
        """Run one command. ``reads`` parses the JSON it prints and says nothing."""
        command = ["az", *args]
        if not (reads and quiet):
            print("  $ " + " ".join(_quoted(a) for a in _printable(args)))
        if self.pretend and not reads:
            return None
        if self.pretend and reads:
            return None
        done = subprocess.run([self.path, *args], capture_output=True, text=True)  # noqa: S603 - fixed argv, no shell
        if done.returncode != 0:
            message = (done.stderr or done.stdout or "").strip()
            if allow_fail:
                self.said = message
                return None
            stop(f"That command failed:\n{_indent(message)}")
        if not reads:
            return None
        text = (done.stdout or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def look(self, *args: str) -> Any:
        """A read that runs in a dry run too, so a plan says what is already there instead of guessing.

        Only for commands that change nothing: a key, a blob's size, whether a
        container is there. Nothing is printed and nothing stops the script:
        with no account made yet, or nobody signed in, the answer is None and
        the plan lists everything, as a plan that could not look.
        """
        if self.path is None:
            return None
        pretend, self.pretend = self.pretend, False
        try:
            return self(*args, reads=True, quiet=True, allow_fail=True)
        finally:
            self.pretend = pretend

    def exists(self, *args: str) -> bool:
        """Whether a ``show`` command finds something. Every script is safe to run twice.

        Most ``show`` commands fail when the thing is not there, so no answer
        means no resource. ``az storage queue exists`` is not one of those: it
        succeeds and answers ``{"exists": false}``. Reading that as a finding
        made 01 say the queue was already there and never create it, and
        Event Grid then had nowhere to write, which is the kind of fault that
        looks like success until the whole thing quietly does nothing.
        """
        if self.pretend:
            return False
        answer = self(*args, reads=True, quiet=True, allow_fail=True)
        if isinstance(answer, dict) and "exists" in answer:
            return bool(answer["exists"])
        return answer is not None

    def state_of(self, *args: str) -> str:
        """A resource's provisioningState, or "" when there is no such resource.

        Worth asking rather than only whether it is there. A Cosmos account
        whose creation failed is still shown by ``az ... show``, so
        ``exists`` said yes, this script said "have it" and wrote down an
        address for an account with no endpoint. Azure will not create over
        one either: it answers "delete the previous instance before
        attempting to recreate this account". So the states worth telling
        apart are Succeeded, something else, and nothing.
        """
        if self.pretend:
            return ""
        answer = self(*args, reads=True, quiet=True, allow_fail=True)
        if not isinstance(answer, dict):
            return ""
        # This CLI returns the ARM shape, with everything under properties.
        found = answer.get("provisioningState") or (answer.get("properties") or {}).get("provisioningState")
        return str(found or "")


# ============================================================================
# THE REGION ORDER
# ============================================================================
#
# INPUT   the region a resource is wanted in, and the regions to try after it
# OUTPUT  the region it went in, or "" when none would take it
#
# No region has room for everything at every moment. So a resource is asked
# for where it is wanted first, and then in each region named after that, in
# the order written, and every script says which one took it.


def regions(first: str, elsewhere: str = "") -> list:
    """The regions to try, in order: the first, then each one named after it, once each.

    ``elsewhere`` is what a person writes in settings.env: ``eastus`` or
    ``eastus, westus``. The order written is the order tried.
    """
    out: list = []
    for one in [first, *str(elsewhere or "").replace(";", ",").split(",")]:
        one = one.strip().lower().replace(" ", "")
        if one and one not in out:
            out.append(one)
    return out


def somewhere(az: "Az", tries: list, create, made, what: str, clear=None) -> str:
    """Make one resource in the first region that takes it. The region, or "" when none did.

    ``create`` is given a region and asks for the resource there, allowed to
    fail. ``made`` says whether it is there afterwards, because a command
    that prints nothing answers the same whether it worked or not.
    ``clear`` takes away what a refused create left behind, since Azure
    will not create over a resource that failed.
    """
    for at, region in enumerate(tries):
        create(region)
        if az.pretend or made():
            return region
        if clear is not None:
            clear()
        following = tries[at + 1] if at + 1 < len(tries) else ""
        note(f"{region} would not take {what}" + (f"; trying {following}" if following else ""))
    return ""


def reason(az: "Az") -> str:
    """Azure's own first sentence for the last refusal, or "" when it gave none."""
    for line in str(getattr(az, "said", "") or "").splitlines():
        line = line.strip()
        if line.lower().startswith("message:"):
            return line.split(":", 1)[1].strip()
    for line in str(getattr(az, "said", "") or "").splitlines():
        if line.strip():
            return line.strip().removeprefix("ERROR:").strip()
    return ""


def _no_region(what: str, tries: list) -> str:
    """What is said when every region refused."""
    return (
        f"No region would take {what}: {', '.join(tries)} were all tried. The reason each gave is above. "
        "Put other regions in VX_ELSEWHERE in settings.env and run this again."
    )


#: Whatever follows one of these is a secret, and so is the value half of a
#: NAME=value whose name reads like one. The first live run printed a storage
#: account key in full, three times, because the command was printed as it was.
SECRET_FLAGS = ("--account-key", "--password", "--key", "--secret", "--admin-key", "--connection-string", "--sas-token")
SECRET_NAMES = ("key", "secret", "password", "token", "connectionstring", "totp")


def _looks_secret(name: str) -> bool:
    plain = name.replace("_", "").replace("-", "").lower()
    return any(word in plain for word in SECRET_NAMES)


def _printable(args: Sequence[str]) -> List[str]:
    """The command as it can safely be shown: every secret replaced by dots.

    What is printed is what a person reads, copies into a note, and pastes
    into a bug report. A key in it is a key in all three.
    """
    out: List[str] = ["az"]
    hide_next = False
    for arg in args:
        if hide_next:
            out.append("...")
            hide_next = False
            continue
        if arg in SECRET_FLAGS:
            out.append(arg)
            hide_next = True
            continue
        if "=" in arg and not arg.startswith("-"):
            name, _, value = arg.partition("=")
            if _looks_secret(name):
                out.append(f"{name}=...")
            elif value.startswith("data:"):
                # A logo or a palette inside its setting: its kind and its size, not a screenful of base64.
                out.append(f"{name}={value.split(',', 1)[0]},...({len(value)} characters)")
            else:
                out.append(arg)
            continue
        out.append(arg)
    return out


def _quoted(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _indent(text: str, by: str = "    ") -> str:
    return "\n".join(by + line for line in text.splitlines())


# ============================================================================
# WORDS: the steps, the links, and the stop
# ============================================================================
#
# INPUT   what a step is doing, an address to click, or what went wrong
# OUTPUT  one line a step on the console; portal, subscription and folder
#         links at the end; a stop that says what is wrong and what to run
#
# Every failure in these scripts comes through stop().

PORTAL = "https://portal.azure.com"


def _at(kept: Dict[str, Any]) -> str:
    """The directory a link opens in, as its domain.

    Without a directory the address reads ``#@/resource/...`` and the portal
    opens whichever one the browser was last in. Somebody in two directories,
    which is anybody with a work account and a personal one, is then told
    "you do not have permission to access this resource" about a resource
    that is plainly theirs.

    The **domain** is what works here, ``contoso.onmicrosoft.com``, not the
    tenant's id. The id is what ``az`` reports and what the portal quietly
    refuses, which is a long way to go to find a link that does not open.
    """
    where = kept.get("tenant_domain") or kept.get("tenant") or ""
    return f"#@{where}" if where else "#@"


def tenant_domain(az: Any) -> str:
    """The signed-in tenant's default domain, asked of whoever will answer.

    ``az account show`` knows it for some accounts and answers null for
    others, so this falls back to Microsoft Graph, which always knows.
    Neither call changes anything.
    """
    found = az("account", "show", reads=True, quiet=True, allow_fail=True) or {}
    domain = (found.get("tenantDefaultDomain") or "").strip()
    if domain:
        return domain
    answer = az(
        "rest", "--method", "get",
        "--url", "https://graph.microsoft.com/v1.0/domains?$select=id,isDefault",
        reads=True, quiet=True, allow_fail=True,
    )
    for entry in (answer or {}).get("value", []):
        if entry.get("isDefault"):
            return str(entry.get("id", ""))
    return ""


def portal(
    kind: str,
    name: str,
    config: Optional[Dict[str, str]] = None,
    kept: Optional[Dict[str, Any]] = None,
    page: str = "overview",
) -> str:
    """The address of one resource in the portal, to click rather than to hunt for.

    The subscription is the one 00_login.py recorded, and the group the one
    in the settings, so a link goes where the thing actually is rather than
    to a search box.
    """
    kept = state() if kept is None else kept
    subscription, group = kept.get("subscription", ""), (config or {}).get("VX_RESOURCE_GROUP", kept.get("resource_group", ""))
    if not (subscription and group):
        return ""
    providers = {
        "group": "",
        "storage": "/providers/Microsoft.Storage/storageAccounts/",
        "search": "/providers/Microsoft.Search/searchServices/",
        "function": "/providers/Microsoft.Web/sites/",
        "cognitive": "/providers/Microsoft.CognitiveServices/accounts/",
        "cosmos": "/providers/Microsoft.DocumentDB/databaseAccounts/",
    }
    root = f"{PORTAL}/{_at(kept)}/resource/subscriptions/{subscription}/resourceGroups/{group}"
    if kind == "group":
        return f"{root}/{page}"
    # The page belongs on the end, named: gluing one to a link that already
    # ends in /overview gives /overview/containersList, which opens nothing.
    return f"{root}{providers[kind]}{name}/{page}"


def subscription_link(what: str, kept: Optional[Dict[str, Any]] = None) -> str:
    """A page that belongs to the subscription rather than to one resource."""
    kept = state() if kept is None else kept
    subscription = kept.get("subscription", "")
    if not subscription:
        return ""
    scope = f"%2Fsubscriptions%2F{subscription}"
    if what == "overview":
        return f"{PORTAL}/{_at(kept)}/resource/subscriptions/{subscription}/overview"
    # A Cost Management view carries its own scope, and the directory comes
    # after the hash the same way.
    if what == "cost":
        return f"{PORTAL}/{_at(kept)}/view/Microsoft_Azure_CostManagement/Menu/~/costanalysis/scope/{scope}"
    if what == "budgets":
        return f"{PORTAL}/{_at(kept)}/view/Microsoft_Azure_CostManagement/Menu/~/budgets/scope/{scope}"
    return ""


def folder_link(path: Path) -> str:
    """A folder on this machine, as something a terminal will make clickable."""
    return path.resolve().as_uri() if path.exists() else ""


def links(*pairs: Any) -> None:
    """A short list of addresses to click, at the end of a script."""
    shown = [(label, url) for label, url in pairs if url]
    if not shown:
        return
    print("\n  Click through:")
    width = max(len(label) for label, _ in shown)
    for label, url in shown:
        print(f"    {label:<{width}}  {url}")


def begin(number: str, title: str, what: str) -> None:
    print(f"\n{number}  {title}\n    {what}\n")


def step(text: str) -> None:
    print(f"\n{text}")


def done(text: str) -> None:
    print(f"  ok   {text}")


def skipped(text: str) -> None:
    print(f"  have {text}")


def note(text: str) -> None:
    print(f"  note {text}")


def finish(text: str, then: str = "") -> None:
    print(f"\n{text}")
    if then:
        print(f"Next: {then}")


def stop(text: str) -> None:
    """Say what is wrong and stop. Every failure in these scripts comes through here."""
    print(f"\nStopped. {text}\n", file=sys.stderr)
    raise SystemExit(1)


# ============================================================================
# ARGUMENTS AND LOCAL FILES
# ============================================================================
#
# INPUT   the options a script takes; a folder of the mirror
# OUTPUT  the parsed options, --dry-run among them; every file under the
#         folder in a fixed order, ignoring what git keeps there
#
# The same options on every script, so they read alike.


def arguments(description: str, extra: Optional[Any] = None) -> Any:
    """The options every script takes, and any of its own."""
    import argparse

    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print every command that would change something, and run none of them")
    if extra is not None:
        extra(parser)
    return parser.parse_args()


def local_files(folder: Path) -> List[Path]:
    """Every file under a folder of the mirror, in a fixed order, ignoring what git keeps there.

    The tree below it is the route: ``.local/blob/data_db/ingestion/raw/financial/td/
    x.pdf`` is the blob ``raw/financial/td/x.pdf`` in the ingestion container.
    Walking it rather than listing one level is what lets a person drop a file
    into the right folder and have it land in the right collection, with
    nothing to edit.
    """
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.rglob("*") if p.is_file() and p.name not in (".gitignore", "README.md")
    )


# ============================================================================
# PUSHING: the raw files
# ============================================================================
#
# INPUT   the local mirror and the ingestion container
# OUTPUT  what the container did not already have, uploaded, and said
#
# A file already there is left alone, so a second push sends only what is new.


def push_raw(az: Any, config: Dict[str, str], *, folder: Optional[Path] = None, again: bool = False, also: Sequence[Tuple[Path, str]] = ()) -> int:
    """Upload what the ingestion container does not already have, and say what went.

    The path under the mirror is the path under ``raw/`` in the container, so
    the folder a file sits in is the collection it lands in and the one under
    that is the client a policy scopes by. Nothing is looked up.

    A file the container already holds at the same size is left alone, which
    is what makes both pushes safe to run again; ``again`` sends it anyway.
    Returns how many went up, because the caller says something different
    when the answer is none.

    A dry run looks before it plans: it reads the account's key and each
    blob's size, which changes nothing, so what is already there is said and
    only the rest is planned. When it cannot look, with no account made yet
    or nobody signed in, it says so and plans every file. ``also`` is files
    still on the shelf, each with the path it will have in the mirror: 07's
    real run moves them in before it sends, so its dry run plans them too.
    """
    where = folder or RAW_FILES
    files = local_files(where)
    shelf = dict(also)
    if not files and not shelf:
        return 0

    account, group = config["VX_STORAGE"], config["VX_RESOURCE_GROUP"]
    named = collections(config)
    key = None
    if az.pretend:
        keys = az.look("storage", "account", "keys", "list", "--account-name", account, "--resource-group", group)
        key = keys[0]["value"] if isinstance(keys, list) and keys else None
        if key is None:
            note(f"could not look inside {account}, so every file below is planned as if the container had none of them")
    else:
        keys = az("storage", "account", "keys", "list", "--account-name", account, "--resource-group", group, reads=True, quiet=True)
        key = keys[0]["value"] if keys else None
        if key is None:
            stop(f"Could not read a key for {account}. Run 01_create_resources.py first.")

    def with_key(*rest: str) -> tuple:
        return (*rest, "--account-name", account, *(("--account-key", key) if key else ()))

    sent = 0
    for path in [*files, *shelf]:
        inside = shelf.get(path) or path.relative_to(where).as_posix()
        blob = f"{RAW}/{inside}"
        collection = inside.split("/")[0] if "/" in inside else ""
        if not collection:
            note(f"{blob} sits in no collection folder, so it is not uploaded. Move it into one of: {', '.join(named)}")
            continue
        if collection not in named:
            note(f"{blob} is in a folder called {collection}, which is not one of: {', '.join(named)}")
            continue
        there = None
        if not again and key:
            asked = with_key("storage", "blob", "show", "--container-name", INGESTION, "--name", blob)
            there = az.look(*asked) if az.pretend else az(*asked, reads=True, quiet=True, allow_fail=True)
        if isinstance(there, dict) and there.get("properties", {}).get("contentLength") == path.stat().st_size:
            skipped(f"{blob}, same size already there")
            continue
        az(
            *with_key("storage", "blob", "upload", "--container-name", INGESTION, "--name", blob, "--file", str(path)),
            "--overwrite", "true",
            "--output", "none",
        )
        if az.pretend:
            print(f"  would upload {blob}, {size(path.stat().st_size)}")
        else:
            done(f"{blob}, {size(path.stat().st_size)}")
        sent += 1
    return sent


# ============================================================================
# RECORDS: each collection's record, pushed
# ============================================================================
#
# INPUT   the mirror's records, or settings.env for a collection that has none
# OUTPUT  each record written into Cosmos DB through the library's
#         CollectionRecords, and how many went
#
# The one thing here that is not an az command: no az command writes an item
# into Cosmos DB, so the record goes up through the library, and what is
# written is exactly what every server reads.


def record_files(config: Dict[str, str]) -> List[Path]:
    """Each collection's record in the mirror, one written from settings.env for a collection that has none.

    A record is where a collection's policy is written, so it is a file a
    person edits rather than something worked out each time. A collection
    with no file yet gets one here naming nobody, so the mirror always shows
    what goes up and nothing answers until somebody says who may ask.
    """
    RECORD_FILES.mkdir(parents=True, exist_ok=True)
    for name in collections(config):
        path = RECORD_FILES / f"{name}.json"
        if path.exists():
            continue
        record: Dict[str, Any] = {
            "name": name,
            "path": f"{RAW}/{name}/",
            # Who may search it: nobody, until the file names them. See the README
            # for the two ways: security groups from the token, or a list of people.
            "policy": None,
        }
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        done(f"{path.relative_to(LOCAL).as_posix()}, written from settings.env")
    return sorted(RECORD_FILES.glob("*.json"))


def _rules_of(record: Any) -> str:
    """A record's rule in words: who may search it."""
    policy = record.policy_object()
    return policy.describe() if policy is not None else "nobody yet"


def push_records(az: Any, config: Dict[str, str], *, by: str, again: bool = False) -> int:
    """Write each collection's record into Cosmos DB, where every server reads it. Returns how many were written.

    The policy goes up from the file whenever the two differ, because the
    file is where a policy is written. Who may see a collection and whether
    it is masked are changed on the dashboard, so a record already there
    keeps those two as the dashboard left them; ``again`` writes the file's
    over them as well. A record in Cosmos with no file here is left alone.
    """
    from dataclasses import replace

    files = record_files(config)
    if not wants(config, "VX_COSMOS"):
        note("VX_COSMOS is not yes, so the records stay here and each collection keeps its policy in its own metadata")
        return 0
    try:
        from vectrixdb.collection_records import CollectionRecord, CollectionRecords
    except ImportError:
        stop("Each record goes up through the library, which is not installed here. From this folder: pip install -r requirements.txt")

    named = collections(config)
    records = []
    for path in files:
        try:
            record = CollectionRecord.from_json(path)
        except Exception as exc:  # noqa: BLE001 - a file somebody wrote: say which and what
            stop(f"{path.relative_to(LOCAL).as_posix()} cannot be a collection's record: {exc}")
        if record.name not in named:
            note(f"{path.name} is the record for {record.name}, which is not one of: {', '.join(named)}. It is not sent")
            continue
        records.append(record)

    account, group = config["VX_COSMOS_NAME"], config["VX_RESOURCE_GROUP"]
    address = f"cosmos://{account}.documents.azure.com/{DATA}/{COLLECTION_RECORDS}"
    print(f"  $ vectrixdb.collection_records: {len(records)} to {address}")
    if az.pretend:
        # A dry run reads what is there and writes nothing, so it says which records would change.
        store = _look_at_records(az, address, account, group)
        if store is None:
            for record in records:
                print(f"  would write collection.{record.name}: {_rules_of(record)}")
            return len(records)
    else:
        try:
            import azure.cosmos  # noqa: F401 - what CollectionRecords writes with
        except ImportError:
            stop("Writing an item into Cosmos DB needs its SDK on this machine. From this folder: pip install -r requirements.txt")
        store = CollectionRecords.open(address, key=_cosmos_key(az, account, group))
    sent = 0
    try:
        for record in records:
            there = store.get(record.name)
            if there is None:
                if az.pretend:
                    print(f"  would write collection.{record.name}: {_rules_of(record)}")
                else:
                    store.put(record, by=by)
                    done(f"collection.{record.name}, {_rules_of(record)}")
                sent += 1
                continue
            wanted = replace(record, generation=there.generation, created_at=there.created_at)
            same = (wanted.policy, wanted.path) == (there.policy, there.path)
            if same:
                skipped(f"collection.{record.name}, {_rules_of(there)}, the same already there")
            else:
                changed = "policy" if wanted.policy != there.policy else "record"
                if az.pretend:
                    print(f"  would write collection.{record.name}, {changed} now {_rules_of(wanted)}")
                else:
                    store.put(wanted, by=by)
                    done(f"collection.{record.name}, {changed} now {_rules_of(wanted)}")
                sent += 1
        extra = sorted(set(store.names()) - {r.name for r in records})
        if extra:
            note(f"in Cosmos and not in the mirror, so left as they are: {', '.join(extra)}")
    finally:
        store.close()
    return sent


def _look_at_records(az: Any, address: str, account: str, group: str) -> Any:
    """The records in Cosmos DB, opened for a dry run to read, or None when it cannot look.

    Opening the store makes its container when it is not there, and a dry run
    makes nothing, so the container is asked for first, reading only, and the
    store is opened only when it is there.
    """
    from vectrixdb.collection_records import CollectionRecords

    try:
        import azure.cosmos  # noqa: F401 - what CollectionRecords reads with
    except ImportError:
        note("the Cosmos DB SDK is not on this machine, so the plan cannot look at the records and lists each as it would be written")
        return None
    there = az.look("cosmosdb", "sql", "container", "show", "--account-name", account, "--resource-group", group,
                    "--database-name", DATA, "--name", COLLECTION_RECORDS)
    keys = az.look("cosmosdb", "keys", "list", "--name", account, "--resource-group", group, "--type", "keys") if there else None
    key = keys.get("primaryMasterKey") if isinstance(keys, dict) else None
    if not key:
        note(f"could not look at the records in {account}, or there are none there yet, so each is listed as it would be written")
        return None
    try:
        return CollectionRecords.open(address, key=str(key))
    except Exception as exc:  # noqa: BLE001 - a plan that cannot look lists everything rather than stopping
        note(f"could not open the records in {account} ({type(exc).__name__}), so each is listed as it would be written")
        return None


def _cosmos_key(az: Any, account: str, group: str) -> str:
    keys = az("cosmosdb", "keys", "list", "--name", account, "--resource-group", group, "--type", "keys", reads=True, quiet=True, allow_fail=True)
    key = (keys or {}).get("primaryMasterKey") if isinstance(keys, dict) else None
    if not key:
        stop(f"Could not read a key for the Cosmos account {account}. Run 03_create_ai_services.py first.")
    return str(key)


# ============================================================================
# SIZES AND THE WHEEL
# ============================================================================
#
# INPUT   a path; an app's folder
# OUTPUT  a size in words; the library wheel beside the app's code, new enough
#         for what the app imports and asks for, with every package it has to
#         end up with
#
# The wheel is checked for the names an app needs before func publishes
# anything.


def size(count: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "bytes" else f"{count:.1f} {unit}"
        count /= 1024.0
    return f"{count:.1f} GB"


def wheel_ready(app: Path, needs: Dict[str, str], dry_run: bool = False, installs: Optional[Dict[str, str]] = None) -> None:
    """The library wheel beside an app's code, new enough for what the app imports and asks for.

    ``requirements.txt`` installs the wheel it names from the app's folder.
    The newest build is copied in, from dist/ or from the other app's folder,
    and looked inside before anything is sent. Three things are checked,
    because each installs without complaint and then fails where nobody is
    watching. ``needs`` maps a file inside the wheel to a line it must hold: a
    wheel built before that line existed makes an app that loads nothing and
    answers 404 to everything. Every extra the requirements ask for must be
    one the wheel offers: pip installs a wheel without an extra it does not
    know, and says so only in a warning, so an app asking for ``ffmpeg`` from
    an old wheel would get no ffmpeg. And ``installs`` maps a package the app
    has to end up with to what it is for: it must be on a line of its own or
    brought by an extra the requirements ask for, or the app finds it missing
    only when a file first needs it.
    """
    import re
    import zipfile

    text = (app / "requirements.txt").read_text(encoding="utf-8")
    wanted = re.search(r"^\./(vectrixdb-[^\[\s]+\.whl)(?:\[([^\]]*)\])?", text, re.M)
    if wanted is None:
        stop(f"{app.name}/requirements.txt names no vectrixdb wheel, so there is nothing to install")
    name = wanted.group(1)
    extras = {extra.strip() for extra in (wanted.group(2) or "").split(",") if extra.strip()}
    here = app / name
    for source in (REPO / "dist" / name, *(folder / name for folder in (MAIN_FUNCTION_APP, EXTRACTION_APP) if folder != app)):
        if source.exists() and (not here.exists() or source.stat().st_mtime > here.stat().st_mtime):
            print(f"  $ copy {source} {here}")
            if not dry_run:
                shutil.copy2(source, here)
            break
    if dry_run:
        return
    rebuild = f"  build it: cd {REPO} && python -m build --no-isolation --wheel\n  then run this again"
    if not here.exists():
        stop(f"There is no {name} to install.\n{rebuild}")
    with zipfile.ZipFile(here) as wheel:
        inside = set(wheel.namelist())
        missing = [
            path for path, line in needs.items()
            if path not in inside or line not in wheel.read(path).decode("utf-8", errors="replace")
        ]
        about = next((wheel.read(n).decode("utf-8", errors="replace") for n in sorted(inside) if n.endswith(".dist-info/METADATA")), "")
    if missing:
        stop(f"{name} was built before what {app.name} imports ({', '.join(missing)}), so it would load nothing.\n{rebuild}")
    offered = {line.split(":", 1)[1].strip() for line in about.splitlines() if line.startswith("Provides-Extra:")}
    unoffered = sorted(extras - offered)
    if unoffered:
        stop(f"{name} offers no {', '.join(unoffered)} extra, so pip would install it without {'them' if len(unoffered) > 1 else 'it'}.\n{rebuild}")
    done(f"{name}, with what {app.name} imports and the extras it asks for")
    if installs:
        own = {_package(line) for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "-", "."))}
        brought = _brought(about, extras) | own
        for package, why in installs.items():
            if _package(package) not in brought:
                stop(
                    f"{app.name}/requirements.txt installs no {package}, which {why}.\n"
                    f"  add it on a line of its own: {package}\n  then run this again"
                )
            done(f"{package}, which {why}")


def _package(requirement: str) -> str:
    """A requirement's package, the way pip compares names: lower case, and ``-``, ``_`` and ``.`` alike."""
    import re

    name = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return re.sub(r"[-_.]+", "-", name.group(1)).lower() if name else ""


def _brought(metadata: str, extras: set) -> set:
    """The packages a wheel installs, from its METADATA, with the extras asked for.

    An extra can ask for another of the same library, the way ``video``
    brings ``vectrixdb[ffmpeg]``, so those are followed too.
    """
    import re

    requires = []
    for line in metadata.splitlines():
        if line.startswith("Requires-Dist:"):
            requirement, _, marker = line.split(":", 1)[1].partition(";")
            extra = re.search(r"extra\s*==\s*[\"']([^\"']+)[\"']", marker)
            requires.append((requirement.strip(), extra.group(1) if extra else None))
    asked, more = set(extras), True
    while more:
        more = False
        for requirement, extra in requires:
            inner = re.search(r"\[([^\]]*)\]", requirement) if extra in asked and _package(requirement) == "vectrixdb" else None
            for named in (part.strip() for part in (inner.group(1).split(",") if inner else [])):
                if named and named not in asked:
                    asked.add(named)
                    more = True
    return {_package(requirement) for requirement, extra in requires if extra is None or extra in asked}


# ============================================================================
# WHAT FOLLOWS A RUN: the picks and the environment
# ============================================================================
#
# INPUT   settings.env
# OUTPUT  INGEST_CHUNKING and RETRIEVAL_SETUP as it names them, auto when it
#         does not; every setting the main Function App runs with, in one
#         place
#
# One place the app's environment is made, so 06 and a settings-only run
# agree.
#: What RETRIEVAL_SETUP says to follow a retrieval run rather than pin a way of searching.
FOLLOWS_A_RUN = ("auto", "finds_the_most", "best_for_balance", "best_for_time")


def picks_of(config: Dict[str, str]) -> Dict[str, str]:
    """INGEST_CHUNKING and RETRIEVAL_SETUP as settings.env names them, ``auto`` when it does not.

    Held to the names the app reads before it is published, so a pin that
    names nothing stops here, saying what a build or a way of searching is,
    rather than failing every file the app is sent.
    """
    cut = str(config.get("INGEST_CHUNKING", "")).strip() or "auto"
    way = str(config.get("RETRIEVAL_SETUP", "")).strip() or "auto"
    try:
        from vectrixdb.evaluation import chunking_build, search_of
    except ImportError:  # the app holds them to the same names when it reads them
        return {"INGEST_CHUNKING": cut, "RETRIEVAL_SETUP": way}
    try:
        if cut != "auto":
            chunking_build(cut)
        if way not in FOLLOWS_A_RUN:
            search_of(way)
    except ValueError as exc:
        stop(f"settings.env: {exc}")
    return {"INGEST_CHUNKING": cut, "RETRIEVAL_SETUP": way}


#: The brand's words and colour, passed as settings.env writes them.
BRAND_WORDS = (
    ("VX_BRAND_NAME", "VECTRIXDB_BRAND_NAME"),
    ("VX_BRAND_ACCENT", "VECTRIXDB_BRAND_ACCENT"),
    ("VX_BRAND_COPYRIGHT", "VECTRIXDB_BRAND_COPYRIGHT"),
)
#: Single sign-on, passed as settings.env writes it. The client secret is best a
#: key vault reference, @Microsoft.KeyVault(SecretUri=...), which Azure reads.
OIDC_SETTINGS = (
    ("VX_OIDC_ISSUER", "VECTRIXDB_OIDC_ISSUER"),
    ("VX_OIDC_CLIENT_ID", "VECTRIXDB_OIDC_CLIENT_ID"),
    ("VX_OIDC_CLIENT_SECRET", "VECTRIXDB_OIDC_CLIENT_SECRET"),
    ("VX_OIDC_ROLE_MAP", "VECTRIXDB_OIDC_ROLE_MAP"),
    ("VX_OIDC_ALLOWED_EMAILS", "VECTRIXDB_OIDC_ALLOWED_EMAILS"),
    # An app calling for the person using it: its token's audience, and the role such a token has here.
    ("VX_OIDC_API_AUDIENCE", "VECTRIXDB_OIDC_API_AUDIENCE"),
    ("VX_OIDC_TOKEN_ROLE", "VECTRIXDB_OIDC_TOKEN_ROLE"),
)
#: A gateway in front of the query app, as its team hands it over: the prefix,
#: each route's own gateway path, whose X-Forwarded-For to believe, and the
#: headers a key and a token arrive in. Its address is VX_GATEWAY_URL.
GATEWAY_SETTINGS = (
    ("VX_QUERY_PREFIX", "VECTRIXDB_PREFIX"),
    ("VX_QUERY_GATEWAY_PATHS", "VECTRIXDB_GATEWAY_PATHS"),
    ("VX_TRUSTED_PROXIES", "VECTRIXDB_TRUSTED_PROXIES"),
    ("VX_KEY_HEADER", "VECTRIXDB_KEY_HEADER"),
    ("VX_TOKEN_HEADER", "VECTRIXDB_TOKEN_HEADER"),
)
#: Emergency sign-in, for while single sign-on is down. The password stays in
#: the key vault; what is set is its hash, which 06 --new-break-glass writes.
BREAK_GLASS_SETTINGS = (
    ("VX_BREAK_GLASS_UNTIL", "VECTRIXDB_BREAK_GLASS_UNTIL"),
    ("VX_BREAK_GLASS_ADMIN", "VECTRIXDB_BREAK_GLASS_ADMIN"),
    ("VX_BREAK_GLASS_PASSWORD_HASH", "VECTRIXDB_BREAK_GLASS_PASSWORD_HASH"),
)


def keep_setting(name: str, value: str) -> None:
    """One line of settings.env set to a value: the line that has the name, commented or not, or a new last line."""
    lines = SETTINGS.read_text(encoding="utf-8").splitlines()
    live = [i for i, line in enumerate(lines) if re.match(rf"\s*{re.escape(name)}\s*=", line)]
    noted = [i for i, line in enumerate(lines) if re.match(rf"\s*#\s*{re.escape(name)}\s*=", line)]
    if live:
        for i in live:
            lines[i] = f"{name}={value}"
    elif noted:
        lines[noted[0]] = f"{name}={value}"
    else:
        lines.append(f"{name}={value}")
    SETTINGS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def data_address(where: str, name: str) -> str:
    """A file on this machine as a ``data:`` address: a function app keeps settings, not files.

    Base64, so the setting holds no quote for the command line to mangle on
    its way to Azure. An image is named by its first bytes; anything else here
    is the palette's JSON, checked as JSON before it goes.
    """
    import base64

    try:
        raw = Path(where).expanduser().read_bytes()
    except OSError as exc:
        stop(f"settings.env: {name} names {where}, which cannot be read: {exc}")
    if name == "VX_BRAND_PALETTE":
        try:
            json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            stop(f"settings.env: {name} names {where}, which is not JSON: {exc}")
        kind = "application/json"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        kind = "image/jpeg"
    else:
        kind = "image/svg+xml"
    return f"data:{kind};base64,{base64.b64encode(raw).decode('ascii')}"


def brand_of(config: Dict[str, str]) -> Dict[str, str]:
    """The company's name, logo, colours and line, for both dashboards: set once, on the query app."""
    import base64

    out = {theirs: str(config[ours]).strip() for ours, theirs in BRAND_WORDS if str(config.get(ours, "")).strip()}
    if wants(config, "VX_BRAND_WORDMARK"):
        out["VECTRIXDB_BRAND_WORDMARK"] = "on"
    for ours, theirs in (("VX_BRAND_LOGO", "VECTRIXDB_BRAND_LOGO"), ("VX_BRAND_LOGO_DARK", "VECTRIXDB_BRAND_LOGO_DARK")):
        given = str(config.get(ours, "")).strip()
        if given:
            out[theirs] = given if given.startswith("data:") else data_address(given, ours)
    palette = str(config.get("VX_BRAND_PALETTE", "")).strip()
    if palette.startswith("{"):
        try:
            json.loads(palette)
        except ValueError as exc:
            stop(f"settings.env: VX_BRAND_PALETTE is not JSON: {exc}")
        out["VECTRIXDB_BRAND_PALETTE"] = "data:application/json;base64," + base64.b64encode(palette.encode("utf-8")).decode("ascii")
    elif palette:
        out["VECTRIXDB_BRAND_PALETTE"] = palette if palette.startswith("data:") else data_address(palette, "VX_BRAND_PALETTE")
    return out


def single_sign_on(config: Dict[str, str]) -> bool:
    """VX_SIGNIN=sso: the company's sign-in, and a work email with a code, from one list. sso-only: the company's sign-in alone. No passkeys with either."""
    return str(config.get("VX_SIGNIN", "")).strip().lower() in ("sso", "sso-only")


def sso_alone(config: Dict[str, str]) -> bool:
    """VX_SIGNIN=sso-only: nobody keeps an authenticator of their own on the query app."""
    return str(config.get("VX_SIGNIN", "")).strip().lower() == "sso-only"


def query_address(config: Dict[str, str]) -> str:
    """The address people type for the query app: the gateway's, when there is one, else the app's own."""
    return str(config.get("VX_GATEWAY_URL", "")).strip().rstrip("/") or f"https://{config['VX_QUERY_APP']}.azurewebsites.net"


def dashboard_address(config: Dict[str, str]) -> str:
    """The dashboard as people reach it: through its gateway path and the prefix, when there are any."""
    tidy = lambda value: "/".join(part for part in str(value or "").strip().split("/") if part)  # noqa: E731
    paths = {}
    for entry in str(config.get("VX_QUERY_GATEWAY_PATHS", "")).split(","):
        name, _, where = entry.partition("=")
        if tidy(name) and tidy(where):
            paths[tidy(name)] = "/" + tidy(where)
    prefix = tidy(config.get("VX_QUERY_PREFIX", ""))
    return f"{query_address(config)}{paths.get('dashboard', '')}{'/' + prefix if prefix else ''}/dashboard/"


def env_of(config: Dict[str, str], kept: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, str]:
    """Every setting the main Function App runs with, in one place.

    It reads every file through the extraction app of step 05: its address, the
    header its key goes in, and the prefix and gateway paths it answers at,
    which are the same two the extraction app is given, so the two apps work
    out the same path for every file. The key itself comes in ``secrets``.
    Document Intelligence, Speech and Vision are the extraction app's
    settings, not this one's.
    """
    setting = {
        "INGEST_QUEUE": QUEUE,
        "INGEST_COLLECTIONS": ",".join(collections(config)),
        "INGEST_BLOB_ACCOUNT": kept.get("blob_account", ""),
        "AZURE_SEARCH_ENDPOINT": kept.get("search_endpoint", ""),
        "VECTRIXDB_EVALUATIONS": f"{kept.get('blob_account', '')}/{EVALS}",
        # A collection's knowledge graph, one object a collection beside its Markdown and chunks, read by every instance.
        "VECTRIXDB_GRAPH_STORE": f"{kept.get('blob_account', '')}/{INGESTION}/graph",
        # The app hosts the library's own API, and this is what stops that
        # API opening an empty SQLite file beside the process instead of the
        # index the worker writes to. The two are one index, two readers.
        "VECTRIXDB_STORAGE_BACKEND": "azure_search",
        # Empty on purpose: the index is the collection, so the service
        # shows financial, media and misc rather than vectrix-financial.
        "AZURE_SEARCH_INDEX_PREFIX": "",
        # A function app's file system is read-only apart from /tmp.
        "INGEST_PATH": "/tmp/vectrixdb",
        "VECTRIXDB_PATH": "/tmp/vectrixdb/_api",
        # Step six's cut and step ten's way of searching: auto follows the
        # runs of steps five and nine, and anything else pins one.
        **picks_of(config),
        # The company's own look, set once here and shown by both dashboards.
        **brand_of(config),
        # A gateway in front, when there is one: the prefix and each route's own path.
        **{theirs: str(config[ours]).strip() for ours, theirs in GATEWAY_SETTINGS if str(config.get(ours, "")).strip()},
    }
    if str(config.get("VX_GATEWAY_URL", "")).strip():
        # Where people reach the app, which is what single sign-on returns to and the links carry.
        setting["VECTRIXDB_PUBLIC_URL"] = query_address(config)
    if kept.get("parent_store"):
        # The parents are read by whichever instance answers, not by the one
        # that wrote them, so they do not live on an instance.
        setting["VECTRIXDB_PARENT_STORE"] = kept["parent_store"]
    if kept.get("chunk_store"):
        # The same for what the collection pages count: every instance writes
        # its chunks there, and the one drawing a page reads them all.
        setting["VECTRIXDB_CHUNK_STORE"] = kept["chunk_store"]
    if kept.get("collection_store"):
        # Each collection's policy, who may see it and whether it is masked,
        # one record each, which steps 04 and 07 write: every instance and the
        # hosted API enforce the same one, however each opened the collection.
        setting["VECTRIXDB_COLLECTION_STORE"] = kept["collection_store"]
    if kept.get("signin_store"):
        # The people, their sessions and their keys, so a sign-in on one
        # instance is a sign-in on the next rather than a file on one disk.
        setting["VECTRIXDB_SIGNIN_STORE"] = kept["signin_store"]
        if wants(config, "VX_SIGNIN") or single_sign_on(config):
            # And sign-in itself, at the query app's address, the one people
            # are given. Without it the app answers anybody: every route but
            # reading a collection with a policy, because a Function App hosts
            # the API itself, and the library's refusal to listen with no key
            # and no sign-in is only in `vectrixdb serve`. The secret the
            # links and cookies are signed with comes in ``secrets``, and the
            # first person is added with 06 --add-person: the app sends no mail.
            # sso is the enterprise way: the company's sign-in and the People
            # list's own passkeys and codes, from one list. sso-only is the
            # company's sign-in alone.
            setting["VECTRIXDB_SIGNIN"] = ("oidc" if sso_alone(config) else "oidc,email") if single_sign_on(config) else "email"
            setting["VECTRIXDB_PUBLIC_URL"] = query_address(config)
            if str(config.get("VX_SIGNIN_USERS", "")).strip():
                # The People list: who may sign in, whichever way, and each one's
                # role. Whoever is missing is added at start; nobody is changed.
                setting["VECTRIXDB_SIGNIN_USERS"] = str(config["VX_SIGNIN_USERS"]).strip()
            if wants(config, "VX_GUESTS"):
                # Somebody not signed in sees the overview, every collection's
                # name and size, and the evaluations, never a search.
                setting["VECTRIXDB_GUESTS"] = "on"
            if single_sign_on(config):
                # The platform's owners sign in with the company's account,
                # and only those on the list: a group alone is not enough.
                setting.update({theirs: str(config[ours]).strip() for ours, theirs in OIDC_SETTINGS if str(config.get(ours, "")).strip()})
                if not sso_alone(config) and str(config.get("VX_SSO_RECHECK_DAYS", "")).strip():
                    # A passkey or a code works only for somebody the company's
                    # sign-in let in within this many days, so a leaver's stops too.
                    setting["VECTRIXDB_SSO_RECHECK_DAYS"] = str(config["VX_SSO_RECHECK_DAYS"]).strip()
            elif wants(config, "VX_SIGNIN_PASSWORDS"):
                # No single sign-on: after the work email, the sign-in box asks
                # for a password and the authenticator's code, the two together.
                setting["VECTRIXDB_SIGNIN_PASSWORDS"] = "on"
            if wants(config, "VX_BREAK_GLASS"):
                # One named admin, while the usual sign-in is down, whichever it
                # is, at /dashboard/#/break-glass, until the time it is given.
                # Nothing links to it. The app is given the hash of the
                # password, never the password.
                setting["VECTRIXDB_BREAK_GLASS"] = "on"
                setting.update({theirs: str(config[ours]).strip() for ours, theirs in BREAK_GLASS_SETTINGS if str(config.get(ours, "")).strip()})
    if kept.get("blob_account") and kept.get("audit_container"):
        # What was decided and who looked, a line each, appended to one blob a
        # day in the audit container, whose policy keeps every line as it was
        # written. The hosted API writes both, and its Audit and Access pages
        # read them back; the steps write the decisions they make too.
        setting["VECTRIXDB_AUDIT_STORE"] = f"{kept['blob_account']}/{kept['audit_container']}/decisions"
        setting["VECTRIXDB_ACCESS_LOG"] = f"{kept['blob_account']}/{kept['audit_container']}/access"
    if kept.get("blob_account"):
        # The Markdown each file was indexed from, where the function keeps
        # it, one folder a collection: what the Documents page opens.
        setting["VECTRIXDB_KEEP_SOURCE"] = f"{kept['blob_account']}/{INGESTION}/{MARKDOWN}"
    if kept.get("extract_host"):
        setting["VECTRIXDB_EXTRACTOR_URL"] = kept["extract_host"]
        setting["VECTRIXDB_EXTRACTOR_KEY_HEADER"] = "api-key"
        # Every file is masked as it is read, so the Markdown this app keeps
        # and the index it builds never held an identifier. What was masked
        # comes back with the document, and the setup spinner counts it.
        setting["VECTRIXDB_EXTRACTOR_MASK"] = "1"
        # The file as the request body and its name in X-Filename, which is
        # what the extraction app reads.
        setting["VECTRIXDB_EXTRACTOR_BODY"] = "raw"
        # Azure ends every HTTP request at 230 seconds, so waiting longer
        # waits for an answer that cannot come.
        setting["VECTRIXDB_EXTRACTOR_TIMEOUT"] = "230"
        for ours, theirs in (
            ("VX_EXTRACT_PREFIX", "EXTRACTION_PREFIX"),
            ("VX_EXTRACT_GATEWAY_PATHS", "EXTRACTION_GATEWAY_PATHS"),
            # The pieces a long file is read in, named in settings.env as in
            # the app; left out, ten minutes and twenty pages.
            ("EXTRACTION_BATCH_MINUTES", "EXTRACTION_BATCH_MINUTES"),
            ("EXTRACTION_BATCH_PAGES", "EXTRACTION_BATCH_PAGES"),
        ):
            if config.get(ours):
                setting[theirs] = config[ours]
    if kept.get("openai_endpoint"):
        setting["AZURE_OPENAI_ENDPOINT"] = kept["openai_endpoint"]
        # Step seven embeds with Azure OpenAI's model beside the built-in one
        # only when it is named, so VX_SECOND_VECTOR=no leaves it unnamed.
        if wants(config, "VX_SECOND_VECTOR"):
            setting["AZURE_OPENAI_EMBED_DEPLOYMENT"] = config["AZURE_OPENAI_EMBED_DEPLOYMENT"]
        # Step four's model, a chat deployment on the same resource, which
        # also judges step five's answers and writes step ten's, and the API
        # version it is called with, when settings.env names them.
        if config.get("AZURE_OPENAI_WRITER_DEPLOYMENT"):
            setting["AZURE_OPENAI_WRITER_DEPLOYMENT"] = config["AZURE_OPENAI_WRITER_DEPLOYMENT"]
        if config.get("AZURE_OPENAI_API_VERSION"):
            setting["AZURE_OPENAI_API_VERSION"] = config["AZURE_OPENAI_API_VERSION"]
    setting.update(secrets)
    return setting
