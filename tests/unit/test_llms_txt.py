"""docs/llms.txt, the index coding agents read, links every page in the docs nav."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = "https://knowusuboaky.github.io/VectrixDB/"


def _nav_pages():
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    nav = text[text.index("\nnav:") :]
    return re.findall(r":\s+([\w/-]+)\.md\s*$", nav, flags=re.M)


def test_every_page_in_the_nav_is_in_llms_txt():
    llms = (ROOT / "docs" / "llms.txt").read_text(encoding="utf-8")
    missing = [page for page in _nav_pages() if page != "index" and f"{SITE}{page}/" not in llms]
    assert missing == [], f"add these to docs/llms.txt: {missing}"


def test_every_link_in_llms_txt_is_a_page():
    llms = (ROOT / "docs" / "llms.txt").read_text(encoding="utf-8")
    for page in re.findall(re.escape(SITE) + r"([\w/-]+?)/?\)", llms):
        if page == "llms.txt":
            continue
        assert (ROOT / "docs" / f"{page}.md").is_file(), page


def test_it_starts_the_way_the_format_asks():
    lines = (ROOT / "docs" / "llms.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# VectrixDB"
    assert lines[2].startswith("> ")
