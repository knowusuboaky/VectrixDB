#!/usr/bin/env python
"""
Build the dashboard for the Backend to serve.

Usage:
    python run_prod.py              # build into dist/
    python run_prod.py --clean      # remove dist/ first

Nothing is served here. The build lands in dist/, and the Backend serves that
folder at / and forwards /api and /auth to the retrieval service, so the pages
and the API are one origin in production as they are in development.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DIST = HERE / "dist"


def npm() -> str:
    found = shutil.which("npm") or shutil.which("npm.cmd")
    if not found:
        sys.exit("npm is not on PATH. Install Node 22.14 or newer, which brings it.")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--clean", action="store_true", help="remove dist/ before building")
    args = parser.parse_args()

    tool = npm()
    if args.clean and DIST.exists():
        shutil.rmtree(DIST)
    if not (HERE / "node_modules" / "vite").exists():
        subprocess.run([tool, "install"], cwd=HERE, check=True)
    built = subprocess.run([tool, "run", "build"], cwd=HERE)
    if built.returncode:
        return built.returncode

    files = sorted(p for p in DIST.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in files)
    print(f"\n{len(files)} files, {size / 1024:.0f} KB in {DIST}")
    print("serve it:  cd ../Backend && python run_servers.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
