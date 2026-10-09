"""Start a VectrixDB server for an SDK's conformance test and say where it is.

    python sdk/conformance/serve.py            # prints {"url": ..., "key": ...}, runs until killed
    python sdk/conformance/serve.py --seconds 120

The server keeps each document's Markdown (so open_document works), takes one
admin key, and writes under a temporary folder that is removed at exit.
Needs the ``api`` extra.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time

__all__ = ["main"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seconds", type=float, default=0, help="stop after this long (0: until killed)"
    )
    parser.add_argument("--key", default=None, help="the admin key (default: a random one)")
    parser.add_argument("--port", type=int, default=0, help="the port (default: a free one)")
    args = parser.parse_args(argv)

    key = args.key or secrets.token_urlsafe(24)
    port = args.port or _free_port()
    root = tempfile.mkdtemp(prefix="vectrixdb-conformance-")
    os.environ["VECTRIXDB_API_KEY"] = key
    os.environ["VECTRIXDB_KEEP_SOURCE"] = "1"
    os.environ.setdefault("VECTRIXDB_AUTO_DOWNLOAD", "0")

    import uvicorn

    from vectrixdb.api.server import create_app

    app = create_app(db_path=root, enable_dashboard=False)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            print("the server did not start", file=sys.stderr)
            return 1
        time.sleep(0.05)
    print(json.dumps({"url": f"http://127.0.0.1:{port}", "key": key}), flush=True)
    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            while thread.is_alive():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
