"""The release notes for a version, read from CHANGELOG.md, and the checks a release must pass first.

    python scripts/release_notes.py 2.2.0              # print the notes for 2.2.0
    python scripts/release_notes.py 2.2.0 --check      # also refuse an undated or missing section
    python scripts/release_notes.py --version          # the version pyproject.toml declares

The release workflow writes the GitHub release from this, so the notes on the
Releases page are always the changelog's own words, and a version cannot go
out while its section still says "Unreleased".
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

#: "## [2.2.0] - 2026-10-03", or "- Unreleased" while it is being written
_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?:\s*-\s*(?P<when>.+?))?\s*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ============================================================================
# READING
# ============================================================================
#
# INPUT   the changelog's text and a version
# OUTPUT  the section's date line and body, or None when there is no section


def section(text: str, version: str) -> Optional[tuple]:
    lines = text.splitlines()
    start = when = None
    for i, line in enumerate(lines):
        if start is not None and line.startswith("## "):
            return when, "\n".join(lines[start:i]).strip()
        m = _HEADING.match(line)
        if m and m.group("version") == version:
            start, when = i + 1, (m.group("when") or "").strip()
    if start is None:
        return None
    return when, "\n".join(lines[start:]).strip()


#: GitHub refuses a release body over 125,000 characters
LIMIT = 120_000
_LEAD = re.compile(r"^- \*\*(?P<lead>.+?)\*\*")
CHANGELOG_URL = "https://github.com/knowusuboaky/VectrixDB/blob/main/CHANGELOG.md"


def notes(text: str, version: str, limit: int = LIMIT) -> Optional[str]:
    """The section whole when it fits on a release, else each entry's bold lead-in and a link to the rest.

    A large release's section runs past what GitHub takes for a release body.
    Every entry opens with its point in bold, so the lead-ins under their
    headings read as the release in brief, and the link carries the detail.
    """
    found = section(text, version)
    if found is None:
        return None
    when, body = found
    if len(body) <= limit:
        return body
    anchor = re.sub(r"[^a-z0-9 -]", "", f"[{version}] - {when}".lower()).replace(" ", "-")
    more = f"\n\nEvery entry in full: [CHANGELOG.md]({CHANGELOG_URL}#{anchor})\n"
    brief = []
    for line in body.splitlines():
        if line.startswith("### "):
            brief.extend(["", line, ""])
            continue
        m = _LEAD.match(line)
        if m:
            brief.append(f"- {m.group('lead')}")
    out = "\n".join(brief).strip()
    if len(out) + len(more) > limit:
        out = out[: limit - len(more)].rsplit("\n", 1)[0]
    return out + more


def declared_version(pyproject: Optional[Path] = None) -> str:
    text = (pyproject or ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit("pyproject.toml declares no version")
    return m.group(1)


# ============================================================================
# THE CHECK
# ============================================================================
#
# INPUT   a version and the changelog's text
# OUTPUT  the problems in words; none means it may be released


def problems(text: str, version: str) -> list:
    found = section(text, version)
    if found is None:
        return [f"CHANGELOG.md has no section for {version}: add '## [{version}] - <date>'"]
    when, body = found
    said = []
    if not _DATE.match(when):
        said.append(
            f"CHANGELOG.md's {version} section is dated {when or 'nothing'!r}: "
            f"write the release date, '## [{version}] - YYYY-MM-DD'"
        )
    if not body:
        said.append(f"CHANGELOG.md's {version} section is empty")
    return said


# ============================================================================
# MAIN SCRIPT
# ============================================================================


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "release", nargs="?", help="the version, such as 2.2.0 (a leading v is dropped)"
    )
    parser.add_argument(
        "--check", action="store_true", help="refuse an undated, empty or missing section"
    )
    parser.add_argument(
        "--version", action="store_true", help="print the version pyproject.toml declares"
    )
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    args = parser.parse_args(argv)

    if args.version:
        print(declared_version())
        return 0
    if not args.release:
        parser.error("name a version, or pass --version")
    version = args.release.lstrip("v")
    text = args.changelog.read_text(encoding="utf-8")
    if args.check:
        said = problems(text, version)
        if said:
            for line in said:
                print(line, file=sys.stderr)
            return 1
    body = notes(text, version)
    if body is None:
        print(f"CHANGELOG.md has no section for {version}", file=sys.stderr)
        return 1
    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
