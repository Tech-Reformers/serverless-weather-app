"""
API middleware package for the Serverless Weather App.

Provides cross-cutting concerns applied to every Lambda handler:

* ``with_middleware`` — decorator that adds security headers, CORS headers,
  correlation-ID injection/echo, and catches unhandled exceptions.
* ``_apply_headers`` — merge security and CORS headers onto a response dict
  (also used by error paths so all responses share the same header set).

Sub-modules:
  * :mod:`.error_handler`  — error-response formatting and HTTP status mapping.
  * :mod:`.validation`     — query-string parameter validation helpers.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import uuid
from typing import Any, Callable, Optional

from src.api.middleware.error_handler import format_error_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Header constants
# ---------------------------------------------------------------------------

_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

_DEFAULT_CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Correlation-ID",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,PUT,OPTIONS",
}


# ---------------------------------------------------------------------------
# _apply_headers
# ---------------------------------------------------------------------------


def _apply_headers(
    response: dict[str, Any],
    *,
    correlation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Merge security, CORS, and correlation-ID headers into *response*.

    Rules:
    * Security headers always win — handler values for these keys are
      overwritten so handlers cannot accidentally weaken them.
    * CORS headers only written when **absent** — handlers may supply a
      stricter ``Access-Control-Allow-Origin`` which is then preserved.
    * ``X-Correlation-ID`` is set to *correlation_id* when provided; existing
      handler-supplied values are kept.

    Args:
        response: The API Gateway proxy response dict to mutate.
        correlation_id: Optional correlation ID to inject into headers.

    Returns:
        The same *response* dict (mutated in place and returned for chaining).
    """
    headers: dict[str, str] = dict(response.get("headers") or {})
    response["headers"] = headers

    # Security headers always overwrite
    headers.update(_SECURITY_HEADERS)

    # CORS headers only when missing
    for key, value in _DEFAULT_CORS_HEADERS.items():
        if key not in headers:
            headers[key] = value

    # Correlation ID — always overwrite so every response echoes the current
    # request's correlation ID rather than inheriting a stale cached value.
    if correlation_id is not None:
        headers["X-Correlation-ID"] = correlation_id

    return response


# ---------------------------------------------------------------------------
# with_middleware decorator
# ---------------------------------------------------------------------------


def with_middleware(handler: Callable) -> Callable:
    """Decorator that wraps a Lambda handler with cross-cutting middleware.

    Applied concerns (in order):
    1. **Correlation ID** — read ``X-Correlation-ID`` from request headers
       (case-insensitive); generate a new UUID4 if absent.
    2. **Handler invocation** — supports both sync and async handlers.
    3. **Security & CORS headers** — merged onto every response via
       :func:`_apply_headers`.
    4. **Unhandled exceptions** — caught and formatted as 500 responses with
       full security headers and the correlation ID preserved.

    Usage::

        @with_middleware
        def handler(event: dict, context) -> dict:
            return {"statusCode": 200, "body": "{}"}

    Args:
        handler: A sync or async Lambda handler callable.

    Returns:
        A wrapped sync callable suitable for use as a Lambda entry-point.
    """
    @functools.wraps(handler)
    def wrapper(event: dict, context: Any) -> dict:
        # ------------------------------------------------------------------
        # 1. Resolve correlation ID (case-insensitive header lookup)
        # ------------------------------------------------------------------
        incoming_headers: dict[str, str] = event.get("headers") or {}
        correlation_id: Optional[str] = None
        for header_name, header_value in incoming_headers.items():
            if header_name.lower() == "x-correlation-id":
                correlation_id = header_value
                break
        if correlation_id is None:
            correlation_id = str(uuid.uuid4())

        # ------------------------------------------------------------------
        # 2. Invoke handler (sync or async)
        # ------------------------------------------------------------------
        try:
            if inspect.iscoroutinefunction(handler):
                response = asyncio.run(handler(event, context))
            else:
                response = handler(event, context)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled exception in Lambda handler: %s", exc)
            response = format_error_response(exc, 500, request_id=correlation_id)

        # ------------------------------------------------------------------
        # 3. Apply security / CORS / correlation-ID headers
        # ------------------------------------------------------------------
        _apply_headers(response, correlation_id=correlation_id)

        return response

    return wrapper
