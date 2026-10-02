"""Who signed in, who read what, and who was turned away.

The audit trail answers "why did this search return what it did". This log
answers the question that comes first in an incident: who was looking. One
line of JSON per event, appended, never rewritten.

What a line holds is kept deliberately small. It names the person, the
action, the collection, and the one chunk or document that was opened when
it was one: its id, which is what an investigator asks for. It never holds a
query, a chunk's text or the ids of what was returned, for the reason the
audit page gives: a log that repeats the data it guards is a second copy of
the data, in a place with weaker rules.

A read that cannot be recorded does not happen. If the line cannot be
written the request is refused, because a log that is quietly empty for the
hour that matters is worse than a server that says it is unwell.

``VECTRIXDB_ACCESS_LOG=stdout`` writes each line to the server's output
instead of a file, for a platform that collects it, and for one whose disk is
read only, where a file would refuse every read. Each line then also carries
``"log": "vectrixdb.access"``, so a collector can pick them out. The
dashboard cannot list a log it does not hold, and says where it went.

A Blob address, ``https://<account>.blob.core.windows.net/<container>/<prefix>``,
keeps the lines in one append blob a UTC day, in a container whose
immutability policy lets a line be added and never changed: every server
behind a load balancer writes to the same log, and the dashboard reads it
back. See :mod:`vectrixdb.append_log`.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..exceptions import VectrixError

__all__ = [
    "EVENTS",
    "AccessLog",
    "AccessLogUnavailable",
]


# ============================================================================
# SETTINGS: the events
# ============================================================================
#
# The whole vocabulary a line may name, so a reader of the log knows it.

#: The events, so that a reader of the log knows the whole vocabulary.
EVENTS = (
    "signin",
    "signin_failed",
    "break_glass_used",
    "signout",
    "locked",
    "enrolled",
    "recovery_code_used",
    "recovery_codes_made",
    "authenticator_reset",
    "authenticator_replaced",
    "authenticator_removed",
    "passkey_added",
    "passkey_removed",
    "password_set",
    "step_up",
    "sessions_ended",
    "person_added",
    "person_changed",
    "person_removed",
    "key_created",
    "key_revoked",
    "policy_changed",
    "access_checked",
    "read",
    "search",
    "write",
    "denied",
)


# ============================================================================
# UNAVAILABLE, AND A SHARE
# ============================================================================
#
# INPUT   a log that could not be written; values and a share
# OUTPUT  the refusal; the value the share is at or under, by nearest rank
#
# A read that cannot be recorded is refused, since a log with holes is not a
# log.


class AccessLogUnavailable(VectrixError):
    """The access log could not be written, so the request it would have recorded was refused."""


def _at(values: list, share: float) -> Optional[float]:
    """The value that ``share`` of these are at or under, by nearest rank. None for none."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int(share * len(ordered) + 0.999999) - 1))], 1)


# ============================================================================
# THE LOG
# ============================================================================
#
# INPUT   who signed in, who read what, who was turned away
# OUTPUT  one JSON line an event, appended, never rewritten; listed with a
#         find box and a page; summarised for the dashboard
#
# It never holds a query, a chunk's text or the ids of what was returned: a
# log that repeats the data it guards is a second copy of the data, in a place
# with weaker rules.


class AccessLog:
    def __init__(self, path: Any) -> None:
        from ..append_log import AppendLog, is_blob_address, open_append_log

        #: True when the log goes to the server's output rather than a file.
        self.to_stdout = str(path).strip().lower() in ("stdout", "-")
        #: The append blobs the lines go to, when the log is kept in Blob; None for a file or the output.
        self.store: Any = None
        if not self.to_stdout and (isinstance(path, AppendLog) and not isinstance(path, (str, Path)) or is_blob_address(path)):
            self.store = open_append_log(path, setting="VECTRIXDB_ACCESS_LOG")
        self.path = Path(str(path)) if self.store is None else None
        self._lock = threading.Lock()
        if not self.to_stdout and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def where(self) -> str:
        if self.to_stdout:
            return "stdout"
        return self.store.describe() if self.store is not None else str(self.path)

    @property
    def kind(self) -> str:
        """stdout, file or blob: what the dashboard is told, since a log sent to the output cannot be listed."""
        return "stdout" if self.to_stdout else "blob" if self.store is not None else "file"

    def _lines(self, since: Optional[float] = None) -> list:
        """Every line kept, oldest first. ``since`` lets a Blob log skip the days wholly before it."""
        if self.store is not None:
            return self.store.lines(since)
        if self.path is None or not self.path.exists():
            return []
        with self._lock:
            return self.path.read_text(encoding="utf-8").splitlines()

    def record(
        self,
        event: str,
        *,
        who: Optional[str] = None,
        role: Optional[str] = None,
        method: Optional[str] = None,
        action: Optional[str] = None,
        collection: Optional[str] = None,
        item: Optional[str] = None,
        route: Optional[str] = None,
        status: Optional[int] = None,
        returned: Optional[int] = None,
        subject: Optional[str] = None,
        reason: Optional[str] = None,
        by: Optional[str] = None,
        address: Optional[str] = None,
        took_ms: Optional[float] = None,
    ) -> dict:
        if event not in EVENTS:
            raise ValueError(f"{event!r} is not an access event")
        line = {
            "access_id": "acc_" + uuid.uuid4().hex[:16],
            "at": time.time(),
            "event": event,
            "who": who,
            "role": role,
            "method": method,
            "action": action,
            "collection": collection,
            "item": item,
            "route": route,
            "status": status,
            "returned": returned,
            "subject": subject,
            "reason": reason,
            "by": by,
            "address": address,
            # How long the server took over a search, in milliseconds. It says
            # nothing about what was asked, and it is what a trend is drawn from.
            "took_ms": round(float(took_ms), 1) if took_ms is not None else None,
        }
        line = {k: v for k, v in line.items() if v is not None}
        try:
            if self.to_stdout:
                with self._lock:
                    sys.stdout.write(json.dumps({"log": "vectrixdb.access", **line}, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
            elif self.store is not None:
                self.store.append(json.dumps(line, separators=(",", ":")))
            else:
                assert self.path is not None
                with self._lock, self.path.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(line, separators=(",", ":")) + "\n")
        except (OSError, ValueError) as exc:
            raise AccessLogUnavailable(f"the access log at {self.where} could not be written: {exc}") from exc
        except Exception as exc:  # a Blob store's own error, whatever it is: the read is refused, as for a file
            if self.store is None:
                raise
            raise AccessLogUnavailable(f"the access log at {self.where} could not be written: {type(exc).__name__}: {exc}") from exc
        return line

    def daily(self, days: int = 14, *, now: Optional[float] = None) -> Optional[dict]:
        """What happened on each of the last ``days`` days, today last, counted from the log.

        Four counts a day: searches; sign-ins; refusals, which are failed
        sign-ins and requests a role did not allow; and denials, which are
        searches a collection's policy turned away, the ``denied`` lines that
        carry the policy's reason. Days are UTC, so the same log reads the
        same wherever the server and the reader are. None when the log goes
        to the server's output, where it cannot be read back. A line that
        does not parse, or has no time, is skipped.
        """
        if self.to_stdout:
            return None
        days = max(1, min(int(days), 90))
        today = int((time.time() if now is None else now) // 86400)
        first = today - days + 1
        counts = {name: [0] * days for name in ("searches", "signins", "refused", "denied")}
        kinds = {"search": "searches", "signin": "signins", "signin_failed": "refused", "denied": "refused"}
        took: list = [[] for _ in range(days)]
        lines = self._lines(since=first * 86400)
        if lines:
            for raw in lines:
                try:
                    line = json.loads(raw)
                    day = int(float(line["at"]) // 86400)
                except (ValueError, KeyError, TypeError):
                    continue
                name = kinds.get(line.get("event"))
                if name == "refused" and line.get("event") == "denied" and line.get("reason"):
                    name = "denied"  # a collection's policy said no; a role's no carries no reason
                if name is not None and first <= day <= today:
                    counts[name][day - first] += 1
                    if name == "searches" and isinstance(line.get("took_ms"), (int, float)):
                        took[day - first].append(float(line["took_ms"]))
        return {
            "days": [time.strftime("%Y-%m-%d", time.gmtime(d * 86400)) for d in range(first, today + 1)],
            **counts,
            # None on a day with no timed search: a line written before searches were timed has no time in it.
            "search_ms_median": [_at(values, 0.5) for values in took],
            "search_ms_p95": [_at(values, 0.95) for values in took],
        }

    def readers(self, days: int = 14, top: int = 6, *, now: Optional[float] = None) -> Optional[list[dict]]:
        """Who searched and read most in the last ``days`` days, the busiest first.

        One row a caller: a person by their address, a key as ``key:name``,
        and everybody who came in without signing in as ``guest``. Searches
        and reads of text are counted apart, since a search returns excerpts
        and a read is somebody asking for a chunk or a whole document. Names
        only, and counts: the log holds no query to show. None when the log
        goes to the server's output.
        """
        if self.to_stdout:
            return None
        days = max(1, min(int(days), 90))
        today = int((time.time() if now is None else now) // 86400)
        first = today - days + 1
        counted: dict[str, dict] = {}
        lines = self._lines(since=first * 86400)
        if lines:
            for raw in lines:
                try:
                    line = json.loads(raw)
                    day = int(float(line["at"]) // 86400)
                except (ValueError, KeyError, TypeError):
                    continue
                event, who = line.get("event"), line.get("who")
                if event not in ("search", "read") or not who or not first <= day <= today:
                    continue
                row = counted.setdefault(str(who), {"who": str(who), "key": line.get("method") == "key", "searches": 0, "reads": 0})
                row["searches" if event == "search" else "reads"] += 1
        busiest = sorted(counted.values(), key=lambda r: (-(r["searches"] + r["reads"]), r["who"]))
        return busiest[: max(1, int(top))]

    def listing(self, limit: int = 10, offset: int = 0, *, q: Optional[str] = None, event: Optional[str] = None, who: Optional[str] = None) -> tuple[list[dict], int]:
        """A page of the newest lines first, and how many match in all.

        ``q`` is a find: a line matches when the text is somewhere in what it
        says, its who, event, role, collection or anything else it names,
        case aside. ``event`` and ``who`` are exact, as :meth:`recent` takes
        them. Nothing when the log goes to stdout.
        """
        if self.to_stdout:
            return [], 0
        needle = (q or "").strip().lower()
        found: list[dict] = []
        for raw in reversed(self._lines()):
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if event and line.get("event") != event:
                continue
            if who and line.get("who") != who:
                continue
            if needle and needle not in " ".join(str(v) for k, v in line.items() if k != "at").lower():
                continue
            found.append(line)
        offset = max(0, int(offset))
        return found[offset : offset + max(1, int(limit))], len(found)

    def recent(self, limit: int = 200, *, event: Optional[str] = None, who: Optional[str] = None) -> list[dict]:
        """The newest lines first. A line that does not parse is skipped, not fatal. Nothing when it goes to stdout.

        A Blob log is read a week back first, and all the way back only when
        that week holds fewer than ``limit`` lines.
        """
        if self.to_stdout:
            return []
        lines = self._lines(since=time.time() - 7 * 86400) if self.store is not None else self._lines()
        if self.store is not None and len(lines) < limit:
            lines = self._lines()
        out: list[dict] = []
        for raw in reversed(lines):
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if event and line.get("event") != event:
                continue
            if who and line.get("who") != who:
                continue
            out.append(line)
            if len(out) >= limit:
                break
        return out
