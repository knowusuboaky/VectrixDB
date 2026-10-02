"""The three AI services that read what a text reader cannot.

    python 03_create_ai_services.py

    Document Intelligence   a page with no text layer, and the two PNGs
    Speech                  the WAV, and the sound of the videos
    Azure AI Vision         what every picture shows, and the words in it
    Azure AI Language       the names, addresses and ids in a document, found so the
                            extraction app can mask them before a word is kept
    Azure OpenAI            a chat model for the golden questions, and pictures in detail when
                            asked; an embedding model for the second vector
    Cosmos DB               the parent sections a search returns, the chunks the
                            collection pages count, and who may see what

The first four are made on their free tiers: 500 pages a month, 5 hours of
audio, 5,000 pictures and 5,000 text records. This test uses a fraction of
each and is billed nothing for them.

Language is what masking reads meaning with. The patterns in the library find
an email, a phone number or a card by its shape; a person's name or a street
address has no shape, and Language finds those, in English and French alike.
The extraction app asks it about every document it reads, and the patterns run
after it, so a shape the model missed is still caught by its form. Without it,
VX_LANGUAGE=no or a refusal, the patterns alone do the masking, and the app
says so in its health.

Vision describes the pictures by default, on purpose. Image Analysis needs
no GPT quota, which a new subscription does not have, and answers two useful
things in one call: a sentence saying what the picture is, and the words
printed inside it, which on a chart are its title, its axis labels and its
numbers. What it will not do is reason about the chart.

The chat model, gpt-5.4-mini unless AZURE_OPENAI_WRITER_DEPLOYMENT names another, is
what the main app drafts the golden questions with, step four, and answers
questions with, step ten. It reads pictures too, so VX_DETAILED_PICTURES=yes
has it describe every picture in detail in front of Vision: who and what is
in it, every word printed in it, and a chart's every value as rows. That is
paid per picture, and needs the GPT quota Vision does not.

Captioning is offered in some regions and not others, so the Vision resource
goes to VX_VISION_LOCATION, eastus by default, while everything else stays in
your own region. The OCR half works everywhere.

Azure OpenAI is the one that can still refuse, the resource or a model's
quota. If it does, this says so and carries on: without the chat model the
golden questions are written by hand, and without the embedding model the
collection has one vector instead of two and the evaluation compares fewer
setups.

Cosmos has no room in every region at every moment. Canada Central refused
outright: "we are currently experiencing high demand in Canada Central region,
and cannot fulfill your request at this time", and East US said the same on
another night. So the account goes to VX_COSMOS_LOCATION when you name one,
and otherwise to your own region, with VX_COSMOS_ELSEWHERE as what to try
next: one region or several, tried in the order written. Parents are read
once per result, so the distance costs a few dozen milliseconds and nothing
worth measuring.

Azure OpenAI is chosen the same way, before the account is made. A region
can offer a model and give a subscription no quota for it, which is only
found out when the deployment is refused. So each region, your own and then
those in VX_OPENAI_ELSEWHERE, is asked what it offers and what quota is
left, and the account goes in the first that has every model wanted.

Translator is what the extraction app's /translate routes answer with. It
is a regional resource, called at the address every Translator shares with
its region beside its key, so the region it landed in is what is kept.

Every other service follows the same order. Document Intelligence, Speech,
Vision and Language are asked for where they are wanted and then in each
region of VX_ELSEWHERE, and each says which region took it.

A Cognitive Services account deleted in the last 48 hours still holds its
name, and deleting the resource group does not let go of it. Such a name is
purged before the account is made again.

Cosmos DB is here for one small thing that matters more than its size. A
chunk is small so it is found precisely; the passage around it is what you
actually want back, and those parent sections have to be somewhere every
instance can read. The function app scales out and the hosted API is another
instance again, so parents kept on an instance's own disk are invisible to
whoever answers the question, and the symptom is not an error: it is
parent-child retrieval quietly returning nothing. One free-tier account a
subscription, 1000 RU/s and 25 GB, which is far more than this needs.

The same goes for the collection pages. A collection's count, its builds,
growth by day, quality, points and a document's chunks were drawn from a
table beside the process, and the instance serving the dashboard is not the
one that ingested. So a second container, ``chunk_records``, holds a record
of every chunk's text and metadata, without its vector, which every instance
writes and the pages read. It is partitioned by collection, because every
page asks one collection, and it indexes only what a page filters or sorts
by.

There is one database, ``data_db``, the name the mirror gives it, and all four
containers are in it. ``parent_sections`` and ``chunk_records`` hold what
ingestion made, kept as rows here where the storage container keeps files.
``collection_records`` holds each collection's record: its path and who may
retrieve from it. ``signin_records`` holds the people, their sessions and
their keys, so a sign-in on one instance is a sign-in on the next. The last
two are the library's records, partitioned by kind with time to live on,
which lets a session or a link expire by itself. Steps 04 and 07 write the
collection records from ``.local/cosmosdb/data_db/collection_records``;
nothing here writes the other three.

The database has a throughput its containers share, because a container with
its own takes 400 RU/s and four of them would be 1600, past the free tier's
1000. ``data_db`` has the free tier's 1000 exactly. A serverless account has
none: it bills per request. An account made before everything moved into
``data_db`` still has an ``ingestion`` database that nothing reads, and this
says what it costs.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager

from typing import Dict, Iterator, List, Optional, Tuple

from _common import (
    DATA,
    CHUNK_RECORDS,
    COLLECTION_RECORDS,
    INGESTION,
    PARENT_SECTIONS,
    SIGNIN_RECORDS,
    Az,
    arguments,
    begin,
    done,
    finish,
    links,
    note,
    portal,
    reason,
    regions,
    remember,
    somewhere,
    settings,
    skipped,
    step,
    stop,
    wants,
)


# ============================================================================
# COGNITIVE: the accounts, what the region offers, and the deployments
# ============================================================================
#
# INPUT   a kind of account and its tier; a model's name; the Azure OpenAI
#         resource
# OUTPUT  one Cognitive Services account, False when Azure refused it; the
#         version and sku the region offers a model, or None; what goes on the
#         resource: each deployment, its model, what it is for, its setting
#         and its capacity
#
# Document Intelligence, Speech, Vision and Language on their free tiers, and
# Azure OpenAI with the chat and embedding models the steps use, at what the
# region offers rather than what a list assumes.


def region_of(az: Az, name: str, group: str) -> str:
    """The region an account is actually in, as Azure writes it in a command: ``canadacentral``. "" when it cannot be read."""
    found = az(
        "cognitiveservices",
        "account",
        "show",
        "--name",
        name,
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    return (
        str(found.get("location") or "").lower().replace(" ", "") if isinstance(found, dict) else ""
    )


def held(az: Az, name: str) -> Optional[Dict[str, str]]:
    """The deleted account Azure is still holding this name for, as its group and region, or None.

    A Cognitive Services account that was deleted is kept for 48 hours, and
    its name with it: a create answers that the resource was soft-deleted and
    has to be restored or purged. Deleting the resource group does not purge.
    """
    if az.pretend:
        return None
    every = (
        az("cognitiveservices", "account", "list-deleted", reads=True, quiet=True, allow_fail=True)
        or []
    )
    for entry in every if isinstance(every, list) else []:
        if str(entry.get("name") or "").lower() != name.lower():
            continue
        # /subscriptions/<id>/providers/Microsoft.CognitiveServices/locations/<region>/resourceGroups/<group>/deletedAccounts/<name>
        parts = str(entry.get("id") or "").split("/")
        lowered = [p.lower() for p in parts]
        group = parts[lowered.index("resourcegroups") + 1] if "resourcegroups" in lowered else ""
        region = str(
            entry.get("location")
            or (parts[lowered.index("locations") + 1] if "locations" in lowered else "")
        )
        if group and region:
            return {"group": group, "region": region}
    return None


def cognitive(
    az: Az, name: str, group: str, region: str, kind: str, sku: str, what: str, elsewhere: str = ""
) -> bool:
    """One Cognitive Services account, in the first region that takes it. False when every one refused."""
    if az.exists("cognitiveservices", "account", "show", "--name", name, "--resource-group", group):
        skipped(f"{name}, {what}")
        return True
    kept = held(az, name)
    if kept:
        note(
            f"{name} was deleted less than 48 hours ago and Azure still holds the name, so it is purged first"
        )
        az(
            "cognitiveservices",
            "account",
            "purge",
            "--name",
            name,
            "--resource-group",
            kept["group"],
            "--location",
            kept["region"],
            allow_fail=True,
        )
    went = somewhere(
        az,
        regions(region, elsewhere),
        lambda where: az(
            "cognitiveservices",
            "account",
            "create",
            "--name",
            name,
            "--resource-group",
            group,
            "--location",
            where,
            "--kind",
            kind,
            "--sku",
            sku,
            "--custom-domain",
            name,
            "--yes",
            "--output",
            "none",
            allow_fail=True,
        ),
        lambda: az.exists(
            "cognitiveservices", "account", "show", "--name", name, "--resource-group", group
        ),
        name,
    )
    if not went:
        return False
    done(f"{name}, {what}" + ("" if went == region else f", in {went}"))
    return True


#: The order a deployment sku is preferred in. GlobalStandard is what most
#: models are offered as and is billed per token; the Provisioned ones reserve
#: capacity by the hour and would quietly cost real money on a test.
SKU_ORDER = ("GlobalStandard", "Standard", "DataZoneStandard", "DeveloperTier")

#: The states of a model version Azure takes no new deployment of.
GOING = ("deprecating", "deprecated", "retired")


def offered(az: Az, region: str, model: str) -> Optional[Tuple[str, str]]:
    """``(version, sku)`` the region actually offers for a model, or None.

    Hard-coding either is how this failed the first time it met a real
    subscription: gpt-4o-mini is version 2024-07-18 and not 1, and it is
    offered as GlobalStandard and not Standard. Both differ by region and by
    month, so the region is asked rather than assumed.

    A version on its way out is not offered, whatever the list says. Azure
    kept listing gpt-4o-mini 2024-07-18 with its skus and its quota, and
    answered every new deployment with "is in deprecating state and cannot
    be used for new deployments".
    """
    every = (
        az(
            "cognitiveservices",
            "model",
            "list",
            "--location",
            region,
            reads=True,
            quiet=True,
            allow_fail=True,
        )
        or []
    )
    best: Optional[Tuple[str, str]] = None
    for entry in every:
        about = entry.get("model") or {}
        if about.get("name") != model or about.get("format") != "OpenAI":
            continue
        if str(about.get("lifecycleStatus") or "").lower() in GOING:
            continue
        skus = [s.get("name") for s in (about.get("skus") or []) if s.get("name")]
        for wanted in SKU_ORDER:
            if wanted in skus:
                version = str(about.get("version") or "")
                # The newest version a region offers, when it offers several.
                if best is None or version > best[0]:
                    best = (version, wanted)
                break
    return best


#: What a deployment asks for, in thousands of tokens a minute. The chat model
#: makes some six hundred calls for a hundred golden questions. The embedding
#: model is sent a whole document at once, 256 chunks a request: a 244-page
#: annual report is some 2,000 chunks and 400,000 tokens, and at 20 one request
#: was more than a minute's allowance, so every try was refused. Both are
#: billed per token, so a higher limit costs nothing by itself. Raise either in
#: the portal if calls wait.
CHAT_CAPACITY, EMBED_CAPACITY = 50, 350


def room(az: Az, region: str, model: str, sku: str, capacity: int) -> bool:
    """Whether the subscription has the quota left in a region to deploy a model at a capacity.

    Being offered is not being allowed: a region can list a model and give
    a subscription no quota line for it, and that is only found out when the
    deployment is refused after the account was made. An answer that cannot
    be read is taken as room, because this does not claim to know what it
    was not told.
    """
    if az.pretend:
        return True
    every = az(
        "cognitiveservices",
        "usage",
        "list",
        "--location",
        region,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    if not isinstance(every, list):
        return True
    wanted = f"OpenAI.{sku}.{model}".lower()
    for entry in every:
        if str((entry.get("name") or {}).get("value") or "").lower() == wanted:
            return (
                float(entry.get("limit") or 0) - float(entry.get("currentValue") or 0) >= capacity
            )
    return False


def home_of_openai(az: Az, config: Dict[str, str], tries: List[str]) -> str:
    """The first region, in the order given, that offers every model wanted and has the quota for it.

    The account is one region and its deployments live in it, so the region
    is chosen before the account is made and not after a deployment fails.
    When no region has everything the first is used, and each deployment
    then says for itself what it could not have.
    """
    for region in tries:
        short = ""
        for _, model, _, _, capacity in deployments(config):
            found = offered(az, region, model)
            if found is None and not az.pretend:
                short = f"{region} does not offer {model}"
                break
            if found is not None and not room(az, region, model, found[1], capacity):
                short = f"{region} has no quota left for {model} as {found[1]}"
                break
        if not short:
            return region
        note(short + (f"; trying {tries[tries.index(region) + 1]}" if region != tries[-1] else ""))
    note(f"no region of {', '.join(tries)} has every model, so the account goes in {tries[0]}")
    return tries[0]


def deployments(config: Dict[str, str]) -> List[Tuple[str, str, str, str, int]]:
    """What goes on the Azure OpenAI resource: ``(deployment, model, what it is for, its setting, capacity)``.

    The chat model always, since the golden questions are drafted with it.
    Pictures in detail use it too, and a second chat model only when
    AZURE_OPENAI_VISION_DEPLOYMENT names another. The embedding model only for a
    second vector.
    """
    writer, vision = (
        config["AZURE_OPENAI_WRITER_DEPLOYMENT"],
        config["AZURE_OPENAI_VISION_DEPLOYMENT"],
    )
    detailed = wants(config, "VX_DETAILED_PICTURES")
    both = detailed and vision == writer
    out = [
        (
            writer,
            writer,
            "drafts the golden questions"
            + (" and describes every picture in detail" if both else ""),
            "AZURE_OPENAI_WRITER_DEPLOYMENT",
            CHAT_CAPACITY,
        )
    ]
    if detailed and not both:
        out.append(
            (
                vision,
                vision,
                "describes every picture in detail",
                "AZURE_OPENAI_VISION_DEPLOYMENT",
                CHAT_CAPACITY,
            )
        )
    if wants(config, "VX_SECOND_VECTOR"):
        out.append(
            (
                config["AZURE_OPENAI_EMBED_DEPLOYMENT"],
                config["AZURE_OPENAI_EMBED_DEPLOYMENT"],
                "is the second vector",
                "AZURE_OPENAI_EMBED_DEPLOYMENT",
                EMBED_CAPACITY,
            )
        )
    return out


# ============================================================================
# WAITING: an account settled, or taken away
# ============================================================================
#
# INPUT   an account Azure is busy with
# OUTPUT  where it ended up; or gone, after a create that failed
#
# Azure answers before it is done, so each account is waited for, and one that
# failed to create is removed before the next try rather than left half-made.
#: States that mean somebody, possibly a run of this script you stopped, is
#: still working on the account. Waiting is right and deleting is not: one of
#: these deleted an account that was three minutes into being created, which
#: then took longer to disappear than it would have taken to finish.
BUSY = ("Creating", "Updating", "Deleting")


def _settled(az: Az, name: str, group: str, state: str, minutes: float = 10.0) -> str:
    """Wait while Azure is busy with this account, and answer where it ended up.

    An empty answer means there is no such account, which is the clean
    slate a create wants.
    """
    note(f"{name} is in {state}; waiting rather than touching it")
    for _ in range(int(minutes * 6)):
        time.sleep(10)
        now = az.state_of("cosmosdb", "show", "--name", name, "--resource-group", group)
        if now not in BUSY:
            return now
    return az.state_of("cosmosdb", "show", "--name", name, "--resource-group", group)


def _removed(az: Az, name: str, group: str, state: str) -> bool:
    """Take away an account that failed to create, and wait until it is gone.

    Azure keeps a failed account and then refuses to create over it:
    "delete the previous instance before attempting to recreate this
    account". Waiting is the part that is easy to leave out. The delete
    returns before the account has gone, so a create issued straight
    afterwards gets that same refusal, which then reads like a second,
    different failure and sent this script chasing the wrong cause.
    """
    note(f"{name} is in {state}, which Azure will not create over, so it is removed first")
    az(
        "cosmosdb",
        "delete",
        "--name",
        name,
        "--resource-group",
        group,
        "--yes",
        "--output",
        "none",
        allow_fail=True,
    )
    for _ in range(60):
        if not az.state_of("cosmosdb", "show", "--name", name, "--resource-group", group):
            return True
        time.sleep(10)
    note(
        f"{name} is still there ten minutes after being deleted; leaving it alone rather than guessing"
    )
    return False


# ============================================================================
# COSMOS DB: the indexing policy, the account, the databases, and what it costs
# ============================================================================
#
# INPUT   the settings, and whether the account bills per request
# OUTPUT  the Cosmos account, its one database and the four containers in it,
#         as the addresses the apps are given; None when refused
#
# The chunk_records indexing policy goes through a file, which is how az takes
# JSON on Windows unmangled. A serverless account bills per request and its
# databases take no throughput of their own; a provisioned one shares
# throughput across a database's containers, and the script says what that
# costs before it is made.
#: How the chunk_records container is indexed: the library's vectrixdb.chunk_store.INDEXING,
#: written out here so this script needs nothing installed. Every path a page
#: filters or sorts by; not the text or the metadata, which no query reads by
#: value and which are most of what indexing a write costs.
CHUNKS_INDEXING = {
    "indexingMode": "consistent",
    "automatic": True,
    "includedPaths": [{"path": "/*"}],
    "excludedPaths": [{"path": "/text/?"}, {"path": "/metadata/*"}, {"path": '/"_etag"/?'}],
}


@contextmanager
def _indexing_file() -> Iterator[str]:
    """The chunk_records container's indexing policy in a file, which is how az takes JSON on Windows unmangled."""
    handle, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(CHUNKS_INDEXING, out)
        yield path
    finally:
        os.unlink(path)


#: The RU/s the database's four containers share on a provisioned account: the free tier's 1000.
SHARED = 1000


def _serverless(az: Az, name: str, group: str) -> bool:
    """Whether the account bills per request, where a database takes no throughput of its own."""
    found = az(
        "cosmosdb",
        "show",
        "--name",
        name,
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    if not isinstance(found, dict):
        return False
    capabilities = (
        found.get("capabilities") or (found.get("properties") or {}).get("capabilities") or []
    )
    return any(isinstance(c, dict) and c.get("name") == "EnableServerless" for c in capabilities)


def _database(az: Az, name: str, group: str, serverless: bool) -> None:
    """The one database, with the throughput its containers share unless the account is serverless."""
    az(
        "cosmosdb",
        "sql",
        "database",
        "create",
        "--account-name",
        name,
        "--resource-group",
        group,
        "--name",
        DATA,
        *(() if serverless else ("--throughput", str(SHARED))),
        "--output",
        "none",
        allow_fail=True,
    )


def _says_what_it_costs(az: Az, name: str, group: str) -> None:
    """An ingestion database from before everything moved into data_db: nothing reads it, and its throughput is billed."""
    if az.pretend:
        return
    old = az(
        "cosmosdb",
        "sql",
        "database",
        "show",
        "--account-name",
        name,
        "--resource-group",
        group,
        "--name",
        INGESTION,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    if old:
        note(
            f"the {INGESTION} database is from before everything moved into {DATA}, and no app reads it now; "
            f"its own throughput is billed past the free tier's 1000 once {DATA} has {SHARED}"
        )
        note(
            f"copy its {PARENT_SECTIONS} and {CHUNK_RECORDS} into {DATA}'s, then delete it in the portal"
        )


def cosmos(
    az: Az, name: str, group: str, region: str, free: bool, elsewhere: str = ""
) -> Optional[Dict[str, str]]:
    """The Cosmos account, its one database and the four containers in it, as the addresses the apps are given. None when refused.

    ``elsewhere`` is one region or several, in the order to try them when
    the one before has no room.

    ``parent_sections`` is partitioned by document, because the only bulk
    thing that ever happens to parents is forgetting a document, and that is
    then one partition rather than a scan. ``chunk_records`` is partitioned
    by collection, because every collection page asks one collection.
    ``collection_records`` and ``signin_records`` are partitioned by kind
    with time to live on and no default, which is how the library's records
    layer keeps them: a record that should expire says so itself.
    """
    state = az.state_of("cosmosdb", "show", "--name", name, "--resource-group", group)
    if state in BUSY:
        state = _settled(az, name, group, state)
    if state == "Succeeded":
        skipped(f"{name}, for the parent sections and the pages' chunks")
    else:
        if state and not _removed(az, name, group, state):
            return None
        az(
            "cosmosdb",
            "create",
            "--name",
            name,
            "--resource-group",
            group,
            "--locations",
            f"regionName={region}",
            "failoverPriority=0",
            "isZoneRedundant=False",
            *(("--enable-free-tier", "true") if free else ("--capabilities", "EnableServerless")),
            "--output",
            "none",
            allow_fail=True,
        )
        # A create that prints nothing answers the same whether it worked or
        # not, so the account is asked what state it ended in.
        if (
            not az.pretend
            and az.state_of("cosmosdb", "show", "--name", name, "--resource-group", group)
            != "Succeeded"
        ):
            if free:
                # Why it was refused is in the output above. The usual reason
                # is that the one free account a subscription is spent, but
                # it is not the only one, so this does not claim to know.
                note(
                    "the free tier was refused; trying serverless, which bills per request and is pennies at this size"
                )
                return cosmos(az, name, group, region, free=False, elsewhere=elsewhere)
            rest = regions(region, elsewhere)[1:]
            if rest:
                note(f"{region} had no room for it, which Cosmos says outright; trying {rest[0]}")
                return cosmos(az, name, group, rest[0], free=False, elsewhere=",".join(rest[1:]))
            return None
        done(f"{name}, {'free tier' if free else 'serverless'} in {region}")

    serverless = _serverless(az, name, group)
    _database(az, name, group, serverless)
    az(
        "cosmosdb",
        "sql",
        "container",
        "create",
        "--account-name",
        name,
        "--resource-group",
        group,
        "--database-name",
        DATA,
        "--name",
        PARENT_SECTIONS,
        "--partition-key-path",
        "/doc",
        "--output",
        "none",
        allow_fail=True,
    )
    with _indexing_file() as policy:
        az(
            "cosmosdb",
            "sql",
            "container",
            "create",
            "--account-name",
            name,
            "--resource-group",
            group,
            "--database-name",
            DATA,
            "--name",
            CHUNK_RECORDS,
            "--partition-key-path",
            "/collection",
            "--idx",
            f"@{policy}",
            "--output",
            "none",
            allow_fail=True,
        )
    for container in (COLLECTION_RECORDS, SIGNIN_RECORDS):
        az(
            "cosmosdb",
            "sql",
            "container",
            "create",
            "--account-name",
            name,
            "--resource-group",
            group,
            "--database-name",
            DATA,
            "--name",
            container,
            "--partition-key-path",
            "/kind",
            "--ttl",
            "-1",
            "--output",
            "none",
            allow_fail=True,
        )
    done(
        f"{DATA}: {PARENT_SECTIONS}, {CHUNK_RECORDS}, {COLLECTION_RECORDS} and {SIGNIN_RECORDS}"
        + ("" if serverless else f", sharing {SHARED} RU/s")
    )
    if not serverless:
        _says_what_it_costs(az, name, group)

    account = f"cosmos://{name}.documents.azure.com"
    return {
        "parent_store": f"{account}/{DATA}/{PARENT_SECTIONS}",
        "chunk_store": f"{account}/{DATA}/{CHUNK_RECORDS}",
        "collection_store": f"{account}/{DATA}/{COLLECTION_RECORDS}",
        "signin_store": f"{account}/{DATA}/{SIGNIN_RECORDS}",
    }


# ============================================================================
# THE ENDPOINT OF A RESOURCE
# ============================================================================
#
# INPUT   a resource as az describes it
# OUTPUT  its endpoint address
#
# One place the address is read from, whichever service it is.


def endpoint_of(az: Az, name: str, group: str) -> str:
    found = az(
        "cognitiveservices",
        "account",
        "show",
        "--name",
        name,
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    return (
        (found or {})
        .get("properties", {})
        .get("endpoint", f"https://{name}.cognitiveservices.azure.com/")
    )


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   settings.env, and what 01 made
# OUTPUT  the services made, their addresses in state.json, the keys never
#         written down
#
# Six services, each skipped when settings.env says no, so a walkthrough can
# leave out what it does not need.


def main() -> int:
    args = arguments(__doc__)
    config = settings()
    az = Az(args.dry_run)
    group, region = config["VX_RESOURCE_GROUP"], config["VX_LOCATION"]
    begin(
        "03",
        "The AI services",
        "Document Intelligence and Speech on their free tiers, then Azure OpenAI.",
    )

    if not args.dry_run and not az.exists("group", "show", "--name", group):
        stop(f"There is no resource group {group} yet. Run 01_create_resources.py first.")

    kept = {}

    step("Document Intelligence, for pages with no text layer")
    # F0 is the free tier: 500 pages a month, and it reads only the first two
    # pages of any one document. Both TD reports have a real text layer, so
    # the library reads those itself and only the PNGs come here.
    if cognitive(
        az,
        config["VX_DOCINTEL"],
        group,
        region,
        "FormRecognizer",
        "F0",
        "free tier, 500 pages a month",
        config["VX_ELSEWHERE"],
    ):
        kept["docintel_endpoint"] = endpoint_of(az, config["VX_DOCINTEL"], group)
        note(
            "the free tier reads the first two pages of a document; a long scan needs S0, about 2 CAD a thousand pages"
        )
    else:
        note("Document Intelligence was refused, so a scanned page will read as nothing")

    step("Speech, for the WAV and the videos")
    if cognitive(
        az,
        config["VX_SPEECH"],
        group,
        region,
        "SpeechServices",
        "F0",
        "free tier, 5 hours a month",
        config["VX_ELSEWHERE"],
    ):
        kept["speech_endpoint"] = endpoint_of(az, config["VX_SPEECH"], group)
    else:
        note("Speech was refused, so the audio and the videos will read as nothing")

    if wants(config, "VX_COSMOS"):
        home = config.get("VX_COSMOS_LOCATION") or region
        step(
            f"Cosmos DB, for the parent sections a search returns, the chunks the pages count, and who may see what, in {home}"
        )
        made = cosmos(
            az,
            config["VX_COSMOS_NAME"],
            group,
            home,
            wants(config, "VX_COSMOS_FREE"),
            elsewhere=config.get("VX_COSMOS_ELSEWHERE", ""),
        )
        if made:
            kept.update(made)
            note(
                "every instance reads these; a file on one instance is invisible to the next and is not kept"
            )
        else:
            note(
                "Cosmos was refused, so parents, the pages' chunks and sign-in stay on each instance: one process only"
            )
    else:
        note(
            "VX_COSMOS is not yes, so parents, the pages' chunks and sign-in stay on each instance: one process only"
        )

    if wants(config, "VX_VISION"):
        step(f"Azure AI Vision, for what each picture shows, in {config['VX_VISION_LOCATION']}")
        if cognitive(
            az,
            config["VX_VISION_NAME"],
            group,
            config["VX_VISION_LOCATION"],
            "ComputerVision",
            "F0",
            "free tier, 5,000 pictures a month",
            config["VX_ELSEWHERE"],
        ):
            kept["vision_endpoint"] = endpoint_of(az, config["VX_VISION_NAME"], group)
            note(
                "it says what a picture is and reads the words in it; it does not reason about a chart"
            )
            if config["VX_VISION_LOCATION"] != config["VX_LOCATION"]:
                note(
                    f"it sits in {config['VX_VISION_LOCATION']} because {config['VX_LOCATION']} cannot caption"
                )
        else:
            note(
                "Vision was refused, so a figure keeps its caption and its picture and gains no description"
            )
    else:
        note("VX_VISION is not yes, so pictures are found by their captions alone")

    if wants(config, "VX_LANGUAGE"):
        step(
            "Azure AI Language, which finds the names, addresses and ids in a document before it is kept"
        )
        if cognitive(
            az,
            config["VX_LANGUAGE_NAME"],
            group,
            config["VX_LOCATION"],
            "TextAnalytics",
            "F0",
            "free tier, 5,000 text records a month",
            config["VX_ELSEWHERE"],
        ):
            kept["language_endpoint"] = endpoint_of(az, config["VX_LANGUAGE_NAME"], group)
            note(
                "the extraction app masks every document with it, in English and French alike; the patterns run after it"
            )
            note(
                f"AZURE_LANGUAGE_ENDPOINT={kept['language_endpoint']}; 05 reads its key from Azure as AZURE_LANGUAGE_KEY and never writes it down"
            )
        else:
            note(
                "Language was refused, so the extraction app masks by the patterns alone: emails, phones, cards and ids by their shape"
            )
    else:
        note(
            "VX_LANGUAGE is not yes, so the extraction app masks by the patterns alone: emails, phones, cards and ids by their shape"
        )

    if wants(config, "VX_TRANSLATOR"):
        step("Azure AI Translator, which the extraction app's /translate routes answer with")
        name = config["VX_TRANSLATOR_NAME"]
        if cognitive(
            az,
            name,
            group,
            region,
            "TextTranslation",
            "F0",
            "free tier, 2 million characters a month",
            config["VX_ELSEWHERE"],
        ):
            # A regional resource is called at the one address every
            # Translator shares, with its region beside its key. Without the
            # region the first call is refused, so the region it landed in
            # is what is kept.
            kept["translator_region"] = region_of(az, name, group) or region
            note(
                f"AZURE_TRANSLATOR_REGION={kept['translator_region']}; 05 reads its key from Azure as AZURE_TRANSLATOR_KEY and never writes it down"
            )
        else:
            note(
                "Translator was refused, so /translate/text and /translate/detect answer 503 and name the setting they need"
            )
    else:
        note(
            "VX_TRANSLATOR is not yes, so /translate/text and /translate/detect answer 503 and name the setting they need"
        )

    if not wants(config, "VX_OPENAI"):
        note("VX_OPENAI is not yes, so no chat model, no detailed pictures and no second vector")
        remember(**kept, openai_endpoint=None)
        finish(
            "The AI services are ready, without Azure OpenAI.", "04_push_local_to_blob_cosmosdb.py"
        )
        return 0

    step("Azure OpenAI")
    name = config["VX_OPENAI_NAME"]
    there = az(
        "cognitiveservices",
        "account",
        "show",
        "--name",
        name,
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    if isinstance(there, dict) and there.get("location"):
        # Made on an earlier run: its deployments go where it already is.
        region = str(there["location"]).lower().replace(" ", "")
        after = ""
    else:
        home, tries = region, regions(region, config.get("VX_OPENAI_ELSEWHERE", ""))
        region = home_of_openai(az, config, tries)
        after = ",".join(tries[tries.index(region) + 1 :])
        if region != home:
            note(f"Azure OpenAI goes in {region}, where the quota is")
    if not cognitive(
        az, name, group, region, "OpenAI", "S0", "the chat model and the second vector", after
    ):
        note(
            "Azure could not make it. Usually that means this subscription has not been given access, "
            "or the region has none left. Everything else works without it: set VX_OPENAI=no in settings.env "
            "and run this again, or ask for access in the portal and come back."
        )
        remember(**kept, openai_endpoint=None)
        finish(
            "The AI services are ready, without Azure OpenAI.", "04_push_local_to_blob_cosmosdb.py"
        )
        return 0

    kept["openai_endpoint"] = endpoint_of(az, name, group)
    landed = az(
        "cognitiveservices",
        "account",
        "show",
        "--name",
        name,
        "--resource-group",
        group,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    if isinstance(landed, dict) and landed.get("location"):
        # Its deployments are asked of the region it is actually in.
        region = str(landed["location"]).lower().replace(" ", "")
    if not wants(config, "VX_SECOND_VECTOR"):
        note(
            "VX_SECOND_VECTOR is no, so no embedding deployment: every chunk is embedded with the built-in model alone"
        )
    for deployment, model, what, setting, capacity in deployments(config):
        step(f"The {deployment} deployment, which {what}")
        if az.exists(
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--name",
            name,
            "--resource-group",
            group,
            "--deployment-name",
            deployment,
        ):
            skipped(deployment)
            continue
        found = offered(az, region, model)
        if found is None:
            note(
                f"{region} does not offer {model} for a new deployment. Open Azure AI Foundry, see which models "
                f"it has, and put one in {setting} in settings.env."
            )
            continue
        version, sku = found
        print(f"       {region} offers version {version} as {sku}")
        made = az(
            "cognitiveservices",
            "account",
            "deployment",
            "create",
            "--name",
            name,
            "--resource-group",
            group,
            "--deployment-name",
            deployment,
            "--model-name",
            model,
            "--model-version",
            version,
            "--model-format",
            "OpenAI",
            "--sku-name",
            sku,
            "--sku-capacity",
            str(capacity),
            "--output",
            "none",
            allow_fail=True,
        )
        if (
            made is None
            and not az.pretend
            and not az.exists(
                "cognitiveservices",
                "account",
                "deployment",
                "show",
                "--name",
                name,
                "--resource-group",
                group,
                "--deployment-name",
                deployment,
            )
        ):
            why = reason(az)
            note(
                f"{model} {version} would not deploy as {sku}. "
                + (f"Azure said: {why}" if why else "Azure gave no reason.")
            )
            continue
        done(f"{deployment}, {model} {version}, {sku}, capacity {capacity}")

    remember(**kept)
    links(
        ("document intelligence", portal("cognitive", config["VX_DOCINTEL"], config)),
        ("speech", portal("cognitive", config["VX_SPEECH"], config)),
        (
            "cosmos db",
            portal("cosmos", config["VX_COSMOS_NAME"], config) if kept.get("parent_store") else "",
        ),
        (
            "azure ai vision",
            portal("cognitive", config["VX_VISION_NAME"], config)
            if kept.get("vision_endpoint")
            else "",
        ),
        (
            "azure ai language",
            portal("cognitive", config["VX_LANGUAGE_NAME"], config)
            if kept.get("language_endpoint")
            else "",
        ),
        (
            "azure ai translator",
            portal("cognitive", config["VX_TRANSLATOR_NAME"], config)
            if kept.get("translator_region")
            else "",
        ),
        ("azure openai", portal("cognitive", name, config)),
        ("its models, in AI Foundry", "https://ai.azure.com/"),
    )
    finish("The AI services are ready.", "04_push_local_to_blob_cosmosdb.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
