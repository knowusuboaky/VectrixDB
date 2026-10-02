#!/usr/bin/env python
"""
Run the dashboard's dev server.

Usage:
    python run_dev.py                         # Vite on 5173, forwarding to the Backend on 8000
    python run_dev.py --port 3000             # another port
    python run_dev.py --upstream http://127.0.0.1:8100
    python run_dev.py --open                  # open a browser once it is up

The pages call /api and /auth on their own origin, and Vite forwards those to
the upstream, so the sign-in cookie behaves as it will in production. The
upstream is normally the Backend beside this folder, which holds the key and
forwards to the retrieval service.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent
DEFAULT_PORT = 5173
DEFAULT_UPSTREAM = "http://127.0.0.1:8000"


def npm() -> str:
    """The npm on this machine, by name on Windows too."""
    found = shutil.which("npm") or shutil.which("npm.cmd")
    if not found:
        sys.exit("npm is not on PATH. Install Node 22.14 or newer, which brings it.")
    return found


def installed(tool: str) -> None:
    """node_modules, once. npm decides whether anything has to be fetched."""
    if not (HERE / "node_modules" / "vite").exists():
        print("installing (first run only)")
        subprocess.run([tool, "install"], cwd=HERE, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--upstream",
        default=os.environ.get("VITE_DEV_UPSTREAM", DEFAULT_UPSTREAM),
        help="where /api and /auth are forwarded",
    )
    parser.add_argument("--open", action="store_true", help="open a browser")
    args = parser.parse_args()

    tool = npm()
    installed(tool)
    where = f"http://127.0.0.1:{args.port}/"
    print(
        f"dashboard  {where}\nforwarding /api, /auth, /health and /openapi.json to {args.upstream}\n"
    )
    if args.open:
        webbrowser.open(where)
    finished = subprocess.run(
        [tool, "run", "dev", "--", "--port", str(args.port), "--strictPort"],
        cwd=HERE,
        env={**os.environ, "VITE_DEV_UPSTREAM": args.upstream},
    )
    return finished.returncode


if __name__ == "__main__":
    raise SystemExit(main())
