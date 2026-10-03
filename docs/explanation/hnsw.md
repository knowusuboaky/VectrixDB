# HNSW and its limits

!!! note "Updated in 2.2"
    `NativeHNSWIndex` was rewritten to batch every distance computation and to
    stop extending candidates during pruning, which took 320 vectors from about
    160s to a few seconds. Every figure on this page is measured after that
    rewrite.

VectrixDB contains two separate approximate-nearest-neighbour paths, and it
matters which one you are using.

## What actually indexes your vectors

`Collection` builds its index with [usearch](https://github.com/unum-cloud/usearch),
a compiled C++ implementation. That is the path taken by `Vectrix`, by
`VectrixDB.create_collection`, and by everything in the quick start. It is fast,
and none of what follows applies to it.

## The pure-Python index

`NativeHNSWIndex` is a from-scratch HNSW implementation in NumPy, exported as
public API:

```python
from vectrixdb import NativeHNSWIndex
```

Nothing inside VectrixDB uses it. It exists so the graph algorithm is readable
and dependency-free, and it is correct: `tests/unit/test_hnsw_recall.py` measures
its recall against brute-force ground truth and it saturates at small corpus
sizes.

It is, however, **too slow to use at realistic scale**, and you should reach for
`Collection` instead unless you are studying the algorithm.

### Measured build cost

On the reference machine, building an index of 16-dimensional vectors:

| Vectors | Build time |
| ------: | ---------: |
|      20 |     0.004s |
|      40 |     0.047s |
|      80 |     0.265s |
|     320 |      2.82s |
|   3,000 |      45.2s |

Measured after the 2.2 rewrite. Doubling from 40 to 80 costs about five and a
half times, and the last row is sixteen times the one before it for nine times
the corpus. Far better than the pre-rewrite figures this table used to carry,
which had 40 vectors at 2.7s and 80 at 15.3s, and still not the O(n log n) that
HNSW is supposed to give you.

### Why it is still slow

Profiling a 320-vector build after the rewrite:

```
  cumtime   ncalls  where
    2.564        1  index.py:373(add)
    1.688     9520  index.py:239(_select_neighbors_heuristic)
    0.570    21170  index.py:172(_distances_to)
    0.455      336  index.py:313(_search_layer)
    0.448    21170  distance.py:84(batch_cosine)
```

Distances are batched now, so `batch_cosine` is a fifth of a second across
21,170 calls rather than the dominant cost it used to be. What is left is the
Python around it: four million interpreter-level function calls to insert 320
vectors, almost all of them inside the neighbour-selection heuristic walking
candidate lists one at a time.

That is the ceiling for a pure-Python graph build. Getting past it means
compiled code, which is what usearch is.

## Choosing between them

| | `Collection` (usearch) | `NativeHNSWIndex` |
|---|---|---|
| Use for | everything | reading the algorithm |
| Scale | millions of vectors | fewer than ~100 |
| Implementation | compiled C++ | pure Python and NumPy |
| Recall | production quality | correct, verified by tests |
