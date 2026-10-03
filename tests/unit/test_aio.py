"""AsyncVectrix keeps the event loop free while the sync calls run."""

import pytest
import asyncio
import time

from vectrixdb.aio import AsyncVectrix


def test_async_context_manager_round_trip(tmp_path):
    async def go():
        async with AsyncVectrix("a", path=str(tmp_path)) as db:
            await db.add(["alpha beta", "gamma delta"])
            assert await db.count() == 2
            hits = await db.search("alpha", limit=1)
            assert hits.top.text == "alpha beta"
            mid = await db.remember("the cat is Miso", session="s")
            assert mid.startswith("mem_")
            block = await db.context("cat", session="s", token_budget=200)
            assert "Miso" in block.text
        return True

    assert asyncio.run(go())


@pytest.mark.slow
def test_the_loop_is_not_blocked_while_embedding(tmp_path):
    """A ticker keeps running while a large add is in flight."""
    ticks = []

    async def ticker(stop):
        while not stop.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.01)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(ticker(stop))
        async with AsyncVectrix("b", path=str(tmp_path)) as db:
            # Forty real embeddings take a few seconds, long enough for the
            # ticker to prove the loop was free; 120 took 45 s.
            await db.add([f"note number {i} on topic {i % 5}" for i in range(40)])
        stop.set()
        await task

    asyncio.run(go())
    assert len(ticks) > 5, "the event loop was starved during add()"


def test_wrap_an_existing_vectrix(tmp_path):
    from vectrixdb import Vectrix

    sync = Vectrix("c", path=str(tmp_path))
    sync.add(["x"])

    async def go():
        db = AsyncVectrix.wrap(sync)
        assert await db.count() == 1
        assert db.sync is sync

    asyncio.run(go())
