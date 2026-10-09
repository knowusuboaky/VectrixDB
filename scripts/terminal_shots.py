"""Film the terminal clips: the four clients against one server, and doctor.

    python scripts/terminal_shots.py              # every clip
    python scripts/terminal_shots.py clients      # one of them

cli.gif points the vectrixdb command at a server serve.py started: a folder
ingested, a search with its citations, whoami, and the plain-HTTP address it
refuses to send a key to.
clients.gif starts a server with sdk/conformance/serve.py, adds a document
with the Python client, and runs the same search with each client's own
example: Python, TypeScript, Go and Rust, each answering with the same
citation. doctor.gif runs ``vectrixdb doctor --offline`` in an empty folder.
Every command on screen is the one that ran, in the folder it names, and what
is shown under it is what it printed. Both are saved in docs/images/terminal,
960 wide.

Needs Pillow, websocket-client, Edge or Chrome (VECTRIXDB_BROWSER names one
somewhere else), jq for cli.gif, and for clients.gif node with sdk/typescript's packages
installed, go and cargo. The Go and Rust examples are built before filming,
so the clip shows what they print and not the compiler.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import container_shots as terminal  # noqa: E402
import dashboard_shots as shots  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SDK = ROOT / "sdk"
OUT = ROOT / "docs" / "images" / "terminal"
#: The clips, each saved as <name>.gif in OUT, and what each shows.
CLIPS = {
    "clients": "One server, one document, the same search from Python, TypeScript, Go and Rust",
    "doctor": "vectrixdb doctor trying every part of a fresh install",
    "cli": "The vectrixdb command on a server: ingest, search, whoami, and a key it will not send",
}
QUERY = "how long do refunds take?"
#: The clips are taller than the containers page's, so a whole run fits without scrolling.
HEIGHT = 900


def run(command: List[str], cwd: Path, env: Optional[Dict[str, str]] = None) -> str:
    """What ``command`` printed, stdout and stderr together; a failure stops the filming."""
    done = subprocess.run(
        command,
        cwd=cwd,
        # The filming's own settings (the browser to use) are not the reader's.
        env={**{k: v for k, v in os.environ.items() if k != "VECTRIXDB_BROWSER"}, **(env or {})},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if done.returncode != 0:
        raise SystemExit(f"{' '.join(command[:4])} failed: {(done.stdout + done.stderr)[-800:]}")
    return done.stdout + done.stderr


def hits(printed: str) -> str:
    """A search example's lines, the score and the citation coloured as a terminal would."""
    out = []
    for line in printed.rstrip("\n").split("\n"):
        found = re.match(r"^(\d\.\d{3})  (.*)$", line)
        if found:
            score, citation = found.groups()
            out.append(
                f'<span class="n">{score}</span>  <span class="ok">{html.escape(citation)}</span>'
            )
        else:
            out.append(html.escape(line))
    return "\n".join(out)


# ============================================================================
# THE CLIENTS
# ============================================================================


def film_clients(reel: shots.Reel, scratch: Path) -> None:
    for tool in ("node", "go", "cargo"):
        if not shutil_which(tool):
            raise SystemExit(f"{tool} is needed for clients.gif and was not found")
    target = scratch / "rust-target"
    rust_env = {"CARGO_TARGET_DIR": str(target)}
    print("  building the Go and Rust examples first", file=sys.stderr)
    run(["go", "build", "-o", str(scratch / "go-search"), "./examples/search"], SDK / "go")
    run(["cargo", "build", "-q", "--example", "search"], SDK / "rust", rust_env)

    server = subprocess.Popen(
        [sys.executable, str(SDK / "conformance" / "serve.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert server.stdout is not None
        where = json.loads(server.stdout.readline())
        env = {"VECTRIXDB_URL": where["url"], "VECTRIXDB_KEY": where["key"]}
        handbook = scratch / "handbook.md"
        handbook.write_bytes((SDK / "conformance" / "handbook.md").read_bytes())

        term = terminal.Terminal(reel, scratch, "vectrixdb/sdk", HEIGHT)
        reel.hold(600)
        term.type(f"export VECTRIXDB_URL={where['url']} VECTRIXDB_KEY=…", 300)

        python = SDK / "python" / "examples"
        added = run(
            [sys.executable, str(python / "ingest.py"), "handbook", str(handbook)], scratch, env
        )
        term.type("python python/examples/ingest.py handbook handbook.md", 300)
        term.show(html.escape(added.rstrip("\n")), 1200)

        steps = [
            (
                f'python python/examples/search.py handbook "{QUERY}"',
                [sys.executable, str(python / "search.py"), "handbook", QUERY],
                ROOT,
            ),
            (
                f'(cd typescript && npx tsx examples/search.ts handbook "{QUERY}")',
                ["npx", "tsx", "examples/search.ts", "handbook", QUERY],
                SDK / "typescript",
            ),
            (
                f'(cd go && go run ./examples/search handbook "{QUERY}")',
                [str(scratch / "go-search"), "handbook", QUERY],
                SDK / "go",
            ),
            (
                f'(cd rust && cargo run -q --example search -- handbook "{QUERY}")',
                ["cargo", "run", "-q", "--example", "search", "--", "handbook", QUERY],
                SDK / "rust",
            ),
        ]
        for shown, command, cwd in steps:
            printed = run(command, cwd, {**env, **rust_env})
            term.type(shown, 300)
            term.show(hits(printed), 1500)
        reel.hold(2600)
    finally:
        server.terminate()
        server.wait(timeout=30)


def shutil_which(tool: str) -> Optional[str]:
    import shutil

    return shutil.which(tool)


# ============================================================================
# DOCTOR
# ============================================================================

LEVELS = {"ok": "ok", "warn": "s", "error": "s", "--": "out"}


def doctored(printed: str) -> str:
    """doctor's report, each line's verdict coloured: ok green, warn amber, -- grey."""
    out = []
    for line in printed.rstrip("\n").split("\n"):
        found = re.match(r"^(  )(ok|warn|error|--)(\s+)(.*)$", line)
        if found:
            lead, level, gap, rest = found.groups()
            out.append(
                f'{lead}<span class="{LEVELS[level]}">{level}</span>{gap}{html.escape(rest)}'
            )
        elif line.startswith(("Healthy", "Unhealthy")):
            out.append(f'<span class="cmd">{html.escape(line)}</span>')
        else:
            out.append(html.escape(line))
    return "\n".join(out)


def film_doctor(reel: shots.Reel, scratch: Path) -> None:
    folder = scratch / "fresh"
    folder.mkdir()
    beside = Path(sys.executable).with_name("vectrixdb")
    command = str(beside) if beside.exists() else "vectrixdb"
    printed = run(
        [command, "doctor", "--offline"],
        folder,
        {"COLUMNS": "100", "TERM": "dumb", "NO_COLOR": "1"},
    )
    term = terminal.Terminal(reel, folder, "vectrixdb doctor", HEIGHT)
    reel.hold(600)
    term.type("vectrixdb doctor --offline", 400)
    lines = doctored(printed).split("\n")
    for at, line in enumerate(lines):
        term.show(line, 260 if at < len(lines) - 1 else 4000)


def _command() -> str:
    beside = Path(sys.executable).with_name("vectrixdb")
    return str(beside) if beside.exists() else "vectrixdb"


def film_cli(reel: shots.Reel, scratch: Path) -> None:
    server = subprocess.Popen(
        [sys.executable, str(SDK / "conformance" / "serve.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert server.stdout is not None
        where = json.loads(server.stdout.readline())
        folder = scratch / "policies"
        folder.mkdir()
        (folder / "handbook.md").write_bytes((SDK / "conformance" / "handbook.md").read_bytes())
        quiet = {"COLUMNS": "100", "TERM": "dumb", "NO_COLOR": "1"}
        env = {"VECTRIXDB_URL": where["url"], "VECTRIXDB_KEY": where["key"], **quiet}

        def vx(*args: str, extra: Optional[Dict[str, str]] = None, fails: bool = False) -> str:
            done = subprocess.run(
                [_command(), *args],
                cwd=scratch,
                env={
                    **{k: v for k, v in os.environ.items() if not k.startswith("VECTRIXDB_")},
                    **(extra if extra is not None else env),
                },
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if (done.returncode != 0) != fails:
                raise SystemExit(
                    f"vectrixdb {' '.join(args)}: {(done.stdout + done.stderr)[-800:]}"
                )
            return (done.stdout + done.stderr).rstrip("\n")

        term = terminal.Terminal(reel, scratch, "vectrixdb, on a server", HEIGHT)
        reel.hold(600)
        term.type(f"export VECTRIXDB_URL={where['url']}", 200)
        term.type('export VECTRIXDB_KEY="$(cat handbook.key)"', 300)
        printed = vx("ingest", "policies", "--name", "handbook")
        term.type("vectrixdb ingest policies --name handbook", 300)
        term.show(html.escape(printed), 1300)
        printed = vx("query", QUERY, "--name", "handbook", "--limit", "2", "--json")
        term.type(
            f'vectrixdb query "{QUERY}" --name handbook --limit 2 --json | jq ".items[] | {{score, citation}}"',
            300,
        )
        # jq itself, given what the command printed: the clip shows what the pipe prints.
        picked = subprocess.run(
            ["jq", ".items[] | {score, citation}"],
            input=printed,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.rstrip("\n")
        term.show(terminal.coloured(picked), 1700)
        printed = vx("whoami")
        term.type("vectrixdb whoami", 300)
        term.show(html.escape(printed), 1400)
        printed = vx(
            "list",
            "--url",
            "http://vectors.example.com",
            extra={"VECTRIXDB_KEY": where["key"], **quiet},
            fails=True,
        )
        term.type("vectrixdb list --url http://vectors.example.com", 300)
        term.show(f'<span class="s">{html.escape(printed)}</span>', 3200)
    finally:
        server.terminate()
        server.wait(timeout=30)


FILMS = {"clients": film_clients, "doctor": film_doctor, "cli": film_cli}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "names", nargs="*", help=f"the clips to film: {', '.join(CLIPS)} (all by default)"
    )
    args = parser.parse_args(argv)
    names = args.names or list(CLIPS)
    unknown = [name for name in names if name not in CLIPS]
    if unknown:
        parser.error(f"no clip named {', '.join(unknown)}; the clips are {', '.join(CLIPS)}")
    OUT.mkdir(parents=True, exist_ok=True)
    browser = shots.Browser()
    try:
        for name in names:
            with tempfile.TemporaryDirectory(prefix=f"vx-{name}-") as scratch:
                reel = shots.Reel(browser)
                FILMS[name](reel, Path(scratch))
                frames, size = reel.save(OUT / f"{name}.gif")
                seconds = sum(ms for _, ms in reel.frames) / 1000
                print(
                    f"  {name}.gif  {frames} frames, {seconds:.1f} s, {size / 1e6:.2f} MB",
                    file=sys.stderr,
                )
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
