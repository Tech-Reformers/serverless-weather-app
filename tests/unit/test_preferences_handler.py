"""
Unit tests for the preferences Lambda handler.

All tests inject a mock UnitConversionService via the module-level
``_set_service`` helper, so no real AWS calls are made.

Coverage
--------
handler()
    - Correct routing for GET, PUT, OPTIONS
    - 405 for unsupported methods

GET /preferences/units (_handle_get_units)
    - Success: returns 200 with unit value
    - user_id defaults to "default" when absent from query params
    - user_id read from query params when present
    - DatabaseError → 500
    - Unexpected error → 500
    - CORS headers present on success and error responses

PUT /preferences/units (_handle_put_units)
    - Success: returns 200 with saved unit and message
    - user_id taken from body when present
    - user_id taken from query params when not in body
    - user_id defaults to "default" when absent from both
    - Missing 'unit' field → 400
    - Invalid unit value → 400
    - Invalid JSON body → 400
    - DatabaseError → 500 with session-retention message (Req 6.6, Property 19)
    - ValidationError → 400
    - Unexpected error → 500

OPTIONS → 200 (CORS pre-flight)
    - CORS headers present
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.exceptions import DatabaseError, ValidationError
from src.domain.models import TempUnit

# ---------------------------------------------------------------------------
# Module under test — imported lazily so _set_service works correctly
# ---------------------------------------------------------------------------

import src.api.handlers.preferences_handler as _mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_service():
    """Ensure the module-level service is cleared between tests."""
    _mod._set_service(None)
    yield
    _mod._set_service(None)


def _make_service(unit: TempUnit = TempUnit.CELSIUS) -> MagicMock:
    """Return a mock UnitConversionService with async methods."""
    svc = MagicMock()
    svc.get_user_preference = AsyncMock(return_value=unit)
    svc.set_user_preference = AsyncMock(return_value=None)
    return svc


def _event(
    method: str = "GET",
    query_params: dict | None = None,
    body: dict | str | None = None,
    path_params: dict | None = None,
) -> dict:
    """Build a minimal API Gateway proxy event."""
    raw_body: str | None = None
    if isinstance(body, dict):
        raw_body = json.dumps(body)
    elif isinstance(body, str):
        raw_body = body

    return {
        "httpMethod": method,
        "queryStringParameters": query_params,
        "pathParameters": path_params,
        "body": raw_body,
    }


# ---------------------------------------------------------------------------
# Helper assertions
# ---------------------------------------------------------------------------


def _assert_cors(response: dict) -> None:
    """Assert that CORS headers are present in the response."""
    headers = response.get("headers", {})
    assert "Access-Control-Allow-Origin" in headers
    assert headers["Access-Control-Allow-Origin"] == "*"


def _body(response: dict) -> dict:
    """Parse the response body JSON."""
    return json.loads(response["body"])


# ===========================================================================
# Routing
# ===========================================================================


class TestRouting:
    def test_options_returns_200(self):
        response = _mod.handler(_event("OPTIONS"), None)
        assert response["statusCode"] == 200
        _assert_cors(response)

    def test_unsupported_method_returns_405(self):
        response = _mod.handler(_event("DELETE"), None)
        assert response["statusCode"] == 405
        assert "error" in _body(response)

    def test_post_returns_405(self):
        response = _mod.handler(_event("POST"), None)
        assert response["statusCode"] == 405

    def test_get_routes_to_get_handler(self):
        svc = _make_service(TempUnit.FAHRENHEIT)
        _mod._set_service(svc)
        response = _mod.handler(_event("GET"), None)
        assert response["statusCode"] == 200
        svc.get_user_preference.assert_awaited_once()

    def test_put_routes_to_put_handler(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(
            _event("PUT", body={"unit": "celsius"}), None
        )
        assert response["statusCode"] == 200
        svc.set_user_preference.assert_awaited_once()


# ===========================================================================
# GET /preferences/units
# ===========================================================================


class TestGetUnits:
    def test_success_celsius(self):
        svc = _make_service(TempUnit.CELSIUS)
        _mod._set_service(svc)
        response = _mod.handler(_event("GET"), None)
        assert response["statusCode"] == 200
        assert _body(response) == {"unit": "celsius"}

    def test_success_fahrenheit(self):
        svc = _make_service(TempUnit.FAHRENHEIT)
        _mod._set_service(svc)
        response = _mod.handler(_event("GET"), None)
        assert response["statusCode"] == 200
        assert _body(response) == {"unit": "fahrenheit"}

    def test_default_user_id_when_no_query_params(self):
        svc = _make_service()
        _mod._set_service(svc)
        _mod.handler(_event("GET", query_params=None), None)
        svc.get_user_preference.assert_awaited_once_with("default")

    def test_default_user_id_when_query_params_empty(self):
        svc = _make_service()
        _mod._set_service(svc)
        _mod.handler(_event("GET", query_params={}), None)
        svc.get_user_preference.assert_awaited_once_with("default")

    def test_user_id_from_query_params(self):
        svc = _make_service()
        _mod._set_service(svc)
        _mod.handler(_event("GET", query_params={"user_id": "alice"}), None)
        svc.get_user_preference.assert_awaited_once_with("alice")

    def test_database_error_returns_500(self):
        svc = _make_service()
        svc.get_user_preference = AsyncMock(side_effect=DatabaseError("db down"))
        _mod._set_service(svc)
        response = _mod.handler(_event("GET"), None)
        assert response["statusCode"] == 500
        assert "error" in _body(response)

    def test_unexpected_error_returns_500(self):
        svc = _make_service()
        svc.get_user_preference = AsyncMock(side_effect=RuntimeError("boom"))
        _mod._set_service(svc)
        response = _mod.handler(_event("GET"), None)
        assert response["statusCode"] == 500

    def test_cors_headers_on_success(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(_event("GET"), None)
        _assert_cors(response)

    def test_cors_headers_on_error(self):
        svc = _make_service()
        svc.get_user_preference = AsyncMock(side_effect=DatabaseError("db down"))
        _mod._set_service(svc)
        response = _mod.handler(_event("GET"), None)
        _assert_cors(response)


# ===========================================================================
# PUT /preferences/units
# ===========================================================================


class TestPutUnits:
    def test_success_celsius(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"unit": "celsius"}), None)
        assert response["statusCode"] == 200
        data = _body(response)
        assert data["unit"] == "celsius"
        assert data["message"] == "Preference saved"

    def test_success_fahrenheit(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"unit": "fahrenheit"}), None)
        assert response["statusCode"] == 200
        assert _body(response)["unit"] == "fahrenheit"

    def test_persists_correct_unit(self):
        svc = _make_service()
        _mod._set_service(svc)
        _mod.handler(_event("PUT", body={"unit": "fahrenheit"}), None)
        svc.set_user_preference.assert_awaited_once_with("default", TempUnit.FAHRENHEIT)

    # -- user_id resolution --------------------------------------------------

    def test_user_id_from_body(self):
        svc = _make_service()
        _mod._set_service(svc)
        _mod.handler(_event("PUT", body={"unit": "celsius", "user_id": "bob"}), None)
        svc.set_user_preference.assert_awaited_once_with("bob", TempUnit.CELSIUS)

    def test_user_id_from_query_params_when_absent_from_body(self):
        svc = _make_service()
        _mod._set_service(svc)
        _mod.handler(
            _event("PUT", query_params={"user_id": "carol"}, body={"unit": "celsius"}),
            None,
        )
        svc.set_user_preference.assert_awaited_once_with("carol", TempUnit.CELSIUS)

    def test_user_id_defaults_to_default(self):
        svc = _make_service()
        _mod._set_service(svc)
        _mod.handler(_event("PUT", body={"unit": "celsius"}), None)
        svc.set_user_preference.assert_awaited_once_with("default", TempUnit.CELSIUS)

    # -- validation -----------------------------------------------------------

    def test_missing_unit_field_returns_400(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"user_id": "alice"}), None)
        assert response["statusCode"] == 400
        assert "unit" in _body(response)["error"]
        svc.set_user_preference.assert_not_awaited()

    def test_invalid_unit_value_returns_400(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"unit": "kelvin"}), None)
        assert response["statusCode"] == 400
        assert "kelvin" in _body(response)["error"]
        svc.set_user_preference.assert_not_awaited()

    def test_empty_unit_value_returns_400(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"unit": ""}), None)
        assert response["statusCode"] == 400

    def test_invalid_json_body_returns_400(self):
        svc = _make_service()
        _mod._set_service(svc)
        ev = _event("PUT")
        ev["body"] = "not-json{{{"
        response = _mod.handler(ev, None)
        assert response["statusCode"] == 400
        assert "JSON" in _body(response)["error"]

    def test_null_body_treated_as_empty_object_returns_400(self):
        """A null body has no 'unit' field so should return 400."""
        svc = _make_service()
        _mod._set_service(svc)
        ev = _event("PUT")
        ev["body"] = None
        response = _mod.handler(ev, None)
        assert response["statusCode"] == 400

    # -- error handling -------------------------------------------------------

    def test_database_error_returns_500_with_session_message(self):
        """Req 6.6 / Property 19: session-only retention message."""
        svc = _make_service()
        svc.set_user_preference = AsyncMock(side_effect=DatabaseError("write failed"))
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"unit": "celsius"}), None)
        assert response["statusCode"] == 500
        error_msg = _body(response)["error"].lower()
        # Must mention session-level retention
        assert "session" in error_msg

    def test_validation_error_from_service_returns_400(self):
        """ValidationError raised by set_user_preference → 400."""
        svc = _make_service()
        svc.set_user_preference = AsyncMock(
            side_effect=ValidationError("Invalid unit: 'kelvin'")
        )
        _mod._set_service(svc)
        # We pass a valid-looking value that the service still rejects
        ev = _event("PUT")
        # Bypass handler-level validation by directly testing service-level path:
        # craft an event that passes handler checks but triggers service ValidationError
        ev["body"] = json.dumps({"unit": "celsius"})
        response = _mod.handler(ev, None)
        assert response["statusCode"] == 400

    def test_unexpected_error_returns_500(self):
        svc = _make_service()
        svc.set_user_preference = AsyncMock(side_effect=RuntimeError("unexpected"))
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"unit": "celsius"}), None)
        assert response["statusCode"] == 500

    # -- CORS -----------------------------------------------------------------

    def test_cors_headers_on_success(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"unit": "celsius"}), None)
        _assert_cors(response)

    def test_cors_headers_on_400(self):
        svc = _make_service()
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={}), None)
        _assert_cors(response)

    def test_cors_headers_on_500(self):
        svc = _make_service()
        svc.set_user_preference = AsyncMock(side_effect=DatabaseError("db"))
        _mod._set_service(svc)
        response = _mod.handler(_event("PUT", body={"unit": "celsius"}), None)
        _assert_cors(response)
