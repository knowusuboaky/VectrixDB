"""Run one SDK's tests against a real VectrixDB server, started here and stopped after.

    python sdk/conformance/serve.py -- node --test sdk/typescript/test
    python sdk/conformance/serve.py -- go test ./...            (from sdk/go)
    python sdk/conformance/serve.py -- cargo test               (from sdk/rust)

A fresh server on a free port, in a temporary folder, with a key made for
this run and documents kept, so every SDK is asked the same questions of
the same server. The command runs with VECTRIXDB_URL and VECTRIXDB_KEY set,
and VECTRIXDB_READ_ONLY_KEY for the refusal a read-only key gets; its exit
code is this script's. Nothing is left behind.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, seconds: float = 90.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/ready", timeout=2) as reply:  # noqa: S310 - our own server
                if reply.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def main(argv: list) -> int:
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        print("usage: serve.py -- <command to run against the server>", file=sys.stderr)
        return 2
    command = argv[argv.index("--") + 1 :]
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    key, read_only = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    # On Windows a stopped server can hold its files for a moment; what is left is the system's to clear.
    with tempfile.TemporaryDirectory(
        prefix="vectrixdb-conformance-", ignore_cleanup_errors=True
    ) as data:
        env = {
            **os.environ,
            "VECTRIXDB_API_KEY": key,
            "VECTRIXDB_READ_ONLY_API_KEY": read_only,
            "VECTRIXDB_KEEP_SOURCE": "1",
            "VECTRIXDB_OFFLINE": "1",
            "VECTRIXDB_DASHBOARD": "0",
            "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        server = subprocess.Popen(
            [sys.executable, "-m", "vectrixdb.cli", "serve", "--port", str(port), "--path", data],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            if not _wait_ready(url):
                server.terminate()
                print(server.stderr.read().decode(errors="replace")[-4000:], file=sys.stderr)
                print("the server did not become ready", file=sys.stderr)
                return 1
            run_env = {
                **os.environ,
                "VECTRIXDB_URL": url,
                "VECTRIXDB_KEY": key,
                "VECTRIXDB_READ_ONLY_KEY": read_only,
            }
            return subprocess.call(command, env=run_env, shell=sys.platform == "win32")
        finally:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
