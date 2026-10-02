"""Links between the repository's own files point at files that exist.

The README grew a Documentation section because nineteen of the twenty-two
docs pages were reachable only from `mkdocs.yml`: they existed, they built,
and nothing on the front page led to them. A reader who never runs the site
locally could not find them.

Having added that section, the failure mode moves. A renamed or deleted page
now leaves a dead link on the first thing anyone sees on GitHub, and nothing
about a dead relative link makes a build fail: the docs build only knows about
pages inside `docs/`, so the README's links to `CONTRIBUTING.md`, `LICENSE`
and the rest are checked by nobody. These tests are that nobody.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: `[text](target)`, ignoring images, which use the same syntax with a `!`.
LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: The process files. A repository without a visible path to these reads as
#: one nobody is running, whatever the code is like.
PROCESS_FILES = [
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "LICENSE",
    "ROADMAP.md",
    "SECURITY.md",
]


#: Folders a build or an install fills with other people's files, whose links are theirs to keep.
INSTALLED = {"node_modules", "dist", ".venv", "venv", "__pycache__"}


def _markdown_files() -> list[Path]:
    examples = [p for p in (ROOT / "examples").rglob("*.md") if not INSTALLED.intersection(p.relative_to(ROOT).parts)]
    return [
        *sorted(ROOT.glob("*.md")),
        *sorted((ROOT / "docs").rglob("*.md")),
        *sorted(examples),
    ]


def _relative_links(path: Path) -> list[tuple[str, str]]:
    """Every link in one file that names a path rather than a URL."""
    found = []
    for text, target in LINK.findall(path.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "#", "mailto:", "<")):
            continue
        found.append((text, target))
    return found


def test_every_relative_link_points_at_something_that_exists():
    """A link that 404s on GitHub is the first impression this repository
    makes, and no build step checks one."""
    broken = []
    for path in _markdown_files():
        for text, target in _relative_links(path):
            # Strip an anchor: the file has to exist, the heading is the
            # site build's business.
            resolved = (path.parent / target.split("#")[0]).resolve()
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT).as_posix()}: [{text}]({target})")

    assert not broken, "these links point at nothing:\n  " + "\n  ".join(broken)


def test_the_readme_links_every_docs_page():
    """The Documentation section is a table of contents, so a page added to
    the docs joins it. Otherwise the section decays back into a sample of the
    documentation, which is what it was written to replace."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    linked = {target.split("#")[0] for _, target in _relative_links(ROOT / "README.md")}

    pages = {
        p.relative_to(ROOT).as_posix()
        for p in sorted((ROOT / "docs").rglob("*.md"))
        # index.md is the site's own front page, the README's counterpart.
        if p.name != "index.md"
    }
    missing = sorted(pages - linked)
    assert not missing, (
        "docs pages nothing on the front page leads to; add them to the "
        "Documentation section:\n  " + "\n  ".join(missing)
    )
    assert "## Documentation" in readme


def test_the_readme_links_the_process_files():
    """Contributing, governance, conduct, security, licence: all present in
    the repository, none of them reachable from the README before."""
    linked = {target.split("#")[0] for _, target in _relative_links(ROOT / "README.md")}
    missing = [name for name in PROCESS_FILES if name not in linked]
    assert not missing, f"in the repository but unreachable from the README: {missing}"


#: `<img src="...">` and `![alt](target)`.
IMAGE = re.compile(r"<img[^>]*\bsrc=\"([^\"]+)\"|!\[[^\]]*\]\(([^)\s]+)")


def test_the_readme_shows_its_own_pictures_by_relative_path():
    """The README's pictures were addresses on main, where they had never
    been pushed, so GitHub drew a broken image on every branch; and a private
    repository's raw addresses do not load for anyone. A relative path shows
    on every branch, public or private. The PyPI page gets addresses on main
    at build time, from the fancy-pypi-readme substitutions in pyproject.toml.
    """
    readme = ROOT / "README.md"
    own = []
    missing = []
    for src in (a or b for a, b in IMAGE.findall(readme.read_text(encoding="utf-8"))):
        if src.startswith(
            ("https://raw.githubusercontent.com/knowusuboaky/", "https://github.com/knowusuboaky/")
        ):
            own.append(src)
        elif not src.startswith(("http://", "https://")) and not (ROOT / src).exists():
            missing.append(src)

    assert not own, "use a relative path for the repository's own pictures:\n  " + "\n  ".join(own)
    assert not missing, "these pictures are not in the repository:\n  " + "\n  ".join(missing)
