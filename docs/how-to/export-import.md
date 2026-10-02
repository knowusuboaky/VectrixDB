# Back up, move and restore a collection

A local collection is a handful of files under its path: the vector index, the SQLite stores for texts and metadata, and, in graph mode, the knowledge graph. `export()` packs all of them into one zip with a manifest; `import_snapshot()` unpacks it and opens it with the same mode and models.

## Export

```python
from vectrixdb import Vectrix

db = Vectrix("notes", path="./data")
db.add(["first note", "second note"])
snapshot = db.export("backups/notes.zip")
```

The collection is flushed first, so the zip holds committed state. The manifest records the name, mode, dense model, dimension, document count and the VectrixDB version.

```python
from vectrixdb.snapshot import read_manifest

read_manifest("backups/notes.zip")
```

## Import

```python
restored = Vectrix.import_snapshot("backups/notes.zip", path="./restored")
restored = Vectrix.import_snapshot("backups/notes.zip", path="./restored", name="notes-copy")
```

Mode and dense model come from the manifest; anything else you pass goes to `Vectrix` on top. Import refuses to overwrite a collection that already exists at the target, so a mistaken restore cannot clobber live data.

The format is deliberately plain: unzip the file and you have exactly what VectrixDB wrote, in the layout it expects.

## Rebuild the index

Deletions leave tombstones in an HNSW graph, and recall drifts as the graph is edited in place. A rebuild starts clean from the live vectors:

```python
db.rebuild_index()
```

The new index is built outside the lock, so searches keep running against the old one until the swap, which is atomic.

Deletions cost speed as well as recall, because a deleted vector stays in the graph and is filtered out after the search rather than skipped during it. Measured on 20,000 vectors: with a fifth of them deleted a query went from 1.0 ms to 18.0 ms. You do not have to watch for it. `Collection.tombstone_ratio` reports the share, and past a fifth `search()` says so on the result:

```python
results = db.search("volcanic rock")
if results.degraded:
    print(results.degraded)
    # 30% of this index is deleted vectors, which slows every search;
    # call rebuild_index() to compact it
```

## Open read-only, larger than memory

```python
db = Vectrix("notes", path="./data", readonly=True)
```

The index is memory-mapped instead of loaded, so opening is instant and a collection larger than RAM can still be searched. Any write raises `ConfigurationError`. Combine with `export()` to ship a built collection to machines that only query it.
