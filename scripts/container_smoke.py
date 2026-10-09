"""Run the two container images the way a careful deployment would, and check them.

    python scripts/container_smoke.py --server vectrixdb:test
    python scripts/container_smoke.py --server vectrixdb:test --extract vectrixdb-extract:test --whisper
    python scripts/container_smoke.py --server vectrixdb:test --extract vectrixdb-extract:test --compose
    python scripts/container_smoke.py --server vectrixdb:full --full

Every container here starts read-only, with every Linux capability dropped,
no privilege escalation, and /tmp as the only scratch space, which is what a
hardened Kubernetes pod or Container Apps revision gives it. Each check asks
what a caller or an operator would: does it refuse to start open, does it
answer its health check, can a collection be made, filled and searched, does
the data outlive the container, does a stop let requests finish, does a
platform that picks its own user id still work, does a feed on the intranet
keep a collection current while the cloud's metadata address, a host nobody
named and a page robots.txt disallows stay out of reach. With --full, a server image
built with MODELS=all must embed in French with no network at all. With
--compose, the two run together from docker/compose.yaml and Jaeger beside
them, as the containers page has a reader do: a scan sent to the server is
read by the extraction service and found by a search, the scan reaches Jaeger
as one trace across both containers, and no span holds a file name, query
text or a key. One line a check is printed, and
the exit code is 1 when one fails, so CI can gate a release on it.

It needs Docker and the standard library, and talks to nothing but the
containers it starts, which it removes, volumes included, whatever happens.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# The flags a hardened platform starts a container with.
HARDENED = [
    "--read-only",
    "--tmpfs",
    "/tmp:rw,size=256m",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    # The image's own check, run often enough that a test need not wait 30s.
    "--health-interval",
    "2s",
]

# Calls to the containers never go through a proxy the environment names.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ============================================================================
# DOCKER AND HTTP
# ============================================================================
#
# INPUT   docker arguments; a method, an address, a body and headers
# OUTPUT  what docker printed and its exit code; the status, headers and body
#         of the reply
#
# Thin on purpose: the checks read as the commands an operator would type.


def docker(
    *args: str, timeout: float = 120.0, env: Optional[Dict[str, str]] = None
) -> Tuple[int, str]:
    """Run docker with these arguments and answer its exit code and output."""
    done = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False, env=env
    )
    return done.returncode, (done.stdout + done.stderr).strip()


def call(
    method: str,
    url: str,
    body: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
) -> Tuple[int, Dict[str, str], bytes]:
    """One HTTP request, answering the status even when it is an error."""
    request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as reply:
            return reply.status, dict(reply.headers.items()), reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


def call_json(method: str, url: str, payload: Any, key: str) -> Tuple[int, Any]:
    status, _, body = call(
        method,
        url,
        json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json", "Accept": "application/json", "api-key": key},
    )
    try:
        return status, json.loads(body.decode("utf-8") or "null")
    except ValueError:
        return status, body.decode("utf-8", "replace")


# ============================================================================
# A CONTAINER, STARTED AND WATCHED
# ============================================================================
#
# INPUT   an image, its port, environment and extra flags
# OUTPUT  the container's name and the address its port is published on;
#         whether it became healthy, by the image's own health check
#
# Published on 127.0.0.1 and a port Docker picks, so two runs never collide.


class Container:
    def __init__(
        self,
        image: str,
        port: int,
        env: Dict[str, str],
        volume: Optional[str] = None,
        user: Optional[str] = None,
        extra: Tuple[str, ...] = (),
    ) -> None:
        self.image, self.port, self.env = image, port, env
        self.volume, self.user, self.extra = volume, user, extra
        self.name = f"vectrixdb-smoke-{uuid.uuid4().hex[:10]}"
        self.base = ""

    def start(self) -> None:
        args = ["run", "-d", "--name", self.name, *HARDENED, "-p", f"127.0.0.1::{self.port}"]
        for key, value in self.env.items():
            args += ["-e", f"{key}={value}"]
        if self.volume:
            args += ["-v", f"{self.volume}:/data"]
        if self.user:
            args += ["--user", self.user]
        args += list(self.extra)
        code, out = docker(*args, self.image)
        if code != 0:
            raise RuntimeError(f"docker run failed: {out}")
        code, out = docker("port", self.name, str(self.port))
        if code != 0 or not out:
            raise RuntimeError(f"no published port: {out}")
        self.base = "http://" + out.splitlines()[0].strip()

    def wait_healthy(self, seconds: float = 120.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            code, out = docker(
                "inspect", "--format", "{{.State.Status}} {{.State.Health.Status}}", self.name
            )
            if code == 0:
                state, _, health = out.partition(" ")
                if health == "healthy":
                    return True
                if state in ("exited", "dead"):
                    return False
            time.sleep(1)
        return False

    def logs(self) -> str:
        return docker("logs", self.name)[1]

    def exec(self, *command: str, timeout: float = 120.0) -> Tuple[int, str]:
        return docker("exec", self.name, *command, timeout=timeout)

    def stop(self, grace: int = 20) -> Tuple[float, int, bool]:
        """Stop it as a platform would: how long it took, its exit code, and whether it shut down cleanly.

        uvicorn finishes the requests in flight, runs the app's shutdown,
        then raises the signal again, so a clean stop exits 143, the code for
        "ended by SIGTERM", and says so in its log.
        """
        began = time.monotonic()
        docker("stop", "-t", str(grace), self.name, timeout=grace + 30)
        took = time.monotonic() - began
        code, out = docker("inspect", "--format", "{{.State.ExitCode}}", self.name)
        exit_code = int(out) if code == 0 and out.strip().lstrip("-").isdigit() else -1
        return took, exit_code, "Application shutdown complete" in self.logs()

    def remove(self) -> None:
        docker("rm", "-f", "--volumes", self.name)


# ============================================================================
# THE CHECKS
# ============================================================================
#
# INPUT   an image and what the run was told to check
# OUTPUT  one line a check, ok or FAIL with what was seen
#
# A check that cannot run because an earlier one failed is reported failed,
# never skipped silently.


class Report:
    def __init__(self) -> None:
        self.failed: List[str] = []

    def check(self, name: str, passed: bool, seen: str = "") -> bool:
        print(
            f"  {'ok  ' if passed else 'FAIL'}  {name}"
            + (f"  ({seen})" if seen and not passed else "")
        )
        if not passed:
            self.failed.append(name)
        return passed


def refuses_open(report: Report, image: str, what: str) -> None:
    """With no key and no sign-in it must not start: every collection would be open."""
    name = f"vectrixdb-smoke-{uuid.uuid4().hex[:10]}"
    code, _ = docker("run", "--name", name, *HARDENED, image, timeout=180)
    logs = docker("logs", name)[1]
    docker("rm", "-f", "--volumes", name)
    report.check(
        f"{what} refuses to start with no key and no sign-in",
        code == 2 and "VECTRIXDB_API_KEY" in logs,
        f"exit {code}: {logs[-300:]}",
    )


def server_checks(report: Report, image: str) -> None:
    print(f"server image {image}")
    refuses_open(report, image, "server")
    key = secrets.token_urlsafe(24)
    volume = f"vectrixdb-smoke-{uuid.uuid4().hex[:10]}"
    docker("volume", "create", volume)
    first = Container(image, 7337, {"VECTRIXDB_API_KEY": key}, volume=volume)
    second = Container(image, 7337, {"VECTRIXDB_API_KEY": key}, volume=volume)
    other_user = Container(image, 7337, {"VECTRIXDB_API_KEY": key}, user="4242:0")
    # As docker/kubernetes runs it: a user and group of its own, a volume the
    # cluster handed to that group (fsGroup), the key as a mounted secret, and
    # the variables Kubernetes sets in a pod for a Service named vectrixdb.
    pod_data, pod_secret = (f"vectrixdb-smoke-{uuid.uuid4().hex[:10]}" for _ in range(2))
    service_links = {
        "VECTRIXDB_SERVICE_HOST": "10.96.0.12",
        "VECTRIXDB_SERVICE_PORT": "7337",
        "VECTRIXDB_PORT": "tcp://10.96.0.12:7337",
        "VECTRIXDB_PORT_7337_TCP": "tcp://10.96.0.12:7337",
        "VECTRIXDB_PORT_7337_TCP_ADDR": "10.96.0.12",
        "VECTRIXDB_PORT_7337_TCP_PORT": "7337",
        "VECTRIXDB_PORT_7337_TCP_PROTO": "tcp",
    }
    pod = Container(
        image,
        7337,
        {"VECTRIXDB_API_KEY_FILE": "/var/run/secrets/vectrixdb/api-key", **service_links},
        volume=pod_data,
        user="10001:10001",
        extra=("-v", f"{pod_secret}:/var/run/secrets/vectrixdb:ro"),
    )
    try:
        first.start()
        if not report.check(
            "server becomes healthy, read-only, no capabilities",
            first.wait_healthy(),
            first.logs()[-500:],
        ):
            return
        report.check("runs as user 10001, not root", first.exec("id", "-u")[1] == "10001")
        status, _, _ = call("GET", first.base + "/api/v1/collections")
        report.check(
            "a read with no key is refused (the image closes open reads)",
            status == 401,
            f"status {status}",
        )
        status, _ = call_json(
            "POST",
            first.base + "/api/v1/collections",
            {"name": "nokey", "dimension": 384},
            "wrong-key",
        )
        report.check("a write with a wrong key is refused", status == 401, f"status {status}")
        status, _ = call_json(
            "POST", first.base + "/api/v1/collections", {"name": "smoke", "dimension": 384}, key
        )
        report.check("a collection is made", status in (200, 201), f"status {status}")
        texts = [
            ("password", "To reset your password, open Settings and choose Security."),
            ("refund", "Refunds are paid back to the card within five working days."),
            ("shipping", "Orders ship from the Montreal warehouse every weekday."),
        ]
        status, answer = call_json(
            "POST",
            first.base + "/api/v1/collections/smoke/text-upsert",
            {"points": [{"id": i, "text": t, "payload": {"topic": i}} for i, t in texts]},
            key,
        )
        report.check(
            "texts are embedded and stored by the bundled model",
            status == 200,
            f"status {status}: {answer}",
        )

        def top(container: Container) -> str:
            status, found = call_json(
                "POST",
                container.base + "/api/v1/collections/smoke/text-search",
                {"query_text": "how do I change my password", "limit": 3},
                key,
            )
            # The reply is {"ok": ..., "data": {"results": [...]}}.
            data = found.get("data", found) if isinstance(found, dict) else {}
            results = data.get("results", []) if isinstance(data, dict) else []
            return (
                str(results[0].get("id"))
                if status == 200 and results
                else f"status {status}: {found}"
            )

        hit = top(first)
        report.check("a search finds the right text first", hit == "password", hit)
        status, headers, page = call("GET", first.base + "/dashboard/")
        report.check(
            "the dashboard is served",
            status == 200
            and "text/html" in headers.get("content-type", headers.get("Content-Type", "")),
            f"status {status}",
        )
        status, info = call_json("GET", first.base + "/api/v1/info", None, key)
        tracing = info.get("tracing") if isinstance(info, dict) else None
        report.check(
            "info says tracing is off until asked",
            status == 200 and tracing == {"on": False, "to": None},
            f"{status}: {tracing}",
        )
        took, code, clean = first.stop()
        report.check(
            "a stop signal lets it shut down cleanly and quickly",
            clean and code in (0, 143) and took < 15,
            f"exit {code} after {took:.1f}s, clean shutdown logged: {clean}",
        )

        second.start()
        if report.check(
            "a new container on the same volume becomes healthy",
            second.wait_healthy(),
            second.logs()[-500:],
        ):
            hit = top(second)
            report.check("the data outlived the first container", hit == "password", hit)

        other_user.start()
        healthy = other_user.wait_healthy()
        made = healthy and call_json(
            "POST",
            other_user.base + "/api/v1/collections",
            {"name": "anyuser", "dimension": 384},
            key,
        )[0] in (200, 201)
        report.check(
            "a platform-chosen user id in group 0 can write /data",
            bool(made),
            other_user.logs()[-500:],
        )

        code, out = docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "sh",
            "-e",
            f"KEY={key}",
            "-v",
            f"{pod_data}:/data",
            "-v",
            f"{pod_secret}:/secret",
            image,
            "-c",
            'chown 10001:10001 /data && chmod 2770 /data && printf %s "$KEY" > /secret/api-key'
            " && chown 10001:10001 /secret/api-key && chmod 0440 /secret/api-key",
        )
        pod.start()
        healthy = code == 0 and pod.wait_healthy()
        made = healthy and call_json(
            "POST", pod.base + "/api/v1/collections", {"name": "inapod", "dimension": 384}, key
        )[0] in (200, 201)
        report.check(
            "as the Kubernetes setup runs it: own group, mounted key, Service variables",
            bool(made),
            (out + "\n" + pod.logs())[-500:],
        )
    finally:
        for container in (first, second, other_user, pod):
            container.remove()
        for name in (volume, pod_data, pod_secret):
            docker("volume", "rm", "-f", name)


# Run in a container of its own beside the server: an intranet site with an
# RSS feed of two entries, which answers 304 to the ETag it gave, and a
# robots.txt that keeps crawlers out of /private/.
_FEED_SITE = r'''
import http.server
FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>City notices</title><link>http://feeds:8000/</link>
<description>Notices from the city</description>
<item><title>Library hours change</title><link>http://feeds:8000/notices/library</link>
<guid>notice-library</guid><pubDate>Mon, 05 Oct 2026 09:00:00 GMT</pubDate>
<description>From Monday the Rosemont library opens at 8 in the morning and closes at 9 at night.</description></item>
<item><title>Pool closed for repairs</title><link>http://feeds:8000/notices/pool</link>
<guid>notice-pool</guid><pubDate>Tue, 06 Oct 2026 09:00:00 GMT</pubDate>
<description>The Verdun swimming pool is closed until the end of the month while its filters are replaced.</description></item>
</channel></rss>"""
PAGES = {"/feed.xml": (FEED, "application/rss+xml"),
         "/robots.txt": (b"User-agent: *\nDisallow: /private/\n", "text/plain"),
         "/private/salaries.html": (b"<html><body><p>Not for crawlers.</p></body></html>", "text/html")}
class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body, kind = PAGES.get(self.path, (None, None))
        if body is None:
            self.send_response(404); self.end_headers(); return
        if self.path == "/feed.xml" and self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/feed.xml":
            self.send_header("ETag", '"v1"')
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", 8000), Site).serve_forever()
'''


def sources_checks(report: Report, image: str) -> None:
    """A collection kept up with a feed on the intranet, and the addresses the server must never fetch."""
    print(f"server image {image}: sources")
    key = secrets.token_urlsafe(24)
    network = f"vectrixdb-smoke-{uuid.uuid4().hex[:10]}"
    site = f"vectrixdb-smoke-{uuid.uuid4().hex[:10]}"
    # feeds is named an intranet host, so it may be private; elsewhere is the
    # same site under a name that is not. The metadata address is named too,
    # and must stay refused all the same.
    server = Container(
        image,
        7337,
        {
            "VECTRIXDB_API_KEY": key,
            "VECTRIXDB_SOURCES_INTERNAL_HOSTS": "feeds,169.254.169.254",
        },
        extra=("--network", network),
    )
    docker("network", "create", network)
    try:
        code, out = docker(
            "run", "-d", "--name", site, "--network", network,
            "--network-alias", "feeds", "--network-alias", "elsewhere",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--no-healthcheck", "--entrypoint", "python", image, "-c", _FEED_SITE,
        )  # fmt: skip
        server.start()
        up = code == 0 and server.wait_healthy()
        if not report.check("the server starts beside an intranet site", up, out[-300:]):
            return
        call_json(
            "POST", server.base + "/api/v1/collections", {"name": "news", "dimension": 384}, key
        )
        sources = server.base + "/api/v1/collections/news/sources"

        def refused(address: str, kind: Optional[str] = None) -> Tuple[bool, str]:
            status, reply = call_json(
                "POST", sources, {"address": address, **({"kind": kind} if kind else {})}, key
            )
            detail = str(reply.get("detail", reply) if isinstance(reply, dict) else reply)
            return 400 <= status < 500, f"{status}: {detail}"

        status, added = call_json(
            "POST", sources, {"address": "http://feeds:8000/feed.xml", "every": "6h"}, key
        )
        kind = (added.get("source") or {}).get("kind") if isinstance(added, dict) else None
        report.check(
            "a feed on a host named as internal is added, and told a feed",
            status == 200 and kind == "feed",
            f"{status}: {added}",
        )
        status, first = call_json("POST", sources + "/refresh", {"force": True}, key)
        report.check(
            "a refresh writes the feed's two entries",
            status == 200 and isinstance(first, dict) and first.get("added") == 2,
            f"{status}: {first}",
        )
        status, found = call_json(
            "POST",
            server.base + "/api/v1/collections/news/text-search",
            {"query_text": "what time does the library open", "limit": 1},
            key,
        )
        data = found.get("data", {}) if isinstance(found, dict) else {}
        results = data.get("results", []) if isinstance(data, dict) else []
        text = str(results[0].get("text", "")) if results else f"status {status}: {found}"
        report.check("a search finds the entry that answers", "8 in the morning" in text, text)
        status, again = call_json("POST", sources + "/refresh", {"force": True}, key)
        report.check(
            "a second refresh writes nothing, the feed unchanged",
            status == 200
            and isinstance(again, dict)
            and again.get("ok") is True
            and again.get("added") == 0
            and again.get("updated") == 0,
            f"{status}: {again}",
        )
        no, seen = refused("http://feeds:8000/private/salaries.html")
        report.check("a page robots.txt disallows is refused", no and "robots.txt" in seen, seen)
        no, seen = refused("http://elsewhere:8000/feed.xml")
        report.check(
            "a private host not named as internal is refused", no and "private" in seen, seen
        )
        no, seen = refused("http://169.254.169.254/latest/meta-data/", kind="page")
        report.check(
            "the cloud metadata address is refused, named as internal or not",
            no and "link-local" in seen,
            seen,
        )
        no, seen = refused("http://feeds:8000/feed.xml?key=${VECTRIXDB_API_KEY}", kind="feed")
        report.check(
            "an address that reads a setting from the environment is refused over the API",
            no and key not in seen,
            seen.replace(key, "***"),
        )
    finally:
        server.remove()
        docker("rm", "-f", site)
        docker("network", "rm", network)


def _tiny_pdf(text: str) -> bytes:
    """A one-page PDF with one line of text, written by hand, so the check needs no library."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n".encode()
    )
    return bytes(out)


# Run inside the extraction container: draws a line of text and reads it back
# through the service's own picture route, so the check needs nothing on the host.
_OCR_PROBE = """
import io, json, os, urllib.request as u
from PIL import Image, ImageDraw, ImageFont
image = Image.new("RGB", (900, 160), "white")
font = ImageFont.load_default(size=56)
ImageDraw.Draw(image).text((30, 40), "Invoice total 4200", fill="black", font=font)
data = io.BytesIO(); image.save(data, format="PNG")
request = u.Request("http://127.0.0.1:%s/transcribe/image" % os.environ["VECTRIXDB_EXTRACT_LISTEN_PORT"],
                    data=data.getvalue(), method="POST",
                    headers={"api-key": os.environ["VECTRIXDB_API_KEY"], "Accept": "application/json",
                             "Content-Type": "image/png", "X-Filename": "invoice.png"})
print(json.loads(u.build_opener(u.ProxyHandler({})).open(request, timeout=120).read())["text"])
"""


# What --build-arg MODELS=all adds, used with no network at all: an air-gapped
# cluster is who asks for that image. A French sentence must land nearer its
# English translation than an unrelated English one.
_MULTILINGUAL = """
from vectrixdb.models.embedded import DenseEmbedder, is_models_installed
missing = [kind for kind in ("dense", "reranker") if not is_models_installed(kind, exact=True)]
assert not missing, "missing: " + ", ".join(missing)
french, english, other = DenseEmbedder(model="multilingual").embed([
    "Le chiffre d'affaires a augmenté dans chaque région.",
    "Revenue grew in every region.",
    "The cat sat on the mat.",
])
near, far = float(french @ english), float(french @ other)
assert near > far, f"translation {near:.3f}, unrelated {far:.3f}"
print("multilingual ok")
"""


def full_checks(report: Report, image: str) -> None:
    print(f"full server image {image}")
    code, out = docker(
        "run",
        "--rm",
        *HARDENED,
        "--network",
        "none",
        "--entrypoint",
        "python",
        image,
        "-c",
        _MULTILINGUAL,
        timeout=240,
    )
    report.check(
        "the multilingual models are inside, and embed with no network",
        code == 0 and "multilingual ok" in out,
        out[-300:],
    )


def extract_checks(report: Report, image: str, whisper: bool) -> None:
    print(f"extraction image {image}")
    refuses_open(report, image, "extraction service")
    key = secrets.token_urlsafe(24)
    service = Container(image, 7338, {"VECTRIXDB_API_KEY": key})
    try:
        service.start()
        if not report.check(
            "extraction service becomes healthy, read-only",
            service.wait_healthy(),
            service.logs()[-500:],
        ):
            return
        report.check("runs as user 10001, not root", service.exec("id", "-u")[1] == "10001")
        status, _, _ = call("POST", service.base + "/extract/txt", b"hello")
        report.check("a call with no key is refused", status == 401, f"status {status}")
        status, _, body = call(
            "POST",
            service.base + "/extract/txt",
            b"Plain words in, plain words out.",
            {"api-key": key, "Content-Type": "text/plain", "Accept": "application/json"},
        )
        report.check(
            "a text file is read", status == 200 and b"Plain words in" in body, f"status {status}"
        )
        status, _, body = call(
            "POST",
            service.base + "/extract/pdf",
            _tiny_pdf("Quarterly revenue grew in every region."),
            {"api-key": key, "Content-Type": "application/pdf", "Accept": "application/json"},
        )
        report.check(
            "a PDF is read",
            status == 200 and b"Quarterly revenue grew" in body,
            f"status {status}: {body[:200]!r}",
        )
        code, out = service.exec("python", "-c", _OCR_PROBE, timeout=240)
        report.check(
            "a picture is read on the machine by RapidOCR", code == 0 and "4200" in out, out[-300:]
        )
        code, out = service.exec(
            "python",
            "-c",
            "import imageio_ffmpeg, subprocess; subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], check=True, capture_output=True); print('ffmpeg runs')",
        )
        report.check(
            "ffmpeg runs, for the sound of a video", code == 0 and "ffmpeg runs" in out, out[-300:]
        )
        if whisper:
            code, out = service.exec(
                "python",
                "-c",
                "from faster_whisper.utils import download_model; print(download_model('base'))",
            )
            report.check(
                "the Whisper model is inside the image, read with no network",
                code == 0 and "/opt/vectrixdb/" in out,
                out[-300:],
            )
        took, code, clean = service.stop()
        report.check(
            "a stop signal lets it shut down cleanly and quickly",
            clean and code in (0, 143) and took < 15,
            f"exit {code} after {took:.1f}s, clean shutdown logged: {clean}",
        )
    finally:
        service.remove()


# ============================================================================
# THE COMPOSE SETUP
# ============================================================================
#
# INPUT   the two images, docker/compose.yaml and docker/compose.tracing.yaml
# OUTPUT  one line a check: the two start together and only the server is
#         published; a scan sent to the server is read by the extraction
#         service and found by a search; both reach Jaeger, the scan as one
#         trace across the two containers, every span VectrixDB's own and none
#         holding a file name, query text or the key
#
# What a reader of the containers page does, with the images named and the
# ports picked free, so a run never collides with a setup already running.

COMPOSE_DIR = Path(__file__).resolve().parent.parent / "docker"

# Drawn inside the extraction container, which has Pillow, and printed as
# base64: the machine running this needs nothing but Python.
_SCAN = """
import base64, io
from PIL import Image, ImageDraw, ImageFont
image = Image.new("RGB", (1000, 160), "white")
ImageDraw.Draw(image).text((30, 40), "Invoice total 4200", fill="black", font=ImageFont.load_default(size=56))
data = io.BytesIO(); image.save(data, format="PNG")
print(base64.b64encode(data.getvalue()).decode())
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _strings(found: Any) -> List[str]:
    """Every string in a JSON reply, at any depth: a trace's span names and attribute values."""
    if isinstance(found, str):
        return [found]
    if isinstance(found, dict):
        return [one for value in found.values() for one in _strings(value)]
    if isinstance(found, list):
        return [one for value in found for one in _strings(value)]
    return []


def _spans(found: Any) -> List[Dict[str, Any]]:
    """The spans in a reply from Jaeger's API v3, which is OTLP JSON."""
    result = found.get("result") if isinstance(found, dict) else None
    return [
        one
        for resource in (result or {}).get("resourceSpans") or []
        for scope in resource.get("scopeSpans") or []
        for one in scope.get("spans") or []
    ]


def compose_checks(report: Report, server: str, extract: str) -> None:
    print("compose setup docker/compose.yaml with docker/compose.tracing.yaml")
    key = secrets.token_urlsafe(24)
    port, jaeger_port = _free_port(), _free_port()
    with tempfile.TemporaryDirectory() as scratch:
        key_file = Path(scratch) / "vectrixdb.key"
        key_file.write_text(key + "\n", encoding="utf-8")
        # The containers' user reads it through a bind mount, as the page says.
        key_file.chmod(0o644)
        env = {
            **os.environ,
            "VECTRIXDB_IMAGE": server,
            "VECTRIXDB_EXTRACT_IMAGE": extract,
            "VECTRIXDB_KEY_FILE": str(key_file),
            "VECTRIXDB_HOST_PORT": str(port),
            "JAEGER_HOST_PORT": str(jaeger_port),
        }
        compose = [
            "compose",
            "--project-name",
            f"vectrixdb-smoke-{uuid.uuid4().hex[:8]}",
            "--project-directory",
            str(COMPOSE_DIR),
            "-f",
            str(COMPOSE_DIR / "compose.yaml"),
            "-f",
            str(COMPOSE_DIR / "compose.tracing.yaml"),
        ]
        base = f"http://127.0.0.1:{port}"
        jaeger = f"http://127.0.0.1:{jaeger_port}"
        try:
            # The two images are the ones named; only Jaeger may be fetched.
            code, out = docker(
                *compose,
                "up",
                "--detach",
                "--pull",
                "missing",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "240",
                timeout=300,
                env=env,
            )
            if not report.check(
                "the server, the extraction service and Jaeger start together, healthy",
                code == 0,
                out[-500:] + "\n" + docker(*compose, "logs", "--tail", "20", env=env)[1][-1500:],
            ):
                return
            code, out = docker(*compose, "port", "extract", "7338", env=env)
            report.check(
                "the extraction service is not published on the machine",
                code != 0 or not out.strip() or out.strip().endswith(":0"),
                out,
            )
            status, _ = call_json(
                "POST", base + "/api/v1/collections", {"name": "scans", "dimension": 384}, key
            )
            report.check("a collection is made", status in (200, 201), f"status {status}")
            code, drawn = docker(*compose, "exec", "-T", "extract", "python", "-c", _SCAN, env=env)
            status, _, body = call(
                "POST",
                base + "/api/v1/collections/scans/documents",
                base64.b64decode(drawn.strip().splitlines()[-1]) if code == 0 else b"",
                {
                    "api-key": key,
                    "Content-Type": "image/png",
                    "X-Filename": "invoice.png",
                    "Accept": "application/json",
                },
                timeout=240,
            )
            report.check(
                "a scan sent to the server is read by the extraction service",
                status == 200,
                f"status {status}: {body[:300]!r}",
            )
            status, found = call_json(
                "POST",
                base + "/api/v1/collections/scans/text-search",
                {"query_text": "invoice total", "limit": 3},
                key,
            )
            data = found.get("data", found) if isinstance(found, dict) else {}
            results = data.get("results", []) if isinstance(data, dict) else []
            text = json.dumps(results[:1])
            report.check(
                "and a search finds what it says",
                status == 200 and "4200" in text,
                f"status {status}: {text[:300]}",
            )
            status, info = call_json("GET", base + "/api/v1/info", None, key)
            tracing = info.get("tracing") if isinstance(info, dict) else None
            report.check(
                "info says tracing is on, to Jaeger",
                status == 200 and tracing == {"on": True, "to": "jaeger"},
                f"{status}: {tracing}",
            )
            # Jaeger's API v3: every span of the last ten minutes, from the
            # server and from the extraction service, as OTLP JSON.
            since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
            until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
            wanted = {"vectrixdb.add_document", "vectrixdb.extract", "vectrixdb.search"}
            spans: Dict[str, Dict[str, Any]] = {}
            answers: List[str] = []
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                answers = []
                for service in ("vectrixdb", "vectrixdb-extract"):
                    status, _, body = call(
                        "GET",
                        f"{jaeger}/api/v3/traces?query.service_name={service}"
                        f"&query.start_time_min={since}&query.start_time_max={until}",
                    )
                    answers.append(f"{service}: {status} {body[:120]!r}")
                    if status == 200:
                        for one in _spans(json.loads(body.decode("utf-8") or "null")):
                            spans[str(one.get("spanId"))] = one
                if wanted <= {str(one.get("name")) for one in spans.values()}:
                    break
                time.sleep(2)
            names = sorted({str(one.get("name")) for one in spans.values()})
            report.check(
                "the scan and the search reach Jaeger as traces",
                wanted <= set(names),
                f"{names}; " + "; ".join(answers),
            )
            report.check(
                "every span is VectrixDB's own, none a request's with its path and query",
                bool(names) and all(name.startswith("vectrixdb.") for name in names),
                ", ".join(names)[:300],
            )
            added = [one for one in spans.values() if one.get("name") == "vectrixdb.add_document"]
            read = [one for one in spans.values() if one.get("name") == "vectrixdb.extract"]
            report.check(
                "the scan is one trace, from the server into the extraction service",
                any(
                    r.get("traceId") == a.get("traceId")
                    and r.get("parentSpanId") == a.get("spanId")
                    for a in added
                    for r in read
                ),
                json.dumps(
                    [
                        {k: s.get(k) for k in ("name", "traceId", "spanId", "parentSpanId")}
                        for s in added + read
                    ]
                )[:300],
            )
            words = _strings(list(spans.values()))
            report.check(
                "and no span holds a file name, query text or the key",
                bool(words) and not any("invoice" in w.lower() or key in w for w in words),
                ", ".join(sorted(set(words)))[:300],
            )
        finally:
            docker(*compose, "down", "--volumes", "--remove-orphans", timeout=180, env=env)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", help="The server image to check")
    parser.add_argument("--extract", help="The extraction image to check")
    parser.add_argument(
        "--full", action="store_true", help="The server image was built with MODELS=all"
    )
    parser.add_argument(
        "--whisper", action="store_true", help="The extraction image was built with a Whisper model"
    )
    parser.add_argument(
        "--compose",
        action="store_true",
        help="Also run both images together from docker/compose.yaml, with Jaeger",
    )
    args = parser.parse_args(argv)
    if not args.server and not args.extract:
        parser.error("name an image: --server, --extract or both")
    if args.compose and not (args.server and args.extract):
        parser.error("--compose runs both images: name --server and --extract")
    if args.full and not args.server:
        parser.error("--full is about the server image: name it with --server")
    report = Report()
    runs: List[Callable[[], None]] = []
    if args.server:
        runs.append(lambda: server_checks(report, args.server))
        runs.append(lambda: sources_checks(report, args.server))
    if args.full:
        runs.append(lambda: full_checks(report, args.server))
    if args.extract:
        runs.append(lambda: extract_checks(report, args.extract, args.whisper))
    if args.compose:
        runs.append(lambda: compose_checks(report, args.server, args.extract))
    for run in runs:
        run()
    if report.failed:
        print(f"\n{len(report.failed)} failed: " + "; ".join(report.failed))
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
