"""The graph built from a fixed corpus matches the recorded fixture exactly.

Entity resolution and extraction are heuristics; a tweak to a threshold can
merge two people or drop an edge without any other test noticing. This pins
the output on one corpus, using the regex extractor so it is the same on every
machine. Update deliberately with scripts/golden_graph.py --update.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "golden_graph.json"
SCRIPT = ROOT / "scripts" / "golden_graph.py"


def test_graph_matches_the_fixture():
    spec = importlib.util.spec_from_file_location("golden_graph", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["golden_graph"] = module
    spec.loader.exec_module(module)

    assert FIXTURE.exists(), "run python scripts/golden_graph.py --update"
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    now = module.build()
    assert now["entities"] == recorded["entities"]
    assert now["edges"] == recorded["edges"]
    assert now["communities"] == recorded["communities"]
    assert "Marie Curie" in now["entities"] and "Pierre Curie" in now["entities"]
