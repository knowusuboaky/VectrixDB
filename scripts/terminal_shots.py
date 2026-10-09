"""Film the terminal clips the docs show, from real runs: every line on screen is what the command printed.

    python scripts/terminal_shots.py                 # every clip, into docs/images/terminal/
    python scripts/terminal_shots.py doctor sdk      # some of them

Each clip types its commands and shows what they printed, in the dashboard's
own ink and amber. Nothing on screen is written by hand: a clip runs its
commands first, against a server it starts in a temporary folder when it
needs one, and only then draws them. A command that fails stops the clip, so
a broken page is never filmed as a working one.

Clips:

    terminal-doctor   vectrixdb doctor, on a fresh install, offline
    terminal-mcp      the MCP tools on a real server: whoami, describe, search with facets
    terminal-sdk      the Python and TypeScript clients searching the same server

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images" / "terminal"

# ============================================================================
# SETTINGS: the look, the pace, the font
# ============================================================================
#
# The dashboard's ink, paper and amber; a monospace face this machine has.

WIDTH, HEIGHT = 1040, 600
PAD = 22
BAR = 34
INK = (26, 23, 15)
INK_2 = (33, 29, 19)
PAPER = (247, 245, 241)
DIM = (142, 138, 127)
AMBER = (232, 168, 26)
GREEN = (127, 207, 157)
RED = (232, 110, 92)
FONT_SIZE = 15
LINE = 22
TYPE_MS = 28  # a character of a typed command
LINE_MS = 55  # a line of output appearing
HOLD_MS = 2600  # the last frame
PAUSE_MS = 700  # after a command's output, before the next


def _font() -> ImageFont.FreeTypeFont:
    for name in ("CascadiaMono.ttf", "consola.ttf", "DejaVuSansMono.ttf", "Menlo.ttc"):
        for folder in (
            Path("C:/Windows/Fonts"),
            Path("/usr/share/fonts/truetype/dejavu"),
            Path("/System/Library/Fonts"),
        ):
            if (folder / name).exists():
                return ImageFont.truetype(str(folder / name), FONT_SIZE)
    return ImageFont.load_default()


FONT = _font()
COLUMNS = (WIDTH - 2 * PAD) // max(1, int(FONT.getlength("M")))
ROWS = (HEIGHT - BAR - 2 * PAD) // LINE


# ============================================================================
# A SESSION: commands and what they printed
# ============================================================================


@dataclass
class Step:
    """One command as typed, and what it printed."""

    shown: str
    output: str


@dataclass
class Session:
    title: str
    steps: List[Step] = field(default_factory=list)

    def run(
        self,
        shown: str,
        argv: Sequence[str],
        *,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[Path] = None,
        keep: Optional[Callable[[str], str]] = None,
    ) -> str:
        """Run a command, keep what it printed, and stop the clip if it failed."""
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "NO_COLOR": "1", "COLUMNS": str(COLUMNS), **(env or {})},
            cwd=str(cwd or ROOT),
            timeout=600,
        )
        printed = (done.stdout + done.stderr).rstrip()
        if done.returncode != 0:
            raise SystemExit(f"{shown!r} failed ({done.returncode}):\n{printed[-3000:]}")
        self.steps.append(Step(shown, keep(printed) if keep else printed))
        return printed

    def said(self, shown: str, output: str) -> None:
        """A step whose output was produced in this process, by the code the command names."""
        self.steps.append(Step(shown, output.rstrip()))


# ============================================================================
# DRAWING
# ============================================================================


def _colour(line: str) -> Tuple[int, int, int]:
    stripped = line.strip().lower()
    if stripped.startswith("$ "):
        return PAPER
    if stripped.startswith(("ok ", "[i]")) or " ok " in stripped[:12]:
        return GREEN if stripped.startswith("ok ") else PAPER
    if stripped.startswith(("error", "[!]", "failed")):
        return RED
    if stripped.startswith(("--", "#", "warn")):
        return DIM
    return PAPER


def _wrap(text: str) -> List[str]:
    out: List[str] = []
    for raw in text.splitlines() or [""]:
        line = raw.rstrip().replace("\t", "    ")
        while len(line) > COLUMNS:
            out.append(line[:COLUMNS])
            line = "  " + line[COLUMNS:]
        out.append(line)
    return out


def _frame(lines: List[Tuple[str, Tuple[int, int, int]]], title: str, cursor: bool) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), INK)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, WIDTH, BAR], fill=INK_2)
    for i, colour in enumerate(((232, 110, 92), AMBER, GREEN)):
        draw.ellipse([PAD + i * 20, BAR // 2 - 6, PAD + i * 20 + 12, BAR // 2 + 6], fill=colour)
    draw.text((WIDTH // 2, BAR // 2), title, fill=DIM, font=FONT, anchor="mm")
    shown = lines[-ROWS:]
    y = BAR + PAD
    for text, colour in shown:
        x = PAD
        if text.startswith("$ "):
            draw.text((x, y), "$", fill=AMBER, font=FONT)
            x += FONT.getlength("$ ")
            text = text[2:]
        draw.text((x, y), text, fill=colour, font=FONT)
        y += LINE
    if cursor and shown:
        last, _ = shown[-1]
        cx = PAD + FONT.getlength(last)
        draw.rectangle([cx + 2, y - LINE + 3, cx + 10, y - 4], fill=AMBER)
    return image


def render(session: Session, path: Path) -> None:
    frames: List[Image.Image] = []
    durations: List[int] = []
    screen: List[Tuple[str, Tuple[int, int, int]]] = []

    def add(cursor: bool, ms: int) -> None:
        frames.append(_frame(screen, session.title, cursor))
        durations.append(ms)

    for step in session.steps:
        typed = "$ "
        screen.append((typed, PAPER))
        for i, ch in enumerate(step.shown):
            typed += ch
            screen[-1] = (typed, PAPER)
            if (
                i % 2 == 1 or i == len(step.shown) - 1
            ):  # two characters a frame keeps the file small
                add(True, TYPE_MS * 2)
        add(False, 250)
        for line in _wrap(step.output):
            screen.append((line, _colour(line)))
            add(False, LINE_MS)
        screen.append(("", PAPER))
        add(False, PAUSE_MS)
    screen.append(("$ ", PAPER))
    add(True, HOLD_MS)
    path.parent.mkdir(parents=True, exist_ok=True)
    palette = frames[-1].convert("P", palette=Image.ADAPTIVE, colors=32)
    quantised = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    quantised[0].save(
        path,
        save_all=True,
        append_images=quantised[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,
    )
    print(
        f"wrote {path.relative_to(ROOT)} ({len(frames)} frames, {path.stat().st_size // 1024} KB)"
    )


# ============================================================================
# A SERVER FOR A CLIP
# ============================================================================


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Server:
    """A real VectrixDB server in a temporary folder, with a handbook collection, for one clip."""

    def __init__(self) -> None:
        self.folder = tempfile.mkdtemp(prefix="vectrixdb-shots-")
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.key = secrets.token_urlsafe(18)
        env = {
            **os.environ,
            "VECTRIXDB_API_KEY": self.key,
            "VECTRIXDB_MCP": "1",
            "VECTRIXDB_KEEP_SOURCE": "1",
            "VECTRIXDB_OFFLINE": "1",
            "PYTHONPATH": str(ROOT),
        }
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "vectrixdb.cli",
                "serve",
                "--port",
                str(self.port),
                "--path",
                self.folder,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.url + "/ready", timeout=2) as reply:  # noqa: S310 - our own server
                    if reply.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            self.close()
            raise SystemExit("the server for the clip did not become ready")
        self._seed()

    def _call(self, method: str, path: str, body: object) -> object:
        request = urllib.request.Request(
            self.url + path,
            method=method,
            data=json.dumps(body).encode(),
            headers={
                "api-key": self.key,
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as reply:  # noqa: S310 - our own server
            return json.loads(reply.read() or b"null")

    def _seed(self) -> None:
        self._call(
            "POST",
            "/api/v2/collections",
            {
                "name": "handbook",
                "dimension": 384,
                "metric": "cosine",
                "enable_text_index": True,
                "tags": ["hybrid"],
                "description": "The staff handbook",
            },
        )
        rows = [
            (
                "refunds",
                "Refunds are paid by the billing team within ten working days of the request.",
                "finance",
            ),
            ("refund-card", "A refund goes back to the card the customer paid with.", "finance"),
            (
                "travel",
                "Travel is booked through the office manager, economy class under six hours.",
                "operations",
            ),
            ("laptops", "Laptops are replaced every three years by the IT desk.", "it"),
            ("leave", "Annual leave is twenty five days, plus public holidays.", "people"),
        ]
        self._call(
            "POST",
            "/api/v1/collections/handbook/text-upsert",
            {"points": [{"id": i, "text": t, "payload": {"team": team}} for i, t, team in rows]},
        )

    def mcp(self, tool: str, arguments: dict) -> str:
        reply = self._call(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        )
        return str(reply["result"]["content"][0]["text"])  # type: ignore[index]

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        shutil.rmtree(self.folder, ignore_errors=True)


# ============================================================================
# THE CLIPS
# ============================================================================


def clip_doctor() -> Session:
    session = Session("vectrixdb doctor")
    folder = tempfile.mkdtemp(prefix="vectrixdb-doctor-")
    try:
        session.run(
            "vectrixdb doctor --quick --offline",
            [
                sys.executable,
                "-m",
                "vectrixdb.cli",
                "doctor",
                "--quick",
                "--offline",
                "--path",
                folder,
            ],
            env={"VECTRIXDB_OFFLINE": "1", "PYTHONPATH": str(ROOT)},
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return session


def clip_mcp() -> Session:
    session = Session("an assistant's tools, on a VectrixDB server")
    server = Server()
    try:
        session.said("mcp call whoami", server.mcp("whoami", {}))
        session.said(
            "mcp call describe_collection collection=handbook",
            server.mcp("describe_collection", {"collection": "handbook"}),
        )
        found = server.mcp(
            "search",
            {
                "collection": "handbook",
                "query": "how are refunds paid",
                "limit": 3,
                "facets": ["team"],
            },
        )
        session.said(
            'mcp call search collection=handbook query="how are refunds paid" limit=3 facets=team',
            found,
        )
    finally:
        server.close()
    return session


def clip_sdk() -> Session:
    session = Session("the same search, from Python and from TypeScript")
    server = Server()
    try:
        env = {"VECTRIXDB_URL": server.url, "VECTRIXDB_KEY": server.key, "PYTHONPATH": str(ROOT)}
        python = (
            "import os, vectrixdb\n"
            "db = vectrixdb.connect(os.environ['VECTRIXDB_URL'], key=os.environ['VECTRIXDB_KEY'], collection='handbook')\n"
            "for r in db.search('how long do refunds take', limit=2):\n"
            "    print(f'[{round(r.relevance * 100)}%] {r.readable_citation}: {r.text}')\n"
        )
        session.run("python search.py", [sys.executable, "-c", python], env=env)
        node = shutil.which("node")
        if node:
            session.run(
                'node quickstart/search.ts "how long do refunds take"',
                [node, "quickstart/search.ts", "how long do refunds take"],
                env=env,
                cwd=ROOT / "sdk" / "typescript",
            )
    finally:
        server.close()
    return session


CLIPS: Dict[str, Callable[[], Session]] = {
    "terminal-doctor": clip_doctor,
    "terminal-mcp": clip_mcp,
    "terminal-sdk": clip_sdk,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("clips", nargs="*", help="doctor, mcp, sdk; every clip when left out")
    args = parser.parse_args(argv)
    wanted = [f"terminal-{c}" if not c.startswith("terminal-") else c for c in args.clips] or list(
        CLIPS
    )
    for name in wanted:
        if name not in CLIPS:
            parser.error(f"no clip {name}; there are {', '.join(CLIPS)}")
        render(CLIPS[name](), OUT / f"{name}.gif")
    return 0


if __name__ == "__main__":
    sys.exit(main())
