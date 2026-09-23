"""
Reusable retry and circuit-breaker utilities for external-API calls.

Design-level specification (design.md §Retry Strategy):

    RetryPolicy
    -----------
    Wraps any async *operation* callable and re-executes it with exponential
    backoff whenever an ExternalServiceError or TimeoutError is raised.  If
    all attempts are exhausted the last exception is propagated to the caller.

    CircuitBreaker
    --------------
    Implements the classic three-state circuit-breaker pattern to prevent
    cascading failures when an external dependency is repeatedly unavailable:

        CLOSED   — normal operation; every call is forwarded to the operation.
        OPEN     — failing fast; calls are rejected immediately with
                   CircuitBreakerOpenError without invoking the operation.
        HALF_OPEN — recovery probe; a single test call is allowed through.
                   * Success  → circuit closes.
                   * Failure  → circuit re-opens with a fresh timeout.

    The circuit opens after *failure_threshold* consecutive failures and
    re-enters HALF_OPEN after *reset_timeout_seconds* have elapsed.

Usage example::

    from src.infrastructure.retry import RetryPolicy, CircuitBreaker

    policy  = RetryPolicy(max_attempts=3, base_delay=1.0)
    breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60.0)

    result = await policy.execute_with_retry(
        breaker.call, my_external_operation, arg1, arg2
    )
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum, auto
from typing import Any, Awaitable, Callable, TypeVar

from src.domain.exceptions import ExternalServiceError, TimeoutError

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------


class RetryPolicy:
    """Execute an async operation with configurable exponential-backoff retry.

    Only :class:`~src.domain.exceptions.ExternalServiceError` and
    :class:`~src.domain.exceptions.TimeoutError` are considered retryable;
    all other exceptions propagate immediately.

    Args:
        max_attempts: Total number of invocation attempts (default 3).
            Must be ≥ 1.
        base_delay: Initial sleep duration (seconds) before the second
            attempt.  Subsequent delays are ``base_delay * 2^n`` where *n*
            is the zero-based retry index (default 1.0).

    Example::

        policy = RetryPolicy(max_attempts=3, base_delay=0.5)
        result = await policy.execute_with_retry(fetch_weather, location)
    """

    def __init__(self, max_attempts: int = 3, base_delay: float = 1.0) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        if base_delay < 0:
            raise ValueError(f"base_delay must be >= 0, got {base_delay}")
        self.max_attempts = max_attempts
        self.base_delay = base_delay

    async def execute_with_retry(
        self,
        operation: Callable[..., Awaitable[T]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Invoke *operation* with retry on transient failures.

        Args:
            operation: An async callable to execute.
            *args: Positional arguments forwarded to *operation*.
            **kwargs: Keyword arguments forwarded to *operation*.

        Returns:
            The return value of the first successful *operation* call.

        Raises:
            ExternalServiceError | TimeoutError: When all attempts are
                exhausted, the last caught exception is re-raised.
            Any other exception: Propagated immediately without retrying.
        """
        last_exc: Exception = ExternalServiceError("No attempts made")

        for attempt in range(self.max_attempts):
            try:
                return await operation(*args, **kwargs)
            except (ExternalServiceError, TimeoutError) as exc:
                last_exc = exc
                if attempt < self.max_attempts - 1:
                    delay = self.base_delay * (2 ** attempt)
                    logger.warning(
                        "Attempt %d/%d failed: %s. Retrying in %.2fs.",
                        attempt + 1,
                        self.max_attempts,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "All %d attempts exhausted. Last error: %s",
                        self.max_attempts,
                        exc,
                    )

        raise last_exc


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreakerOpenError(ExternalServiceError):
    """Raised when a call is attempted while the circuit is OPEN.

    Callers should treat this the same as ExternalServiceError — the
    underlying dependency is (assumed to be) unavailable.
    """


class CircuitBreaker:
    """Three-state circuit breaker for external API calls.

    State transitions:

        CLOSED  ──(consecutive failures == failure_threshold)──► OPEN
        OPEN    ──(reset_timeout_seconds elapsed)───────────────► HALF_OPEN
        HALF_OPEN ──(test call succeeds)────────────────────────► CLOSED
        HALF_OPEN ──(test call fails)───────────────────────────► OPEN

    Args:
        failure_threshold: Number of consecutive failures required to open
            the circuit (default 5).
        reset_timeout_seconds: Seconds to wait in OPEN state before
            transitioning to HALF_OPEN (default 60.0).

    Example::

        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60.0)
        result  = await breaker.call(fetch_weather, location)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 60.0,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")
        if reset_timeout_seconds <= 0:
            raise ValueError(
                f"reset_timeout_seconds must be > 0, got {reset_timeout_seconds}"
            )
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._opened_at: float = 0.0  # monotonic timestamp when circuit was opened

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Current circuit state (read-only snapshot)."""
        return self._state

    async def call(
        self,
        operation: Callable[..., Awaitable[T]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute *operation* through the circuit breaker.

        Args:
            operation: An async callable to protect.
            *args: Positional arguments forwarded to *operation*.
            **kwargs: Keyword arguments forwarded to *operation*.

        Returns:
            The return value of *operation* on success.

        Raises:
            CircuitBreakerOpenError: When the circuit is OPEN and the
                reset timeout has not yet elapsed.
            Any exception raised by *operation*: Propagated after
                updating failure/success counters.
        """
        self._maybe_transition_to_half_open()

        if self._state is CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker is OPEN. "
                f"Retry after {self._seconds_until_reset():.1f}s."
            )

        try:
            result = await operation(*args, **kwargs)
        except Exception as exc:
            self._on_failure()
            raise exc from None

        self._on_success()
        return result

    # ------------------------------------------------------------------
    # State-machine helpers
    # ------------------------------------------------------------------

    def _maybe_transition_to_half_open(self) -> None:
        """Transition OPEN → HALF_OPEN once the reset timeout has elapsed."""
        if self._state is CircuitState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.reset_timeout_seconds:
                logger.info(
                    "Circuit breaker transitioning OPEN → HALF_OPEN "
                    "after %.1fs elapsed.",
                    elapsed,
                )
                self._state = CircuitState.HALF_OPEN

    def _on_success(self) -> None:
        """Record a successful call; reset the circuit to CLOSED."""
        if self._state is not CircuitState.CLOSED:
            logger.info(
                "Circuit breaker transitioning %s → CLOSED after successful call.",
                self._state.name,
            )
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def _on_failure(self) -> None:
        """Record a failed call; open the circuit when threshold is reached."""
        self._failure_count += 1
        logger.warning(
            "Circuit breaker failure %d/%d (state=%s).",
            self._failure_count,
            self.failure_threshold,
            self._state.name,
        )

        if self._state is CircuitState.HALF_OPEN or (
            self._failure_count >= self.failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            logger.error(
                "Circuit breaker opened. Will probe again in %.0fs.",
                self.reset_timeout_seconds,
            )

    def _seconds_until_reset(self) -> float:
        """Return estimated seconds remaining in OPEN state (may be 0)."""
        if self._state is not CircuitState.OPEN:
            return 0.0
        elapsed = time.monotonic() - self._opened_at
        return max(0.0, self.reset_timeout_seconds - elapsed)
