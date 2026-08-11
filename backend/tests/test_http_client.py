import pytest

from app.integrations.http_client import CircuitBreaker, call_with_retry


class _CountedFailure(Exception):
    pass


def test_call_with_retry_retries_on_matching_exception():
    calls = {"count": 0}

    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise _CountedFailure("transient")
        return "ok"

    assert call_with_retry(flaky, retries=3, delay_s=0) == "ok"
    assert calls["count"] == 3


def test_call_with_retry_non_retryable_raises_immediately_without_retry():
    """A quota-exceeded error is the motivating case: retrying it three
    times with a real network call each time just burns more of the same
    already-exhausted quota. `non_retryable` short-circuits that even
    though the exception still matches the default `retry_on=(Exception,)`."""
    calls = {"count": 0}

    def always_fails():
        calls["count"] += 1
        raise _CountedFailure("not retryable")

    with pytest.raises(_CountedFailure):
        call_with_retry(always_fails, retries=3, delay_s=0, non_retryable=(_CountedFailure,))

    assert calls["count"] == 1


def test_call_with_retry_non_retryable_skips_circuit_breaker_bookkeeping():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=60)

    def always_fails():
        raise _CountedFailure("not retryable")

    with pytest.raises(_CountedFailure):
        call_with_retry(
            always_fails,
            retries=3,
            delay_s=0,
            non_retryable=(_CountedFailure,),
            circuit_breaker=breaker,
        )

    # A non-retryable failure isn't evidence the dependency itself is
    # unhealthy, so it must not trip the circuit breaker -- the next call
    # should still be allowed through rather than short-circuited.
    breaker.before_call()
