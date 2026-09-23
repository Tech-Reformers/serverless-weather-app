"""
Exception hierarchy for the Serverless Weather App.

All application-specific exceptions derive from WeatherAppError, allowing
callers to catch the entire family with a single except clause when needed,
while still being able to handle individual error types precisely.

Every concrete exception class carries:
  * ``error_code`` – a SCREAMING_SNAKE_CASE string used in API error responses
    (e.g. ``"VALIDATION_ERROR"``).

The base class provides:
  * ``to_dict(request_id=None)`` – serialise the exception to the canonical
    error-response envelope expected by API Gateway clients.

A module-level helper:
  * ``format_error_response(exc, request_id=None)`` – convenience wrapper for
    ``to_dict``, safe to call with any ``Exception`` (falls back to
    ``"INTERNAL_ERROR"`` for non-WeatherApp exceptions).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


class WeatherAppError(Exception):
    """Base exception for all weather app errors.

    Every domain, application, and infrastructure exception in this project
    should inherit from this class so that unhandled errors can be caught at
    the API boundary and converted to a consistent error response.

    Subclasses MUST override ``error_code`` with a unique SCREAMING_SNAKE_CASE
    string.  The optional ``details`` parameter accepts a free-form dict that
    is forwarded verbatim into the ``"details"`` key of the response envelope.

    Example::

        raise ValidationError("Query too short", details={"field": "q", "min_length": 2})
    """

    error_code: str = "WEATHER_APP_ERROR"

    def __init__(
        self,
        message: str = "",
        *,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = details or {}

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self, *, request_id: Optional[str] = None) -> dict[str, Any]:
        """Return the canonical API error-response envelope.

        The shape matches the design specification::

            {
                "error": {
                    "code":       "VALIDATION_ERROR",
                    "message":    "Query must contain at least 2 characters",
                    "details":    {"field": "query", "constraint": "min_length_2"},
                    "timestamp":  "2024-01-16T14:30:00Z",
                    "request_id": "abc-123"   # omitted when None
                }
            }

        Args:
            request_id: Optional correlation / AWS request ID to include in the
                response for client-side debugging.

        Returns:
            A fully serialisable ``dict`` ready to be passed to
            ``json.dumps``.
        """
        error: dict[str, Any] = {
            "code": self.error_code,
            "message": str(self) or self.__class__.__name__,
            "details": self.details,
            "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if request_id is not None:
            error["request_id"] = request_id
        return {"error": error}


# ---------------------------------------------------------------------------
# Input / validation errors
# ---------------------------------------------------------------------------


class ValidationError(WeatherAppError):
    """Raised when user-supplied input fails validation rules.

    Examples:
        - Location search query shorter than 2 non-whitespace characters
          (Requirements 1.2, Property 9).
        - Temperature unit value other than Celsius/Fahrenheit
          (Requirements 6.3, Property 2).
    """

    error_code: str = "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# External dependency errors
# ---------------------------------------------------------------------------


class ExternalServiceError(WeatherAppError):
    """Raised when an external third-party API returns a failure or is
    unreachable.

    Examples:
        - Weather provider returns a 5xx response.
        - Geocoding API is temporarily unavailable.
    """

    error_code: str = "EXTERNAL_SERVICE_ERROR"


class TimeoutError(WeatherAppError):
    """Raised when an operation exceeds its configured time limit.

    Examples:
        - Location search exceeds 10 seconds (Requirements 1.5).
        - Current conditions retrieval exceeds 10 seconds (Requirements 2.6).
        - Refresh operation exceeds 5 seconds (Requirements 7.3, Property 15).
        - Device location detection exceeds 30 seconds (Requirements 4.4).
    """

    error_code: str = "TIMEOUT_ERROR"


# ---------------------------------------------------------------------------
# Infrastructure / persistence errors
# ---------------------------------------------------------------------------


class CacheError(WeatherAppError):
    """Raised when a cache read or write operation fails.

    This covers DynamoDB cache table errors as well as any in-process
    LRU cache failures.  The application should degrade gracefully by
    falling back to the external API when this is raised.
    """

    error_code: str = "CACHE_ERROR"


class DatabaseError(WeatherAppError):
    """Raised when a DynamoDB persistence operation fails.

    Distinct from CacheError because the favorites and preferences tables
    are primary stores, not caches — failures here cannot be silently
    bypassed.
    """

    error_code: str = "DATABASE_ERROR"


# ---------------------------------------------------------------------------
# Business rule violations
# ---------------------------------------------------------------------------


class FavoriteLimitExceededError(WeatherAppError):
    """Raised when a user tries to add a favourite location but already has
    50 saved (Requirements 5.2, Property 1).

    The caller must surface this to the user as a capacity error and must
    not modify the existing favourites list.
    """

    error_code: str = "FAVORITES_LIMIT_EXCEEDED"


class DuplicateFavoriteError(WeatherAppError):
    """Raised when a user tries to add a location that is already saved as a
    favourite (Requirements 5.7, Property 14).

    The Favorites Service should retain exactly one entry and return this
    error so that the API layer can communicate the no-op to the caller.
    """

    error_code: str = "DUPLICATE_FAVORITE"


# ---------------------------------------------------------------------------
# Module-level convenience helper
# ---------------------------------------------------------------------------


def format_error_response(
    exc: Exception,
    *,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """Serialise *any* exception to the canonical API error-response envelope.

    When *exc* is a :class:`WeatherAppError` (or subclass) its own
    ``to_dict`` implementation is used so that structured details and the
    correct error code are preserved.

    For every other :class:`Exception` a generic ``"INTERNAL_ERROR"`` response
    is returned so that internal implementation details are never leaked to
    callers.

    Args:
        exc: The exception to format.
        request_id: Optional correlation / AWS request ID.

    Returns:
        A fully serialisable ``dict`` matching the design error-response shape.

    Example::

        try:
            ...
        except Exception as exc:
            body = format_error_response(exc, request_id=context.aws_request_id)
            return {"statusCode": 500, "body": json.dumps(body)}
    """
    if isinstance(exc, WeatherAppError):
        return exc.to_dict(request_id=request_id)

    # Generic fallback — do NOT propagate internal exception messages.
    error: dict[str, Any] = {
        "code": "INTERNAL_ERROR",
        "message": "An unexpected error occurred.",
        "details": {},
        "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if request_id is not None:
        error["request_id"] = request_id
    return {"error": error}
