"""How many threads one ONNX session uses, read once a process.

Every session asked for four threads, whatever the machine. On a container
held to one or two CPUs, four threads take turns on cores they do not have
and the runtime spins waiting for them, so a model call that takes 20 ms on
a laptop takes seconds under load. ``VECTRIXDB_THREADS`` says how many; left
out, it is the CPUs this process may use (the container's CPU quota when
there is one), at most four, so a laptop or a large machine runs as before.
"""

from __future__ import annotations

import functools
import math
import os
from pathlib import Path
from typing import Optional

__all__ = ["session_threads", "available_cpus"]

#: The most threads one session is given unless VECTRIXDB_THREADS says more.
CAP = 4


def _cgroup_quota(root: Path = Path("/sys/fs/cgroup")) -> Optional[float]:
    """The CPUs a cgroup lets this process use, or None when it sets no limit."""
    try:
        # cgroup v2: "max 100000" or "150000 100000"
        quota, period = (root / "cpu.max").read_text().split()[:2]
        if quota != "max" and float(period) > 0:
            return float(quota) / float(period)
        return None
    except (OSError, ValueError):
        pass
    try:
        # cgroup v1
        quota_v1 = float((root / "cpu" / "cpu.cfs_quota_us").read_text())
        period_v1 = float((root / "cpu" / "cpu.cfs_period_us").read_text())
        if quota_v1 > 0 and period_v1 > 0:
            return quota_v1 / period_v1
    except (OSError, ValueError):
        pass
    return None


def available_cpus(root: Path = Path("/sys/fs/cgroup")) -> int:
    """The CPUs this process may run on: the affinity mask, held to the cgroup quota."""
    try:
        cpus = len(os.sched_getaffinity(0))  # type: ignore[attr-defined,unused-ignore]
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    quota = _cgroup_quota(root)
    if quota is not None:
        cpus = min(cpus, max(1, math.ceil(quota)))
    return max(1, cpus)


@functools.lru_cache(maxsize=1)
def session_threads() -> int:
    """``VECTRIXDB_THREADS`` when set to a whole number above zero; else the CPUs available, at most four."""
    raw = os.environ.get("VECTRIXDB_THREADS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return min(CAP, available_cpus())
