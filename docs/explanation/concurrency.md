# Threads, processes and what is safe to share

## One `Vectrix` across threads

A single `Vectrix` instance may be used from several threads at once. Three things make that safe:

- **SQLite connections are per thread.** Each thread gets its own connection to the collection's database, which is what SQLite's WAL mode is designed for. Concurrent readers never block each other; writers serialise on the file.
- **The vector index is behind a lock.** Adds, deletes and searches on the `usearch` index take a re-entrant lock, so an insert cannot interleave with a search.
- **ONNX Runtime sessions are thread-safe for inference.** Several threads may embed at the same time through one session.

What you gain from threads is overlap of I/O, not parallel embedding: the embedding step is CPU-bound Python calling into ONNX Runtime, and the interpreter lock limits it to roughly one core per process. For throughput on embedding, use processes.

`add()` from many threads is correct but not faster than one thread doing the same work in a batch. Prefer `db.add(texts)` with a list over a thread per text.

## Processes

Open a `Vectrix` per process. Never share one across `fork()`: the SQLite connections, the index handle and the ONNX session do not survive being copied into a child.

Several processes may **read** one collection at the same time; that is ordinary SQLite. Several processes must not **write** one collection at the same time: the ANN index is a file the process rewrites on save, so two writers race and the last one to save wins. Route writes through one process.

## Async

`AsyncVectrix` wraps the same collection for `async` code:

```python
import asyncio

from vectrixdb.aio import AsyncVectrix


async def main():
    async with AsyncVectrix("notes", path="./data") as db:
        await db.add(["the release is on Friday"])
        return await db.search("when do we ship")


hits = asyncio.run(main())
```

There is nothing genuinely awaitable underneath: embedding and search are
CPU-bound calls into ONNX Runtime and usearch. What the wrapper gives an
async application is that those calls run in a thread executor instead of
blocking the event loop, which is what you would otherwise write by hand.

## Closing

`Vectrix` is a context manager. `close()` saves the index, closes the database and, in graph mode, the knowledge-graph pipeline; it is safe to call twice, and any call after it raises `ConfigurationError` rather than operating on a half-closed store. Nothing relies on `__del__`.

```python
from vectrixdb import Vectrix

texts = ["the release is on Friday"]

with Vectrix("notes", path="./data") as db:
    db.add(texts)
```
