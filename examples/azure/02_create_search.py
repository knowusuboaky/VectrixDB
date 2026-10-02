"""The Azure AI Search service, which is where the index lives.

    python 02_create_search.py
    python 02_create_search.py --tier free      # overrides settings.env once

This is the only resource here billed by the hour, so it is the one to delete
when you stop for the day.

    free    0 CAD. 50 MB, three indexes, no semantic ranker. One per
            subscription, so if you already have one this will refuse.
    basic   about 3 to 4 CAD a day while it exists. 2 GB, and the semantic
            ranker, which the library can ask for per search.

The index itself is made by the library on the first write, not here, because
the field it needs depends on the collection: the vectors, the filterable
fields a policy reads, and whether a second vector is coming.
"""

from __future__ import annotations

from _common import Az, _no_region, arguments, begin, done, finish, links, note, portal, regions, remember, settings, skipped, somewhere, step, stop


# ============================================================================
# OPTIONS
# ============================================================================
#
# INPUT   the command line
# OUTPUT  the tier, free or basic, from --tier or from settings.env
#
# --tier overrides settings.env once.


def options(parser) -> None:
    parser.add_argument("--tier", choices=("free", "basic"), default=None, help="overrides VX_SEARCH_TIER for this run")


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   the tier
# OUTPUT  the search service, its address remembered for 06
#
# The only resource here billed by the hour, so the one to delete when you
# stop for the day. The index itself is made by the library on the first
# write, not here, because the fields it needs depend on the collection.


def main() -> int:
    args = arguments(__doc__, options)
    config = settings()
    az = Az(args.dry_run)
    group, name = config["VX_RESOURCE_GROUP"], config["VX_SEARCH"]
    tier = args.tier or config["VX_SEARCH_TIER"]
    begin("02", "Azure AI Search", f"One service, {tier} tier. The index is made later, by the first write.")

    if not args.dry_run and not az.exists("group", "show", "--name", group):
        stop(f"There is no resource group {group} yet. Run 01_create_resources.py first.")

    step("The service")
    if az.exists("search", "service", "show", "--name", name, "--resource-group", group):
        skipped(f"{name}")
    else:
        if tier == "free":
            note("the free tier is one per subscription; if you have one already this will refuse")
        # One replica and one partition: the smallest shape, and all the free
        # tier allows. It is also all a test of this size needs.
        shown = ("search", "service", "show", "--name", name, "--resource-group", group)
        tries = regions(config["VX_LOCATION"], config["VX_ELSEWHERE"])

        def create(where: str) -> None:
            az(
                "search", "service", "create",
                "--name", name,
                "--resource-group", group,
                "--location", where,
                "--sku", tier,
                "--replica-count", "1",
                "--partition-count", "1",
                "--output", "none",
                allow_fail=True,
            )

        def clear() -> None:
            if az.exists(*shown):
                az("search", "service", "delete", "--name", name, "--resource-group", group, "--yes", allow_fail=True)

        went = somewhere(az, tries, create, lambda: az.state_of(*shown).lower() == "succeeded", f"the search service {name}", clear)
        if not went:
            stop(_no_region(name, tries))
        done(f"{name}, {tier} tier, in {went}")

    if tier == "basic":
        step("The semantic ranker")
        # Free for the first thousand requests a month, which this test will
        # not come near. It has to be turned on before a search can ask for it.
        az(
            "search", "service", "update",
            "--name", name,
            "--resource-group", group,
            "--semantic-search", "free",
            "--output", "none",
            allow_fail=True,
        )
        done("turned on, free plan, a thousand requests a month")
    else:
        note("no semantic ranker on the free tier, so the evaluation will compare fewer setups")

    endpoint = f"https://{name}.search.windows.net"
    remember(search_service=name, search_endpoint=endpoint, search_tier=tier)
    links(
        ("the search service", portal("search", name, config)),
        ("its indexes, once there are any", portal("search", name, config, page="indexes")),
    )
    finish(
        f"The search service is at {endpoint}",
        "03_create_ai_services.py",
    )
    if tier == "basic":
        note("this is the one being billed by the hour now: 99_delete_everything.py stops that")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
