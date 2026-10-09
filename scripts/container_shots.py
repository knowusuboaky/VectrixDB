"""Film the containers page's two clips from the images as they are now.

    python scripts/container_shots.py
    python scripts/container_shots.py --server vectrixdb:test --extract vectrixdb-extract:test

compose.gif is a terminal: the steps the containers page gives, run for real
in a folder of their own with the Compose files from docker/, and what each
answered. Compose starts the server, the extraction service and Jaeger, a scan
goes to the server, and a search finds its words. trace.gif is Jaeger with that
scan's trace, from the server into the extraction service, and a search's span.
Both are saved in docs/images/containers, 960 wide.

--server and --extract film other images than the published ones. They are
tagged with the published names for the run, so the commands on screen are the
page's own, and the names are given back to what they named before, if
anything. Everything it starts is removed, volumes included, whatever happens.

Needs Docker, curl, Pillow, websocket-client, and Edge or Chrome;
VECTRIXDB_BROWSER names the browser when it is somewhere else.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import container_smoke as smoke  # noqa: E402
import dashboard_shots as shots  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images" / "containers"
#: The clips, each saved as <name>.gif in OUT, and what each shows.
CLIPS = {
    "compose": "Compose starting the three, a scan read and a search that finds it",
    "trace": "The scan's trace in Jaeger, server into extraction service, and a search's",
}
COMPOSE_FILES = ("compose.yaml", "compose.tracing.yaml")
#: How the terminal clip is framed: the browser's size, which the GIF is saved at.
WIDTH, HEIGHT = 960, 600

# ============================================================================
# THE TERMINAL
# ============================================================================
#
# A page drawn like a terminal window, with a prompt to type at and a place for
# what a command printed. Typing is filmed a few letters a frame.

TERMINAL = """<!doctype html><meta charset="utf-8"><title>terminal</title>
<style>
  html, body { margin: 0; height: 100%; background: #e8ecf2; }
  body { display: flex; align-items: center; justify-content: center;
    font: 14.5px/1.5 "Cascadia Mono", Consolas, "DejaVu Sans Mono", "Liberation Mono", monospace; }
  .window { width: 912px; height: 552px; background: #0f141c; border-radius: 10px; overflow: hidden;
    box-shadow: 0 14px 38px rgba(15, 20, 28, .32); display: flex; flex-direction: column; }
  .bar { flex: none; height: 34px; background: #1b222d; display: flex; align-items: center;
    gap: 8px; padding: 0 14px; color: #8b95a7; font-size: 13px; }
  .dot { width: 12px; height: 12px; border-radius: 50%; }
  .r { background: #ff5f57; } .y { background: #febc2e; } .g { background: #28c840; }
  .title { margin-left: 10px; }
  #screen { flex: 1; margin: 0; padding: 14px 18px; color: #d7dde8; white-space: pre-wrap;
    overflow-wrap: anywhere; overflow: hidden; font: inherit; }
  .prompt { color: #7ee2b8; } .cmd { color: #ffffff; } .out { color: #aab4c5; }
  .ok { color: #3fb950; } .k { color: #79c0ff; } .s { color: #e3b26f; } .n { color: #d2a8ff; }
  .cursor { display: inline-block; width: .6em; height: 1.15em; background: #d7dde8;
    vertical-align: text-bottom; }
</style>
<div class="window">
  <div class="bar"><span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
    <span class="title">TITLE</span></div>
  <pre id="screen"></pre>
</div>
<script>
  const screen = document.getElementById('screen');
  const cursor = document.createElement('span');
  cursor.className = 'cursor';
  let line = null;
  const keep = () => { screen.appendChild(cursor); screen.scrollTop = screen.scrollHeight; };
  window.__prompt = (sign) => {
    const p = document.createElement('span');
    p.className = 'prompt'; p.textContent = sign; screen.appendChild(p);
    line = document.createElement('span'); line.className = 'cmd'; screen.appendChild(line);
    keep();
  };
  window.__type = (text) => { line.textContent += text; keep(); };
  window.__enter = () => { screen.appendChild(document.createTextNode('\\n')); keep(); };
  window.__out = (markup) => {
    const o = document.createElement('span');
    o.className = 'out'; o.innerHTML = markup + '\\n'; screen.appendChild(o); keep();
  };
  window.__last = (markup) => { screen.querySelectorAll('.out')[screen.querySelectorAll('.out').length - 1].innerHTML = markup + '\\n'; keep(); };
</script>"""


def coloured(reply: str) -> str:
    """JSON as jq prints it, its keys, strings and numbers coloured as a terminal does."""
    out = []
    token = re.compile(
        r'("(?:[^"\\]|\\.)*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)|\b(true|false|null)\b'
    )
    at = 0
    for match in token.finditer(reply):
        out.append(html.escape(reply[at : match.start()]))
        text, colon, number, word = match.groups()
        if text is not None:
            kind = "k" if colon else "s"
            out.append(f'<span class="{kind}">{html.escape(text)}</span>{html.escape(colon or "")}')
        else:
            out.append(f'<span class="n">{html.escape(number or word)}</span>')
        at = match.end()
    out.append(html.escape(reply[at:]))
    return "".join(out)


class Terminal:
    """A terminal on the page, filmed by ``reel``: typed commands and what they printed."""

    def __init__(self, reel: shots.Reel, folder: Path, title: str, height: int = HEIGHT) -> None:
        self.reel = reel
        page = folder / "terminal.html"
        drawn = TERMINAL.replace("TITLE", html.escape(title))
        drawn = drawn.replace("height: 552px;", f"height: {height - 48}px;")
        page.write_text(drawn, encoding="utf-8")
        browser = reel.browser
        browser.size(WIDTH, height)
        browser.send("Page.navigate", url=page.as_uri())
        browser.wait_for("document.readyState === 'complete' && !!window.__prompt")

    def type(self, command: str, pause: int = 500) -> None:
        """Type ``command`` at a prompt, a line at a time when it runs over several."""
        js = self.reel.browser.js
        for number, text in enumerate(command.split("\n")):
            if number:
                js("__enter()")
            js(f"__prompt({json.dumps('$ ' if number == 0 else '> ')})")
            self.reel.frame(140 if number == 0 else 60)
            for end in range(0, len(text), 5):
                js(f"__type({json.dumps(text[end : end + 5])})")
                self.reel.frame(45)
        self.reel.hold(pause)
        js("__enter()")

    def show(self, markup: str, hold: int = 0) -> None:
        self.reel.browser.js(f"__out({json.dumps(markup)})")
        self.reel.frame(max(40, hold))

    def replace(self, markup: str, hold: int = 0) -> None:
        self.reel.browser.js(f"__last({json.dumps(markup)})")
        self.reel.frame(max(40, hold))


# ============================================================================
# THE SETUP, RUN FOR REAL
# ============================================================================
#
# INPUT   the images to film, Docker, curl
# OUTPUT  a folder holding the Compose files, the key and a scan, with the
#         setup running from it on the page's own ports; what each step answered
#
# The page's commands run in that folder as a reader runs them. Only the ports
# are the page's, so a server already on 7337 has to be stopped first.


def published_names() -> Dict[str, str]:
    """The image names docker/compose.yaml runs when nothing else is named."""
    text = (smoke.COMPOSE_DIR / "compose.yaml").read_text(encoding="utf-8")
    found = {
        part: re.search(r"\$\{" + setting + r":-([^}]+)\}", text)
        for part, setting in (("server", "VECTRIXDB_IMAGE"), ("extract", "VECTRIXDB_EXTRACT_IMAGE"))
    }
    if not all(found.values()):
        raise SystemExit("docker/compose.yaml no longer names its images the way this reads them")
    return {part: match.group(1) for part, match in found.items() if match}


def image_id(name: str) -> Optional[str]:
    code, out = smoke.docker("image", "inspect", "--format", "{{.Id}}", name)
    return out.strip() if code == 0 else None


def draw_scan(path: Path) -> None:
    """The scan the page sends: one line of an invoice, drawn as a picture."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1000, 160), "white")
    ImageDraw.Draw(image).text(
        (30, 40), "Invoice total 4200", fill="black", font=ImageFont.load_default(size=56)
    )
    image.save(path, format="PNG")


def run(command: List[str], folder: Path, stdin: Optional[bytes] = None) -> str:
    done = subprocess.run(
        command, cwd=folder, input=stdin, capture_output=True, timeout=300, check=False
    )
    if done.returncode != 0:
        raise SystemExit(
            f"{' '.join(command[:4])} failed: {(done.stdout + done.stderr).decode()[-800:]}"
        )
    return done.stdout.decode("utf-8")


def jq(reply: str, keep: Any) -> str:
    """What ``jq`` prints for the reply after ``keep`` picks from it: two-space JSON."""
    return json.dumps(keep(json.loads(reply)), indent=2, ensure_ascii=False)


def film_terminal(reel: shots.Reel, folder: Path, compose: List[str]) -> str:
    """Run the page's steps in ``folder``, filming each as typed and what it answered. Returns the key."""
    term = Terminal(reel, folder, "vectrixdb/docker")
    reel.hold(700)

    term.type('python -c "import secrets; print(secrets.token_urlsafe(32))" > vectrixdb.key', 300)
    key = run([sys.executable, "-c", "import secrets; print(secrets.token_urlsafe(32))"], folder)
    (folder / "vectrixdb.key").write_text(key, encoding="utf-8")
    key = key.strip()
    term.type("chmod 644 vectrixdb.key", 300)
    (folder / "vectrixdb.key").chmod(0o644)

    term.type("docker compose -f compose.yaml -f compose.tracing.yaml up -d --wait", 400)
    # Each container's last state as it reaches it, with the time it took, which is
    # what Compose leaves on a terminal when it is done.
    started = time.monotonic()
    process = subprocess.Popen(
        ["docker", *compose, "up", "-d", "--wait"],
        cwd=folder,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    done: Dict[str, Tuple[str, float]] = {}
    order: List[str] = []
    final = {"Created", "Started", "Healthy", "Running"}
    assert process.stdout is not None
    term.show("[+] Running 0/0", 200)
    for raw in process.stdout:
        found = re.match(r"\s*(Network|Volume|Container)\s+(\S+)\s+(\w+)\s*$", raw.strip())
        if not found:
            continue
        kind, name, state = found.groups()
        if state not in final:
            continue
        thing = f"{kind} {name}"
        if thing not in order:
            order.append(thing)
        done[thing] = (state, time.monotonic() - started)
        width = max(len(t) for t in order)
        rows = [
            f'<span class="ok">✔</span> {html.escape(t.ljust(width))}  '
            f"{done[t][0]:<8} {done[t][1]:5.1f}s"
            for t in order
        ]
        term.replace(
            f"[+] Running {len(order)}/{len(order)}\n" + "\n".join(" " + r for r in rows), 260
        )
    if process.wait(timeout=300) != 0:
        raise SystemExit("docker compose up did not bring the setup up healthy")
    reel.hold(1300)

    term.type('KEY="$(cat vectrixdb.key)"', 300)
    made = run(
        [
            "curl",
            "-s",
            "-X",
            "POST",
            "http://localhost:7337/api/v1/collections",
            "-H",
            f"api-key: {key}",
            "-H",
            "Content-Type: application/json",
            "-d",
            '{"name": "scans", "dimension": 384}',
        ],
        folder,
    )
    term.type(
        'curl -s -X POST http://localhost:7337/api/v1/collections -H "api-key: $KEY" \\\n'
        '  -H \'Content-Type: application/json\' -d \'{"name": "scans", "dimension": 384}\' \\\n'
        "  | jq .ok",
        300,
    )
    term.show(coloured(jq(made, lambda r: r.get("ok"))), 900)

    added = run(
        [
            "curl",
            "-s",
            "-X",
            "POST",
            "http://localhost:7337/api/v1/collections/scans/documents",
            "-H",
            f"api-key: {key}",
            "-H",
            "X-Filename: invoice.png",
            "-H",
            "Content-Type: image/png",
            "--data-binary",
            "@invoice.png",
        ],
        folder,
    )
    term.type(
        'curl -s -X POST http://localhost:7337/api/v1/collections/scans/documents -H "api-key: $KEY" \\\n'
        '  -H "X-Filename: invoice.png" -H "Content-Type: image/png" --data-binary @invoice.png \\\n'
        "  | jq '{doc_id, chunks, quality, extractor}'",
        300,
    )
    term.show(
        coloured(
            jq(
                added,
                lambda r: {k: r.get(k) for k in ("doc_id", "chunks", "quality", "extractor")},
            )
        ),
        1800,
    )

    found = run(
        [
            "curl",
            "-s",
            "-X",
            "POST",
            "http://localhost:7337/api/v1/collections/scans/text-search",
            "-H",
            f"api-key: {key}",
            "-H",
            "Content-Type: application/json",
            "-d",
            '{"query_text": "invoice total", "limit": 3}',
        ],
        folder,
    )
    term.type(
        'curl -s -X POST http://localhost:7337/api/v1/collections/scans/text-search -H "api-key: $KEY" \\\n'
        '  -H \'Content-Type: application/json\' -d \'{"query_text": "invoice total", "limit": 3}\' \\\n'
        "  | jq '.data.results[0] | {id, text, relevance}'",
        300,
    )
    term.show(
        coloured(
            jq(
                found,
                lambda r: {k: r["data"]["results"][0].get(k) for k in ("id", "text", "relevance")},
            )
        ),
        3200,
    )
    return key


# ============================================================================
# JAEGER
# ============================================================================


def wait_for_spans(jaeger: str, wanted: set) -> None:
    """Until Jaeger holds every span name in ``wanted``, or a minute has gone."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        names = set()
        for service in ("vectrixdb", "vectrixdb-extract"):
            status, _, body = smoke.call(
                "GET",
                f"{jaeger}/api/v3/traces?query.service_name={service}"
                f"&query.start_time_min={since}&query.start_time_max={until}",
            )
            if status == 200:
                names |= {
                    str(s.get("name")) for s in smoke._spans(json.loads(body.decode() or "null"))
                }
        if wanted <= names:
            return
        time.sleep(2)
    raise SystemExit(f"Jaeger never held {sorted(wanted)}")


def film_trace(reel: shots.Reel, jaeger: str) -> None:
    """The server's traces, the scan's opened with each span's attributes, then a search's."""
    browser = reel.browser
    browser.size(1280, 800)
    browser.send("Page.navigate", url=f"{jaeger}/search?service=vectrixdb&lookback=1h&limit=20")
    browser.wait_for("document.querySelectorAll('a[href^=\"/trace/\"]').length >= 2", 60)
    # Each trace as a card, with its services and spans, rather than a table row.
    browser.js(
        "[...document.querySelectorAll('button, label')]"
        ".find((b) => b.textContent.trim() === 'List')?.click()"
    )
    browser.wait_for("document.querySelectorAll('.ResultItem').length >= 2", 30)
    time.sleep(1.0)
    reel.hold(2000)
    browser.js(
        "[...document.querySelectorAll('.ResultItem')].find((e) => /add_document/.test(e.textContent))"
        ".querySelector('a[href^=\"/trace/\"]').id = 'vx-upload'"
    )
    reel.press("#vx-upload")
    browser.wait_for("document.querySelectorAll('.span-row').length >= 2", 30)
    time.sleep(0.8)
    reel.hold(1400)
    rows = "[...document.querySelectorAll('.span-row')]"
    attributes = (
        "[...document.querySelectorAll('.detail-row .AccordionAttributes--header')]"
        ".find((e) => /^Attributes/.test(e.textContent.trim()))"
    )
    # Each press gets an id of its own: a page keeps the ids given before it.
    for n, (span, hold) in enumerate(
        (("vectrixdb.add_document", 2800), ("vectrixdb.extract", 2600))
    ):
        # One span's details open at a time, so each fits on the screen.
        browser.js(
            "document.querySelectorAll('.detail-row').length &&"
            f" {rows}[0].querySelector('.span-name').click()"
        )
        time.sleep(0.4)
        browser.js(
            f"{rows}.find((r) => r.textContent.includes({json.dumps(span)}))"
            f".querySelector('.span-name').id = 'vx-span-{n}'"
        )
        reel.press(f"#vx-span-{n}")
        browser.wait_for(attributes, 10)
        reel.frame(500)
        browser.js(f"({attributes}).id = 'vx-attributes-{n}'")
        reel.press(f"#vx-attributes-{n}")
        reel.film(0.4)
        reel.hold(hold)
    browser.send(
        "Page.navigate",
        url=f"{jaeger}/search?service=vectrixdb&operation=vectrixdb.search&lookback=1h&limit=20",
    )
    browser.wait_for("document.querySelectorAll('.ResultItem').length >= 1", 60)
    time.sleep(1.0)
    reel.hold(900)
    browser.js("document.querySelector('.ResultItem a[href^=\"/trace/\"]').id = 'vx-search'")
    reel.press("#vx-search")
    browser.wait_for("document.querySelectorAll('.span-row').length >= 1", 30)
    time.sleep(0.8)
    browser.js(f"{rows}[0].querySelector('.span-name').id = 'vx-span'")
    reel.press("#vx-span")
    browser.wait_for(attributes, 10)
    reel.frame(500)
    browser.js(f"({attributes}).id = 'vx-attributes'")
    reel.press("#vx-attributes")
    reel.film(0.4)
    reel.hold(3400)


# ============================================================================
# START TO END
# ============================================================================


def saved(reel: shots.Reel, name: str) -> None:
    frames, size = reel.save(OUT / f"{name}.gif")
    seconds = sum(ms for _, ms in reel.frames) / 1000
    print(f"  {name}.gif  {frames} frames, {seconds:.1f} s, {size / 1e6:.2f} MB", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--server", help="The server image to film, instead of the published one")
    parser.add_argument(
        "--extract", help="The extraction image to film, instead of the published one"
    )
    args = parser.parse_args(argv)
    for tool in ("docker", "curl"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is needed and was not found")
    names = published_names()
    given = {"server": args.server, "extract": args.extract}
    before = {part: image_id(names[part]) for part in names if given[part]}
    OUT.mkdir(parents=True, exist_ok=True)
    browser = None
    with tempfile.TemporaryDirectory(prefix="vx-containers-") as scratch:
        folder = Path(scratch) / "docker"
        folder.mkdir()
        for name in COMPOSE_FILES:
            shutil.copy(smoke.COMPOSE_DIR / name, folder / name)
        draw_scan(folder / "invoice.png")
        compose = ["compose", *[a for name in COMPOSE_FILES for a in ("-f", name)]]
        try:
            for part, image in given.items():
                if image:
                    code, out = smoke.docker("tag", image, names[part])
                    if code != 0:
                        raise SystemExit(f"could not tag {image} as {names[part]}: {out}")
            browser = shots.Browser()
            reel = shots.Reel(browser)
            film_terminal(reel, folder, compose)
            saved(reel, "compose")
            wait_for_spans(
                "http://127.0.0.1:16686",
                {"vectrixdb.add_document", "vectrixdb.extract", "vectrixdb.search"},
            )
            reel = shots.Reel(browser)
            film_trace(reel, "http://127.0.0.1:16686")
            saved(reel, "trace")
        finally:
            if browser is not None:
                browser.close()
            subprocess.run(
                ["docker", *compose, "down", "--volumes", "--remove-orphans"],
                cwd=folder,
                capture_output=True,
                timeout=180,
                check=False,
            )
            for part, previous in before.items():
                if previous:
                    smoke.docker("tag", previous, names[part])
                else:
                    smoke.docker("rmi", "--no-prune", names[part])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
