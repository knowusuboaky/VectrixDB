# Writing past RAM

Until 2.2 the rule was: a collection you write to lives in memory, and a collection you only search can be memory-mapped and exceed it. This page is the second half of that rule going away, and the measurements that decided what it costs.

```python
from vectrixdb import Vectrix

texts = [f"paper number {i}" for i in range(1_000)]

db = Vectrix("papers", path="./data", shard_size=500_000)
db.add(texts)          # seals a shard to disk every 500,000 vectors
```

`shard_size` defaults to `None`, which is the single in-memory index every collection has always had. Nothing changes unless you set it.

## Why writes were in memory

usearch builds an HNSW graph by inserting one vector at a time, and every insertion walks the existing graph from the top layer down, touching neighbours at random. That walk is what makes the index fast to query and hostile to disk: the neighbours of a new vector are anywhere in the file, and a page fault per hop turns a microsecond insertion into a millisecond one. usearch therefore keeps the whole graph in RAM while it is mutable.

The consequence was the ceiling in [Memory per vector](memory.md). At 384 dimensions in float32, with links, a vector costs about 1.6 KB of RAM, the 1,684 bytes that page measures, so a 16 GB machine builds roughly ten million before it swaps.

## Sealed shards

The invariant stays, and the mutable graph gets small.

A collection is a list of shards. Exactly one, the head, is open for writing and holds at most `shard_size` vectors. When it fills it is sealed: written to disk and reopened as a memory-mapped view, which the operating system pages in and out as searches touch it. A new empty head takes over. Sealed shards never change, so nothing but the head is ever held in memory by the library.

The head is the same file an unsharded collection writes, and sealed shards sit beside it as `name.s0.usearch`, `name.s1.usearch` and so on. A collection that never fills a shard is byte for byte what it was, and an existing collection can be reopened with `shard_size` set without converting anything.

The four operations:

- **add** goes to the head, splitting a batch across shards so none exceeds the size.
- **search** asks every shard and merges by distance. Keys are the collection's own ids, unique across shards, so no remapping is needed.
- **delete** works as it always did. A deleted id leaves the collection's map and is filtered out of results, whichever shard its vector sits in.
- **re-adding an id** puts the new vector in the head while the old copy stays in a sealed shard that cannot be edited. Shards are read newest first and the first copy of a key wins, so the head shadows what came before it.

`rebuild_index()` is also the compaction: it writes fresh shards holding only the live vectors, so deletions and shadowed copies are dropped and the shards are packed full again. It stages the new files under a temporary name and only then releases the old mappings and renames, because a memory-mapped file cannot be replaced on Windows while it is mapped.

## What it costs

`scripts/shard_bench.py` builds the same vectors as one index and as several sharded ones and reports latency and recall against exact search, so a faster number cannot hide a worse answer. These rows are 20,000 random vectors of 128 dimensions, 200 queries, k=10, at the collection's default search width (`ef_search=100`), on one laptop CPU, on 19 September 2026; the raw figures are the `benchmarks/shards_*.json` files. Random Gaussian vectors have no cluster structure, which is the hardest case for HNSW and the reason the single index's recall is low.

| Shards | Vectors each | Query p50 | Query p99 | recall@10 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 20,000 | 0.80 ms | 1.40 ms | 0.638 |
| 10 | 2,000 | 2.66 ms | 4.55 ms | 0.976 |
| 50 | 400 | 4.86 ms | 6.65 ms | 0.982 |
| 100 | 200 | 7.05 ms | 9.92 ms | 0.985 |

Three things to read off this, and the first one corrects what this page claimed while it was still a design.

**Fan-out is not free, and it is not linear either.** The design said a hundred shards would cost about what one big index costs, because HNSW search is logarithmic in graph size. That was wrong: ten shards cost 3.3 times one shard and a hundred cost 8.9. It is sublinear, which is why a hundred shards is usable at all, but it is a real multiple and not a rounding error.

**Fan-out buys recall.** Every shard is searched independently, so the union examines far more candidates than one traversal of one graph does. That is why the sharded rows score 0.98 and up where the single index scores 0.64. Comparing them directly flatters sharding, so compare at matched recall: raising the single index's search width to `ef_search=400` lifts it to 0.948 at 1.90 ms, against 0.976 at 2.66 ms for ten shards. **At about the same accuracy, ten shards cost about 1.4 times one index.**

**Deletions cost more than sharding does.** With a fifth of the documents deleted, the single index goes from 0.80 ms to 17.1 ms and ten shards from 2.66 ms to 27.7 ms. That is the tombstone over-fetch, which both paths have: a search asks for as many extra results as there are deleted ids, so that deleted neighbours cannot crowd out live ones. It is the strongest argument for calling `rebuild_index()` after a large delete.

Reopened from disk, which maps the sealed shards again and loads a single index from its file, the numbers barely move: 0.71 ms at 0.634 recall for one index and 2.79 ms at 0.980 for ten shards. The files are small and freshly written, so the operating system still had them cached; a collection larger than RAM, paged in from disk, pays for its page faults in a way this run does not measure. (An earlier version of this page reported the reopened single index at 0.457 recall. Measured again at the default width, reopening leaves recall where it was.)

## Choosing a shard size

Pick the largest shard that comfortably fits in memory, which keeps the shard count small, because the cost is in the number of shards and not their size. A million vectors a shard is about 2 GB at 384 dimensions. Ten shards of that is ten million vectors on a machine that could never have built them as one graph, at about 1.4 times the query latency of an index that fits, at matched recall.

If your collection fits in RAM, leave `shard_size` alone. This is for the case where it does not.

## What is deliberately not here

Distributing shards across machines. The shards are files in one directory on one machine and the fan-out is a loop, not a network call. Replication and cross-machine sharding stay on the not-planned list in the [roadmap](https://github.com/knowusuboaky/VectrixDB/blob/main/ROADMAP.md), for the reason given there: they are server concerns, and VectrixDB is a library.

A disk-native graph such as DiskANN or SPANN. Those are better designs for a billion vectors on one SSD, and they are a different index, a different build pipeline and a different set of quality trade-offs. Sealed shards get a collection past RAM with the index that is already here and already measured.
