"""Prepare a release: check the changelog, stamp the version, print the git steps.

    python scripts/release.py --check            # CI: the unreleased section has entries
    python scripts/release.py 2.2.0              # stamp 2.2.0 and today's date, bump pyproject
    python scripts/release.py 2.2.0 --dry-run    # show what would change
    python scripts/release.py --clients 2.2.0    # the release workflow: every client says 2.2.0
    python scripts/release.py --client-version 2.3.0rc1   # 2.3.0-rc.1, as npm, crates.io and Go take it

What it does on a version:

1. Refuses anything that is not MAJOR.MINOR.PATCH with an optional
   pre-release suffix (2.2.0, 2.2.0rc1).
2. Finds the unreleased entry in CHANGELOG.md, either ``## [Unreleased]`` or
   ``## [X.Y.Z] - Unreleased``, and refuses if it has no bullet points.
3. Rewrites that heading to ``## [<version>] - <today>`` and inserts a fresh
   ``## [Unreleased]`` above it.
4. Sets ``version = "<version>"`` in pyproject.toml, and the same version in
   each client: sdk/typescript (package.json, its lockfile and the user-agent
   constant), sdk/rust (Cargo.toml and Cargo.lock) and sdk/go (the user-agent
   constant). npm, crates.io and Go take a pre-release as 2.3.0-rc.1, so a
   2.3.0rc1 is written that way in the clients.
5. Prints the git commands to commit and push. It never runs them. The
   Release workflow (Actions > Release > Run workflow) then makes the tag
   and the GitHub release from the changelog and publishes to PyPI.
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
PRE_RE = re.compile(r"^(\d+\.\d+\.\d+)(a|b|rc)(\d+)$")

#: Where each client says its version: the file, and a pattern whose second
#: group is the version. Each must match exactly once.
CLIENTS = {
    "sdk/typescript/package.json": r'\A(\{\n  "name": "vectrixdb",\n  "version": ")([^"]+)(")',
    "sdk/typescript/package-lock.json (top)": r'\A(\{\n  "name": "vectrixdb",\n  "version": ")([^"]+)(")',
    "sdk/typescript/package-lock.json (package)": r'(\n    "": \{\n      "name": "vectrixdb",\n      "version": ")([^"]+)(")',
    "sdk/typescript/src/index.ts": r'^(export const VERSION = ")([^"]+)(";)',
    "sdk/rust/Cargo.toml": r'^(\[package\]\nname = "vectrixdb"\nversion = ")([^"]+)(")',
    "sdk/rust/Cargo.lock": r'^(name = "vectrixdb"\nversion = ")([^"]+)(")',
    "sdk/go/client.go": r'^(const Version = ")([^"]+)(")',
}


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


# ============================================================================
# THE CLIENTS: one version for all four
# ============================================================================
#
# INPUT   the release's version, as pyproject.toml has it
# OUTPUT  the version each client is published at; each client file read, or
#         rewritten at it; the clients that say something else
#
# A release is refused while any client still says another version, so npm,
# crates.io and the Go tag always match PyPI.


def client_version(version: str) -> str:
    """The version npm, crates.io and Go take: 2.3.0rc1 is 2.3.0-rc.1 there."""
    pre = PRE_RE.match(version)
    if not pre:
        return version
    base, kind, number = pre.groups()
    name = {"a": "alpha", "b": "beta"}.get(kind, kind)
    return f"{base}-{name}.{number}"


def _client_file(label: str) -> Path:
    return ROOT / label.split(" ")[0]


def client_versions() -> dict[str, str]:
    """What each client file says its version is."""
    found = {}
    for label, pattern in CLIENTS.items():
        text = _client_file(label).read_text(encoding="utf-8")
        hits = re.findall(pattern, text, re.MULTILINE)
        if len(hits) != 1:
            raise SystemExit(f"{label}: expected one version, found {len(hits)}")
        found[label] = hits[0][1]
    return found


def check_clients(version: str) -> int:
    wanted = client_version(version)
    wrong = {k: v for k, v in client_versions().items() if v != wanted}
    for label, said in wrong.items():
        print(f"{label} says {said}, not {wanted}")
    if wrong:
        print(f"Run: python scripts/release.py {version}")
        return 1
    print(f"Every client says {wanted}.")
    return 0


def stamp_clients(version: str) -> dict[Path, str]:
    """Each client file's text with the version set, not yet written."""
    wanted = client_version(version)
    texts: dict[Path, str] = {}
    for label, pattern in CLIENTS.items():
        path = _client_file(label)
        text = texts.get(path) or path.read_text(encoding="utf-8")
        text, n = re.subn(
            pattern, lambda m: f"{m.group(1)}{wanted}{m.group(3)}", text, flags=re.MULTILINE
        )
        if n != 1:
            raise SystemExit(f"{label}: expected one version, found {n}")
        texts[path] = text
    return texts


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
    clients = stamp_clients(version)

    if dry_run:
        print(
            f"would set pyproject version to {version}, every client to "
            f"{client_version(version)}, and stamp CHANGELOG as [{version}] - {today}"
        )
    else:
        CHANGELOG.write_text(new_text, encoding="utf-8")
        PYPROJECT.write_text(py_new, encoding="utf-8")
        for path, text in clients.items():
            path.write_text(text, encoding="utf-8")
        print(f"stamped {version} ({today}) in CHANGELOG.md, pyproject.toml and the clients")

    print(
        "\nNext, by hand:\n"
        f"  git add CHANGELOG.md pyproject.toml sdk\n"
        f'  git commit -m "Release {version}"\n'
        "  git push\n"
        "then Actions > Release > Run workflow. It checks the changelog, runs the suite,\n"
        f"tags v{version}, writes the GitHub release from the changelog and, after your\n"
        "approval on the pypi environment, uploads to PyPI; then npm and crates.io get\n"
        f"the clients and Go the tag sdk/go/v{client_version(version)}."
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
    parser.add_argument(
        "--clients", action="store_true", help="only verify every client says the version"
    )
    parser.add_argument(
        "--client-version", action="store_true", help="print the version the clients take"
    )
    args = parser.parse_args(argv)
    if args.client_version:
        if not args.version:
            parser.error("--client-version needs the version")
        print(client_version(args.version))
        return 0
    if args.clients:
        if not args.version:
            parser.error("--clients needs the version")
        return check_clients(args.version)
    if args.check or not args.version:
        return check(CHANGELOG.read_text(encoding="utf-8"))
    return stamp(args.version, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
