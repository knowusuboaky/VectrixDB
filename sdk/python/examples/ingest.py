"""Add a file to a collection, creating the collection when it is not there.

VECTRIXDB_URL=http://127.0.0.1:8000 VECTRIXDB_KEY=... python examples/ingest.py handbook handbook.pdf
"""

import os
import sys
from pathlib import Path

from vectrixdb import connect
from vectrixdb.client import NotFoundError

collection, path = sys.argv[1], Path(sys.argv[2])

with connect(
    os.environ.get("VECTRIXDB_URL", "http://127.0.0.1:8000"), key=os.environ.get("VECTRIXDB_KEY")
) as db:
    try:
        db.describe(collection)
    except NotFoundError:
        db.create_collection(collection)
    added = db.add_document(collection, path, doc_id=path.name)
    print(f"{added.doc_id}: {added.chunks} chunks, cited as {', '.join(added.citations)}")
