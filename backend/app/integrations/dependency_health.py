"""Per-dependency health surfacing for the classification hot path (KTD18).

A rate-limited or degraded response from the LLM, Last.fm, or GetSongBPM
must be distinguishable from a genuine "not found" and surfaced through
health status (extending KTD17's never-blend-signals principle to these
three dependencies) rather than silently treated as a clean miss.
"""

from enum import Enum
from threading import Lock
from typing import Optional


class DependencyStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"


class _DependencyHealthStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._status: dict[str, tuple[DependencyStatus, Optional[str]]] = {}

    def set_status(self, name: str, status: DependencyStatus, reason: Optional[str] = None) -> None:
        with self._lock:
            self._status[name] = (status, reason)

    def get_status(self, name: str) -> tuple[DependencyStatus, Optional[str]]:
        with self._lock:
            return self._status.get(name, (DependencyStatus.OK, None))


dependency_health_store = _DependencyHealthStore()
