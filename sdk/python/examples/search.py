"""Search a collection and print each hit with its citation.

VECTRIXDB_URL=http://127.0.0.1:8000 VECTRIXDB_KEY=... python examples/search.py handbook "how long do refunds take?"
"""

import os
import sys

from vectrixdb import connect

collection = sys.argv[1] if len(sys.argv) > 1 else "handbook"
query = " ".join(sys.argv[2:]) or "how long do refunds take?"

with connect(
    os.environ.get("VECTRIXDB_URL", "http://127.0.0.1:8000"), key=os.environ.get("VECTRIXDB_KEY")
) as db:
    for hit in db.search(collection, query, limit=3):
        print(f"{hit.score:.3f}  {hit.citation}")
        print(f"       {' '.join(hit.text.split())}")
