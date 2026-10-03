"""The dashboard's pictures and the script that takes them agree.

What is being held to: every picture the docs show is one the script takes,
and every picture the script takes is one the docs show, so a page added,
renamed or removed cannot leave a picture behind or a picture missing; the
script names each picture with its size; and it refuses a name it does not
know. The script itself drives a browser against a server and is run by hand;
nothing here starts either.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dashboard_shots.py"
spec = importlib.util.spec_from_file_location("dashboard_shots", SCRIPT)
shots = importlib.util.module_from_spec(spec)
sys.modules["dashboard_shots"] = shots
spec.loader.exec_module(shots)

PICTURES = ROOT / "docs" / "images" / "dashboard"


def shown_in_docs() -> set:
    names = set()
    for page in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
        names |= set(
            re.findall(r"images/dashboard/([a-z-]+)\.png", page.read_text(encoding="utf-8"))
        )
    return names


def test_every_picture_the_docs_show_is_one_the_script_takes():
    assert shown_in_docs() <= set(shots.SHOTS), shown_in_docs() - set(shots.SHOTS)


def test_every_picture_the_script_takes_is_shown_and_on_disk():
    assert set(shots.SHOTS) <= shown_in_docs(), set(shots.SHOTS) - shown_in_docs()
    assert {p.stem for p in PICTURES.glob("*.png")} == set(shots.SHOTS), (
        "a picture on disk the script no longer takes, or one missing"
    )


def test_it_lists_each_picture_with_its_size(capsys):
    assert shots.main(["--list"]) == 0
    out = capsys.readouterr().out
    for name, shot in shots.SHOTS.items():
        assert re.search(rf"^{name}\s+{shot[1]} by {shot[2]}\s", out, re.M), name


def test_a_name_it_does_not_know_is_refused():
    with pytest.raises(SystemExit):
        shots.main(["settings"])


def gifs_shown_in_docs() -> set:
    names = set()
    for page in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
        names |= set(
            re.findall(r"images/dashboard/([a-z-]+)\.gif", page.read_text(encoding="utf-8"))
        )
    return names


def test_every_clip_the_docs_show_is_one_the_script_films():
    assert gifs_shown_in_docs() <= set(shots.CLIPS), gifs_shown_in_docs() - set(shots.CLIPS)


def test_every_clip_the_script_films_is_on_disk_and_has_its_steps():
    assert {p.stem for p in PICTURES.glob("*.gif")} == set(shots.CLIPS), (
        "a GIF on disk the script no longer films, or one missing"
    )
    assert set(shots.CLIP_STEPS) == set(shots.CLIPS)


def test_the_clip_that_adds_a_document_is_filmed_last():
    """Adding a document changes the sample and keeps the server busy, so nothing after it may show it."""
    assert list(shots.CLIPS)[-1] == "tour-ingest"


def test_it_lists_each_clip_with_its_size(capsys):
    assert shots.main(["--list"]) == 0
    out = capsys.readouterr().out
    for name, clip in shots.CLIPS.items():
        assert re.search(
            rf"^{name}\s+{clip[0]} by {clip[1]}\s.*a GIF {shots.GIF_WIDTH} wide$", out, re.M
        ), name


def test_the_policy_pdf_is_a_pdf_the_library_reads(tmp_path):
    pytest.importorskip("pypdfium2")
    from vectrixdb.extract.pdf_text import read_pdf

    path = tmp_path / "policy.pdf"
    path.write_bytes(shots.policy_pdf())
    pages = read_pdf(path).pages
    assert len(pages) == len(shots.POLICY_PAGES)
    assert "Travel and expenses policy" in pages[0] and "Meals and taxis" in pages[1]
