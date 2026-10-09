# Use it from async code

An `async` application, a FastAPI route, a chat bot or an agent loop, must not
hold its event loop while a model embeds a question. VectrixDB has two async
faces, one for each place a collection can be:

| The collection is | Use | What it does |
| --- | --- | --- |
| In your process, on this machine or on a store your process opens | `vectrixdb.aio.AsyncVectrix` | Runs each call of `Vectrix` in a worker thread and awaits it |
| On a VectrixDB server | `vectrixdb.connect_async` | Sends each call to the server over `httpx`'s async client |

Plain `Vectrix` and `vectrixdb.connect` are the right choice everywhere else:
a script, a notebook, a batch job, a worker that handles one event at a time.

## A collection in your process

```python
import asyncio

from vectrixdb.aio import AsyncVectrix


async def main():
    async with AsyncVectrix("notes", path="./data") as db:
        await db.add(["the release is on Friday", "the office is closed on Monday"])
        found = await db.search("when do we ship", limit=1)
        print(found.top.text, await db.count())


asyncio.run(main())
```

`AsyncVectrix` takes the arguments `Vectrix` takes. Opening it loads the models,
which can take seconds, so that is awaited too: `async with`, or
`await db.open()` and `await db.close()`.

There is nothing to await inside embedding and search: they are calls into ONNX
Runtime and the vector index that keep a CPU busy. What the wrapper gives you is
that they run in the default thread executor, through `asyncio.to_thread`, and
the event loop keeps serving other requests meanwhile. One `Vectrix` is safe to
use from several threads at once, so nothing is copied.

| Awaitable | The `Vectrix` call it runs |
| --- | --- |
| `add(texts, **options)` | `add`; returns the wrapper, as `add` returns the collection |
| `search(query, **options)` | `search`, with every option in [Search options](search-options.md) |
| `embed(texts)` | `embed` |
| `get(ids)`, `delete(ids)`, `count()` | `get`, `delete`, `count` |
| `remember(text, **options)`, `recall(query, **options)`, `feedback(id, outcome, **options)`, `context(query, **options)` | The conversation memory calls; see [Keep conversation memory](conversation-memory.md) |
| `open()`, `close()` | Opening and closing the collection |

Several searches at once run in several threads:

```python
import asyncio

from vectrixdb.aio import AsyncVectrix


async def ask_all(questions):
    async with AsyncVectrix("notes", path="./data") as db:
        return await asyncio.gather(*(db.search(q, limit=3) for q in questions))


answers = asyncio.run(ask_all(["when do we ship", "is the office open"]))
print([a.top.text for a in answers])
```

Threads overlap waiting more than they overlap embedding, since the model work
keeps the CPU busy. For more throughput on embedding, run more processes; see
[Threads and processes](../explanation/concurrency.md).

### A collection you already opened, and calls not wrapped

`AsyncVectrix.wrap(db)` wraps a `Vectrix` you opened yourself. `db.sync` is the
`Vectrix` underneath, for a call the wrapper does not have; run it in a thread
the same way:

```python
import asyncio

from vectrixdb import Vectrix
from vectrixdb.aio import AsyncVectrix

local = Vectrix("notes", path="./data")
db = AsyncVectrix.wrap(local)

LEAVE = "# Leave\n\nParental leave is eighteen weeks on full pay, taken in one block or two.\n"


async def main():
    await asyncio.to_thread(db.sync.add_document, LEAVE, doc_id="leave.md")
    return await db.search("parental leave", limit=1)


print(asyncio.run(main()).top.text)
local.close()
```

`await db.close()` on a wrapper closes the `Vectrix` underneath too. Closing
twice is safe.

## A collection on a server

```python
import asyncio
import os

import vectrixdb


async def main():
    async with vectrixdb.connect_async("https://vectors.company.com", key=os.environ["VECTRIXDB_KEY"]) as client:
        db = client.collection("handbook")
        found = await db.search("how long do refunds take", limit=5)
        for result in found:
            print(result.relevance, result.readable_citation, result.text)


asyncio.run(main())
```

`connect_async` takes what `connect` takes and gives the same calls as
coroutines: every call in [Call a server from your code](clients.md#every-call)
is awaited. It needs the `client` extra. `async with`, or `await client.aclose()`,
closes its connections. With `collection=` it gives that collection at once,
and `db.client` is the client to close.

A token for it may be a string, a function that returns one, or an `async`
function that returns one, called before each request, so a token from an
async identity library needs no wrapper:

```python
import asyncio

import vectrixdb


async def fresh_token():
    return "the access token your identity library returns"


async def main():
    async with vectrixdb.connect_async("https://vectors.company.com", token=fresh_token) as client:
        print(await client.whoami())


asyncio.run(main())
```

A busy server's `429` and `503`, and a dropped connection, are tried again with
`asyncio.sleep` between tries, so a wait never holds the event loop. Refusals
are the same exceptions as the blocking client raises: see
[When the server says no](clients.md#when-the-server-says-no).

## Which to use

| Situation | Use |
| --- | --- |
| A script, a notebook, a nightly job | `Vectrix` |
| An async web app with the collection on its own disk or store | `AsyncVectrix` |
| An async web app with a VectrixDB server to call | `connect_async` |
| A program that calls a server one request at a time | `connect` |
| An `async` route in your own FastAPI app that already opened a `Vectrix` | `AsyncVectrix.wrap(db)` |
