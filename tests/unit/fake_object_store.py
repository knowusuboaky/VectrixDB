"""An in-memory object store with a retention lock, for the sink's tests.

It enforces what the real one enforces: an object under a lock cannot be
overwritten or deleted until the retention date, and a bucket with the lock
off accepts everything and keeps nothing safe. ``fail_next`` makes the next
N puts raise the way an unreachable store would.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence


class LockedObjectError(PermissionError):
    pass


class FakeObjectStore:
    def __init__(self, *, locked: bool = True, default_retain_days: int = 2555) -> None:
        self.locked = locked
        self.default_retain_days = default_retain_days
        self.objects: dict[str, tuple[bytes, Optional[datetime], Optional[str]]] = {}
        self.fail_next = 0
        self.puts: list[dict[str, Any]] = []

    def lock_configuration(self) -> Optional[Mapping[str, Any]]:
        if not self.locked:
            return None
        return {
            "ObjectLockEnabled": "Enabled",
            "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": self.default_retain_days}},
        }

    def put_locked(self, key: str, body: bytes, *, retain_until: datetime, mode: str) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("object store unreachable")
        existing = self.objects.get(key)
        if existing is not None and existing[1] and existing[1] > datetime.now(timezone.utc):
            raise LockedObjectError(f"{key} is under retention until {existing[1]}")
        self.puts.append(
            {"key": key, "retain_until": retain_until, "mode": mode, "size": len(body)}
        )
        self.objects[key] = (
            body,
            retain_until if self.locked else None,
            mode if self.locked else None,
        )

    def delete(self, key: str) -> None:
        """What a compromised credential would try."""
        body, retain_until, _ = self.objects[key]
        if retain_until and retain_until > datetime.now(timezone.utc):
            raise LockedObjectError(f"{key} is under retention until {retain_until}")
        del self.objects[key]

    def tamper(self, key: str, body: bytes) -> None:
        """Overwrite in place, bypassing the lock: a store that was not really
        locked, or a bug in one. Exists so the reader's hash check has
        something to catch."""
        self.objects[key] = (body, self.objects[key][1], self.objects[key][2])

    def list(self, prefix: str) -> Sequence[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get(self, key: str) -> bytes:
        return self.objects[key][0]
