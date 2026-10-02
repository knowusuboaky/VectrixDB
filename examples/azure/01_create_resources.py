"""The resource providers, the resource group, the storage account, its three containers and the queue.

    python 01_create_resources.py
    python 01_create_resources.py --dry-run      # every az command printed, none run

This is where the files live and where the events come from. Five things
happen, in this order, because each needs the one before it.

1. THE RESOURCE PROVIDERS, SWITCHED ON
--------------------------------------

A subscription can only make a kind of resource once the provider for that
kind is registered, and a new subscription has almost none registered. The
first create of each kind is then refused with "SubscriptionNotFound", which
sends you looking at the subscription when the fault is the provider. So
before anything is made, every provider the walkthrough will ever need is
registered at once, whichever script will use it:

    Microsoft.Storage               the storage account, its containers, the queue (here)
    Microsoft.Search                Azure AI Search (02)
    Microsoft.Web                   the two Function Apps (05 and 06)
    Microsoft.CognitiveServices     Document Intelligence, Speech, Vision, Azure OpenAI (03)
    Microsoft.DocumentDB            Cosmos DB, where the parent sections, the pages' chunks and
                                    who may see what live (03)
    Microsoft.EventGrid             the event a new blob raises, and the link 06 makes
                                    from the storage account to the queue
    Microsoft.Insights              the functions' logs
    Microsoft.OperationalInsights   where those logs are kept

Microsoft.EventGrid is the one worth knowing about. Nothing in this script
uses it: the event subscription that carries a blob's event to the queue is
made by 06_create_main_function_app.py, a few scripts later. Without the provider that create is
refused, so it is switched on here with the rest, and the link can be made
when its turn comes. The storage account raises the events itself; the
provider is what lets anything subscribe to them.

Registering is free, costs nothing afterwards, and happens once in the life
of a subscription. Azure does it in the background, so asking is not enough:
a create that follows too closely fails the same way it did before. The
script asks for every missing provider together, then checks every ten
seconds until each says Registered, for up to four minutes. One still
registering after that is Azure being slow, not a failure; run this again and
it carries on. The list is ``PROVIDERS`` in ``_common.py``, and ``register``
there does the asking and the waiting.

2. THE RESOURCE GROUP
---------------------

Everything the walkthrough makes goes in this one group, so
99_delete_everything.py removes all of it with one command.

3. THE STORAGE ACCOUNT
----------------------

Locally redundant and hot, no public blob access, TLS 1.2 at least. Both
Function Apps use it as their own storage too.

4. THE THREE CONTAINERS
-----------------------

    ingestion   raw/       what you upload. An event fires for every blob here.
                markdown/  the Markdown each document was read into, so a
                           rechunk needs no second OCR bill, and its pictures.
                chunks/    the chunks each document was cut into, a JSON line
                           each, written before anything embeds them.
    evals                  the golden questions and the runs the Evaluate
                           page reads.
    audit       decisions/ a line for each decision an entitlement policy makes; empty unless a record carries one.
                access/    every sign-in and every read, a line each.

The three folders share one container and are kept apart by name, which is
why the event is filtered to raw/ and nothing else: markdown/ and chunks/ are
written by the function, and an event for either would feed the function its
own output. Nothing makes the folders; each appears with its first file. A
file deleted from raw/ takes its Markdown and its chunks with it.

The audit container is the one that is kept apart, because what it holds is
evidence: which restricted documents exist, and who was refused them. It
carries a time-based retention policy of VX_AUDIT_DAYS days, 7 unless you say,
with protected append writes allowed. Each log is one append blob a UTC day,
``decisions/2026/09/23.jsonl``, and a line can be added to it while nothing
already written can be changed or deleted until the days have passed, by the
app or by you. The library will not write to a container without such a
policy.

The policy is left unlocked, so 99_delete_everything.py can take it off and
delete everything, as a walkthrough should. A trail that nobody can shorten is
a locked policy, ``az storage container immutability-policy lock``, and that
cannot be undone: from then on neither the container nor the storage account
can be deleted until every blob in it is VX_AUDIT_DAYS days old.

5. THE QUEUE
------------

One queue, ``ingest``, in the same storage account. It sits in the middle of
the pipeline:

    a blob lands in ingestion/raw/
        -> the storage account raises BlobCreated (or BlobDeleted)
        -> the Event Grid link, made by 06, writes one message to ``ingest``
        -> the main Function App's queue trigger takes that message and reads the file

This script makes only the queue, because it has to exist before Event Grid
can be pointed at it. 06 makes the other two: the link, and the function
that reads the queue.

The queue is why a two hundred page report is never cut off half way. Event
Grid could call the function directly, but it waits thirty seconds for an
answer and sends the event again when none comes, so a long read would be
started more than once and cut off each time. Writing a queue message takes
milliseconds and never fails for being slow. The function then takes the
message in its own time and has its whole timeout, an hour, for one file.

A message is not lost when a read fails. It goes back on the queue, ten
minutes later, and is tried again, five times in all. After the fifth, Azure
moves it to ``ingest-poison``, a second queue it makes beside this one the
first time it needs it. That queue is the list of files that would not read;
put an alert on its length and no file ever vanishes quietly.

``az storage queue exists`` answers ``{"exists": false}`` rather than failing
when there is no queue, so it is read for its answer. Reading it the other way
once made this script say the queue was there and never make it, and Event
Grid then had nowhere to write.

Storage costs pennies at this size. Nothing here is billed by the hour.
Safe to run twice: everything is checked before it is made.
"""

from __future__ import annotations

from _common import (
    AUDIT,
    EVALS,
    INGESTION,
    QUEUE,
    Az,
    arguments,
    begin,
    done,
    finish,
    links,
    portal,
    register,
    regions,
    somewhere,
    _no_region,
    stop,
    note,
    remember,
    settings,
    skipped,
    step,
)


# ============================================================================
# RETENTION: how long the audit container keeps a line
# ============================================================================
#
# INPUT   the audit container
# OUTPUT  its time-based retention policy, or an empty dict when it has none
#
# The audit container lets a line be added and never changed. The policy is
# read before it is set, so a second run leaves the one that is there.


def retention_of(az: Az, account: str, group: str) -> dict:
    """The audit container's time-based retention policy, or an empty dict when it has none.

    Azure answers for a container with no policy too, with a period of 0, so
    the period is what says whether there is one.
    """
    found = az(
        "storage", "container", "immutability-policy", "show",
        "--account-name", account, "--resource-group", group, "--container-name", AUDIT,
        reads=True, quiet=True, allow_fail=True,
    )
    if not isinstance(found, dict):
        return {}
    policy = {**(found.get("properties") or {}), **{k: v for k, v in found.items() if k != "properties"}}
    return policy if int(policy.get("immutabilityPeriodSinceCreationInDays") or 0) > 0 else {}


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --dry-run, and settings.env
# OUTPUT  the providers registered, then the resource group, the storage
#         account, its three containers and the queue, each recorded in
#         state.json
#
# Five things in this order, because each needs the one before it. --dry-run
# prints every az command and runs none.


def main() -> int:
    args = arguments(__doc__)
    config = settings()
    az = Az(args.dry_run)
    group, account, region = config["VX_RESOURCE_GROUP"], config["VX_STORAGE"], config["VX_LOCATION"]
    begin("01", "Storage and the queue", "The resource providers, the resource group, one storage account, three containers, one queue.")

    # Before anything else: a subscription that has never made one of these
    # refuses every create with a message about the subscription not existing.
    # Every provider the walkthrough needs is switched on here, including
    # Microsoft.EventGrid, which nothing uses until 06 makes the link from
    # the storage account to the queue. See section 1 of the docstring.
    register(az)
    tries = regions(region, config["VX_ELSEWHERE"])

    step("The resource group")
    if az.exists("group", "show", "--name", group):
        skipped(f"{group}")
    else:
        went = somewhere(
            az, tries,
            lambda where: az("group", "create", "--name", group, "--location", where, "--output", "none", allow_fail=True),
            lambda: az.exists("group", "show", "--name", group),
            f"the resource group {group}",
        )
        if not went:
            stop(_no_region(group, tries))
        done(f"{group} in {went}")

    step("The storage account")
    if az.exists("storage", "account", "show", "--name", account, "--resource-group", group):
        skipped(f"{account}")
    else:
        shown = ("storage", "account", "show", "--name", account, "--resource-group", group)

        def create(where: str) -> None:
            az(
                "storage", "account", "create",
                "--name", account,
                "--resource-group", group,
                "--location", where,
                # Locally redundant is the cheapest and enough for a test. Hot,
                # because everything here is read within the hour.
                "--sku", "Standard_LRS",
                "--access-tier", "Hot",
                "--allow-blob-public-access", "false",
                "--min-tls-version", "TLS1_2",
                "--output", "none",
                allow_fail=True,
            )

        def clear() -> None:
            if az.state_of(*shown):
                az("storage", "account", "delete", "--name", account, "--resource-group", group, "--yes", allow_fail=True)

        went = somewhere(az, tries, create, lambda: az.state_of(*shown) == "Succeeded", f"the storage account {account}", clear)
        if not went:
            stop(_no_region(account, tries))
        region = went
        done(f"{account} in {went}")

    # One key, read now and used for the rest of this script. It is never
    # written to disk: the other scripts ask for it again when they need it.
    key = None
    if not args.dry_run:
        keys = az("storage", "account", "keys", "list", "--account-name", account, "--resource-group", group, reads=True, quiet=True)
        key = keys[0]["value"] if keys else None

    def with_key(*args_: str) -> tuple:
        return (*args_, "--account-name", account, *(("--account-key", key) if key else ()))

    step("The three containers")
    for container, what in (
        (INGESTION, "raw/ what you upload, markdown/ and chunks/ what the function wrote"),
        (EVALS, "questions and runs"),
        (AUDIT, "decisions/ and access/, appended and never changed"),
    ):
        if az.exists(*with_key("storage", "container", "show", "--name", container)):
            skipped(f"{container}, {what}")
        else:
            az(*with_key("storage", "container", "create", "--name", container), "--output", "none")
            done(f"{container}, {what}")

    # A line can be added and nothing written can be changed: see section 4.
    step(f"The {AUDIT} container's retention policy")
    days = config["VX_AUDIT_DAYS"]
    held = retention_of(az, account, group)
    if held:
        skipped(f"{held.get('immutabilityPeriodSinceCreationInDays')} days, {str(held.get('state', '')).lower()}, appends allowed")
    else:
        az(
            "storage", "container", "immutability-policy", "create",
            "--account-name", account,
            "--resource-group", group,
            "--container-name", AUDIT,
            "--period", days,
            "--allow-protected-append-writes", "true",
            "--output", "none",
        )
        done(f"{days} days, unlocked, appends allowed")
    note("left unlocked, so 99 can take it off; locking it cannot be undone, see section 4")

    # The middle of the pipeline: Event Grid writes one message here for each
    # blob under ingestion/raw/, and the main app's queue trigger reads it.
    # Made here because it has to exist before 06 points Event Grid at it.
    # See section 5 of the docstring.
    step("The queue")
    if az.exists(*with_key("storage", "queue", "exists", "--name", QUEUE)):
        skipped(f"{QUEUE}")
    else:
        az(*with_key("storage", "queue", "create", "--name", QUEUE), "--output", "none")
        done(f"{QUEUE}")
    note("Azure makes ingest-poison beside it the first time a message fails five times")

    blob = f"https://{account}.blob.core.windows.net"
    remember(resource_group=group, location=region, storage=account, blob_account=blob, audit_container=AUDIT)
    links(
        ("the resource group", portal("group", group, config)),
        ("the storage account", portal("storage", account, config)),
        ("the containers", portal("storage", account, config, page="containersList")),
    )
    finish(
        f"Storage is ready at {blob}",
        "02_create_search.py",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
