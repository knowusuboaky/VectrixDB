# Memory per vector

Measured by `scripts/measure_memory.py` on 5,000 random 384-dimensional
float32 vectors (Windows, Python 3.13.14,
NumPy 2.3.4). Regenerate the page with the script; the numbers
are not typed by hand.

## On disk, per vector

| Store | Bytes per vector |
| --- | ---: |
| ANN index (`usearch`, float32) | 1,684 |
| SQLite (ids, text, metadata, timestamps) | 2,234 |
| Total | 3,918 |

The raw vector is 1,536 bytes. What the index adds is the graph: neighbour
lists for every node.

The SQLite figure is mostly a second copy of the vector, in the local
document store (the `documents` table in the collection-level `.db`),
beside the ids, text, metadata and timestamps. That column used to hold
the vector as JSON, at about 5.7 times what float32 costs; it is a
float32 blob now, which is where most of the drop from the figure this
page used to carry has come from.

So budget about 2.6x the raw vector size on disk for a local
collection: 3,918 bytes against the 1,536 the vector itself is.

## Quantisers, per vector

The collection index stores float32 today. The quantisers ship as standalone
classes (`vectrixdb.core.quantization`) for callers who store or transmit
vectors themselves; this is what each costs after `fit()` and `encode()`.

| Quantiser | Bytes per vector | Compression |
| --- | ---: | ---: |
| float32 (none) | 1,536 | 1.0x |
| scalar 8-bit | 384 | 4.0x |
| binary 1-bit | 48 | 32.0x |
| product (8 subvectors, 256 clusters) | 8 | 192.0x |

Recall under each quantiser is measured separately in
`tests/unit/test_quantization_accuracy.py`; smaller is not free.
