"""A budget on the subscription, so a forgotten resource cannot quietly eat the credit.

    python 00b_set_budget.py                 # 25 CAD a month, alerts at 50, 80 and 100 percent
    python 00b_set_budget.py --amount 50
    python 00b_set_budget.py --show          # what the budget is, and what has been spent
    python 00b_set_budget.py --remove        # take it off again

This is the only thing here that protects the money rather than spending it,
and it is the one step worth doing before anything exists. A budget costs
nothing, changes nothing, and stops nothing: Azure does not switch a service
off when you pass it. What it does is **tell you**, by email, while there is
still credit left to save.

The alerts are on what has actually been spent, at half the budget, at four
fifths and at all of it, and one more on what Azure **forecasts** you will
have spent by the end of the month. The forecast one is the useful one: it
fires days before the money is gone rather than after.

A budget covers the whole subscription, not just this walkthrough's resource
group, which is deliberate. A resource made outside the group by hand is
exactly the kind that gets forgotten, and a group-scoped budget would not
see it.

The spend a budget reports lags a few hours behind, the way all Azure billing
does. It is a guard rail, not a meter.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from _common import Az, arguments, begin, done, finish, links, note, settings, state, step, stop, subscription_link


# ============================================================================
# SETTINGS: the budget's name, and the API it is written through
# ============================================================================
#
# One budget, named so a second run finds it, written through the consumption
# API; the alerts on what is spent and on what is forecast sit at fixed places
# in it.
#: One budget a subscription, named so a second run replaces it rather than adding another.
NAME = "vectrixdb-walkthrough"
API = "2023-05-01"
#: Where the alerts fire, as a share of the amount. The last is on the forecast.
SPENT_AT = (50, 80, 100)
FORECAST_AT = 100


# ============================================================================
# THE BUDGET: its options, its address, its months and its alerts
# ============================================================================
#
# INPUT   the amount and the emails, from the command line
# OUTPUT  the budget's address; its start, the first of a month, which is the
#         only start Azure allows; one alert a threshold, at half, four fifths
#         and all of what is spent, and one on what Azure forecasts
#
# Azure keys the alerts by name, so the names are fixed, and a second run
# replaces rather than adds.


def options(parser) -> None:
    parser.add_argument("--amount", type=float, default=25.0, help="the budget, in the subscription's own currency")
    parser.add_argument("--email", default=None, help="who is told; the signed-in account by default")
    parser.add_argument("--show", action="store_true", help="show the budget and what has been spent, change nothing")
    parser.add_argument("--remove", action="store_true", help="take the budget off again")
    parser.add_argument("--name", default=NAME, help="the budget's name, to keep more than one")


def url(subscription: str, name: str) -> str:
    return f"/subscriptions/{subscription}/providers/Microsoft.Consumption/budgets/{name}?api-version={API}"


def months(start: datetime, ahead: int = 12) -> Dict[str, str]:
    """A monthly budget runs from the first of a month. Azure refuses any other start."""
    first = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = first.replace(year=first.year + 1) if ahead >= 12 else first + timedelta(days=30 * ahead)
    return {"startDate": first.strftime("%Y-%m-%dT00:00:00Z"), "endDate": end.strftime("%Y-%m-%dT00:00:00Z")}


def alerts(amount: float, emails: List[str]) -> Dict[str, Any]:
    """One notification a threshold. Azure keys them by name, so the names are fixed."""
    made: Dict[str, Any] = {}
    for share in SPENT_AT:
        made[f"spent-{share}"] = {
            "enabled": True,
            "operator": "GreaterThanOrEqualTo",
            "threshold": float(share),
            "contactEmails": emails,
            "thresholdType": "Actual",
        }
    # The one that fires before the money is gone rather than after.
    made[f"forecast-{FORECAST_AT}"] = {
        "enabled": True,
        "operator": "GreaterThanOrEqualTo",
        "threshold": float(FORECAST_AT),
        "contactEmails": emails,
        "thresholdType": "Forecasted",
    }
    return made


# ============================================================================
# SHOWING WHAT IS SET
# ============================================================================
#
# INPUT   the budget as Azure holds it
# OUTPUT  what the budget is and what has been spent against it so far,
#         printed
#
# --show reads and prints; it changes nothing.


def show(az: Az, subscription: str, name: str) -> int:
    found = az("rest", "--method", "get", "--url", url(subscription, name), reads=True, quiet=True, allow_fail=True)
    if not found:
        note(f"there is no budget called {name} on this subscription")
        note("  python 00b_set_budget.py        sets one at 25 a month")
        return 1
    about = found.get("properties", {})
    spent = about.get("currentSpend") or {}
    amount = about.get("amount")
    step(f"{name}")
    print(f"       budget      {amount} {spent.get('unit', '')} a {str(about.get('timeGrain', '')).lower()}")
    if spent:
        used = float(spent.get("amount") or 0)
        share = (100 * used / float(amount)) if amount else 0
        print(f"       spent       {used:.2f} {spent.get('unit', '')} so far, {share:.0f} percent of it")
    forecast = about.get("forecastSpend") or {}
    if forecast:
        unit = forecast.get("unit") or spent.get("unit") or ""
        print(f"       forecast    {float(forecast.get('amount') or 0):.2f} {unit} by the end of the month".rstrip())
    told = sorted({e for n in (about.get("notifications") or {}).values() for e in (n.get("contactEmails") or [])})
    print(f"       alerts      {', '.join(f'{s}%' for s in SPENT_AT)} of what is spent, and {FORECAST_AT}% of the forecast")
    print(f"       told        {', '.join(told) or 'nobody'}")
    note("what a budget reports lags a few hours behind, the way all Azure billing does")
    links(("this budget in the portal", subscription_link("budgets", {**state(), "subscription": subscription})))
    return 0


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --amount, --show, --remove
# OUTPUT  the budget written, shown, or taken off
#
# The only thing here that protects the money rather than spending it, and the
# one step worth doing before anything exists. A budget costs nothing, changes
# nothing and stops nothing: Azure does not switch a service off when you pass
# it. What it does is tell you, by email, while there is still credit left to
# save.


def main() -> int:
    args = arguments(__doc__, options)
    settings()  # read, so a broken settings.env is found here rather than later
    az = Az(args.dry_run)
    begin("00b", "A budget", "Nothing is created that costs anything. This is the guard on the credit.")

    who = az("account", "show", reads=True, quiet=True, allow_fail=True) if not args.dry_run else None
    if who is None and not args.dry_run:
        stop("Not signed in. Run 00_login.py first.")
    subscription = (who or {}).get("id", "<your subscription>")
    email = args.email or (who or {}).get("user", {}).get("name", "")

    if args.show:
        return 0 if args.dry_run else show(az, subscription, args.name)

    if args.remove:
        step(f"Taking off {args.name}")
        az("rest", "--method", "delete", "--url", url(subscription, args.name), allow_fail=True)
        done("gone, and nothing else changed")
        note("the resources it was watching are still there: 99_delete_everything.py is what removes those")
        return 0

    if args.dry_run and not email:
        email = "you@example.com"
    if not email or "@" not in email:
        stop(
            "Could not work out who to email. Pass one: python 00b_set_budget.py --email you@example.com\n"
            "  A budget with nobody to tell is a budget that tells nobody."
        )

    step(f"A budget of {args.amount:g} a month on {subscription}")
    body = {
        "properties": {
            "category": "Cost",
            "amount": args.amount,
            "timeGrain": "Monthly",
            "timePeriod": months(datetime.now(timezone.utc)),
            "notifications": alerts(args.amount, [email]),
        }
    }
    print(f"       covers      the whole subscription, not one resource group")
    print(f"       alerts      {', '.join(f'{s}%' for s in SPENT_AT)} of what is spent, and {FORECAST_AT}% of the forecast")
    print(f"       emails      {email}")
    az(
        "rest",
        "--method", "put",
        "--url", url(subscription, args.name),
        "--headers", "Content-Type=application/json",
        "--body", json.dumps(body),
        "--output", "none",
    )
    done(f"{args.name} is set")
    note("Azure does not switch anything off when you pass a budget; it tells you, which is the point")
    note("run this again with another --amount to change it: one budget, replaced, never two")

    links(
        ("the budget in the portal", subscription_link("budgets", {**state(), "subscription": subscription})),
        ("what it has cost so far", subscription_link("cost", {**state(), "subscription": subscription})),
    )
    finish(
        f"The credit is watched. An email goes to {email} at {SPENT_AT[0]} percent of {args.amount:g}.",
        "01_create_resources.py",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
