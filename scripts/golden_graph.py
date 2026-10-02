"""Record what the graph extractor produces on a fixed corpus.

    python scripts/golden_graph.py            # compare with the recorded fixture
    python scripts/golden_graph.py --update   # record the current output

Uses the regex extractor (spaCy forced off) so the result is the same on every
machine. A change in entity resolution or extraction shows up as a diff here
before anyone notices retrieval getting quietly worse.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================================
# SETTINGS: the fixture, the corpus, the optional libraries
# ============================================================================
#
# The fixture the graph is recorded in, the fixed corpus it is built from, and
# the community libraries that may be installed but are never relied on.

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "golden_graph.json"

CORPUS = [
    "Marie Curie discovered radium in Paris.",
    "Pierre Curie worked with Marie Curie on radioactivity.",
    "Radium is a radioactive element used in early medicine.",
    "The Nobel Prize was awarded for research on radioactivity.",
    "Radioactivity research in Paris led to the Nobel Prize.",
    "Curie Institute continues cancer research in France.",
]


#: Community detection uses leidenalg and igraph when they are installed, else
#: networkx, else connected components, and each finds a different number of
#: communities on the same graph. The last needs nothing installed, so it is
#: the one pinned here: hiding the other two keeps the fixture the same on a
#: machine that has them and in a CI job that does not.
OPTIONAL_COMMUNITY_LIBRARIES = ("igraph", "leidenalg", "networkx")


# ============================================================================
# BUILDING THE GRAPH
# ============================================================================
#
# INPUT   the corpus
# OUTPUT  what the regex extractor makes of it, spaCy forced off
#
# The same on every machine, so a change in entity resolution or extraction
# shows up as a diff here before anyone notices retrieval getting quietly
# worse.


def build() -> dict:
    saved = {name: sys.modules.get(name) for name in OPTIONAL_COMMUNITY_LIBRARIES}
    for name in OPTIONAL_COMMUNITY_LIBRARIES:
        sys.modules[name] = None  # type: ignore[assignment]  # an import of it now fails
    try:
        return _build()
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _build() -> dict:
    from vectrixdb.core.graphrag import ExtractorType, GraphRAGConfig, create_pipeline

    with tempfile.TemporaryDirectory() as tmp:
        config = GraphRAGConfig(enabled=True, extractor=ExtractorType.NLP)
        pipeline = create_pipeline(config=config, path=Path(tmp) / "g")
        pipeline.extractor._nlp = None  # regex fallback, deterministic everywhere
        pipeline.add_documents(CORPUS)
        graph = pipeline.graph
        entities = sorted(e.name for e in graph.nodes.values())
        edges = sorted(
            f"{graph.nodes[r.source_id].name} -> {graph.nodes[r.target_id].name}"
            for r in graph.edges.values()
            if r.source_id in graph.nodes and r.target_id in graph.nodes
        )
        communities = pipeline.hierarchy.total_communities if pipeline.hierarchy else 0
        pipeline.close()
    return {"entities": entities, "edges": edges, "communities": communities}


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --update
# OUTPUT  a diff against the recorded fixture, or the fixture rewritten
#
# Compare by default; record only when told.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args(argv)

    now = build()
    if args.update:
        FIXTURE.write_text(json.dumps(now, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {FIXTURE}: {len(now['entities'])} entities, {len(now['edges'])} edges")
        return 0
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key in ("entities", "edges", "communities"):
        if now[key] != recorded[key]:
            print(f"{key} differ:\n  now:      {now[key]}\n  recorded: {recorded[key]}")
            return 1
    print("golden graph matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
