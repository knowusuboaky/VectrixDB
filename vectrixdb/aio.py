"""An async face for ``Vectrix``.

Embedding and search are CPU-bound calls into ONNX Runtime and usearch, so
there is nothing to await inside them; what an async application needs is for
those calls not to block the event loop. Each method here runs its
synchronous twin in the default thread executor and awaits it. The underlying
``Vectrix`` is safe to share across threads (see
``docs/explanation/concurrency.md``), so nothing is copied.

    async with AsyncVectrix("notes", path="./data") as db:
        await db.add(["first note"])
        hits = await db.search("note")
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, List, Optional, Union

from .easy import Results, Vectrix

__all__ = ["AsyncVectrix"]


# ============================================================================
# THE ASYNC FACE
# ============================================================================
#
# INPUT   the calls Vectrix takes
# OUTPUT  each run in a thread, so it does not block the event loop
#
# Embedding and search are CPU-bound calls into ONNX Runtime and usearch, so
# there is nothing to await inside them; what an async application needs is
# for those calls not to block.


class AsyncVectrix:
    """``Vectrix`` with every blocking call moved off the event loop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._sync: Optional[Vectrix] = None
        self._args = args
        self._kwargs = kwargs

    @classmethod
    def wrap(cls, db: Vectrix) -> "AsyncVectrix":
        """Wrap an already-open ``Vectrix``."""
        wrapper = cls()
        wrapper._sync = db
        return wrapper

    async def open(self) -> "AsyncVectrix":
        """Open the collection. Loading models can take seconds, so it is awaited too."""
        if self._sync is None:
            self._sync = await asyncio.to_thread(Vectrix, *self._args, **self._kwargs)
        return self

    @property
    def sync(self) -> Vectrix:
        """The underlying synchronous object, for anything not wrapped here."""
        if self._sync is None:
            raise RuntimeError("AsyncVectrix is not open; use `async with` or `await db.open()`")
        return self._sync

    async def _run(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(partial(getattr(self.sync, name), *args, **kwargs))

    async def add(self, texts: Union[str, List[str]], **kwargs: Any) -> "AsyncVectrix":
        await self._run("add", texts, **kwargs)
        return self

    async def search(self, query: str, **kwargs: Any) -> Results:
        return await self._run("search", query, **kwargs)

    async def embed(self, texts: Union[str, List[str]]) -> Any:
        return await self._run("embed", texts)

    async def get(self, ids: Union[str, List[str]]) -> Any:
        return await self._run("get", ids)

    async def delete(self, ids: Union[str, List[str]]) -> "AsyncVectrix":
        await self._run("delete", ids)
        return self

    async def count(self) -> int:
        return await self._run("count")

    async def remember(self, text: str, **kwargs: Any) -> str:
        return await self._run("remember", text, **kwargs)

    async def recall(self, query: str, **kwargs: Any) -> Results:
        return await self._run("recall", query, **kwargs)

    async def feedback(self, id: str, outcome: str, **kwargs: Any) -> Optional[str]:
        return await self._run("feedback", id, outcome, **kwargs)

    async def context(self, query: str, **kwargs: Any) -> Any:
        return await self._run("context", query, **kwargs)

    async def close(self) -> None:
        if self._sync is not None:
            await asyncio.to_thread(self._sync.close)

    async def __aenter__(self) -> "AsyncVectrix":
        return await self.open()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"AsyncVectrix({self._sync!r})" if self._sync else "AsyncVectrix(not open)"
