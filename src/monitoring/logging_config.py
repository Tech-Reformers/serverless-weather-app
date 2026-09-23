"""
Lambda-oriented structured logging configuration.

This module is the single place that Lambda handlers call to:
  1. Boot structlog once per cold start (:func:`configure_lambda_logging`).
  2. Attach per-request context fields to every subsequent log call within
     the same invocation (:func:`bind_request_context`).
  3. Clear that per-request state at invocation end so the next invocation
     starts clean (:func:`clear_request_context`).

Design
------
structlog's ``contextvars`` integration provides an async-safe, thread-safe
store for bound key/value pairs.  ``merge_contextvars`` (already installed in
the processor chain by :func:`~src.api.middleware.logging.setup_logging`)
merges them into every log event automatically, so callers only need to call
:func:`bind_request_context` once at the start of each Lambda invocation.

Usage example
-------------
::

    from src.monitoring.logging_config import (
        configure_lambda_logging,
        bind_request_context,
        clear_request_context,
    )
    from src.monitoring.logging_config import get_logger

    # ---- module level (cold-start) ----------------------------------------
    configure_lambda_logging("weather-app-weather")

    logger = get_logger(__name__)

    # ---- handler (every invocation) ---------------------------------------
    def handler(event, context):
        correlation_id = (
            (event.get("headers") or {}).get("X-Correlation-ID")
            or str(uuid.uuid4())
        )
        bind_request_context(
            correlation_id=correlation_id,
            aws_request_id=getattr(context, "aws_request_id", None),
        )
        try:
            logger.info("handler_invoked")
            ...
            return response
        finally:
            clear_request_context()
"""
from __future__ import annotations

import logging
from typing import Optional

import structlog

from src.api.middleware.logging import get_logger, setup_logging


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_lambda_logging(
    function_name: str,
    log_level: str = "INFO",
) -> None:
    """Boot structlog for a Lambda cold start.

    Safe to call multiple times — structlog's ``cache_logger_on_first_use``
    makes subsequent calls effectively no-ops once the first invocation has
    bound a logger.

    Parameters
    ----------
    function_name:
        The Lambda function name (e.g. ``"weather-app-weather"``).  Bound as
        ``function_name`` so every log entry identifies its source function.
    log_level:
        String log level (``"DEBUG"``, ``"INFO"``, ``"WARNING"``, etc.).
        Defaults to ``"INFO"``.
    """
    numeric_level: int = getattr(logging, log_level.upper(), logging.INFO)
    setup_logging(numeric_level)

    # Bind function name as a persistent contextvars entry so it appears in
    # every log line emitted by this Lambda instance, even after per-request
    # context is cleared.
    structlog.contextvars.bind_contextvars(function_name=function_name)


def bind_request_context(
    correlation_id: str,
    aws_request_id: Optional[str] = None,
) -> None:
    """Bind per-request fields so they appear in all subsequent log events.

    Should be called at the top of every Lambda handler invocation, before
    any business logic runs.

    Parameters
    ----------
    correlation_id:
        ``X-Correlation-ID`` from the API Gateway request headers, or a newly
        generated UUID4 if the header was absent.
    aws_request_id:
        Lambda context ``aws_request_id``.  Omit (or pass ``None``) when
        invoking outside a real Lambda context (e.g. local testing).
    """
    ctx: dict = {"correlation_id": correlation_id}
    if aws_request_id is not None:
        ctx["aws_request_id"] = aws_request_id
    structlog.contextvars.bind_contextvars(**ctx)


def clear_request_context() -> None:
    """Remove per-request context fields set by :func:`bind_request_context`.

    Call this in a ``finally`` block at the end of each Lambda invocation so
    that a warm-container reuse does not carry forward a previous request's
    ``correlation_id`` or ``aws_request_id``.

    Note: ``function_name`` bound during :func:`configure_lambda_logging` is
    **not** cleared — it is a cold-start invariant and should persist for the
    lifetime of the container.
    """
    structlog.contextvars.unbind_contextvars("correlation_id", "aws_request_id")


# Re-export get_logger for callers that import from this module
__all__ = [
    "configure_lambda_logging",
    "bind_request_context",
    "clear_request_context",
    "get_logger",
]
