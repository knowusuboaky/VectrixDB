"""GraphRAG over a book: entities, relations, communities, then questions.

Run it:  python examples/graphrag_over_a_book.py [path-or-url-to-a-text]

With no argument it uses a short built-in excerpt so it runs offline in a
few seconds. Give it a plain-text book (a Project Gutenberg file works:
python examples/graphrag_over_a_book.py https://www.gutenberg.org/cache/epub/1342/pg1342.txt)
and it chunks it, extracts a knowledge graph, and answers questions that
need more than one passage. Extraction uses the bundled rule-based
extractor; install the nlp extra for spaCy, or pass an LLM extractor.
"""

import sys
from pathlib import Path

from vectrixdb import Vectrix

EXCERPT = """
Marie Curie was born in Warsaw in 1867 and moved to Paris to study physics.
In Paris, Marie Curie met Pierre Curie, and they married in 1895.
Marie Curie and Pierre Curie discovered polonium and radium in 1898.
Henri Becquerel shared the Nobel Prize in Physics with Marie Curie and Pierre Curie in 1903.
After Pierre Curie died in 1906, Marie Curie took his chair at the Sorbonne.
Marie Curie won a second Nobel Prize, in Chemistry, in 1911.
Irene Joliot-Curie, the daughter of Marie Curie, won the Nobel Prize in Chemistry in 1935.
"""


def load_text(arg: str | None) -> str:
    if not arg:
        return EXCERPT
    if arg.startswith("http"):
        import urllib.request

        with urllib.request.urlopen(arg, timeout=60) as resp:  # noqa: S310 - user-supplied
            return resp.read().decode("utf-8", errors="replace")
    return Path(arg).read_text(encoding="utf-8", errors="replace")


text = load_text(sys.argv[1] if len(sys.argv) > 1 else None)

db = Vectrix("book", path="./example_data", mode="graph")
if db.count() == 0:
    n = db.add_document(text, chunk="sentence", chunk_size=400, overlap=0, doc_id="book")
    print(f"Indexed {n} chunks.")

for question in (
    "Who shared the 1903 Nobel Prize with the Curies?",
    "What did Marie Curie do after Pierre died?",
    "Which prizes did the Curie family win, and when?",
):
    results = db.search(question, limit=3, explain=True)
    print(f"\nQ: {question}")
    for r in results:
        boost = (r.explain or {}).get("graph_boost")
        tag = f" (graph +{boost:.2f})" if boost else ""
        print(f"  {r.score:.3f}{tag}  {r.text.strip()[:110]}")
    if results.degraded:
        print(f"  note: {results.degraded}")
