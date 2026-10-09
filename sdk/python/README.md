# The Python client

The Python client is part of the `vectrixdb` package, not a separate one:

```bash
pip install "vectrixdb[client]"
```

```python
from vectrixdb import connect

db = connect("https://vectors.example.com", key="...")
for hit in db.search("handbook", "how long do refunds take", limit=3):
    print(f"{hit.score:.2f} [{hit.citation}] {hit.text}")
```

`search` returns the same `Results` a local `Vectrix` does. The code lives in
`vectrixdb/client.py`; its tests, including the conformance walk from
`sdk/CONTRACT.md`, are `tests/unit/test_client.py`. This folder holds the
examples the other languages have too:

- `examples/search.py`: search a collection and print each hit's citation
- `examples/ingest.py`: add a file, creating the collection first if needed
- `examples/signin_token.py`: search as a signed-in person, with their token

Each reads `VECTRIXDB_URL` and `VECTRIXDB_KEY` (or `VECTRIXDB_TOKEN`).
