"""How recent attempts went, per kind of work (emails sent, files stored), kept in memory for the status collector.

Each `record` is one attempt. Only the last few minutes matter, so each kind keeps a short ring of (time, worked). Recording
never raises and never waits, so it can sit on any hot path."""
from __future__ import annotations

import time
from collections import deque

WINDOW_SECONDS = 10 * 60
_rings: dict[str, deque] = {}


def record(kind: str, ok: bool) -> None:
    try:
        _rings.setdefault(kind, deque(maxlen=500)).append((time.time(), bool(ok)))
    except Exception:  # noqa: BLE001 - a counter must never break the work it counts
        pass


def recent(kind: str, seconds: int = WINDOW_SECONDS) -> tuple[int, int]:
    """(attempts, successes) in the last `seconds`."""
    cutoff = time.time() - seconds
    rows = [ok for at, ok in _rings.get(kind, ()) if at >= cutoff]
    return len(rows), sum(1 for ok in rows if ok)


def reset() -> None:
    _rings.clear()
