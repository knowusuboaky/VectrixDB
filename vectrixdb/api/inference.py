"""The server's model work, kept off the event loop and in line.

An ``async`` route that runs the embedding model itself holds the event loop
for as long as the model runs: one large upload, and every other request
waits, ``/health`` among them, until an orchestrator decides the server is
dead and restarts it, which loads the models again and starts the spiral
over. So the model runs in a worker thread, and no more than
``VECTRIXDB_INFERENCE_CONCURRENCY`` texts batches at a time run at once.
An upload is embedded a batch at a time, each batch queuing on its own, so
a search that arrives during a long upload waits for one batch, never the
whole document.

A request that has waited ``VECTRIXDB_INFERENCE_WAIT_SECONDS`` for its turn
is answered 503 with ``Retry-After``: a caller told to come back is better
than a queue that grows until the process runs out of memory.

The models themselves are warmed by :func:`warm`, which the server's
lifespan starts in a thread when ``VECTRIXDB_WARM`` is on; ``/ready`` answers
from :func:`readiness` until then.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

__all__ = [
    "BATCH",
    "Busy",
    "concurrency",
    "embed",
    "embed_blocking",
    "in_line",
    "run_in_line",
    "readiness",
    "start_warming",
    "wait_seconds",
    "warm",
    "warm_enabled",
]

# ============================================================================
# SETTINGS: how many at once, how long to wait, how big a batch
# ============================================================================
#
# Read once a process, from the environment, so a running server cannot be
# changed under the requests it is answering.

#: Texts a model call takes at a time. The embedders batch by the same size.
BATCH = 32
_ON = ("1", "true", "yes", "on")


def concurrency() -> int:
    """How many model calls run at once: ``VECTRIXDB_INFERENCE_CONCURRENCY``, 2 when unset."""
    return _positive_int("VECTRIXDB_INFERENCE_CONCURRENCY", 2)


def wait_seconds() -> float:
    """How long a request waits for its turn before a 503: ``VECTRIXDB_INFERENCE_WAIT_SECONDS``, 30."""
    raw = os.environ.get("VECTRIXDB_INFERENCE_WAIT_SECONDS", "").strip()
    try:
        value = float(raw) if raw else 30.0
    except ValueError:
        value = 30.0
    return value if value > 0 else 30.0


def warm_enabled() -> bool:
    """Whether the models load at start: ``VECTRIXDB_WARM``. ``vectrixdb serve`` and the image turn it on."""
    return os.environ.get("VECTRIXDB_WARM", "").strip().lower() in _ON


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return value if value > 0 else default


# ============================================================================
# THE LINE: one semaphore a process, a batch at a time
# ============================================================================
#
# INPUT   texts
# OUTPUT  their vectors, from a worker thread; Busy when the wait ran out
#
# A threading semaphore rather than an asyncio one, because the turn is
# taken in the worker thread: the event loop never waits on it.


class Busy(Exception):
    """Every model slot stayed taken for the whole wait."""

    def __init__(self, waited: float) -> None:
        self.waited = waited
        super().__init__(
            f"The server is busy embedding other requests and this one waited {waited:.0f} seconds "
            "for its turn. Try again shortly."
        )


_slots_lock = threading.Lock()
_slots: Optional[threading.BoundedSemaphore] = None
_slots_size = 0


def _turns() -> threading.BoundedSemaphore:
    global _slots, _slots_size
    with _slots_lock:
        wanted = concurrency()
        if _slots is None or _slots_size != wanted:
            _slots, _slots_size = threading.BoundedSemaphore(wanted), wanted
        return _slots


def embed_blocking(texts: Sequence[str], embedder: Any = None) -> np.ndarray:
    """Embed ``texts`` in this thread, a batch at a time, each batch waiting its turn. Raises Busy."""
    from .server import get_text_embedder

    model = embedder if embedder is not None else get_text_embedder()
    texts = list(texts)
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    turns, limit = _turns(), wait_seconds()
    parts: List[np.ndarray] = []
    for start in range(0, len(texts), BATCH):
        began = time.monotonic()
        if not turns.acquire(timeout=limit):
            raise Busy(time.monotonic() - began)
        try:
            parts.append(np.asarray(model.embed(texts[start : start + BATCH])))
        finally:
            turns.release()
    return parts[0] if len(parts) == 1 else np.vstack(parts)


def in_line(work: Any) -> Any:
    """Run ``work()`` in this thread once a model slot is free: for a reranker, or any other model call. Raises Busy."""
    turns, limit = _turns(), wait_seconds()
    began = time.monotonic()
    if not turns.acquire(timeout=limit):
        raise Busy(time.monotonic() - began)
    try:
        return work()
    finally:
        turns.release()


async def run_in_line(work: Any) -> Any:
    """``work()`` in a worker thread, once a model slot is free. A wait that runs out is a 503 with Retry-After."""
    from fastapi import HTTPException
    from starlette.concurrency import run_in_threadpool

    try:
        return await run_in_threadpool(in_line, work)
    except Busy as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "5"}) from exc


async def embed(texts: Sequence[str], embedder: Any = None) -> np.ndarray:
    """Embed ``texts`` in a worker thread, in line. A wait that runs out is a 503 with Retry-After."""
    from fastapi import HTTPException
    from starlette.concurrency import run_in_threadpool

    try:
        return await run_in_threadpool(embed_blocking, list(texts), embedder)
    except Busy as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "5"}) from exc


# ============================================================================
# WARM AND READY
# ============================================================================
#
# INPUT   the server starting
# OUTPUT  the default models loaded and run once; what /ready answers
#
# Warmed in a thread so the server answers /health while it loads, and an
# orchestrator can tell "alive" from "ready".

_state_lock = threading.Lock()
_state: Dict[str, Any] = {"warming": False, "warmed": False, "error": None, "seconds": None}


def warm() -> None:
    """Load the default embedding model and run it once. Records the outcome for ``/ready``."""
    with _state_lock:
        _state.update(warming=True, error=None)
    started = time.perf_counter()
    try:
        embed_blocking(["ready"])
    except Exception as exc:  # noqa: BLE001 - /ready says what went wrong
        with _state_lock:
            _state.update(warming=False, warmed=False, error=f"{type(exc).__name__}: {exc}")
        return
    with _state_lock:
        _state.update(
            warming=False,
            warmed=True,
            seconds=round(time.perf_counter() - started, 2),
        )


def start_warming() -> threading.Thread:
    """Warm in a daemon thread, so the server takes requests meanwhile."""
    with _state_lock:
        _state.update(warming=True, warmed=False, error=None)
    thread = threading.Thread(target=warm, name="vectrixdb-warm", daemon=True)
    thread.start()
    return thread


def readiness() -> Dict[str, Any]:
    """What ``/ready`` says: ready or not, and why.

    With warming off the models load on first use, so the server is ready
    once it is up; with it on, once the default model has answered.
    """
    with _state_lock:
        state = dict(_state)
    if state["error"]:
        return {"ready": False, "models": "failed", "error": state["error"]}
    if state["warming"]:
        return {"ready": False, "models": "loading"}
    if state["warmed"]:
        return {"ready": True, "models": "loaded", "seconds": state["seconds"]}
    return {"ready": True, "models": "on first use"}


def _reset_for_tests() -> None:
    global _slots, _slots_size
    with _slots_lock:
        _slots, _slots_size = None, 0
    with _state_lock:
        _state.update(warming=False, warmed=False, error=None, seconds=None)
