"""Retake the dashboard's pictures in docs/images/dashboard from the dashboard as it is now.

    python scripts/dashboard_shots.py                   # all twelve
    python scripts/dashboard_shots.py search signin     # some of them
    python scripts/dashboard_shots.py --list            # their names, and what each shows
    python scripts/dashboard_shots.py tour-search       # one of the moving pictures, a GIF

Run it after any change to the pages, and the docs show what a person sees.

It fills in a sample server first, in a folder of its own: four small
collections and who may search each, one of them with a per-document policy,
fourteen days of sign-ins, searches and audit decisions written by the
library's own logs with the clock set back, and one evaluation run measured on
the handbook collection with twenty golden questions. Then it starts the
server in this process on a free port, signs three people in the way a person
does (the emailed link, then the code from an authenticator app), and drives
an installed Edge or Chrome, headless, over the DevTools protocol to each page,
saving each picture at one pixel a CSS pixel: 1280 wide, 390 by 844 for the
phone. The moving pictures (the tour-* names) are filmed the same way, a
person's steps with a pointer drawn on the page, and saved as GIFs 960 wide.

The sample is made up and says so in the docs; the evaluation's numbers were
measured on it and say nothing about retrieval anywhere else. Nothing leaves
the machine but the page's request for its fonts. The folder and the
browser's profile are deleted when it ends, whatever happened.

Needs the api and signin extras, websocket-client (pip install
websocket-client), Pillow for the GIFs, and Edge or Chrome; VECTRIXDB_BROWSER names the browser when
it is somewhere else.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "images" / "dashboard"

# ============================================================================
# SETTINGS: the pictures, the sample, and the people
# ============================================================================
#
# One entry a picture: where the page is, its size, the theme, what must be
# on the page before it is taken, and anything done to it first. The sample
# is a small company handbook and three people; the addresses are reserved
# example ones.

#: name: (address, width, height, theme, ready, then, what it shows)
SHOTS: Dict[str, Tuple[str, int, int, str, str, str, str]] = {
    "overview": (
        "#/overview",
        1280,
        960,
        "light",
        "#page-overview.active svg",
        "",
        "The Overview, with fourteen days of charts",
    ),
    "collections": (
        "#/collections",
        1280,
        820,
        "light",
        "#page-collections.active #col-grid > *",
        "",
        "Every collection and who may search it",
    ),
    "points": (
        "#/collections/handbook/points",
        1280,
        820,
        "light",
        "#page-collection.active #cpanel-points.on button",
        "show_text",
        "One collection's points, one chunk opened",
    ),
    "policy": (
        "#/collections/handbook/policy",
        1280,
        820,
        "light",
        "#page-collection.active #cpanel-policy.on > *",
        "",
        "A collection's Policy tab: who may search it",
    ),
    "search": (
        "#/search",
        1280,
        820,
        "light",
        "#page-search.active #s-collection option[value=handbook]",
        "search:How long does a refund take?",
        "Search results, each led by its relevance",
    ),
    "evaluate": (
        "#/evaluate/retrieval",
        1280,
        1000,
        "light",
        "#page-evaluate.active svg",
        "",
        "The evaluation run: the picks, and every setup ranked",
    ),
    "ingest": (
        "#/ingest",
        1280,
        820,
        "light",
        "#page-ingest.active #ing-collection option, #page-ingest.active select option",
        "",
        "Adding documents",
    ),
    "audit": (
        "#/audit",
        1280,
        960,
        "light",
        "#page-audit.active svg",
        "",
        "Audit: totals, decisions a day, the newest records",
    ),
    "access": (
        "#/access",
        1280,
        820,
        "light",
        "#page-access.active #access-body > *",
        "",
        "People, what each role may do, and the access log",
    ),
    "phone": (
        "#/overview",
        390,
        844,
        "light",
        "#page-overview.active svg",
        "",
        "The Overview on a phone",
    ),
    "dark": (
        "#/search",
        1280,
        820,
        "dark",
        "#page-search.active #s-collection option[value=handbook]",
        "search:travel expenses",
        "The dashboard in the dark theme",
    ),
    "signin": (
        "#/overview",
        1280,
        820,
        "light",
        "#page-overview.active",
        "sign_in",
        "The sign-in box, over the guests' Overview",
    ),
}

ADMIN, OPERATOR, VIEWER = "admin@example.test", "operator@example.test", "viewer@example.test"
PEOPLE = ((ADMIN, "admin"), (OPERATOR, "operator"), (VIEWER, "viewer"))

HANDBOOK = {
    "refunds": (
        "Refunds",
        "Refunds are handled by the billing team within ten working days, to the card that paid.",
    ),
    "travel": (
        "Travel expenses",
        "Economy fares for trips under six hours. Hotels up to the city rate in the travel table. Receipts within thirty days.",
    ),
    "emergency-fund": (
        "Emergency fund access",
        "Displaced customers get expedited access to their funds and waived withdrawal fees for thirty days.",
    ),
    "deferment": (
        "Payment deferment",
        "Customers in a declared disaster area can defer loan payments for up to ninety days with no late fees. Interest still accrues during the deferral.",
    ),
    "remote-work": (
        "Working from home",
        "Staff may work from home up to three days a week with their manager's agreement. Core hours are ten to three.",
    ),
    "security": (
        "Reporting a security incident",
        "Report a lost laptop or a suspicious email to the service desk within one hour, and do not forward the email.",
    ),
    "leave": (
        "Annual leave",
        "Full-time staff have twenty-five days of annual leave plus public holidays, and up to five days carry into the next year.",
    ),
    "expenses": (
        "Expense claims",
        "Claims go in the expenses app with a photo of each receipt, and are paid with the next salary.",
    ),
    "retention": (
        "Keeping records",
        "Customer records are kept for seven years after an account closes, and then deleted.",
    ),
}
HR_ARCHIVE = {
    "parental-leave": (
        "Parental leave",
        "Parents have twenty-six weeks of paid leave, which may be split into two blocks.",
    )
}
CALLS = {
    "call-0312": (
        "Call, 12 March",
        "The customer asked to move their renewal to April and to be sent the new terms by email.",
    ),
    "call-0319": (
        "Call, 19 March",
        "The customer confirmed the April renewal and asked for invoices to go to their finance team.",
    ),
}
CLIENT_NOTES = {
    "cl-1001": (
        "Client 1001, onboarding",
        "Onboarding call held; the client wants monthly statements and one named contact.",
        "CL-1001",
    ),
    "cl-1002": (
        "Client 1002, renewal",
        "The renewal is agreed in principle; pricing to be confirmed by the end of the quarter.",
        "CL-1002",
    ),
    "cl-1003": (
        "Client 1003, complaint",
        "A late payment was reported and resolved; a goodwill credit was applied.",
        "CL-1003",
    ),
}
GOLDEN = [
    ("When do I get my money back?", "refunds"),
    ("Who handles refunds?", "refunds"),
    ("Which fare do I book for a four hour flight?", "travel"),
    ("How much can I spend on a hotel?", "travel"),
    ("When are travel receipts due?", "travel"),
    ("Can I get to my savings quickly after a flood?", "emergency-fund"),
    ("Are withdrawal fees waived in an emergency?", "emergency-fund"),
    ("Can I pause loan payments after a hurricane?", "deferment"),
    ("Does interest stop during a deferral?", "deferment"),
    ("How many days a week can I work from home?", "remote-work"),
    ("What are the core hours?", "remote-work"),
    ("I lost my laptop, who do I tell?", "security"),
    ("What do I do with a phishing email?", "security"),
    ("How many days of holiday do I get?", "leave"),
    ("Can I carry leave into next year?", "leave"),
    ("How do I claim an expense?", "expenses"),
    ("When are expenses paid?", "expenses"),
    ("How long are customer records kept?", "retention"),
    ("When is a closed account's data deleted?", "retention"),
    ("Do I need a receipt for every expense?", "expenses"),
]


# ============================================================================
# THE SAMPLE: collections, who may search them, and fourteen days of history
# ============================================================================
#
# INPUT   an empty folder
# OUTPUT  four collections with their policies, one with a per-document
#         policy; the access log and the audit trail filled back fourteen
#         days; one evaluation run on the handbook
#
# Every line is written by the library's own code. Only the clock is set back
# while the history is written.


def _document(title: str, body: str) -> str:
    return f"# {title}\n\n{body}\n"


def fill(folder: Path, audit_key: bytes, rng: random.Random) -> None:
    from vectrixdb import Vectrix
    from vectrixdb.audit import DENY, AuditContext, JSONLSink
    from vectrixdb.collection_access import AccessPolicy
    from vectrixdb.collection_records import CollectionRecords
    from vectrixdb.evaluation import Target, evaluate
    from vectrixdb.policy import Overlap, Policy
    from vectrixdb.signin.access import AccessLog

    db_path = str(folder / "db")
    for name, docs in (("handbook", HANDBOOK), ("hr-archive", HR_ARCHIVE), ("calls", CALLS)):
        with Vectrix(name, path=db_path, mode="hybrid") as db:
            for doc_id, (title, body) in docs.items():
                db.add_document(_document(title, body), doc_id=doc_id)

    # The per-document policy: a note is for whoever covers its client.
    policy = Policy([Overlap("entitlements.client_id", "client_coverage", scope=True)])
    sink = JSONLSink(folder / "audit.jsonl", query_key=audit_key, on_failure=DENY)
    notes = Vectrix("client-notes", path=db_path, mode="hybrid", policy=policy, on_retrieval=sink)
    for doc_id, (title, body, client) in CLIENT_NOTES.items():
        notes.add_document(
            _document(title, body), doc_id=doc_id, metadata={"entitlements": {"client_id": client}}
        )

    now = datetime.now(timezone.utc)
    covering = [
        (OPERATOR, {"client_coverage": ["CL-1001", "CL-1002"]}),
        (ADMIN, {"client_coverage": ["CL-1003"]}),
        (VIEWER, {"client_coverage": []}),
    ]
    questions = [
        "renewal pricing",
        "monthly statements",
        "late payment credit",
        "named contact",
        "invoices",
    ]
    for day in range(13, -1, -1):
        for _ in range(rng.randint(6, 26)):
            at = now - timedelta(days=day, seconds=rng.randint(0, 20 * 3600))
            person, principal = rng.choice(covering)
            with mock.patch("vectrixdb.easy._utcnow", return_value=at):
                notes.as_principal(principal, audit=AuditContext(principal_id=person)).search(
                    rng.choice(questions), limit=3
                )
    notes.close()

    # Who may search each collection, in the record the server reads. A record
    # names the collection's generation, the time it was made, so a collection
    # made again under the same name does not inherit it.
    from vectrixdb import VectrixDB

    records = CollectionRecords.open(str(folder / "records.db"))
    core = VectrixDB(db_path)
    try:
        for name, rule in (
            (
                "handbook",
                {
                    "method": "store",
                    "allow": [{"email": ADMIN}, {"email": OPERATOR}, {"email": VIEWER}],
                },
            ),
            ("hr-archive", {"method": "store", "allow": [{"domain": "example.test"}]}),
            ("client-notes", {"method": "store", "allow": [{"email": ADMIN}, {"email": OPERATOR}]}),
        ):
            made = core.get_collection(name).info().created_at.isoformat()
            records.set_policy(name, AccessPolicy.from_dict(rule), by=ADMIN, generation=made)
        # calls keeps no policy, so the list shows an admin what is unavailable to anyone yet.
    finally:
        core.close()
        records.close()

    log = AccessLog(folder / "access.jsonl")
    who = [ADMIN, OPERATOR, OPERATOR, VIEWER, VIEWER, VIEWER]
    for day in range(13, -1, -1):
        busy = 1.9 if day in (3, 9) else 1.0
        start = (now - timedelta(days=day)).replace(hour=7, minute=0, second=0, microsecond=0)
        events: List[Tuple[float, Callable[[], Any]]] = []
        for person in set(who):
            if rng.random() < 0.8:
                events.append(
                    (
                        rng.uniform(0, 3600),
                        lambda p=person: log.record("signin", who=p, method="email"),
                    )
                )
        for _ in range(rng.randint(0, 3)):
            events.append(
                (
                    rng.uniform(0, 36000),
                    lambda: log.record(
                        "signin_failed", who=rng.choice(who), method="email", reason="wrong_code"
                    ),
                )
            )
        for _ in range(int(rng.randint(20, 70) * busy)):
            slow = 9.0 if day == 5 and rng.random() < 0.1 else 1.0
            took = rng.lognormvariate(3.4, 0.45) * slow
            person, collection = (
                rng.choice(who),
                rng.choice(["handbook", "handbook", "hr-archive", "calls"]),
            )
            events.append(
                (
                    rng.uniform(0, 36000),
                    lambda p=person, c=collection, t=took: log.record(
                        "search",
                        who=p,
                        method="email",
                        action="search",
                        collection=c,
                        status=200,
                        returned=10,
                        took_ms=t,
                    ),
                )
            )
        for _ in range(rng.randint(0, 8)):
            events.append(
                (
                    rng.uniform(0, 36000),
                    lambda: log.record(
                        "read",
                        who=rng.choice([ADMIN, OPERATOR]),
                        method="email",
                        action="content.read",
                        collection="handbook",
                        item="refunds:0",
                    ),
                )
            )
        for _ in range(rng.randint(0, 4)):
            events.append(
                (
                    rng.uniform(0, 36000),
                    lambda: log.record(
                        "denied",
                        who=VIEWER,
                        method="email",
                        action="search",
                        collection="client-notes",
                        status=403,
                        reason="not_on_policy",
                    ),
                )
            )
        for offset, write in sorted(events, key=lambda e: e[0]):
            with mock.patch(
                "vectrixdb.signin.access.time.time",
                return_value=(start + timedelta(seconds=offset)).timestamp(),
            ):
                write()

    golden = folder / "golden.jsonl"
    golden.write_text(
        "".join(
            json.dumps({"id": f"q{i + 1:02d}", "question": q, "expected": [doc]}) + "\n"
            for i, (q, doc) in enumerate(GOLDEN)
        ),
        encoding="utf-8",
    )
    target = Target(
        Vectrix("handbook", path=db_path, mode="ultimate", readonly=True), collection="handbook"
    )
    try:
        evaluate([target], str(golden), save_to=str(folder / "db" / "evaluations"))
    finally:
        target.db.close()


# ============================================================================
# THE SERVER, AND THREE PEOPLE SIGNED IN
# ============================================================================
#
# INPUT   the filled folder
# OUTPUT  the server on a free port in this process; the admin's cookies
#
# The people sign in over HTTP exactly as the page does: the emailed link,
# then a code from the authenticator secret the enrolment hands back.


class Server:
    def __init__(self, folder: Path, audit_key: bytes) -> None:
        import uvicorn

        from vectrixdb.api.server import create_app
        from vectrixdb.signin import SignInConfig

        self.mail: List[str] = []
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.home = os.getcwd()
        os.chdir(folder)
        os.environ["VECTRIXDB_AUDIT_JSONL"] = "audit.jsonl"
        os.environ["VECTRIXDB_AUDIT_QUERY_KEY"] = audit_key.decode()
        config = SignInConfig(
            methods=("email",),
            secrets=(base64.b64encode(os.urandom(36)).decode(),),
            public_url=self.base,
            users=PEOPLE,
            store_path=folder / "auth" / "signin.db",
            access_log=folder / "access.jsonl",
            sender=lambda to, subject, text: self.mail.append(text),
            guests=True,
        )
        self.app = create_app(
            db_path=str(folder / "db"),
            enable_dashboard=True,
            signin=config,
            collection_store=str(folder / "records.db"),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(300):
            if self.server.started:
                return
            time.sleep(0.1)
        raise RuntimeError("the server did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)
        if self.app.state.signin is not None:
            self.app.state.signin.close()
        records = getattr(self.app.state, "collection_store", None)
        if records is not None:
            records.close()
        os.chdir(self.home)

    def sign_in(self, email: str) -> http.cookiejar.CookieJar:
        from vectrixdb.signin import totp

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def post(path: str, body: dict) -> dict:
            request = urllib.request.Request(
                self.base + path,
                data=json.dumps(body).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with opener.open(request, timeout=60) as reply:
                return json.loads(reply.read().decode() or "{}")

        post("/auth/email/begin", {"email": email})
        token = re.search(r"#/enrol\?token=([\w-]+)", self.mail[-1]).group(1)
        begun = post("/auth/email/enrol/begin", {"token": token})["data"]
        post(
            "/auth/email/enrol/confirm",
            {"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())},
        )
        return jar

    def call(
        self,
        jar: http.cookiejar.CookieJar,
        method: str,
        path: str,
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> dict:
        """One request as the person the cookies belong to, with the CSRF token the page would send."""
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        sent = {"Content-Type": "application/json", **(headers or {})}
        token = next((c.value for c in jar if c.name in ("__Host-vx_csrf", "vx_csrf")), None)
        if token:
            sent["X-CSRF-Token"] = token
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers=sent,
        )
        with opener.open(request, timeout=600) as reply:
            return json.loads(reply.read().decode() or "{}")

    def prepare(self, admin: http.cookiejar.CookieJar) -> None:
        """Two keys for the Access page, one of them used; and every search mode run once, so the models are loaded."""
        print("  two keys for the Access page", file=sys.stderr)
        made = self.call(
            admin, "POST", "/api/v1/keys", {"name": "search-widget", "role": "searcher"}
        )["data"]
        self.call(admin, "POST", "/api/v1/keys", {"name": "nightly-ingest", "role": "operator"})
        key = made.get("key") or made.get("secret")
        if key:
            print("  one key used", file=sys.stderr)
            urllib.request.urlopen(
                urllib.request.Request(self.base + "/api/v1/collections", headers={"api-key": key}),
                timeout=120,
            ).read()
        # The modes the pictures show. Reranking is left out: the evaluation above has run it already.
        # With the excerpts the page asks for: cutting them has a first time of its own.
        for route in ("text-hybrid-search", "text-search", "keyword-search"):
            print(f"  warming up: {route}", file=sys.stderr)
            for query in ("How long does a refund take?", "travel expenses"):
                self.call(
                    admin,
                    "POST",
                    f"/api/v1/collections/handbook/{route}?snippet=200",
                    {"query_text": query, "limit": 10},
                )


# ============================================================================
# THE BROWSER: Edge or Chrome, headless, over the DevTools protocol
# ============================================================================
#
# INPUT   a browser on this machine; a page, its size, its theme and what to
#         wait for
# OUTPUT  a PNG a picture
#
# A throwaway profile under the system's temporary folder, a short path,
# because a Chromium browser writes nothing under a path near 260
# characters. It is deleted when the browser closes.

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
)


def delete(folder: Path) -> None:
    """Delete a folder this script made, trying for fifteen seconds; name it if it stays."""
    for _ in range(30):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        time.sleep(0.5)
    print(
        f"Could not delete {folder}: something still has a file in it open. Delete it by hand.",
        file=sys.stderr,
    )


def find_browser() -> str:
    given = os.environ.get("VECTRIXDB_BROWSER", "").strip()
    for candidate in ([given] if given else []) + list(BROWSERS):
        path = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if path:
            return path
    raise SystemExit("No Edge or Chrome found. Name one with VECTRIXDB_BROWSER.")


class Browser:
    def __init__(self) -> None:
        try:
            import websocket
        except ImportError:
            raise SystemExit(
                "This script drives the browser with websocket-client: pip install websocket-client"
            )
        self.profile = Path(tempfile.mkdtemp(prefix="vxshots-browser-"))
        # A group of its own, so every process it starts can be stopped with it: a Chromium browser
        # leaves its renderers and helpers running, holding the profile, when only its first process ends.
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        self.process = subprocess.Popen(
            [
                find_browser(),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                "--remote-allow-origins=*",
                "--remote-debugging-port=0",
                f"--user-data-dir={self.profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **group,
        )
        port_file = self.profile / "DevToolsActivePort"
        for _ in range(300):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.1)
        port = port_file.read_text().split()[0]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=10) as reply:
            self.browser_socket_url = json.loads(reply.read().decode())["webSocketDebuggerUrl"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as reply:
            page = next(t for t in json.loads(reply.read().decode()) if t["type"] == "page")
        self.socket = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120)
        self.next_id = 0
        for domain in ("Page", "Runtime", "Network"):
            self.send(f"{domain}.enable")
        self.send(
            "Page.addScriptToEvaluateOnNewDocument",
            source=(
                "(() => { let n = 0; const f = window.fetch.bind(window); window.__vxPending = () => n;"
                " window.fetch = (...a) => { n += 1; return f(...a).finally(() => { n -= 1; }); }; })();"
            ),
        )

    def send(self, method: str, **params: Any) -> dict:
        self.next_id += 1
        self.socket.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def js(self, expression: str) -> Any:
        result = self.send(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"{expression[:80]}: {result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, seconds: float = 60) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if self.js(f"!!({expression})"):
                return
            time.sleep(0.2)
        # Say what the page shows instead, since that is what a change to the page will have moved.
        shown = self.js(
            "(document.querySelector('.page.active') || document.body).innerText.slice(0, 600)"
        )
        raise RuntimeError(f"waited {seconds}s for {expression}\nThe page shows: {shown!r}")

    def size(self, width: int, height: int) -> None:
        self.send(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=width < 768,
        )

    def picture(self, path: Path) -> None:
        shot = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        path.write_bytes(base64.b64decode(shot["data"]))

    def close(self) -> None:
        """Close the browser and every process it started, then delete its profile."""
        import websocket

        try:
            self.socket.close()
            whole = websocket.create_connection(self.browser_socket_url, timeout=10)
            whole.send(json.dumps({"id": 1, "method": "Browser.close"}))
            whole.close()
            self.process.wait(timeout=15)
        except Exception:
            pass
        finally:
            self.stop_every_process()
            delete(self.profile)

    def stop_every_process(self) -> None:
        """Every process with this profile on its command line: the first one's children outlive it."""
        if os.name != "nt":
            import signal

            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return
        # Matched by the folder's own name, which is unique and has no path in it to be spelled two ways.
        name = self.profile.name
        for _ in range(10):
            listed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{name}*' }} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }",
                ],
                capture_output=True,
                text=True,
            )
            if not listed.stdout.strip():
                return
            time.sleep(1)


# ============================================================================
# THE PICTURES
# ============================================================================
#
# INPUT   the pictures asked for
# OUTPUT  each one in docs/images/dashboard, named as the docs name it
#
# The sign-in picture is taken last, with the cookies gone, since it is what
# somebody who has not signed in sees.


def cookie_selector(names: List[str]) -> str:
    return " || ".join(f"document.querySelector({json.dumps(n)})" for n in names)


def ready(expression: str) -> str:
    fonts = "document.fonts.status === 'loaded'"
    # Anything still loading that a person would see: a skeleton or a spinner with a box on screen.
    loading = "![...document.querySelectorAll('.skeleton, .spinner')].some((e) => e.getClientRects().length)"
    quiet = "(!window.__vxPending || window.__vxPending() === 0)"
    return f"({cookie_selector([s.strip() for s in expression.split(',')])}) && {fonts} && {loading} && {quiet}"


def act(browser: Browser, then: str) -> None:
    if then == "show_text":
        browser.js(
            "(() => { const b = [...document.querySelectorAll('#page-collection button')].find((x) => /Show text/.test(x.textContent)); b && b.click(); })()"
        )
        browser.wait_for(
            "[...document.querySelectorAll('#page-collection button')].some((x) => /Hide text/.test(x.textContent))"
        )
    elif then.startswith("search:"):
        query = then.split(":", 1)[1]
        browser.js(
            f"(() => {{ const s = document.querySelector('#s-collection'); s.value = 'handbook'; s.dispatchEvent(new Event('change', {{bubbles: true}})); const q = document.querySelector('#s-query'); q.value = {json.dumps(query)}; q.dispatchEvent(new Event('input', {{bubbles: true}})); }})()"
        )
        # Twice: the first search after the page opens waits on the page's own start-up work, and the
        # round trip it shows is that wait. The second is what somebody using the page sees.
        for _ in range(2):
            browser.js(
                "(() => { const b = [...document.querySelectorAll('#page-search button')].find((x) => x.textContent.trim() === 'Search'); b && b.click(); })()"
            )
            browser.wait_for(
                "document.querySelector('#s-results') && !/Searching/.test(document.querySelector('#s-results').textContent) && document.querySelector('#s-results').children.length > 0"
            )
            time.sleep(0.5)
    elif then == "sign_in":
        browser.js(
            "(() => { const b = [...document.querySelectorAll('button, a')].find((x) => x.textContent.trim() === 'Sign in'); b && b.click(); })()"
        )
        browser.wait_for(
            "document.querySelector('.gate .box') && document.querySelector('.gate input[type=email]')"
        )


def quiet_resets() -> None:
    """A browser leaving a page closes its connections at once, and asyncio on Windows logs each one."""
    import logging

    def keep(record: logging.LogRecord) -> bool:
        return not (record.exc_info and isinstance(record.exc_info[1], ConnectionResetError))

    logging.getLogger("asyncio").addFilter(keep)


def take(names: List[str]) -> int:
    """Run the work in a process of its own and delete its folder when that process has ended.

    An open file cannot be deleted on Windows, and a server in this process
    keeps some open (an index is mapped into memory) for as long as the
    process lives. A child's files are all closed when it exits.
    """
    folder = Path(tempfile.mkdtemp(prefix="vxshots-"))
    try:
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--inside", str(folder), *names]
        ).returncode
    finally:
        delete(folder)


def inside(folder: Path, names: List[str]) -> None:
    """The work itself: fill the sample in ``folder``, start the server, sign in, and take each picture."""
    quiet_resets()
    rng = random.Random(20260929)
    home = os.getcwd()
    audit_key = base64.b16encode(os.urandom(16)).lower()
    server = browser = None
    try:
        print(f"filling a sample server in {folder}", file=sys.stderr)
        fill(folder, audit_key, rng)
        server = Server(folder, audit_key)
        print("  three people sign in", file=sys.stderr)
        jars = {email: server.sign_in(email) for email, _ in PEOPLE}
        server.prepare(jars[ADMIN])

        def signed_in() -> Browser:
            fresh = Browser()
            for c in jars[ADMIN]:
                fresh.send(
                    "Network.setCookie",
                    name=c.name,
                    value=c.value,
                    url=server.base + "/",
                    path=c.path or "/",
                    httpOnly=bool(c.has_nonstandard_attr("HttpOnly")),
                )
            return fresh

        browser = signed_in()
        OUT.mkdir(parents=True, exist_ok=True)
        # The pictures first, then the clips in CLIPS' order (the one that adds a document last,
        # so nothing else shows it), then the sign-in picture, taken with the cookies gone.
        stills = [n for n in names if n in SHOTS and n != "signin"]
        clips = [n for n in CLIPS if n in names]
        for name in stills + clips + (["signin"] if "signin" in names else []):
            if name in CLIPS:
                # A browser of its own: after a few hundred frames a headless one stops painting.
                filming = signed_in()
                try:
                    record(filming, server.base, folder, name)
                finally:
                    filming.close()
                continue
            where, width, height, theme, wait, then, _ = SHOTS[name]
            if name == "signin":
                browser.send("Network.clearBrowserCookies")
            browser.size(width, height)
            open_page(browser, server.base, where, theme, wait)
            act(browser, then)
            time.sleep(0.8)  # the charts' last frame and the fonts' swap
            browser.picture(OUT / f"{name}.png")
            print(f"  {name}.png  {width} by {height}, {theme}", file=sys.stderr)
    finally:
        if browser is not None:
            try:
                browser.send("Page.navigate", url="about:blank")
            except Exception:
                pass
        if server is not None:
            server.stop()
        if browser is not None:
            browser.close()
        os.chdir(home)


# ============================================================================
# THE MOVING PICTURES
# ============================================================================
#
# INPUT   the clips asked for, on the same sample server as the pictures
# OUTPUT  each one in docs/images/dashboard as a GIF, GIF_WIDTH wide
#
# A clip is what a person does on a page, filmed: a pointer moves to what is
# pressed, a question is typed a few letters a frame, and the page's own
# answer is filmed until it has settled. Each frame is held for the time it
# really took, so the clip runs at the page's own speed, and a frame the
# same as the one before only lengthens that one.

#: name: (width, height, theme, what it shows). The steps are in CLIP_STEPS. They are filmed in
#: this order, the one that adds a document last: a server still reading it answers slowly.
CLIPS: Dict[str, Tuple[int, int, str, str]] = {
    "tour-search": (
        1280,
        800,
        "light",
        "A question typed, and its results, each led by its relevance and what found it",
    ),
    "tour-pages": (
        1280,
        800,
        "light",
        "A walk through Overview, Collections, Evaluate and Audit",
    ),
    "tour-ingest": (
        1280,
        800,
        "light",
        "A PDF added, read by the server, its pages and chunks counted",
    ),
}
#: How wide the GIFs are saved, the width the docs show them at.
GIF_WIDTH = 960
#: How many colours a GIF keeps. The pages are flat colour, so this is plenty.
GIF_COLOURS = 128
#: The PDF the ingest clip adds: two pages of a travel policy.
POLICY_PAGES = (
    (
        "Travel and expenses policy",
        "Book economy fares for trips under six hours.",
        "Hotels are paid up to the city rate in the travel table.",
        "Send receipts within thirty days of coming back.",
    ),
    (
        "Meals and taxis",
        "Meals are paid up to the daily allowance for the city.",
        "Take a taxi when no train or bus gets there in time.",
        "Claims go in the expenses app with a photo of each receipt.",
    ),
)

#: A pointer drawn on the page, since a headless browser shows none. It glides to what is pressed.
POINTER = """(() => {
  if (window.__vxPointTo) return;
  const make = () => {
    let p = document.getElementById('vx-pointer');
    if (p) return p;
    p = document.createElement('div');
    p.id = 'vx-pointer';
    p.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24"><path d="M5 2.5l14 9.2-6.3 1.3-3.6 6.5z" fill="#14181f" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(p.style, {position: 'fixed', left: (window.__vxAt || [640, 420])[0] + 'px', top: (window.__vxAt || [640, 420])[1] + 'px',
      zIndex: 2147483647, pointerEvents: 'none', transition: 'left .55s ease-in-out, top .55s ease-in-out, transform .12s', transformOrigin: '5px 3px'});
    document.body.appendChild(p);
    return p;
  };
  window.__vxPointTo = (selector) => {
    const e = typeof selector === 'string' ? document.querySelector(selector) : selector;
    if (!e) return false;
    e.scrollIntoView({block: 'nearest'});
    const b = e.getBoundingClientRect();
    const p = make();
    const x = Math.round(b.left + Math.min(b.width / 2, 60)), y = Math.round(b.top + b.height / 2);
    p.style.left = x + 'px'; p.style.top = y + 'px';
    return [x, y];
  };
  window.__vxPress = (down) => { make().style.transform = down ? 'scale(.82)' : 'scale(1)'; };
})()"""


def policy_pdf() -> bytes:
    """A small PDF with real text on two pages, written by hand so the clip needs no PDF library."""

    def escaped(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects: List[bytes] = []
    pages = []
    font = 3 + 2 * len(POLICY_PAGES)
    for n, (title, *lines) in enumerate(POLICY_PAGES):
        rows = [f"BT /F1 20 Tf 72 720 Td ({escaped(title)}) Tj ET"]
        rows += [
            f"BT /F1 12 Tf 72 {680 - 22 * i} Td ({escaped(line)}) Tj ET"
            for i, line in enumerate(lines)
        ]
        stream = "\n".join(rows).encode("latin-1")
        page, content = 3 + 2 * n, 4 + 2 * n
        pages.append(page)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    kids = " ".join(f"{p} 0 R" for p in pages)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        *objects,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    table = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        table,
    )
    return bytes(out)


class Reel:
    """The frames of one clip, each held for as long as it was on screen."""

    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self.frames: List[Tuple[bytes, int]] = []
        self.at: Tuple[int, int] = (640, 420)

    def frame(self, ms: int) -> None:
        shot = self.browser.send(
            "Page.captureScreenshot", format="png", captureBeyondViewport=False
        )
        picture = base64.b64decode(shot["data"])
        if self.frames and self.frames[-1][0] == picture:
            self.frames[-1] = (picture, self.frames[-1][1] + ms)
        else:
            self.frames.append((picture, ms))

    def hold(self, ms: int) -> None:
        self.frame(ms)

    def film(self, seconds: float, until: str = "") -> None:
        """Frames for ``seconds``, or until ``until`` is true and then ``seconds`` more."""
        if until:
            end = time.time() + 60
            while time.time() < end and not self.browser.js(f"!!({until})"):
                started = time.time()
                self.frame(0)
                self._took(started)
        end = time.time() + seconds
        while time.time() < end:
            started = time.time()
            self.frame(0)
            self._took(started)

    def _took(self, started: float) -> None:
        picture, ms = self.frames[-1]
        self.frames[-1] = (picture, ms + max(40, int((time.time() - started) * 1000)))

    def point(self, selector: str) -> None:
        """Glide the pointer to what ``selector`` names, filming the way there."""
        self.browser.js(f"window.__vxAt = {list(self.at)}")
        self.browser.js(POINTER)
        where = self.browser.js(f"window.__vxPointTo({json.dumps(selector)})")
        if not where:
            raise RuntimeError(f"nothing on the page is {selector}")
        self.at = (int(where[0]), int(where[1]))
        self.film(0.7)

    def press(self, selector: str) -> None:
        """Point at ``selector`` and press it."""
        self.point(selector)
        self.browser.js("window.__vxPress(true)")
        self.frame(120)
        self.browser.js(f"document.querySelector({json.dumps(selector)}).click()")
        self.browser.js("window.__vxPress(false)")

    def scroll(self, top: int, smooth: bool = True) -> None:
        """Scroll what holds the page to ``top``, filming it when ``smooth``."""
        # The page may scroll inside a box of its own rather than the window: the nearest that does.
        self.browser.js(
            "(() => { let e = document.querySelector('.page.active');"
            " while (e && e !== document.body) { const s = getComputedStyle(e).overflowY;"
            " if ((s === 'auto' || s === 'scroll') && e.scrollHeight > e.clientHeight) break; e = e.parentElement; }"
            " (e && e !== document.body ? e : document.scrollingElement)"
            f".scrollTo({{top: {top}, behavior: '{'smooth' if smooth else 'instant'}'}}); }})()"
        )
        if smooth:
            self.film(0.9)

    def type(self, selector: str, text: str) -> None:
        """Type ``text`` into ``selector`` a few letters a frame, as fast as a person types."""
        self.point(selector)
        for end in range(2, len(text) + 2, 2):
            self.browser.js(
                f"(() => {{ const e = document.querySelector({json.dumps(selector)}); e.value = {json.dumps(text[:end])};"
                " e.dispatchEvent(new Event('input', {bubbles: true})); })()"
            )
            self.frame(110)

    def save(self, path: Path) -> Tuple[int, int]:
        """Write the clip as a GIF, GIF_WIDTH wide, one palette for every frame: ``(frames, bytes)``."""
        try:
            from PIL import Image
        except ImportError:
            raise SystemExit("The clips are written with Pillow: pip install pillow")
        import io

        frames = []
        for picture, _ in self.frames:
            image = Image.open(io.BytesIO(picture)).convert("RGB")
            height = round(image.height * GIF_WIDTH / image.width)
            frames.append(image.resize((GIF_WIDTH, height), Image.Resampling.LANCZOS))
        # One palette from the first, middle and last frames, so a colour cannot flicker between frames.
        sample = [frames[0], frames[len(frames) // 2], frames[-1]]
        strip = Image.new("RGB", (GIF_WIDTH, sum(f.height for f in sample)))
        top = 0
        for f in sample:
            strip.paste(f, (0, top))
            top += f.height
        palette = strip.quantize(colors=GIF_COLOURS, method=Image.Quantize.MEDIANCUT)
        coloured = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
        coloured[0].save(
            path,
            save_all=True,
            append_images=coloured[1:],
            duration=[max(20, ms) for _, ms in self.frames],
            loop=0,
            optimize=True,
            disposal=1,
        )
        return len(coloured), path.stat().st_size


def open_page(browser: Browser, base: str, where: str, theme: str, wait: str) -> None:
    """A fresh load of the dashboard at ``where`` in ``theme``, waited on until ``wait`` is there and it is quiet."""
    # The theme is kept in the page's own storage, so it is set on the dashboard's origin
    # first. Then a blank page between, so the address below is a fresh load and never a
    # change of only the part after the #, which a single-page app may handle in its own time.
    browser.send("Page.navigate", url=f"{base}/dashboard/")
    browser.wait_for(
        "document.readyState === 'complete' && location.pathname.endsWith('/dashboard/')"
    )
    browser.js(f"localStorage.setItem('vectrixdb.theme', {json.dumps(theme)})")
    browser.send("Page.navigate", url="about:blank")
    browser.wait_for("location.href === 'about:blank'")
    browser.send("Page.navigate", url=f"{base}/dashboard/{where}")
    browser.wait_for(f"location.hash === {json.dumps(where)} && document.readyState === 'complete'")
    browser.wait_for(ready(wait))
    time.sleep(0.6)
    browser.wait_for(ready(wait))


def clip_search(reel: Reel, base: str, theme: str, folder: Path) -> None:
    browser = reel.browser
    open_page(
        browser, base, "#/search", theme, "#page-search.active #s-collection option[value=handbook]"
    )
    pick = "(() => { const s = document.querySelector('#s-collection'); s.value = 'handbook'; s.dispatchEvent(new Event('change', {bubbles: true})); })()"
    browser.js(pick)
    # One search off camera: the first after the page opens waits on its start-up work.
    act(browser, "search:refund")
    open_page(
        browser, base, "#/search", theme, "#page-search.active #s-collection option[value=handbook]"
    )
    browser.js(pick)
    reel.hold(900)
    reel.type("#s-query", "How long does a refund take?")
    reel.hold(300)
    search = "[...document.querySelectorAll('#page-search button')].find((x) => x.textContent.trim() === 'Search')"
    browser.js(f"{search}.id = 'vx-search-button'")
    reel.press("#vx-search-button")
    reel.film(
        1.8,
        until="document.querySelector('#s-results') && !/Searching/.test(document.querySelector('#s-results').textContent) && document.querySelector('#s-results').children.length > 0",
    )
    reel.hold(1400)
    # Down the results, each led by its relevance and what found it, and back up.
    reel.scroll(420)
    reel.hold(1600)
    reel.scroll(0)
    reel.hold(1800)


def clip_ingest(reel: Reel, base: str, theme: str, folder: Path) -> None:
    browser = reel.browser
    pdf = folder / "travel-policy.pdf"
    pdf.write_bytes(policy_pdf())
    open_page(
        browser,
        base,
        "#/ingest",
        theme,
        "#page-ingest.active #ing-collection option, #page-ingest.active select option",
    )
    browser.js(
        "(() => { const t = document.querySelector('#ing-target'); t.value = 'server'; t.dispatchEvent(new Event('change', {bubbles: true}));"
        " const c = document.querySelector('#ing-collection'); c.value = 'handbook'; c.dispatchEvent(new Event('change', {bubbles: true})); })()"
    )
    reel.hold(900)
    reel.point("#drop label")
    root = browser.send("DOM.getDocument")["root"]["nodeId"]
    node = browser.send("DOM.querySelector", nodeId=root, selector="#file-input")["nodeId"]
    browser.send("DOM.setFileInputFiles", nodeId=node, files=[str(pdf)])
    # Choosing a file scrolls the page to the hidden input; a person's page stays where it was.
    reel.scroll(0, smooth=False)
    reel.film(1.2, until="document.querySelector('#file-list').children.length > 0")
    browser.js(
        "[...document.querySelectorAll('#page-ingest button')].find((x) => x.textContent.trim() === 'Ingest').id = 'vx-ingest-button'"
    )
    reel.press("#vx-ingest-button")
    # Back up to This run, where the server says what it read: pages, chunks and their citations.
    reel.scroll(0)
    reel.film(2.0, until="document.querySelector('#ing-log .notice')")
    reel.point("#ing-log .notice")
    reel.hold(3500)


def clip_pages(reel: Reel, base: str, theme: str, folder: Path) -> None:
    browser = reel.browser
    open_page(browser, base, "#/overview", theme, "#page-overview.active svg")
    reel.film(2.2)
    for page, wait in (
        ("collections", "#page-collections.active #col-grid > *"),
        ("evaluate", "#page-evaluate.active svg"),
        ("audit", "#page-audit.active svg"),
    ):
        reel.press(f"nav a[data-page={page}]")
        if page == "evaluate":
            # The retrieval run, with its three picks, rather than the chunking tab it opens on.
            reel.film(0.6, until=ready("#page-evaluate.active #etab-retrieval"))
            reel.press("#etab-retrieval")
        reel.film(2.4, until=ready(wait))


#: What each clip does, by name.
CLIP_STEPS: Dict[str, Callable[[Reel, str, str, Path], None]] = {
    "tour-search": clip_search,
    "tour-ingest": clip_ingest,
    "tour-pages": clip_pages,
}


def record(browser: Browser, base: str, folder: Path, name: str) -> None:
    width, height, theme, _ = CLIPS[name]
    browser.size(width, height)
    reel = Reel(browser)
    CLIP_STEPS[name](reel, base, theme, folder)
    frames, size = reel.save(OUT / f"{name}.gif")
    seconds = sum(ms for _, ms in reel.frames) / 1000
    print(
        f"  {name}.gif  {GIF_WIDTH} wide, {frames} frames, {seconds:.1f} s, {size / 1e6:.2f} MB",
        file=sys.stderr,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "names", nargs="*", help="the pictures to take; all of them when none is named"
    )
    parser.add_argument("--list", action="store_true", help="name every picture and what it shows")
    parser.add_argument("--inside", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.list:
        for name, shot in SHOTS.items():
            print(f"{name:12} {shot[1]} by {shot[2]}  {shot[6]}")
        for name, clip in CLIPS.items():
            print(f"{name:12} {clip[0]} by {clip[1]}  {clip[3]}, a GIF {GIF_WIDTH} wide")
        return 0
    known = [*SHOTS, *CLIPS]
    unknown = [n for n in args.names if n not in known]
    if unknown:
        parser.error(f"no picture called {', '.join(unknown)}. They are: {', '.join(known)}")
    if args.inside:
        inside(Path(args.inside), args.names or known)
        return 0
    return take(args.names or known)


if __name__ == "__main__":
    raise SystemExit(main())
