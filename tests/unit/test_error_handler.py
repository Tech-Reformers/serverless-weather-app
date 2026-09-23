"""
Unit tests for the error-handling middleware and exception hierarchy.

Coverage
--------
* WeatherAppError.to_dict()               – envelope shape, optional request_id
* format_error_response()                 – WeatherApp exceptions + plain Exception
* handle_exception()                      – HTTP status mapping, response body,
                                           logging (via structlog capture)
* All concrete exception error_code values
* Details dict propagation

Requirements validated: Error handling across all requirements (task 8.2).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from src.api.middleware.error_handler import (
    ERROR_CODE_TO_STATUS,
    handle_exception,
)
from src.domain.exceptions import (
    CacheError,
    DatabaseError,
    DuplicateFavoriteError,
    ExternalServiceError,
    FavoriteLimitExceededError,
    TimeoutError,
    ValidationError,
    WeatherAppError,
    format_error_response,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_error(body: dict[str, Any]) -> dict[str, Any]:
    """Return the inner ``error`` dict from a response body."""
    assert "error" in body, f"Expected 'error' key in body, got: {body!r}"
    return body["error"]


# ---------------------------------------------------------------------------
# WeatherAppError base class
# ---------------------------------------------------------------------------

class TestWeatherAppErrorBase:
    """Tests for WeatherAppError itself (not a subclass)."""

    def test_error_code_default(self):
        exc = WeatherAppError("something went wrong")
        assert exc.error_code == "WEATHER_APP_ERROR"

    def test_message_preserved(self):
        exc = WeatherAppError("something went wrong")
        assert str(exc) == "something went wrong"

    def test_details_default_empty(self):
        exc = WeatherAppError("msg")
        assert exc.details == {}

    def test_details_stored(self):
        exc = WeatherAppError("msg", details={"field": "q", "min": 2})
        assert exc.details == {"field": "q", "min": 2}

    def test_to_dict_shape(self):
        exc = WeatherAppError("base error")
        result = exc.to_dict()
        error = _extract_error(result)
        assert error["code"] == "WEATHER_APP_ERROR"
        assert error["message"] == "base error"
        assert "timestamp" in error
        assert "details" in error
        assert "request_id" not in error   # omitted when not provided

    def test_to_dict_with_request_id(self):
        exc = WeatherAppError("base error")
        result = exc.to_dict(request_id="req-abc-123")
        error = _extract_error(result)
        assert error["request_id"] == "req-abc-123"

    def test_to_dict_timestamp_format(self):
        exc = WeatherAppError("ts test")
        result = exc.to_dict()
        ts = _extract_error(result)["timestamp"]
        # Must parse as ISO 8601 UTC
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        assert parsed.year >= 2024

    def test_to_dict_details_propagated(self):
        exc = WeatherAppError("detail test", details={"key": "value"})
        result = exc.to_dict()
        assert _extract_error(result)["details"] == {"key": "value"}

    def test_to_dict_empty_message_uses_class_name(self):
        exc = WeatherAppError("")
        result = exc.to_dict()
        # Falls back to class name when message is empty
        assert _extract_error(result)["message"] == "WeatherAppError"


# ---------------------------------------------------------------------------
# Concrete exception error codes
# ---------------------------------------------------------------------------

class TestExceptionErrorCodes:
    """Each concrete subclass must declare the correct error_code."""

    @pytest.mark.parametrize("exc_class, expected_code", [
        (ValidationError,            "VALIDATION_ERROR"),
        (ExternalServiceError,       "EXTERNAL_SERVICE_ERROR"),
        (TimeoutError,               "TIMEOUT_ERROR"),
        (CacheError,                 "CACHE_ERROR"),
        (DatabaseError,              "DATABASE_ERROR"),
        (FavoriteLimitExceededError, "FAVORITES_LIMIT_EXCEEDED"),
        (DuplicateFavoriteError,     "DUPLICATE_FAVORITE"),
    ])
    def test_error_code(self, exc_class, expected_code):
        exc = exc_class("test")
        assert exc.error_code == expected_code

    def test_all_subclass_of_weather_app_error(self):
        for exc_class in [
            ValidationError, ExternalServiceError, TimeoutError,
            CacheError, DatabaseError, FavoriteLimitExceededError,
            DuplicateFavoriteError,
        ]:
            assert issubclass(exc_class, WeatherAppError)

    def test_subclass_to_dict_uses_own_error_code(self):
        exc = ValidationError("too short", details={"field": "q", "min_length": 2})
        error = _extract_error(exc.to_dict())
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["field"] == "q"


# ---------------------------------------------------------------------------
# format_error_response()
# ---------------------------------------------------------------------------

class TestFormatErrorResponse:
    """Tests for the module-level format_error_response convenience helper."""

    def test_weather_app_error_delegates_to_to_dict(self):
        exc = ValidationError("bad input")
        result = format_error_response(exc)
        assert _extract_error(result)["code"] == "VALIDATION_ERROR"

    def test_plain_exception_returns_internal_error(self):
        exc = RuntimeError("unexpected crash")
        result = format_error_response(exc)
        error = _extract_error(result)
        assert error["code"] == "INTERNAL_ERROR"
        # Must NOT leak internal exception message
        assert "unexpected crash" not in error["message"]

    def test_plain_exception_generic_message(self):
        exc = ValueError("sensitive detail")
        result = format_error_response(exc)
        error = _extract_error(result)
        assert error["message"] == "An unexpected error occurred."

    def test_request_id_forwarded_for_weather_app_error(self):
        exc = TimeoutError("took too long")
        result = format_error_response(exc, request_id="rid-001")
        assert _extract_error(result)["request_id"] == "rid-001"

    def test_request_id_forwarded_for_plain_exception(self):
        exc = RuntimeError("boom")
        result = format_error_response(exc, request_id="rid-002")
        assert _extract_error(result)["request_id"] == "rid-002"

    def test_no_request_id_when_not_supplied(self):
        exc = ValidationError("bad")
        result = format_error_response(exc)
        assert "request_id" not in _extract_error(result)

    def test_result_is_json_serialisable(self):
        exc = FavoriteLimitExceededError("limit reached")
        result = format_error_response(exc)
        # Should not raise
        encoded = json.dumps(result)
        decoded = json.loads(encoded)
        assert decoded["error"]["code"] == "FAVORITES_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------
# handle_exception() — HTTP status mapping
# ---------------------------------------------------------------------------

class TestHandleExceptionStatusCodes:
    """Verify the correct HTTP status code is returned for each exception."""

    @pytest.mark.parametrize("exc, expected_status", [
        (ValidationError("bad"),            400),
        (FavoriteLimitExceededError("max"), 422),
        (DuplicateFavoriteError("dup"),     409),
        (TimeoutError("slow"),              504),
        (ExternalServiceError("svc down"),  502),
        (CacheError("cache fail"),          500),
        (DatabaseError("db fail"),          500),
        (WeatherAppError("base"),           500),
        (RuntimeError("generic"),           500),
        (ValueError("val"),                 500),
        (Exception("any"),                  500),
    ])
    def test_status_code(self, exc, expected_status):
        status, _ = handle_exception(exc)
        assert status == expected_status, (
            f"{type(exc).__name__} should map to {expected_status}, got {status}"
        )


# ---------------------------------------------------------------------------
# handle_exception() — response body
# ---------------------------------------------------------------------------

class TestHandleExceptionResponseBody:
    """Verify that the response body is correctly shaped."""

    def test_body_has_error_key(self):
        _, body = handle_exception(ValidationError("oops"))
        assert "error" in body

    def test_error_code_in_body(self):
        _, body = handle_exception(ValidationError("oops"))
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_request_id_in_body_when_supplied(self):
        _, body = handle_exception(ValidationError("oops"), request_id="req-xyz")
        assert body["error"]["request_id"] == "req-xyz"

    def test_request_id_absent_when_not_supplied(self):
        _, body = handle_exception(ValidationError("oops"))
        assert "request_id" not in body["error"]

    def test_plain_exception_returns_internal_error_code(self):
        _, body = handle_exception(RuntimeError("crash"))
        assert body["error"]["code"] == "INTERNAL_ERROR"

    def test_details_propagated(self):
        exc = ValidationError("bad", details={"field": "lat", "constraint": "range"})
        _, body = handle_exception(exc)
        assert body["error"]["details"] == {"field": "lat", "constraint": "range"}

    def test_body_is_json_serialisable(self):
        _, body = handle_exception(DatabaseError("db"))
        encoded = json.dumps(body)
        decoded = json.loads(encoded)
        assert decoded["error"]["code"] == "DATABASE_ERROR"

    def test_timestamp_present(self):
        _, body = handle_exception(TimeoutError("slow"))
        assert "timestamp" in body["error"]


# ---------------------------------------------------------------------------
# handle_exception() — logging
# ---------------------------------------------------------------------------

class TestHandleExceptionLogging:
    """Verify that handle_exception() emits structured log records."""

    def test_warning_logged_for_client_error(self):
        """4xx errors should log at WARNING via the stdlib fallback logger."""
        import src.api.middleware.error_handler as mod
        with patch.object(mod._stdlib_log, "warning") as mock_warn:
            # Force the structlog path to fail so stdlib fallback fires
            original = mod._log
            class _FailLog:
                def warning(self, *a, **kw): raise RuntimeError("structlog down")
                def error(self, *a, **kw): raise RuntimeError("structlog down")
            mod._log = _FailLog()  # type: ignore[assignment]
            try:
                handle_exception(ValidationError("too short"))
            finally:
                mod._log = original
        mock_warn.assert_called_once()

    def test_error_logged_for_server_error(self):
        """5xx errors should log at ERROR via the stdlib fallback logger."""
        import src.api.middleware.error_handler as mod
        with patch.object(mod._stdlib_log, "error") as mock_err:
            original = mod._log
            class _FailLog:
                def warning(self, *a, **kw): raise RuntimeError("structlog down")
                def error(self, *a, **kw): raise RuntimeError("structlog down")
            mod._log = _FailLog()  # type: ignore[assignment]
            try:
                handle_exception(DatabaseError("db down"))
            finally:
                mod._log = original
        mock_err.assert_called_once()

    def test_logging_does_not_raise_on_structlog_misconfiguration(self):
        """A broken structlog setup must not crash handle_exception()."""
        import src.api.middleware.error_handler as mod
        original = mod._log

        class _BrokenLogger:
            def warning(self, *a, **kw):
                raise RuntimeError("structlog broken")
            def error(self, *a, **kw):
                raise RuntimeError("structlog broken")

        mod._log = _BrokenLogger()  # type: ignore[assignment]
        try:
            # Should not raise even with a broken structlog logger
            status, body = handle_exception(ValidationError("test"))
            assert status == 400
        finally:
            mod._log = original


# ---------------------------------------------------------------------------
# ERROR_CODE_TO_STATUS public mapping
# ---------------------------------------------------------------------------

class TestErrorCodeToStatusMapping:
    """The public mapping dict should be consistent with handle_exception."""

    def test_mapping_covers_all_weather_app_exceptions(self):
        expected = {
            ValidationError, FavoriteLimitExceededError, DuplicateFavoriteError,
            TimeoutError, ExternalServiceError, CacheError, DatabaseError,
            WeatherAppError,
        }
        assert expected.issubset(set(ERROR_CODE_TO_STATUS.keys()))

    def test_mapping_values_are_valid_http_codes(self):
        for exc_type, status in ERROR_CODE_TO_STATUS.items():
            assert 400 <= status <= 599, (
                f"{exc_type.__name__} maps to invalid HTTP status {status}"
            )
