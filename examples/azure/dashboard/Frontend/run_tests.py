#!/usr/bin/env python
"""
Run the dashboard's page tests.

Usage:
    python run_tests.py             # every Node test under ../tests

Node's own test runner, no test framework. The unit tests read the copied pages
and hold them to two things: no demo data anywhere, and every file we did not
edit still byte for byte the library's. The functional ones read the build.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
TESTS = HERE.parent / "tests"


def main() -> int:
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        sys.exit("node is not on PATH. Install Node 22.14 or newer.")
    if not TESTS.exists():
        sys.exit(f"{TESTS} is not there.")
    # The files, named. A folder as the argument is read as one file on
    # Windows and fails with MODULE_NOT_FOUND, which reads like a broken test.
    files = sorted(str(p) for p in TESTS.rglob("*.test.mjs"))
    if not files:
        sys.exit(f"no *.test.mjs under {TESTS}")
    return subprocess.run([node, "--test", *files], cwd=HERE).returncode


if __name__ == "__main__":
    raise SystemExit(main())
