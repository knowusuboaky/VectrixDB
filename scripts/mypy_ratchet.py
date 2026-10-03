"""Fail when `mypy vectrixdb` reports more errors than the recorded baseline.

It began as a ratchet for the backlog the pre-2.2 code carried, which gating
on zero would have left permanently red. The backlog is gone and the baseline
is 0, so CI now also runs mypy as a plain gate; this stays as the second gate,
so a deliberate, temporary regression can be recorded rather than argued over.

    python scripts/mypy_ratchet.py            # compare against .mypy-baseline
    python scripts/mypy_ratchet.py --update   # record the current count

Exit status is 1 only when the count went up. Going down prints a reminder to
lower the baseline so the improvement is locked in.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


# ============================================================================
# SETTINGS: the baseline
# ============================================================================
#
# .mypy-baseline holds the error count a change is held to.

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".mypy-baseline"


# ============================================================================
# COUNTING AND DECIDING
# ============================================================================
#
# INPUT   mypy's output; then the count against the baseline
# OUTPUT  the error count from the summary line, 0 for a clean run; an exit
#         status and its message
#
# Exit 1 only when the count went up. Going down prints a reminder to lower
# the baseline so the improvement is locked in.


def parse_count(output: str) -> int:
    """The error count from mypy's summary line, or 0 for a clean run."""
    match = re.search(r"Found (\d+) errors?", output)
    if match:
        return int(match.group(1))
    if "Success: no issues found" in output:
        return 0
    raise RuntimeError("could not find mypy's summary line in its output")


def decide(count: int, baseline: int) -> tuple[int, str]:
    """Exit status and message for a count against a baseline."""
    if count > baseline:
        return 1, (
            f"mypy: {count} errors, baseline is {baseline}. {count - baseline} new. "
            "Fix them, or annotate them away; do not raise the baseline."
        )
    if count < baseline:
        return 0, (
            f"mypy: {count} errors, baseline is {baseline}. Down {baseline - count}: "
            "run `python scripts/mypy_ratchet.py --update` to lock that in."
        )
    return 0, f"mypy: {count} errors, matching the baseline."


# ============================================================================
# RUNNING MYPY
# ============================================================================
#
# INPUT   nothing
# OUTPUT  mypy's output over the vectrixdb package
#
# Run the way CI runs it.


def run_mypy() -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "vectrixdb"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.stdout + proc.stderr


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --update to record the current count
# OUTPUT  the verdict, or the baseline rewritten
#
# It began as a ratchet for the backlog the pre-2.2 code carried. The backlog
# is gone and the baseline is 0, so CI also runs mypy as a plain gate; this
# stays as the second gate, so a deliberate, temporary regression can be
# recorded rather than argued over.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--update", action="store_true", help="record the current count")
    args = parser.parse_args(argv)
    output = run_mypy()
    count = parse_count(output)

    if args.update:
        BASELINE.write_text(f"{count}\n", encoding="utf-8")
        print(f"mypy baseline set to {count}")
        return 0

    if not BASELINE.exists():
        print(f"No {BASELINE.name}; run with --update to create it. Current count: {count}")
        return 1

    baseline = int(BASELINE.read_text(encoding="utf-8").strip())
    status, message = decide(count, baseline)
    print(message)
    return status


if __name__ == "__main__":
    sys.exit(main())
