"""
Centralised error-handling middleware for AWS Lambda / API Gateway handlers.

Responsibilities
----------------
1. **HTTP status mapping** — translates WeatherAppError subclasses (and plain
   ``Exception``) to the appropriate HTTP status code.
2. **Response formatting** — produces the canonical ``{"error": {...}}``
   envelope defined in the design document and returns a complete API Gateway
   proxy response dict (with ``statusCode`` and ``body``).
3. **Structured logging** — emits a JSON log record to CloudWatch via
   ``structlog`` so that every unhandled exception is traceable by its
   ``request_id`` correlation field.

Public API
----------
``format_error_response(exc_or_message, status_code, *, request_id=None, details=None)``
    Build a complete API Gateway response dict for an error.

``body_from_response(response_dict)``
    Parse the ``body`` field of an API Gateway response dict.

``handle_exception(exc, request_id=None) -> tuple[int, dict]``
    Translate any exception to ``(http_status, body_dict)``.

``ERROR_CODE_TO_STATUS``
    Read-only mapping used internally and exposed for tests / introspection.

Error code → HTTP status mapping
---------------------------------
+-------------------------------+------------------+
| Exception class               | HTTP status code |
+===============================+==================+
| ValidationError               | 400              |
| FavoriteLimitExceededError    | 422              |
| DuplicateFavoriteError        | 409              |
| TimeoutError                  | 504              |
| ExternalServiceError          | 502              |
| CacheError                    | 500              |
| DatabaseError                 | 500              |
| WeatherAppError (base)        | 500              |
| Any other Exception           | 500              |
+-------------------------------+------------------+
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Union

import structlog

from src.domain.exceptions import (
    CacheError,
    DatabaseError,
    DuplicateFavoriteError,
    ExternalServiceError,
    FavoriteLimitExceededError,
    TimeoutError,
    ValidationError,
    WeatherAppError,
)
from src.domain.exceptions import format_error_response as _domain_format

# ---------------------------------------------------------------------------
# HTTP status mapping
# ---------------------------------------------------------------------------

# Order matters when iterating — more-specific subclasses must come before
# their parents so that isinstance checks resolve correctly.
_STATUS_MAP: list[tuple[type[WeatherAppError], int]] = [
    (ValidationError, 400),
    (FavoriteLimitExceededError, 422),
    (DuplicateFavoriteError, 409),
    (TimeoutError, 504),
    (ExternalServiceError, 502),
    (CacheError, 500),
    (DatabaseError, 500),
    (WeatherAppError, 500),
]

# Public read-only view (class → status) for tests and documentation.
ERROR_CODE_TO_STATUS: dict[type[Exception], int] = dict(_STATUS_MAP)


def _status_for(exc: Exception) -> int:
    """Return the HTTP status code for *exc*.

    Walks ``_STATUS_MAP`` in order so the most-derived class wins.
    Falls back to ``500`` for non-WeatherApp exceptions.
    """
    for exc_type, status in _STATUS_MAP:
        if isinstance(exc, exc_type):
            return status
    return 500


# ---------------------------------------------------------------------------
# Structured logger (CloudWatch-compatible JSON via structlog)
# ---------------------------------------------------------------------------

_log = structlog.get_logger(__name__)
_stdlib_log = logging.getLogger(__name__)


def _log_exception(
    exc: Exception,
    *,
    status_code: int,
    request_id: Optional[str],
) -> None:
    """Emit a structured log record describing the error.

    The record is designed to be searchable in CloudWatch Logs Insights:

    .. code-block:: json

        {
            "event":       "unhandled_exception",
            "error_type":  "ValidationError",
            "error_code":  "VALIDATION_ERROR",
            "status_code": 400,
            "message":     "Query must contain at least 2 characters",
            "request_id":  "abc-123",
            "level":       "warning"
        }

    ``WARNING`` is used for client errors (4xx); ``ERROR`` for server errors
    (5xx).
    """
    error_code = getattr(exc, "error_code", "INTERNAL_ERROR")
    log_kwargs: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "error_code": error_code,
        "status_code": status_code,
        "message": str(exc) or type(exc).__name__,
    }
    if request_id is not None:
        log_kwargs["request_id"] = request_id

    try:
        if status_code < 500:
            _log.warning("unhandled_exception", **log_kwargs)
        else:
            _log.error("unhandled_exception", exc_info=exc, **log_kwargs)
    except Exception:  # noqa: BLE001
        if status_code < 500:
            _stdlib_log.warning("unhandled_exception: %s", log_kwargs)
        else:
            _stdlib_log.error("unhandled_exception: %s", log_kwargs, exc_info=exc)


# ---------------------------------------------------------------------------
# _error_code_for — determine error code string from exc_or_message
# ---------------------------------------------------------------------------

def _error_code_for(exc_or_message: Union[Exception, str]) -> str:
    """Derive the error code string for ``exc_or_message``.

    * WeatherAppError subclass  → uses ``exc.error_code``
    * Plain Exception           → ``"INTERNAL_ERROR"``
    * Plain string              → ``"ERROR"``
    """
    if isinstance(exc_or_message, WeatherAppError):
        return exc_or_message.error_code
    if isinstance(exc_or_message, Exception):
        return "INTERNAL_ERROR"
    return "ERROR"


def _message_for(exc_or_message: Union[Exception, str]) -> str:
    """Derive a safe user-facing message from ``exc_or_message``.

    * WeatherAppError           → ``str(exc)``
    * Plain Exception           → generic message (never leaks internals)
    * Plain string              → returned as-is
    """
    if isinstance(exc_or_message, WeatherAppError):
        return str(exc_or_message) or type(exc_or_message).__name__
    if isinstance(exc_or_message, Exception):
        return "An unexpected error occurred."
    return str(exc_or_message)


# ---------------------------------------------------------------------------
# Public API — format_error_response
# ---------------------------------------------------------------------------


def format_error_response(
    exc_or_message: Union[Exception, str],
    status_code: int,
    *,
    request_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a complete API Gateway proxy response for an error.

    This is the primary formatting entry-point consumed by Lambda handlers and
    the :func:`with_middleware` decorator.

    The returned dict conforms to the API Gateway proxy response contract and
    the design-document error envelope::

        {
            "statusCode": 400,
            "headers":    {"Content-Type": "application/json"},
            "body": '{"error": {"code": "VALIDATION_ERROR", "message": "...",
                               "details": {...}, "timestamp": "..."}}'
        }

    Args:
        exc_or_message: An exception instance **or** a plain string message.
            When a :class:`WeatherAppError` is passed its ``error_code`` and
            ``details`` are used automatically (unless *details* is overridden).
        status_code: HTTP status code to embed in ``statusCode``.
        request_id: Optional correlation / AWS request ID.
        details: Free-form detail dict.  Overrides any details attached to a
            WeatherAppError when explicitly provided.

    Returns:
        A fully serialisable API Gateway proxy response dict.
    """
    code = _error_code_for(exc_or_message)
    message = _message_for(exc_or_message)

    # Merge details: caller-supplied overrides exception-level details
    if details is None and isinstance(exc_or_message, WeatherAppError):
        details = exc_or_message.details
    resolved_details: dict[str, Any] = details or {}

    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "details": resolved_details,
        "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if request_id is not None:
        error["request_id"] = request_id

    _log_exception(
        exc_or_message if isinstance(exc_or_message, Exception) else Exception(exc_or_message),
        status_code=status_code,
        request_id=request_id,
    )

    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": error}),
    }


# ---------------------------------------------------------------------------
# Public API — body_from_response
# ---------------------------------------------------------------------------


def body_from_response(response: dict[str, Any]) -> dict[str, Any]:
    """Parse and return the ``body`` dict from an API Gateway response dict.

    Convenience helper used in tests and middleware::

        parsed = body_from_response(format_error_response("oops", 400))
        assert parsed["error"]["code"] == "ERROR"

    Args:
        response: An API Gateway proxy response dict with a JSON ``body``.

    Returns:
        The parsed body as a ``dict``.

    Raises:
        KeyError: If ``"body"`` is missing from *response*.
        json.JSONDecodeError: If ``body`` is not valid JSON.
    """
    return json.loads(response["body"])


# ---------------------------------------------------------------------------
# Public API — handle_exception (lower-level, returns (status, body_dict))
# ---------------------------------------------------------------------------


def handle_exception(
    exc: Exception,
    *,
    request_id: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    """Translate *exc* into an ``(http_status, response_body_dict)`` pair.

    Unlike :func:`format_error_response` this does **not** return a full API
    Gateway response — it returns ``(status_code, body_dict)`` so that the
    caller controls header construction:

    .. code-block:: python

        except Exception as exc:
            status_code, body = handle_exception(exc, request_id=request_id)
            return {"statusCode": status_code, "headers": HEADERS,
                    "body": json.dumps(body)}

    Args:
        exc: Any exception (WeatherAppError subclass or plain Exception).
        request_id: Optional AWS Lambda / API Gateway request ID.

    Returns:
        A ``(status_code, body_dict)`` tuple ready for JSON-serialisation.
    """
    status_code = _status_for(exc)
    body = _domain_format(exc, request_id=request_id)
    _log_exception(exc, status_code=status_code, request_id=request_id)
    return status_code, body
