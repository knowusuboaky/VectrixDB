"""Fail when test coverage drops below the recorded baseline.

    python scripts/coverage_ratchet.py            # run the suite with coverage, compare
    python scripts/coverage_ratchet.py --update   # record the current percentage
    python scripts/coverage_ratchet.py --from coverage.json   # compare an existing report

The baseline lives in .coverage-baseline as a percentage with one decimal.
Going up prints a reminder to lock it in; going down by more than the
tolerance fails.

Measure it with the `test` extra installed, ``pip install -e ".[test]"`` in a
fresh environment, because that is what CI's coverage job installs. What is
installed decides which tests run and so moves the number: an environment
without those extras reads about 2.7 points lower, since a test whose extra is
missing skips rather than fails. A test that fails also fails this, since a
percentage from a broken run means nothing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# ============================================================================
# SETTINGS: the baseline and the tolerance
# ============================================================================
#
# .coverage-baseline holds a percentage with one decimal; a drop within the
# tolerance passes, a bigger one fails.

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".coverage-baseline"
TOLERANCE = 0.5


# ============================================================================
# MEASURING, READING AND DECIDING
# ============================================================================
#
# INPUT   where the coverage report goes; then the report; then the current
#         number against the baseline
# OUTPUT  the percentage measured, read back, and a verdict: up, level, or
#         down beyond the tolerance
#
# Measure it with the test extra installed, as CI's coverage job does: what is
# installed decides which tests run and so moves the number, and an
# environment without those extras reads about 2.7 points lower. A test that
# fails also fails this, since a percentage from a broken run means nothing.


def measure(report: Path) -> float:
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "not slow",
            "--cov=vectrixdb",
            f"--cov-report=json:{report}",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    # The run's own summary, so the job log says what the percentage came from.
    summary = [line for line in run.stdout.splitlines() if " passed" in line or " failed" in line or " error" in line]
    if summary:
        print(summary[-1])
    if run.returncode != 0:
        print(run.stdout[-4000:])
        raise SystemExit(f"the test run failed (exit {run.returncode}), so its coverage is not a number to compare")
    return read(report)


def read(report: Path) -> float:
    data = json.loads(report.read_text(encoding="utf-8"))
    return round(float(data["totals"]["percent_covered"]), 1)


def decide(current: float, baseline: float) -> tuple[int, str]:
    if current < baseline - TOLERANCE:
        return (
            1,
            f"coverage {current:.1f}% is below the baseline {baseline:.1f}%. Add tests for what you touched.",
        )
    if current > baseline + TOLERANCE:
        return 0, f"coverage {current:.1f}%, up from {baseline:.1f}%: run --update to lock it in."
    return 0, f"coverage {current:.1f}%, baseline {baseline:.1f}%."


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --update to record the current percentage, or --from an existing
#         report
# OUTPUT  exit 1 on a drop; a reminder to lock in a rise
#
# Going up prints a reminder to raise the baseline; going down by more than
# the tolerance fails.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--from", dest="existing", type=Path, help="an existing coverage.json")
    args = parser.parse_args(argv)

    report = args.existing or (ROOT / "coverage.json")
    current = read(report) if args.existing else measure(report)

    if args.update:
        BASELINE.write_text(f"{current:.1f}\n", encoding="utf-8")
        print(f"coverage baseline set to {current:.1f}%")
        return 0
    if not BASELINE.exists():
        print(f"No {BASELINE.name}; run with --update. Current: {current:.1f}%")
        return 1
    status, message = decide(current, float(BASELINE.read_text().strip()))
    print(message)
    return status


if __name__ == "__main__":
    sys.exit(main())
