"""Delete the resource group, and with it everything these scripts made.

    python 99_delete_everything.py
    python 99_delete_everything.py --wait      # stay until Azure says it is gone

This is the script that protects your credit. Every resource went into one
group for exactly this reason: one command removes all of them, and it cannot
miss one it has never heard of.

It asks you to type the group's name first, because it cannot be undone. It
deletes nothing outside that group, so a resource you made yourself elsewhere
is untouched.

Run it when you stop for the day. The search service is billed by the hour
whether or not anybody searches it, and that is the whole of the running cost.

The audit container keeps what it holds until its retention runs out, and
Azure will not delete a storage account holding a blob it is keeping. So its
retention policy is taken off first, which an unlocked policy allows, and
then the group goes. A policy somebody locked cannot be taken off, by anybody:
then everything else is deleted and the storage account stays until every
blob in the container is old enough, and this says so rather than pretending.
"""

from __future__ import annotations

from _common import (
    AUDIT,
    OLD_CATALOG,
    STATE,
    Az,
    arguments,
    begin,
    done,
    finish,
    links,
    note,
    portal,
    search_indexes,
    settings,
    size,
    step,
    stop,
)


# ============================================================================
# OPTIONS
# ============================================================================
#
# INPUT   the command line
# OUTPUT  --wait, to stay until Azure says it is gone
#
# By default the delete is asked for and the script returns.


def options(parser) -> None:
    parser.add_argument(
        "--wait", action="store_true", help="stay until Azure says it is gone, a few minutes"
    )
    parser.add_argument(
        "--yes", action="store_true", help="do not ask; for a script that knows what it is doing"
    )


# ============================================================================
# LETTING GO: the audit container's retention
# ============================================================================
#
# INPUT   the audit container
# OUTPUT  its policy taken off when it is unlocked; a plain message when it is
#         locked
#
# Azure will not delete a storage account holding a blob it is keeping, so the
# retention policy comes off first; a locked one cannot be removed, and the
# script says so.


def let_go(az: Az, account: str, group: str, days: str) -> None:
    """Take the audit container's policy off when it is unlocked, and say so plainly when it is locked."""
    found = az(
        "storage",
        "container",
        "immutability-policy",
        "show",
        "--account-name",
        account,
        "--resource-group",
        group,
        "--container-name",
        AUDIT,
        reads=True,
        quiet=True,
        allow_fail=True,
    )
    policy = {
        **((found or {}).get("properties") or {}),
        **{k: v for k, v in (found or {}).items() if k != "properties"},
    }
    if int(policy.get("immutabilityPeriodSinceCreationInDays") or 0) <= 0:
        note("it has none, so nothing holds the storage account back")
        return
    if str(policy.get("state", "")).lower() == "locked":
        note(
            f"it is locked, so Azure keeps the {AUDIT} container, and the storage account it is in, until every blob "
            f"in it is {policy.get('immutabilityPeriodSinceCreationInDays', days)} days old. Everything else goes now; "
            "run this again after that to take the account too"
        )
        return
    az(
        "storage",
        "container",
        "immutability-policy",
        "delete",
        "--account-name",
        account,
        "--resource-group",
        group,
        "--container-name",
        AUDIT,
        "--if-match",
        str(policy.get("etag", "*")),
        "--output",
        "none",
        allow_fail=True,
    )
    done("taken off, which an unlocked policy allows, so the storage account can go with the rest")


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   the group's name, typed to confirm; --wait
# OUTPUT  the resource group deleted, and everything in it
#
# Every resource went into one group for exactly this reason: one command
# removes all of them, and it cannot miss one it has never heard of. Nothing
# outside the group is touched.


def main() -> int:
    args = arguments(__doc__, options)
    config = settings()
    az = Az(args.dry_run)
    group = config["VX_RESOURCE_GROUP"]
    begin("99", "Delete everything", f"The whole of {group}, which cannot be undone.")

    if not az.exists("group", "show", "--name", group):
        note(f"there is no {group}, so there is nothing to delete and nothing being billed")
        STATE.unlink(missing_ok=True)
        return 0

    links(("what you are about to delete", portal("group", group, config)))
    step("What is in it")
    inside = az("resource", "list", "--resource-group", group, reads=True, quiet=True) or []
    for one in sorted(inside, key=lambda r: r.get("type", "")):
        print(f"       {one.get('type', '?').split('/')[-1]:<22} {one.get('name')}")
    print(
        f"\n  {len(inside)} resources. Deleting the group deletes every one of them, and the documents in them."
    )
    # The indexes are not resources, so the list above does not show them,
    # and an index is where the documents actually are. Each is shown with
    # its count, so a leftover from an earlier run is visible here rather
    # than only in the portal.
    indexes = (
        search_indexes(az, config)
        if any(str(one.get("type", "")).lower().endswith("searchservices") for one in inside)
        else None
    )
    if indexes:
        print(f"\n  In the search service {config['VX_SEARCH']}:")
        for index in indexes:
            why = (
                "   empty: a leftover from before the prefix was set to nothing, which 06 deletes"
                if index["name"] == OLD_CATALOG and not index["documents"]
                else ""
            )
            print(
                f"       {'index':<22} {index['name']:<28} {index['documents']} documents, {size(index['bytes'])}{why}"
            )
    elif indexes is not None:
        print(f"\n  The search service {config['VX_SEARCH']} holds no index yet.")

    if not args.yes and not args.dry_run:
        typed = input(f"\n  Type {group} to delete it, or anything else to stop: ").strip()
        if typed != group:
            stop("Nothing was deleted.")

    step(f"The {AUDIT} container's retention policy")
    let_go(az, config["VX_STORAGE"], group, config.get("VX_AUDIT_DAYS", "7"))

    step("Deleting")
    az(
        "group",
        "delete",
        "--name",
        group,
        "--yes",
        *(() if args.wait else ("--no-wait",)),
        "--output",
        "none",
    )
    STATE.unlink(missing_ok=True)

    if args.wait:
        done(f"{group} is gone, and nothing from it is being billed")
    else:
        done(f"{group} is being deleted, which takes a few minutes")
        note("Azure is doing it in the background; the portal shows it disappearing")
        note(f"  check with: az group exists --name {group}")
    finish(
        "Everything these scripts made is gone.",
        "your data folder and settings.env are still here, so 01 onwards will rebuild it whenever you want",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
