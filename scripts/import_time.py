"""Measure what importing the package costs, in a fresh interpreter each time.

    python scripts/import_time.py            # report
    python scripts/import_time.py --check 300   # exit 1 if `import vectrixdb` exceeds 300 ms

Two numbers matter. ``import vectrixdb`` should be nearly free: the package
resolves names lazily. ``from vectrixdb import Vectrix`` pays for numpy and the
collection machinery, and is the honest figure for "time to first call".
"""

from __future__ import annotations

import argparse
import subprocess
import sys


# ============================================================================
# SETTINGS: the cases timed
# ============================================================================
#
# The statements timed: import vectrixdb, which should be nearly free because
# the package resolves names lazily, and from vectrixdb import Vectrix, which
# pays for numpy and the collection machinery and is the honest figure for
# time to first call.

CASES = {
    "import vectrixdb": "import vectrixdb",
    "from vectrixdb import Vectrix": "from vectrixdb import Vectrix",
}


# ============================================================================
# MEASURING
# ============================================================================
#
# INPUT   a statement, and how many runs
# OUTPUT  the best-of-N wall time in milliseconds, each run in a fresh
#         interpreter
#
# A fresh process every time, so nothing already imported flatters the number.


def measure(statement: str, runs: int = 5) -> float:
    """Best-of-N wall time in milliseconds for one statement in a fresh process."""
    code = (
        "import time; t = time.perf_counter(); "
        f"{statement}; "
        "print((time.perf_counter() - t) * 1000)"
    )
    best = float("inf")
    for _ in range(runs):
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout
        best = min(best, float(out.strip().splitlines()[-1]))
    return best


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --check with a limit in milliseconds
# OUTPUT  the report; exit 1 when import vectrixdb exceeds the limit
#
# Two numbers, and a gate on the one that should stay small.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check", type=float, metavar="MS", help="fail if `import vectrixdb` is slower"
    )
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(argv)

    results = {label: measure(stmt, args.runs) for label, stmt in CASES.items()}
    width = max(len(k) for k in results)
    for label, ms in results.items():
        print(f"{label:<{width}}  {ms:7.1f} ms  (best of {args.runs})")

    if args.check is not None and results["import vectrixdb"] > args.check:
        print(f"import vectrixdb exceeded {args.check:.0f} ms", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
