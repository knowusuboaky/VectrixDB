"""Prepare a release: check the changelog, stamp the version, print the git steps.

    python scripts/release.py --check            # CI: the unreleased section has entries
    python scripts/release.py 2.2.0              # stamp 2.2.0 and today's date, bump pyproject
    python scripts/release.py 2.2.0 --dry-run    # show what would change

What it does on a version:

1. Refuses anything that is not MAJOR.MINOR.PATCH with an optional
   pre-release suffix (2.2.0, 2.2.0rc1).
2. Finds the unreleased entry in CHANGELOG.md, either ``## [Unreleased]`` or
   ``## [X.Y.Z] - Unreleased``, and refuses if it has no bullet points.
3. Rewrites that heading to ``## [<version>] - <today>`` and inserts a fresh
   ``## [Unreleased]`` above it.
4. Sets ``version = "<version>"`` in pyproject.toml.
5. Prints the exact git commands to commit, tag and push. It never runs
   them: tagging is the maintainer's act, and the publish workflow fires on
   the GitHub release, not on the tag.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path


# ============================================================================
# SETTINGS: the changelog, the project file, and the version shapes
# ============================================================================
#
# CHANGELOG.md and pyproject.toml, and the patterns a version and the
# unreleased heading must match: MAJOR.MINOR.PATCH with an optional pre-
# release suffix.

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")
UNRELEASED_RE = re.compile(r"^## \[(Unreleased|[^\]]+)\](?: - Unreleased)?\s*$", re.MULTILINE)


# ============================================================================
# THE CHANGELOG: read, checked, stamped
# ============================================================================
#
# INPUT   the changelog's text; then a version and whether this is a dry run
# OUTPUT  the unreleased heading and its body; a verdict that it has entries;
#         the heading stamped with the version and today's date, a fresh
#         Unreleased above it, and pyproject's version set
#
# An unreleased entry with no bullet points is refused. It never runs git:
# tagging is the maintainer's act, and the publish workflow fires on the
# GitHub release, not on the tag.


def unreleased_section(text: str) -> tuple[re.Match, str]:
    """The heading match for the unreleased entry and its body text."""
    for m in UNRELEASED_RE.finditer(text):
        if m.group(1) == "Unreleased" or "Unreleased" in m.group(0):
            nxt = re.search(r"^## \[", text[m.end() :], re.MULTILINE)
            body = text[m.end() : m.end() + nxt.start()] if nxt else text[m.end() :]
            return m, body
    raise SystemExit(
        "CHANGELOG.md has no unreleased section (## [Unreleased] or ## [X] - Unreleased)"
    )


def check(text: str) -> int:
    _, body = unreleased_section(text)
    bullets = [line for line in body.splitlines() if line.lstrip().startswith("- ")]
    if not bullets:
        print("CHANGELOG.md: the unreleased section is empty. Add a line for what changed.")
        return 1
    print(f"CHANGELOG.md: {len(bullets)} unreleased entries.")
    return 0


def stamp(version: str, dry_run: bool) -> int:
    if not VERSION_RE.match(version):
        raise SystemExit(f"{version!r} is not MAJOR.MINOR.PATCH (with optional a/b/rc suffix)")
    text = CHANGELOG.read_text(encoding="utf-8")
    if check(text):
        return 1
    m, _ = unreleased_section(text)
    today = dt.date.today().isoformat()
    new_heading = f"## [Unreleased]\n\n## [{version}] - {today}"
    new_text = text[: m.start()] + new_heading + text[m.end() :]

    py = PYPROJECT.read_text(encoding="utf-8")
    py_new, n = re.subn(
        r'^version = "[^"]+"', f'version = "{version}"', py, count=1, flags=re.MULTILINE
    )
    if n != 1:
        raise SystemExit("pyproject.toml has no version line")

    if dry_run:
        print(
            f"would set pyproject version to {version} and stamp CHANGELOG as [{version}] - {today}"
        )
    else:
        CHANGELOG.write_text(new_text, encoding="utf-8")
        PYPROJECT.write_text(py_new, encoding="utf-8")
        print(f"stamped {version} ({today}) in CHANGELOG.md and pyproject.toml")

    print(
        "\nNext, by hand:\n"
        f"  git add CHANGELOG.md pyproject.toml\n"
        f'  git commit -m "Release {version}"\n'
        f'  git tag -a v{version} -m "VectrixDB {version}"\n'
        f"  git push && git push origin v{version}\n"
        "then publish a GitHub release for the tag; the publish workflow runs the suite and uploads to PyPI."
    )
    return 0


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --check, or a version with --dry-run
# OUTPUT  the exact git commands to commit, tag and push, printed and never
#         run
#
# CI runs --check; a maintainer runs the version.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("version", nargs="?")
    parser.add_argument(
        "--check", action="store_true", help="only verify the unreleased section has entries"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.check or not args.version:
        return check(CHANGELOG.read_text(encoding="utf-8"))
    return stamp(args.version, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
