"""The terminal clips and the script that films them agree.

Every clip the docs show from docs/images/terminal is one the script films
and is on disk, and every clip it films is shown somewhere. The script starts
a server, a browser and four toolchains and is run by hand; nothing here
starts any of them.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "terminal_shots", ROOT / "scripts" / "terminal_shots.py"
)
shots = importlib.util.module_from_spec(spec)
sys.modules["terminal_shots"] = shots
spec.loader.exec_module(shots)


def shown_in_docs() -> set:
    names = set()
    for page in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
        names |= set(
            re.findall(r"images/terminal/([a-z-]+)\.gif", page.read_text(encoding="utf-8"))
        )
    return names


def test_every_clip_shown_is_one_the_script_films_and_every_one_is_shown():
    assert shown_in_docs() == set(shots.CLIPS)
    assert {p.stem for p in shots.OUT.glob("*.gif")} == set(shots.CLIPS)


def test_the_four_search_examples_ask_the_same_question():
    """The clip runs each language's own example; they must print the same way to be compared."""
    for example in [
        ROOT / "sdk" / "python" / "examples" / "search.py",
        ROOT / "sdk" / "typescript" / "examples" / "search.ts",
        ROOT / "sdk" / "go" / "examples" / "search" / "main.go",
        ROOT / "sdk" / "rust" / "examples" / "search.rs",
    ]:
        text = example.read_text(encoding="utf-8")
        assert "VECTRIXDB_URL" in text and "VECTRIXDB_KEY" in text, example
        assert "how long do refunds take?" in text, example


def test_doctor_lines_are_coloured_by_verdict_and_escaped():
    out = shots.doctored("  ok     Install    <b>\n  warn   Models     x\nHealthy, 1 warning.")
    assert '<span class="ok">ok</span>' in out and "&lt;b&gt;" in out
    assert '<span class="s">warn</span>' in out
    assert '<span class="cmd">Healthy, 1 warning.</span>' in out


def test_a_search_line_colours_the_score_and_the_citation():
    out = shots.hits("0.851  handbook.md#Refunds\n       Refunds <are> paid")
    assert '<span class="n">0.851</span>  <span class="ok">handbook.md#Refunds</span>' in out
    assert "Refunds &lt;are&gt; paid" in out
