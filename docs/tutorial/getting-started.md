# Getting started

By the end of this you will have indexed a small set of documents, searched them
four different ways, and persisted them to disk. It takes about ten minutes.

## Install

```bash
pip install vectrixdb
```

Nothing else. The embedding model ships inside the package, so there is no API
key to obtain and no service to start.

## Your first search

```python
from vectrixdb import Vectrix

db = Vectrix("tutorial")
db.add(
    [
        "The mitochondria is the powerhouse of the cell",
        "Python was released in 1991 by Guido van Rossum",
        "The Pacific is the largest and deepest ocean",
        "Rust guarantees memory safety without a garbage collector",
    ]
)

results = db.search("programming languages")
print(results.top.text)
```

The query shares no words with any of the documents, so whatever comes back was
matched on meaning rather than on text.

## Look at more than the top hit

```python
for result in db.search("programming languages", limit=3):
    print(f"{result.score:.3f}  {result.text}")
```

Scores are similarities, so higher is closer. Expect a clear gap between the two
programming sentences and the biology one.

## Attach metadata, then filter on it

Text alone is rarely enough; you usually need to know where a passage came from.

```python
db = Vectrix("tutorial_meta")
db.add(
    ["Revenue grew 12% in Q3", "Headcount fell to 410 in Q3", "Revenue grew 4% in Q2"],
    metadata=[
        {"quarter": "Q3", "topic": "finance"},
        {"quarter": "Q3", "topic": "people"},
        {"quarter": "Q2", "topic": "finance"},
    ],
)

for r in db.search("how did revenue do", limit=5, filter={"quarter": "Q3"}):
    print(r.text, r.metadata)
```

The Q2 revenue line is excluded before the results are handed back. The search
widens its candidate window until it has filled your limit or run out of index,
so a selective filter costs time rather than results.

## Try the other search modes

A mode is chosen when you build the instance, not per query, because each one
loads different models:

```python
from vectrixdb import Vectrix

hybrid = Vectrix("tutorial_hybrid", mode="hybrid")   # meaning + exact terms
hybrid.add(["Q3 revenue rose 12 percent", "Headcount fell in Q3"])
hybrid.search("Q3 revenue")
```

`mode="dense"` is the default and needs no argument. `mode="ultimate"` adds
late interaction on top of hybrid. Asking a dense instance for a hybrid search
raises rather than quietly giving you a dense one.

`hybrid` is the one to reach for when your text mixes prose with identifiers such
as `Q3`, error codes or SKUs, which dense embeddings handle poorly.
[Search modes](../explanation/search-modes.md) explains why.

## Where it lives

Everything above was already on disk. With no `path`, a collection is created
under `./vectrixdb_data` in the working directory, so a second run of this
tutorial reopens what the first one wrote. Name a path to put it somewhere you
choose:

```python
db = Vectrix("tutorial", path="./vectrix_data")
db.add(["Durable across restarts"])

# ... later, in a new process
db = Vectrix("tutorial", path="./vectrix_data")
print(db.count())
```

The default backend is SQLite in WAL mode, so committed writes survive an abrupt
shutdown, and several threads can read and write at once.

## Handle failure

Wrap calls that touch storage. One exception type covers the library:

```python
from vectrixdb import VectrixError

try:
    results = db.search("anything")
except VectrixError as exc:
    print("search unavailable:", exc)
```

A document that does not exist gives you an empty list from `get()`, or `None`
from `get_one()`, which takes a single id. A database you cannot reach raises.
Those are different things and VectrixDB keeps them different, which is why you
can trust an empty result to mean "nothing matched".

## Where next

- [Handle errors](../how-to/handle-errors.md) for the full exception hierarchy
- [Choose a storage backend](../how-to/storage-backends.md) to move off SQLite
- [Run the REST API](../how-to/rest-api.md) to query over HTTP
