# ============================================================================
# A CIRCUIT BREAKER A DEPENDENCY
# ============================================================================
#
# INPUT   what the reads of a file said about the extraction app: reached, or
#         not; a clock
# OUTPUT  whether the next message should be tried at all, one small state
#         file every instance reads, and the state in words for the page
#
# When the extraction app is down, every message would fail five times and
# the poison queue would fill with files that were fine. After a run of
# failures the breaker opens: messages wait instead of being tried, one
# probe a window goes through, and a probe that reads closes it. No Azure
# import, so it is tested against stand-ins like ingest_queue.py beside it.
#
# Author: Kwadwo Daddy Nyame Owusu - Boakye

"""A circuit breaker for the extraction app, kept where every instance reads it.

One small JSON file, ``ingestion/state/<name>.json``. Closed, every message
is tried. After ``threshold`` reads in a row that could not reach the app,
it opens for ``open_for`` seconds: ``allow()`` says no and the worker puts
the message back with a delay instead of burning a try. When the window
has passed, one probe is let through; a probe that reads closes the breaker
and clears the count, a probe that fails opens it for another window. The
Ingest page reads ``status()``: paused since when, until when, why.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional

from collections_of import CONTAINER

__all__ = ["Breaker", "STATE", "is_transient", "state_files"]

#: The folder beside raw/, markdown/, chunks/ and failed/: one JSON state a dependency.
STATE = "state"
_TRANSIENT = ("could not be reached", "answered 408", "answered 429", "answered 502", "answered 503", "answered 504", "timed out", "timeout", "connection")


def is_transient(error: Any) -> bool:
    """Whether a read failed because the app could not be reached or was busy, rather than because of the file."""
    low = str(error or "").lower()
    return any(word in low for word in _TRANSIENT)


def state_files(blobs: Any) -> Any:
    from vectrixdb.documents import BlobFiles

    return BlobFiles(blobs, CONTAINER, prefix=STATE)


def _when(seconds: Optional[float]) -> Optional[str]:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds)) if seconds else None


class Breaker:
    """Closed, open, or letting one probe through: read and written as one file, so every instance sees the same."""

    def __init__(self, files: Any, name: str, *, threshold: int = 5, open_for: float = 300.0, clock: Callable[[], float] = time.time) -> None:
        self.files = files
        self.name = name
        self.threshold = max(1, int(threshold))
        self.open_for = max(1.0, float(open_for))
        self.clock = clock
        self._rel = f"{name.replace(' ', '-')}.json"

    def _read(self) -> Dict[str, Any]:
        try:
            if not self.files.exists(self._rel):
                return {}
            state = json.loads(self.files.read(self._rel).decode("utf-8"))
            return state if isinstance(state, dict) else {}
        except (FileNotFoundError, ValueError):
            return {}

    def _write(self, state: Dict[str, Any]) -> None:
        self.files.write(self._rel, json.dumps(state).encode("utf-8"))

    def allow(self) -> bool:
        """Whether to try the next message: yes while closed, no while open, and one probe once the window has passed."""
        state = self._read()
        opened = state.get("opened_at")
        if not opened:
            return True
        now = self.clock()
        probe_at = state.get("probe_at")
        if probe_at is None and now >= float(opened) + self.open_for:
            state["probe_at"] = now
            self._write(state)
            return True
        if probe_at is not None and now >= float(probe_at) + self.open_for:
            # The probe never reported back: an instance died with it. Let another through.
            state["probe_at"] = now
            self._write(state)
            return True
        return False

    def record_failure(self, error: Any) -> Dict[str, Any]:
        """A read that could not reach the app. The count grows; at the threshold the breaker opens; a failed probe opens it again."""
        state = self._read()
        now = self.clock()
        state["failures"] = int(state.get("failures") or 0) + 1
        state["last_error"] = str(error or "")[:300]
        state["last_failure_at"] = now
        if state.get("probe_at") is not None:
            state["opened_at"] = now
            state.pop("probe_at", None)
        elif not state.get("opened_at") and state["failures"] >= self.threshold:
            state["opened_at"] = now
        self._write(state)
        return state

    def record_success(self) -> bool:
        """A read got through: closed, and the count starts again. True when there was something to clear."""
        state = self._read()
        if not state:
            return False
        try:
            self.files.delete(self._rel)
        except FileNotFoundError:
            pass
        return True

    def status(self) -> Dict[str, Any]:
        """For the page: paused or not, since when, the next try, how many failures in a row, and the last error."""
        state = self._read()
        opened = state.get("opened_at")
        probe = state.get("probe_at")
        until = (float(probe) if probe is not None else float(opened)) + self.open_for if opened else None
        paused = bool(opened)
        said = (f"the {self.name} has not answered since {_when(opened)}, {int(state.get('failures') or 0)} reads in a row; the next try is at {_when(until)}"
                if paused else f"the {self.name} is answering")
        return {
            "dependency": self.name,
            "paused": paused,
            "probing": bool(probe is not None),
            "since": _when(opened),
            "until": _when(until),
            "failures": int(state.get("failures") or 0),
            "last_error": state.get("last_error"),
            "said": said,
        }
