"""Shared timeout/retry/circuit-breaking wrapper (KTD24).

Used by every external integration (YouTube Music, the Data API, the LLM
provider, Last.fm) instead of each rolling its own retry loop — KTD18's
per-dependency isolation in the classification hot path is one application
of this wrapper, not a separate implementation.
"""

import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised when a dependency has failed enough times that calls are short-circuited."""


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    reset_timeout_s: float = 60.0
    _failures: int = field(default=0, init=False, repr=False)
    _opened_at: float | None = field(default=None, init=False, repr=False)

    def before_call(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at < self.reset_timeout_s:
            raise CircuitOpenError("circuit open — dependency failed too recently")
        # Reset window elapsed: allow a trial call through.
        self._opened_at = None
        self._failures = 0

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()


def call_with_retry(
    fn: Callable[[], T],
    *,
    retries: int = 3,
    delay_s: float = 1.0,
    retry_on: tuple[type[Exception], ...] = (Exception,),
    circuit_breaker: CircuitBreaker | None = None,
) -> T:
    """Retries `fn` on exceptions matching `retry_on`, honoring an optional circuit breaker.

    `retry_on` should name specific transient exception types for each
    integration (network errors, rate-limit responses) rather than the
    legacy script's blind retry-on-any-exception.
    """
    if circuit_breaker is not None:
        circuit_breaker.before_call()

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            result = fn()
        except retry_on as exc:
            last_exc = exc
            if circuit_breaker is not None:
                circuit_breaker.record_failure()
            if attempt < retries:
                time.sleep(delay_s)
            continue
        else:
            if circuit_breaker is not None:
                circuit_breaker.record_success()
            return result

    assert last_exc is not None
    raise last_exc
