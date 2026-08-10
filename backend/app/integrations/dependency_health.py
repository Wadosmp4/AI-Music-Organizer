"""Per-dependency health surfacing for the classification hot path (KTD18).

A rate-limited or degraded response from the LLM or Last.fm must be
distinguishable from a genuine "not found" and surfaced through health
status (extending KTD17's never-blend-signals principle to these
dependencies) rather than silently treated as a clean miss.
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

    def failed_with_no_progress(self, name: str, made_progress: bool) -> bool:
        """R14/KTD2: distinguish a genuinely failed run from one that
        legitimately found/did nothing. A dependency is only DEGRADED here if
        a real call against it failed during this run -- a healthy run that
        simply has nothing to do leaves it OK, so that legitimate case still
        counts as success. A run that made some progress before a later
        failure also counts as success; only a run with zero progress at all
        alongside a DEGRADED signal is a genuine failure.
        """
        status, _ = self.get_status(name)
        return status == DependencyStatus.DEGRADED and not made_progress


dependency_health_store = _DependencyHealthStore()
