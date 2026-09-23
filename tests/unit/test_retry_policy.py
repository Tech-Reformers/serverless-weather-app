"""
Unit tests for RetryPolicy and CircuitBreaker.

asyncio.sleep is patched throughout so the tests run without real delays.

Validates: Requirements 1.5, 2.6, 3.5, 4.4, 5.5, 7.3
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.exceptions import ExternalServiceError, TimeoutError
from src.infrastructure.retry import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    RetryPolicy,
)


# ===========================================================================
# Helpers
# ===========================================================================


async def _ok(value=42):
    """Trivially successful async operation."""
    return value


def _fail_then_succeed(fail_times: int, error_factory=None):
    """Return an async callable that raises on the first *fail_times* calls,
    then succeeds with ``"ok"``."""
    if error_factory is None:
        error_factory = lambda: ExternalServiceError("transient")  # noqa: E731
    calls = [0]

    async def _op(*args, **kwargs):
        calls[0] += 1
        if calls[0] <= fail_times:
            raise error_factory()
        return "ok"

    return _op


def _always_fail(error_factory=None):
    if error_factory is None:
        error_factory = lambda: ExternalServiceError("permanent")  # noqa: E731

    async def _op(*args, **kwargs):
        raise error_factory()

    return _op


# ===========================================================================
# RetryPolicy — success paths
# ===========================================================================


class TestRetryPolicySuccess:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self):
        """No retries needed; result is returned immediately."""
        policy = RetryPolicy(max_attempts=3, base_delay=1.0)
        result = await policy.execute_with_retry(_ok)
        assert result == 42

    @pytest.mark.asyncio
    @patch("src.infrastructure.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_succeeds_on_second_attempt_external_service_error(self, mock_sleep):
        """Retries once on ExternalServiceError and returns success."""
        policy = RetryPolicy(max_attempts=3, base_delay=1.0)
        op = _fail_then_succeed(1, lambda: ExternalServiceError("boom"))

        result = await policy.execute_with_retry(op)

        assert result == "ok"
        mock_sleep.assert_awaited_once_with(1.0)  # base_delay * 2^0

    @pytest.mark.asyncio
    @patch("src.infrastructure.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_succeeds_on_second_attempt_timeout_error(self, mock_sleep):
        """Retries once on TimeoutError and returns success."""
        policy = RetryPolicy(max_attempts=3, base_delay=2.0)
        op = _fail_then_succeed(1, lambda: TimeoutError("timed out"))

        result = await policy.execute_with_retry(op)

        assert result == "ok"
        mock_sleep.assert_awaited_once_with(2.0)

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs_to_operation(self):
        """Arguments are forwarded to the wrapped operation unchanged."""
        received: list = []

        async def _capture(*args, **kwargs):
            received.extend(args)
            received.append(kwargs)
            return "captured"

        policy = RetryPolicy()
        result = await policy.execute_with_retry(_capture, "a", "b", key="val")

        assert result == "captured"
        assert received == ["a", "b", {"key": "val"}]


# ===========================================================================
# RetryPolicy — failure / exhaustion paths
# ===========================================================================


class TestRetryPolicyFailure:
    @pytest.mark.asyncio
    @patch("src.infrastructure.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_raises_last_exception_when_all_attempts_fail(self, mock_sleep):
        """Propagates the last exception once max_attempts is exhausted."""
        policy = RetryPolicy(max_attempts=3, base_delay=1.0)
        op = _always_fail(lambda: ExternalServiceError("permanent"))

        with pytest.raises(ExternalServiceError, match="permanent"):
            await policy.execute_with_retry(op)

    @pytest.mark.asyncio
    @patch("src.infrastructure.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_raises_timeout_error_when_all_attempts_time_out(self, mock_sleep):
        """TimeoutError propagates after exhausting all attempts."""
        policy = RetryPolicy(max_attempts=2, base_delay=0.5)
        op = _always_fail(lambda: TimeoutError("deadline exceeded"))

        with pytest.raises(TimeoutError):
            await policy.execute_with_retry(op)

    @pytest.mark.asyncio
    async def test_does_not_retry_non_transient_exceptions(self):
        """ValueError is not retried and propagates immediately."""
        calls = [0]

        async def _op():
            calls[0] += 1
            raise ValueError("bad input")

        policy = RetryPolicy(max_attempts=3)
        with pytest.raises(ValueError, match="bad input"):
            await policy.execute_with_retry(_op)

        assert calls[0] == 1  # no retries

    @pytest.mark.asyncio
    @patch("src.infrastructure.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_exponential_backoff_delays(self, mock_sleep):
        """Delays follow base_delay * 2^n (n = 0-indexed retry number)."""
        policy = RetryPolicy(max_attempts=4, base_delay=0.5)
        op = _always_fail()

        with pytest.raises(ExternalServiceError):
            await policy.execute_with_retry(op)

        # 3 sleeps for 4 attempts (no sleep after the last attempt)
        assert mock_sleep.await_count == 3
        delays = [call.args[0] for call in mock_sleep.await_args_list]
        assert delays == [0.5, 1.0, 2.0]  # 0.5*2^0, 0.5*2^1, 0.5*2^2

    @pytest.mark.asyncio
    async def test_single_attempt_no_sleep(self):
        """max_attempts=1 means no retry and no sleep."""
        with patch("src.infrastructure.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            policy = RetryPolicy(max_attempts=1, base_delay=1.0)
            op = _always_fail()
            with pytest.raises(ExternalServiceError):
                await policy.execute_with_retry(op)
            mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.infrastructure.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_two_attempts_one_sleep(self, mock_sleep):
        """max_attempts=2 should sleep exactly once (after attempt 1)."""
        policy = RetryPolicy(max_attempts=2, base_delay=1.0)
        op = _always_fail()
        with pytest.raises(ExternalServiceError):
            await policy.execute_with_retry(op)
        assert mock_sleep.await_count == 1
        mock_sleep.assert_awaited_once_with(1.0)


# ===========================================================================
# RetryPolicy — constructor validation
# ===========================================================================


class TestRetryPolicyValidation:
    def test_max_attempts_below_one_raises(self):
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=0)

    def test_negative_base_delay_raises(self):
        with pytest.raises(ValueError, match="base_delay"):
            RetryPolicy(base_delay=-0.1)

    def test_zero_base_delay_is_valid(self):
        """Zero base delay means immediate retries."""
        policy = RetryPolicy(max_attempts=2, base_delay=0.0)
        assert policy.base_delay == 0.0


# ===========================================================================
# CircuitBreaker — CLOSED state (normal operation)
# ===========================================================================


class TestCircuitBreakerClosed:
    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self):
        breaker = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=10.0)
        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_successful_call_returns_value(self):
        breaker = CircuitBreaker()
        result = await breaker.call(_ok)
        assert result == 42

    @pytest.mark.asyncio
    async def test_stays_closed_on_single_failure_below_threshold(self):
        breaker = CircuitBreaker(failure_threshold=3)
        op = _fail_then_succeed(1)
        with pytest.raises(ExternalServiceError):
            await breaker.call(op)
        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_opens_after_consecutive_failures_reach_threshold(self):
        breaker = CircuitBreaker(failure_threshold=3)
        op = _always_fail()

        for _ in range(3):
            with pytest.raises(ExternalServiceError):
                await breaker.call(op)

        assert breaker.state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_does_not_open_before_threshold(self):
        """Two failures with threshold=3 must not open the circuit."""
        breaker = CircuitBreaker(failure_threshold=3)
        op = _always_fail()

        for _ in range(2):
            with pytest.raises(ExternalServiceError):
                await breaker.call(op)

        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_resets_failure_count_after_success(self):
        """A success resets the consecutive-failure counter."""
        breaker = CircuitBreaker(failure_threshold=3)
        failing = _always_fail()
        success = _ok

        # 2 failures (threshold not yet reached)
        for _ in range(2):
            with pytest.raises(ExternalServiceError):
                await breaker.call(failing)

        # one success — counter should reset
        await breaker.call(success)

        # 2 more failures — circuit should still be CLOSED (count reset to 0)
        for _ in range(2):
            with pytest.raises(ExternalServiceError):
                await breaker.call(failing)

        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_forwards_args_to_operation(self):
        """Arguments are passed through to the wrapped callable."""
        received: list = []

        async def _op(*args, **kwargs):
            received.extend(args)
            received.append(kwargs)

        breaker = CircuitBreaker()
        await breaker.call(_op, 1, 2, x=3)
        assert received == [1, 2, {"x": 3}]


# ===========================================================================
# CircuitBreaker — OPEN state (failing fast)
# ===========================================================================


class TestCircuitBreakerOpen:
    @pytest.mark.asyncio
    async def test_open_circuit_rejects_without_calling_operation(self):
        """Once OPEN, the operation is never invoked."""
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=60.0)
        call_count = [0]

        async def _op():
            call_count[0] += 1
            raise ExternalServiceError("fail")

        # Trip the breaker
        with pytest.raises(ExternalServiceError):
            await breaker.call(_op)

        assert breaker.state is CircuitState.OPEN
        call_count[0] = 0  # reset counter

        # Next call should be rejected immediately
        with pytest.raises(CircuitBreakerOpenError):
            await breaker.call(_op)

        assert call_count[0] == 0  # operation was never called

    @pytest.mark.asyncio
    async def test_open_state_error_is_subclass_of_external_service_error(self):
        """`CircuitBreakerOpenError` IS-A `ExternalServiceError` for easy catch."""
        assert issubclass(CircuitBreakerOpenError, ExternalServiceError)

    @pytest.mark.asyncio
    async def test_open_circuit_stays_open_before_timeout(self):
        """Circuit stays OPEN while reset_timeout has not elapsed."""
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=9999.0)
        with pytest.raises(ExternalServiceError):
            await breaker.call(_always_fail())
        assert breaker.state is CircuitState.OPEN

        with pytest.raises(CircuitBreakerOpenError):
            await breaker.call(_ok)

        assert breaker.state is CircuitState.OPEN


# ===========================================================================
# CircuitBreaker — HALF_OPEN state (recovery probe)
# ===========================================================================


class TestCircuitBreakerHalfOpen:
    @pytest.mark.asyncio
    async def test_transitions_to_half_open_after_timeout(self):
        """After the reset timeout, the circuit moves to HALF_OPEN."""
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.001)
        with pytest.raises(ExternalServiceError):
            await breaker.call(_always_fail())
        assert breaker.state is CircuitState.OPEN

        # Simulate elapsed time by backdating _opened_at
        breaker._opened_at = time.monotonic() - 1.0

        # Any call triggers the OPEN → HALF_OPEN transition check
        # We call something that succeeds so we can inspect state mid-flow
        await breaker.call(_ok)
        # After success, state should now be CLOSED
        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_success_closes_circuit(self):
        """A successful test call in HALF_OPEN closes the circuit."""
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.001)
        with pytest.raises(ExternalServiceError):
            await breaker.call(_always_fail())

        # Force into HALF_OPEN
        breaker._opened_at = time.monotonic() - 1.0

        result = await breaker.call(_ok)
        assert result == 42
        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self):
        """A failed test call in HALF_OPEN re-opens the circuit."""
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.001)
        with pytest.raises(ExternalServiceError):
            await breaker.call(_always_fail())

        # Force into HALF_OPEN
        breaker._opened_at = time.monotonic() - 1.0
        # Trigger the OPEN → HALF_OPEN transition
        breaker._state = CircuitState.HALF_OPEN

        with pytest.raises(ExternalServiceError):
            await breaker.call(_always_fail())

        assert breaker.state is CircuitState.OPEN


# ===========================================================================
# CircuitBreaker — constructor validation
# ===========================================================================


class TestCircuitBreakerValidation:
    def test_failure_threshold_below_one_raises(self):
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreaker(failure_threshold=0)

    def test_reset_timeout_zero_raises(self):
        with pytest.raises(ValueError, match="reset_timeout_seconds"):
            CircuitBreaker(reset_timeout_seconds=0)

    def test_reset_timeout_negative_raises(self):
        with pytest.raises(ValueError, match="reset_timeout_seconds"):
            CircuitBreaker(reset_timeout_seconds=-1.0)


# ===========================================================================
# Integration: RetryPolicy + CircuitBreaker together
# ===========================================================================


class TestRetryPolicyWithCircuitBreaker:
    @pytest.mark.asyncio
    @patch("src.infrastructure.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_wrapping_circuit_breaker(self, mock_sleep):
        """RetryPolicy can wrap a CircuitBreaker.call for combined behaviour."""
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60.0)
        policy = RetryPolicy(max_attempts=2, base_delay=0.0)

        call_count = [0]

        async def _op():
            call_count[0] += 1
            raise ExternalServiceError("service down")

        with pytest.raises(ExternalServiceError):
            await policy.execute_with_retry(breaker.call, _op)

        # 2 attempts through the circuit breaker
        assert call_count[0] == 2
        # Only 1 failure recorded in the breaker per policy attempt
        # (breaker threshold=5, not yet open)
        assert breaker.state is CircuitState.CLOSED
