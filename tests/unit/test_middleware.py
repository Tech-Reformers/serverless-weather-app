"""
Unit tests for the API Gateway middleware layer (task 8.1).

Covers:
  - error_handler.format_error_response — envelope shape and field types
  - validation.validate_request — missing / blank parameter detection
  - validation.parse_float_param — type coercion and edge cases
  - with_middleware decorator — security headers, CORS headers,
    correlation ID generation and echo, request/response logging hooks

No real AWS services or external network calls are involved.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.api.middleware import _apply_headers, with_middleware
from src.api.middleware.error_handler import (
    body_from_response,
    format_error_response,
)
from src.api.middleware.validation import parse_float_param, validate_request
from src.domain.exceptions import (
    DatabaseError,
    DuplicateFavoriteError,
    ExternalServiceError,
    FavoriteLimitExceededError,
    TimeoutError,
    ValidationError,
    WeatherAppError,
)


# ===========================================================================
# error_handler — format_error_response
# ===========================================================================


class TestFormatErrorResponse:
    """Validate the standardised error envelope described in design.md."""

    def test_returns_dict_with_statusCode_body(self):
        resp = format_error_response("oops", 400)
        assert "statusCode" in resp
        assert "body" in resp

    def test_status_code_is_preserved(self):
        for code in (400, 404, 500, 502, 504):
            assert format_error_response("msg", code)["statusCode"] == code

    def test_body_is_valid_json_string(self):
        resp = format_error_response("oops", 500)
        # Must not raise
        parsed = json.loads(resp["body"])
        assert isinstance(parsed, dict)

    def test_envelope_has_error_key(self):
        parsed = body_from_response(format_error_response("oops", 400))
        assert "error" in parsed

    def test_error_block_has_required_fields(self):
        parsed = body_from_response(format_error_response("some message", 400))
        error = parsed["error"]
        assert "code" in error
        assert "message" in error
        assert "timestamp" in error

    def test_message_is_preserved(self):
        parsed = body_from_response(format_error_response("my error message", 400))
        assert parsed["error"]["message"] == "my error message"

    def test_request_id_included_when_provided(self):
        rid = "req-abc-123"
        parsed = body_from_response(format_error_response("msg", 400, request_id=rid))
        assert parsed["error"]["request_id"] == rid

    def test_request_id_absent_when_not_provided(self):
        parsed = body_from_response(format_error_response("msg", 400))
        assert "request_id" not in parsed["error"]

    def test_details_included_when_provided(self):
        details = {"field": "query", "constraint": "min_length_2"}
        parsed = body_from_response(
            format_error_response("bad input", 400, details=details)
        )
        assert parsed["error"]["details"] == details

    # --- Exception → code mapping -------------------------------------------

    def test_validation_error_maps_to_VALIDATION_ERROR(self):
        parsed = body_from_response(
            format_error_response(ValidationError("bad query"), 400)
        )
        assert parsed["error"]["code"] == "VALIDATION_ERROR"

    def test_timeout_error_maps_to_TIMEOUT_ERROR(self):
        parsed = body_from_response(
            format_error_response(TimeoutError("timed out"), 504)
        )
        assert parsed["error"]["code"] == "TIMEOUT_ERROR"

    def test_external_service_error_maps_to_EXTERNAL_SERVICE_ERROR(self):
        parsed = body_from_response(
            format_error_response(ExternalServiceError("api down"), 502)
        )
        assert parsed["error"]["code"] == "EXTERNAL_SERVICE_ERROR"

    def test_favorites_limit_exceeded_maps_correctly(self):
        parsed = body_from_response(
            format_error_response(FavoriteLimitExceededError("limit hit"), 409)
        )
        assert parsed["error"]["code"] == "FAVORITES_LIMIT_EXCEEDED"

    def test_duplicate_favorite_maps_correctly(self):
        parsed = body_from_response(
            format_error_response(DuplicateFavoriteError("already saved"), 409)
        )
        assert parsed["error"]["code"] == "DUPLICATE_FAVORITE"

    def test_database_error_maps_to_DATABASE_ERROR(self):
        parsed = body_from_response(
            format_error_response(DatabaseError("db write failed"), 500)
        )
        assert parsed["error"]["code"] == "DATABASE_ERROR"

    def test_unknown_exception_maps_to_INTERNAL_ERROR(self):
        parsed = body_from_response(
            format_error_response(RuntimeError("surprise"), 500)
        )
        assert parsed["error"]["code"] == "INTERNAL_ERROR"

    def test_weather_app_base_exception_maps_to_WEATHER_APP_ERROR(self):
        parsed = body_from_response(
            format_error_response(WeatherAppError("generic"), 500)
        )
        assert parsed["error"]["code"] == "WEATHER_APP_ERROR"

    def test_exception_message_used_as_message_field(self):
        parsed = body_from_response(
            format_error_response(ValidationError("query too short"), 400)
        )
        assert parsed["error"]["message"] == "query too short"

    def test_plain_string_code_is_ERROR(self):
        parsed = body_from_response(format_error_response("plain string error", 500))
        assert parsed["error"]["code"] == "ERROR"


# ===========================================================================
# validation — validate_request
# ===========================================================================


class TestValidateRequest:
    """Validate parameter-presence and blank checks."""

    def _event(self, params: dict | None) -> dict:
        return {"queryStringParameters": params}

    def test_no_required_params_returns_valid(self):
        ok, err = validate_request(self._event({}))
        assert ok is True
        assert err is None

    def test_present_params_return_valid(self):
        ok, err = validate_request(self._event({"q": "London"}), ["q"])
        assert ok is True
        assert err is None

    def test_missing_param_returns_invalid(self):
        ok, err = validate_request(self._event({}), ["q"])
        assert ok is False
        assert err is not None
        assert err["statusCode"] == 400

    def test_missing_param_message_contains_param_name(self):
        _, err = validate_request(self._event({}), ["lat"])
        body = json.loads(err["body"])
        assert "lat" in body["error"]["message"]

    def test_null_queryStringParameters_treated_as_missing(self):
        ok, err = validate_request({"queryStringParameters": None}, ["q"])
        assert ok is False
        assert err["statusCode"] == 400

    def test_blank_param_returns_invalid(self):
        ok, err = validate_request(self._event({"q": "   "}), ["q"])
        assert ok is False
        assert err["statusCode"] == 400

    def test_empty_string_param_returns_invalid(self):
        ok, err = validate_request(self._event({"q": ""}), ["q"])
        assert ok is False

    def test_multiple_params_all_present_returns_valid(self):
        ok, err = validate_request(self._event({"lat": "51.5", "lon": "-0.1"}), ["lat", "lon"])
        assert ok is True

    def test_multiple_params_one_missing_returns_invalid(self):
        ok, err = validate_request(self._event({"lat": "51.5"}), ["lat", "lon"])
        assert ok is False
        body = json.loads(err["body"])
        assert "lon" in body["error"]["message"]

    def test_empty_required_list_returns_valid(self):
        ok, err = validate_request(self._event({}), [])
        assert ok is True


# ===========================================================================
# validation — parse_float_param
# ===========================================================================


class TestParseFloatParam:
    """Validate safe float coercion from query-string values."""

    def test_valid_integer_string_parsed(self):
        assert parse_float_param({"x": "42"}, "x") == pytest.approx(42.0)

    def test_valid_float_string_parsed(self):
        assert parse_float_param({"x": "51.5074"}, "x") == pytest.approx(51.5074)

    def test_negative_value_parsed(self):
        assert parse_float_param({"x": "-0.1278"}, "x") == pytest.approx(-0.1278)

    def test_missing_key_returns_none(self):
        assert parse_float_param({"y": "1.0"}, "x") is None

    def test_non_numeric_string_returns_none(self):
        assert parse_float_param({"x": "abc"}, "x") is None

    def test_empty_string_returns_none(self):
        assert parse_float_param({"x": ""}, "x") is None

    def test_none_value_returns_none(self):
        assert parse_float_param({"x": None}, "x") is None

    def test_none_dict_returns_none(self):
        assert parse_float_param(None, "x") is None

    def test_empty_dict_returns_none(self):
        assert parse_float_param({}, "x") is None


# ===========================================================================
# with_middleware — decorator behaviour
# ===========================================================================


class TestWithMiddleware:
    """Test the with_middleware decorator cross-cutting concerns."""

    # --- Helpers ------------------------------------------------------------

    def _wrap(self, status: int = 200, extra_headers: dict | None = None):
        """Create a wrapped handler that returns a minimal success response."""
        base_headers = {"Content-Type": "application/json"}
        if extra_headers:
            base_headers.update(extra_headers)

        @with_middleware
        def inner(event, context):
            return {
                "statusCode": status,
                "headers": base_headers,
                "body": '{"ok": true}',
            }

        return inner

    def _event(self, headers: dict | None = None) -> dict:
        return {
            "httpMethod": "GET",
            "path": "/test",
            "queryStringParameters": None,
            "headers": headers or {},
        }

    # --- Security headers ---------------------------------------------------

    def test_x_content_type_options_added(self):
        handler = self._wrap()
        resp = handler(self._event(), None)
        assert resp["headers"]["X-Content-Type-Options"] == "nosniff"

    def test_x_frame_options_added(self):
        handler = self._wrap()
        resp = handler(self._event(), None)
        assert resp["headers"]["X-Frame-Options"] == "DENY"

    def test_strict_transport_security_added(self):
        handler = self._wrap()
        resp = handler(self._event(), None)
        assert "max-age=31536000" in resp["headers"]["Strict-Transport-Security"]

    def test_security_headers_override_handler_values(self):
        """Handlers must not be able to weaken security headers."""
        handler = self._wrap(
            extra_headers={"X-Frame-Options": "ALLOW", "X-Content-Type-Options": "invalid"}
        )
        resp = handler(self._event(), None)
        assert resp["headers"]["X-Frame-Options"] == "DENY"
        assert resp["headers"]["X-Content-Type-Options"] == "nosniff"

    # --- CORS headers -------------------------------------------------------

    def test_cors_allow_origin_present(self):
        handler = self._wrap()
        resp = handler(self._event(), None)
        assert "Access-Control-Allow-Origin" in resp["headers"]

    def test_cors_allow_methods_present(self):
        handler = self._wrap()
        resp = handler(self._event(), None)
        assert "Access-Control-Allow-Methods" in resp["headers"]

    def test_cors_handler_value_preserved_over_default(self):
        """Handler-supplied CORS origin should not be overwritten."""
        handler = self._wrap(extra_headers={"Access-Control-Allow-Origin": "https://example.com"})
        resp = handler(self._event(), None)
        assert resp["headers"]["Access-Control-Allow-Origin"] == "https://example.com"

    # --- Correlation ID -----------------------------------------------------

    def test_correlation_id_generated_when_not_in_headers(self):
        handler = self._wrap()
        resp = handler(self._event(), None)
        cid = resp["headers"].get("X-Correlation-ID", "")
        # Should look like a UUID4
        assert len(cid) == 36
        uuid.UUID(cid)  # raises if not a valid UUID

    def test_correlation_id_from_header_is_echoed(self):
        handler = self._wrap()
        incoming_id = "my-trace-id-abc"
        resp = handler(self._event(headers={"X-Correlation-ID": incoming_id}), None)
        assert resp["headers"]["X-Correlation-ID"] == incoming_id

    def test_lowercase_x_correlation_id_header_accepted(self):
        handler = self._wrap()
        incoming_id = "lower-case-id-xyz"
        resp = handler(self._event(headers={"x-correlation-id": incoming_id}), None)
        assert resp["headers"]["X-Correlation-ID"] == incoming_id

    def test_each_request_without_header_gets_unique_id(self):
        handler = self._wrap()
        r1 = handler(self._event(), None)
        r2 = handler(self._event(), None)
        assert r1["headers"]["X-Correlation-ID"] != r2["headers"]["X-Correlation-ID"]

    # --- Unhandled exceptions -----------------------------------------------

    def test_unhandled_exception_returns_500(self):
        @with_middleware
        def boom(event, context):
            raise RuntimeError("something exploded")

        resp = boom(self._event(), None)
        assert resp["statusCode"] == 500

    def test_unhandled_exception_response_has_security_headers(self):
        @with_middleware
        def boom(event, context):
            raise ValueError("bang")

        resp = boom(self._event(), None)
        assert "X-Content-Type-Options" in resp["headers"]

    def test_unhandled_exception_response_has_correlation_id(self):
        @with_middleware
        def boom(event, context):
            raise ValueError("bang")

        incoming = "err-corr-id"
        resp = boom(self._event(headers={"X-Correlation-ID": incoming}), None)
        assert resp["headers"]["X-Correlation-ID"] == incoming

    # --- Status code passthrough --------------------------------------------

    def test_200_status_preserved(self):
        handler = self._wrap(200)
        assert handler(self._event(), None)["statusCode"] == 200

    def test_404_status_preserved(self):
        handler = self._wrap(404)
        assert handler(self._event(), None)["statusCode"] == 404

    # --- Async handler support ----------------------------------------------

    def test_async_handler_is_executed(self):
        @with_middleware
        async def async_handler(event, context):
            return {"statusCode": 200, "body": '{"async": true}'}

        resp = async_handler(self._event(), None)
        assert resp["statusCode"] == 200
        assert resp["headers"]["X-Frame-Options"] == "DENY"
