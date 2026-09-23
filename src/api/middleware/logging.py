"""
Structured logging middleware.

Sets up structlog with JSON output and correlation-ID injection so that
every log entry emitted by a Lambda handler carries a consistent,
machine-parseable format that integrates with CloudWatch Insights.

Public surface
--------------
setup_logging(log_level)
    Configure structlog for the process.  Call once during cold-start.

get_logger(name)
    Return a structlog BoundLogger that includes ``logger_name`` in every
    event.

log_request(event, context, *, correlation_id)
    Emit a structured ``request_received`` log entry.

log_response(response, duration_ms, *, correlation_id)
    Emit a structured ``response_sent`` log entry.

Usage example
-------------
    from src.api.middleware.logging import setup_logging, get_logger, log_request, log_response

    setup_logging()
    logger = get_logger(__name__)

    def handler(event, context):
        correlation_id = event.get("headers", {}).get("X-Correlation-ID", str(uuid4()))
        log_request(event, context, correlation_id=correlation_id)
        ...
        log_response(response, duration_ms=42, correlation_id=correlation_id)
        return response
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup_logging(log_level: int = logging.INFO) -> None:
    """Configure structlog with JSON rendering and stdlib integration.

    Call this once during Lambda cold-start (module level or inside the
    handler on first invocation).  Subsequent calls are idempotent thanks
    to structlog's internal caching.

    The resulting log format is::

        {
            "timestamp": "2024-01-16T14:30:00.000Z",
            "level": "info",
            "logger": "src.api.handlers.weather_handler",
            "event": "request_received",
            "correlation_id": "abc-123",
            "aws_request_id": "lambda-req-id",
            ...
        }

    Fields guaranteed in every log entry:
      - ``timestamp`` — ISO-8601 UTC (added by TimeStamper)
      - ``level`` — log level name (added by add_log_level)
      - ``event`` — the message string
      - ``logger`` — set explicitly via :func:`get_logger` as ``logger_name``
      - ``correlation_id`` — injected via contextvars when bound
      - ``aws_request_id`` — injected via contextvars when bound
    """
    # Stdlib root logger → structlog bridge
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    structlog.configure(
        processors=[
            # Pull any bound contextvars (correlation_id, aws_request_id, …)
            structlog.contextvars.merge_contextvars,
            # Add "level" field
            structlog.stdlib.add_log_level,
            # Add "timestamp" in ISO-8601 UTC
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Final renderer — CloudWatch-friendly JSON
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------


def get_logger(name: str) -> Any:
    """Return a structlog BoundLogger tagged with *name*.

    The ``logger_name`` key appears in every emitted JSON event so CloudWatch
    Insights queries can filter by originating module.

    Parameters
    ----------
    name:
        Typically ``__name__`` of the calling module.  Appears as
        ``"logger_name"`` in every emitted event.
    """
    return structlog.get_logger(name).bind(logger=name)


# ---------------------------------------------------------------------------
# Request / response log helpers
# ---------------------------------------------------------------------------


def log_request(
    event: dict,
    context: Any,
    *,
    correlation_id: str,
) -> None:
    """Emit a ``request_received`` structured log entry.

    Captures method, path, source IP, and user-agent without logging
    sensitive query-string values or request bodies (following the
    least-exposure principle for API keys / PII).

    Parameters
    ----------
    event:
        Raw API Gateway proxy event.
    context:
        Lambda context object (may be ``None`` in tests).
    correlation_id:
        UUID or upstream correlation header value to attach.
    """
    logger = get_logger("middleware.logging")

    headers: dict = event.get("headers") or {}
    request_context: dict = event.get("requestContext") or {}

    logger.info(
        "request_received",
        correlation_id=correlation_id,
        method=event.get("httpMethod", "UNKNOWN"),
        path=event.get("path", "/"),
        source_ip=request_context.get("identity", {}).get("sourceIp", "unknown"),
        user_agent=headers.get("User-Agent") or headers.get("user-agent", "unknown"),
        function_name=getattr(context, "function_name", "local") if context else "local",
        aws_request_id=getattr(context, "aws_request_id", None) if context else None,
    )


def log_response(
    response: dict,
    duration_ms: float,
    *,
    correlation_id: str,
) -> None:
    """Emit a ``response_sent`` structured log entry.

    Parameters
    ----------
    response:
        API Gateway proxy response dict.
    duration_ms:
        Wall-clock milliseconds from request start to response ready.
    correlation_id:
        Same value used in :func:`log_request` for this invocation.
    """
    logger = get_logger("middleware.logging")
    status = response.get("statusCode", 0)

    log_fn = logger.warning if status >= 400 else logger.info
    log_fn(
        "response_sent",
        correlation_id=correlation_id,
        status_code=status,
        duration_ms=round(duration_ms, 3),
    )
