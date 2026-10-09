"""The containers page's clips and the script that films them agree.

What is being held to: every clip the docs show from docs/images/containers
is one the script films and is on disk, and every clip it films is shown; the
image names it films come from docker/compose.yaml, so a new version there
needs nothing here; and what it prints as jq's output is what jq prints, with
the reply escaped before it is coloured. The script itself starts Docker and a
browser and is run by hand; nothing here starts either.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "container_shots", ROOT / "scripts" / "container_shots.py"
)
shots = importlib.util.module_from_spec(spec)
sys.modules["container_shots"] = shots
spec.loader.exec_module(shots)


def shown_in_docs() -> set:
    names = set()
    for page in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
        names |= set(
            re.findall(r"images/containers/([a-z-]+)\.gif", page.read_text(encoding="utf-8"))
        )
    return names


def test_every_clip_shown_is_one_the_script_films_and_every_one_is_shown():
    assert shown_in_docs() == set(shots.CLIPS)
    assert {p.stem for p in shots.OUT.glob("*.gif")} == set(shots.CLIPS), (
        "a GIF on disk the script no longer films, or one missing"
    )


def test_the_images_it_films_are_the_ones_the_compose_file_runs():
    compose = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    names = shots.published_names()
    assert set(names) == {"server", "extract"}
    assert f"${{VECTRIXDB_IMAGE:-{names['server']}}}" in compose
    assert f"${{VECTRIXDB_EXTRACT_IMAGE:-{names['extract']}}}" in compose


def test_a_reply_is_printed_as_jq_prints_it():
    reply = json.dumps({"doc_id": "a.png", "chunks": 1, "pages": [1, 2], "ok": True})
    assert shots.jq(reply, lambda r: {k: r[k] for k in ("doc_id", "pages")}) == (
        '{\n  "doc_id": "a.png",\n  "pages": [\n    1,\n    2\n  ]\n}'
    )
    assert shots.jq(reply, lambda r: r["ok"]) == "true"


def test_a_reply_is_escaped_then_coloured():
    shown = shots.coloured(
        '{\n  "text": "<b>4200</b> \\"due\\"",\n  "relevance": 0.87,\n  "ok": true\n}'
    )
    assert "<b>" not in shown and "&lt;b&gt;4200&lt;/b&gt;" in shown
    assert '<span class="k">&quot;text&quot;</span>:' in shown
    assert '<span class="n">0.87</span>' in shown and '<span class="n">true</span>' in shown
