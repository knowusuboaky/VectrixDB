"""Run tests with an old behaviour restored; succeed only if they fail.

    python scripts/replay.py tests/replays/old_notin.py tests/unit/test_hierarchy_persistence.py

The replay plugin monkeypatches the package back to the bug. If every selected
test still passes, the tests are not load-bearing and this exits 1. If pytest
could not run them at all (a wrong path, a plugin that fails to import, no
tests selected) that is not a replay, and this exits 2 with pytest's output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


# ============================================================================
# SETTINGS: where the repository is, and the exit code
# ============================================================================
#
# The checkout the tests run in, and the exit code pytest gives when tests
# fail, which here is the outcome wanted.

ROOT = Path(__file__).resolve().parent.parent


#: pytest's exit status when it ran the tests and some failed. Anything else
#: other than 0 means it never got that far.
TESTS_FAILED = 1


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   a replay plugin and the tests to run
# OUTPUT  exit 0 when the tests fail with the old behaviour restored; 1 when
#         they still pass; 2 when pytest could not run them at all
#
# The replay plugin monkeypatches the package back to the bug. Tests that
# still pass with the bug back are not load-bearing. A wrong path, a plugin
# that fails to import or no tests selected is not a replay, and exits 2 with
# pytest's output.


def main(argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    plugin = Path(argv[0]).resolve()
    targets = argv[1:]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            plugin.stem,
            *targets,
        ],
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": str(plugin.parent)},
        capture_output=True,
        text=True,
    )
    summary = [line for line in proc.stdout.splitlines() if "passed" in line or "failed" in line]
    print("\n".join(summary) or proc.stdout[-2000:])
    # Only a test failure proves the tests catch the bug. A collection error,
    # a plugin that did not import or a path that selected nothing also exits
    # non-zero, and was read as a replay that passed.
    if proc.returncode not in (0, TESTS_FAILED):
        print(proc.stderr[-2000:])
        print(f"replay {plugin.name}: pytest could not run the tests (exit {proc.returncode})")
        return 2
    if proc.returncode == TESTS_FAILED:
        print(f"replay {plugin.name}: the tests fail against the old behaviour, as they should")
        return 0
    print(f"replay {plugin.name}: every test still passed; they do not catch the regression")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
