"""Sign in to Azure, pick the subscription, and check the names are free.

    python 00_login.py

Nothing is created here and nothing is billed. It signs you in, shows which
subscription the rest of the scripts will spend against, and asks Azure
whether the two global names, the storage account and the search service,
are still free. Finding a name taken now costs a second; finding it in the
middle of 01 leaves half a resource group behind.

It ends by telling you how to set a budget alert. That is the one thing
protecting your credit, and no script can do it for you.
"""

from __future__ import annotations

import sys

from _common import (
    regions,
    PROVIDERS,
    Az,
    arguments,
    begin,
    done,
    finish,
    links,
    note,
    remember,
    settings,
    state,
    step,
    stop,
    subscription_link,
    tenant_domain,
)


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   nothing; it asks az to sign you in
# OUTPUT  the subscription the other scripts will spend against, shown;
#         whether the storage account and search service names are still free;
#         and how to set a budget alert
#
# Nothing is created here and nothing is billed. Finding a taken name now
# costs a second; finding it in the middle of 01 leaves half a resource group
# behind. It ends by telling you how to set a budget alert, the one thing
# protecting your credit that no script can do for you.


def main() -> int:
    args = arguments(__doc__)
    config = settings()
    az = Az(args.dry_run)
    begin("00", "Sign in", "No resource is made here, and nothing is billed.")

    step("Who you are")
    account = az("account", "show", reads=True, allow_fail=True)
    if account is None and not args.dry_run:
        note("not signed in yet, so a browser will open")
        az("login")
        account = az("account", "show", reads=True)
    if account is None:
        account = {"name": "(a subscription)", "id": "(its id)", "user": {"name": "(you)"}}

    wanted = config.get("VX_SUBSCRIPTION", "").strip()
    if wanted and wanted not in (account.get("id"), account.get("name")):
        step(f"Switching to {wanted}")
        az("account", "set", "--subscription", wanted)
        account = az("account", "show", reads=True) or account

    done(f"signed in as {account.get('user', {}).get('name', '?')}")
    done(f"spending against {account.get('name')} ({account.get('id')})")
    if not args.dry_run and len(sys.argv) and account.get("id"):
        remember(
            subscription=account["id"],
            subscription_name=account.get("name"),
            # Every portal link opens in this directory. Without it the portal
            # picks whichever one the browser was last in and refuses the link.
            tenant=account.get("tenantId") or account.get("homeTenantId"),
            # Its domain, not its id: the portal refuses a link carrying the id.
            tenant_domain=tenant_domain(az) or None,
        )

    step("Is every name still free")
    # Two of these names are global across all of Azure, so they are the two
    # that can already be somebody else's. The rest only have to be unique
    # inside the resource group, which is new.
    free = True
    taken = az(
        "storage",
        "account",
        "check-name",
        "--name",
        config["VX_STORAGE"],
        reads=True,
        allow_fail=True,
    )
    if taken is not None and not taken.get("nameAvailable", True):
        free = False
        print(
            f"  no   the storage account {config['VX_STORAGE']} is taken: {taken.get('message', '')}"
        )
    else:
        done(f"storage account {config['VX_STORAGE']}")

    # A search service has no check-name command, so this asks whether its
    # address answers. A name nobody has used does not resolve.
    import socket

    host = f"{config['VX_SEARCH']}.search.windows.net"
    try:
        socket.getaddrinfo(host, 443)
        free = False
        print(f"  no   the search service {config['VX_SEARCH']} is taken: {host} already answers")
    except socket.gaierror:
        done(f"search service {config['VX_SEARCH']}")

    if not free:
        stop(
            "A name is taken. Open settings.env, change VX_PREFIX to something with more digits in it, "
            "and run this again. Every other name is built from it."
        )

    step("What the rest of this will make")
    for label, value in (
        ("resource group", config["VX_RESOURCE_GROUP"]),
        ("region", config["VX_LOCATION"]),
        (
            "if it has no room",
            ", then ".join(regions(config["VX_LOCATION"], config["VX_ELSEWHERE"])[1:])
            or "nowhere else",
        ),
        ("storage account", config["VX_STORAGE"]),
        ("search service", f"{config['VX_SEARCH']} ({config['VX_SEARCH_TIER']} tier)"),
        ("function app", config["VX_FUNCTION_APP"]),
        ("document intelligence", config["VX_DOCINTEL"]),
        ("speech", config["VX_SPEECH"]),
        (
            "azure openai",
            config["VX_OPENAI_NAME"] if config["VX_OPENAI"] == "yes" else "not asked for",
        ),
    ):
        print(f"       {label:<22} {value}")

    step("Resource providers")
    off = []
    for namespace in PROVIDERS:
        found = az(
            "provider", "show", "--namespace", namespace, reads=True, quiet=True, allow_fail=True
        )
        if (found or {}).get("registrationState") != "Registered":
            off.append(namespace)
    if off and not args.dry_run:
        note(f"{len(off)} of {len(PROVIDERS)} are off, which is normal on a new subscription")
        note(
            "01_create_resources.py switches them on and waits; it takes a minute or two, once ever"
        )
    elif not args.dry_run:
        done(f"all {len(PROVIDERS)} are on")

    note(
        "everything goes in that one resource group, so 99_delete_everything.py can remove all of it"
    )
    if config["VX_SEARCH_TIER"] == "basic":
        note(
            "the search service is the only part billed by the hour, about 3 to 4 CAD a day while it exists"
        )

    kept = state()
    links(
        ("the subscription", subscription_link("overview", kept)),
        ("what it has cost so far", subscription_link("cost", kept)),
    )
    finish(
        "Signed in, and every name is free.",
        "00b_set_budget.py, which puts a budget on the credit before anything can spend it",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
