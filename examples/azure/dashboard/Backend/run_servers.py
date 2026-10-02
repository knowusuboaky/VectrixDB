#!/usr/bin/env python
"""
Run the dashboard's Backend.

Usage:
    python run_servers.py                     # 8000, serving the build and forwarding
    python run_servers.py --port 8100
    python run_servers.py --reload            # reload on a code change
    python run_servers.py --upstream http://127.0.0.1:8200

It serves Frontend/dist at / and forwards /api, /auth, /health, /docs,
/openapi.json and the live socket at /ws to the retrieval service. With no build yet, / says how to make
one; with no UPSTREAM, every forwarded call says that instead of failing
strangely.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


# ============================================================================
# SETTINGS: where this file is
# ============================================================================
#
# The folder this file is in, so the app beside it imports.

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --port, --reload, --upstream
# OUTPUT  the Backend serving Frontend/dist at / and forwarding /api, /auth,
#         /health, /docs, /openapi.json and /ws to the retrieval service
#
# With no build yet, / says how to make one; with no UPSTREAM, every forwarded
# call says that instead of failing strangely.


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--host", default="127.0.0.1", help="loopback by default: it holds a signed-in session"
    )
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--upstream", default=None, help="the retrieval service's address, overriding .env"
    )
    args = parser.parse_args()

    if args.upstream:
        os.environ["UPSTREAM"] = args.upstream.rstrip("/")

    try:
        import uvicorn
    except ImportError:
        sys.exit("uvicorn is not installed. Run: pip install -r requirements.txt")

    from app.core.settings import Settings

    settings = Settings.from_env()
    print(f"dashboard    http://{args.host}:{args.port}/")
    print(
        f"forwarding   {', '.join(settings.forwarded)}, /ws -> {settings.upstream or 'nothing: UPSTREAM is not set'}"
    )
    print(
        f"pages        {settings.site}{'' if settings.built else '  (not built yet: cd ../Frontend && python run_prod.py)'}\n"
    )

    uvicorn.run(
        "app.main:app", host=args.host, port=args.port, reload=args.reload, app_dir=str(HERE)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
