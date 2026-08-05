"""Two independently surfaced auth health states (KTD17, extends KTD6).

The write path (cookie auth, used for playlist reads/writes) and the
detection path (OAuth, used to detect new likes) fail independently and
must never be blended into one signal — a stalled detection path and a
degraded write path need distinguishable reconnect actions.
"""

from enum import Enum
from threading import Lock
from typing import Optional


class AuthStatus(str, Enum):
    OK = "ok"
    NEEDS_RECONNECT = "needs_reconnect"


class _AuthStatusStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._write_status = AuthStatus.OK
        self._write_reason: Optional[str] = None
        self._detection_status = AuthStatus.OK
        self._detection_reason: Optional[str] = None

    def set_write_status(self, status: AuthStatus, reason: Optional[str] = None) -> None:
        with self._lock:
            self._write_status = status
            self._write_reason = reason

    def get_write_status(self) -> tuple[AuthStatus, Optional[str]]:
        with self._lock:
            return self._write_status, self._write_reason

    def set_detection_status(self, status: AuthStatus, reason: Optional[str] = None) -> None:
        with self._lock:
            self._detection_status = status
            self._detection_reason = reason

    def get_detection_status(self) -> tuple[AuthStatus, Optional[str]]:
        with self._lock:
            return self._detection_status, self._detection_reason


auth_status_store = _AuthStatusStore()
