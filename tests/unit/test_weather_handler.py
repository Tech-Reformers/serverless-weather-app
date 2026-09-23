"""
Unit tests for the weather Lambda handler (task 7.2 + 7.7).

Tests cover:
  - Coordinate validation (missing, non-numeric, out-of-range)
  - Temperature unit parameter validation
  - GET /weather/current happy path (Celsius and Fahrenheit)
  - POST /weather/refresh happy path — combined payload (Req 7.1, 7.2)
  - TimeoutError → 504 retaining previous data (Req 7.3)
  - ExternalServiceError → 502 retaining previous data (Req 7.4)
  - Missing / unresolvable location → 404
  - CORS headers on all responses
  - OPTIONS pre-flight

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 7.1, 7.2,
           7.3, 7.4
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.handlers import weather_handler
from src.domain.exceptions import ExternalServiceError
from src.domain.exceptions import TimeoutError as DomainTimeoutError
from src.domain.models import (
    CurrentConditions,
    DailyForecast,
    HourlyForecast,
    Location,
    TempUnit,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _make_event(
    path: str = "/weather/current",
    method: str = "GET",
    params: dict | None = None,
) -> dict:
    return {
        "path": path,
        "httpMethod": method,
        "queryStringParameters": params or {},
    }


def _make_location() -> Location:
    return Location(
        id="london-uk",
        name="London",
        region="England",
        country="UK",
        latitude=51.5074,
        longitude=-0.1278,
        timezone="Europe/London",
    )


def _make_conditions(temp: float = 15.0, feels_like: float = 13.0) -> CurrentConditions:
    return CurrentConditions(
        temperature=temp,
        feels_like=feels_like,
        humidity=65,
        pressure=1013,
        wind_speed=5.3,
        wind_direction=220,
        condition="Partly cloudy",
        icon="03d",
        timestamp=datetime(2024, 1, 16, 14, 0, 0, tzinfo=timezone.utc),
        sunrise=datetime(2024, 1, 16, 7, 45, 0, tzinfo=timezone.utc),
        sunset=datetime(2024, 1, 16, 16, 20, 0, tzinfo=timezone.utc),
        visibility=9000,
    )


def _make_hourly_entries(count: int = 24) -> list[HourlyForecast]:
    base = datetime(2024, 1, 16, 15, 0, 0, tzinfo=timezone.utc)
    return [
        HourlyForecast(
            timestamp=datetime(base.year, base.month, base.day, (base.hour + i) % 24, 0, 0, tzinfo=timezone.utc),
            temperature=10.0 + i * 0.5,
            feels_like=9.0 + i * 0.5,
            humidity=70,
            condition="Cloudy",
            icon="04d",
            precipitation_probability=20,
            wind_speed=4.0,
        )
        for i in range(count)
    ]


def _make_daily_entries(count: int = 7) -> list[DailyForecast]:
    base = datetime(2024, 1, 17, 0, 0, 0, tzinfo=timezone.utc)
    return [
        DailyForecast(
            date=datetime(base.year, base.month, base.day + i, 0, 0, 0, tzinfo=timezone.utc),
            high_temp=15.0 + i,
            low_temp=5.0 + i,
            condition="Sunny",
            icon="01d",
            precipitation_probability=10,
            sunrise=datetime(2024, 1, 17 + i, 7, 45, 0, tzinfo=timezone.utc),
            sunset=datetime(2024, 1, 17 + i, 16, 20, 0, tzinfo=timezone.utc),
        )
        for i in range(count)
    ]


def _body(response: dict) -> dict:
    return json.loads(response["body"])


# ---------------------------------------------------------------------------
# Fixtures — patch both lazy-init service factories at the module level
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_service_singletons():
    """Reset module-level singletons before each test to ensure isolation."""
    weather_handler._weather_service = None
    weather_handler._location_service = None
    weather_handler._forecast_service = None
    yield
    weather_handler._weather_service = None
    weather_handler._location_service = None
    weather_handler._forecast_service = None


@pytest.fixture()
def mock_weather_svc():
    svc = AsyncMock()
    svc.get_current_conditions.return_value = _make_conditions()
    svc.refresh_current_conditions.return_value = _make_conditions()
    with patch.object(weather_handler, "_get_weather_service", return_value=svc):
        yield svc


@pytest.fixture()
def mock_location_svc():
    svc = AsyncMock()
    svc.get_location_by_coordinates.return_value = _make_location()
    with patch.object(weather_handler, "_get_location_service", return_value=svc):
        yield svc


@pytest.fixture()
def mock_forecast_svc():
    svc = AsyncMock()
    svc.get_hourly_forecast.return_value = _make_hourly_entries()
    svc.get_daily_forecast.return_value = _make_daily_entries()
    with patch.object(weather_handler, "_get_forecast_service", return_value=svc):
        yield svc


# ---------------------------------------------------------------------------
# OPTIONS pre-flight
# ---------------------------------------------------------------------------


def test_options_preflight_returns_200(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """OPTIONS request returns 200 without calling services."""
    event = _make_event(method="OPTIONS")
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 200


def test_options_preflight_includes_cors_headers(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(method="OPTIONS")
    resp = weather_handler.handler(event, None)
    assert "Access-Control-Allow-Origin" in resp["headers"]


# ---------------------------------------------------------------------------
# Unknown route
# ---------------------------------------------------------------------------


def test_unknown_route_returns_404(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(path="/weather/unknown", method="GET")
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# Coordinate validation — GET /weather/current
# ---------------------------------------------------------------------------


def test_missing_lat_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400
    assert "lat" in _body(resp)["error"].lower() or "required" in _body(resp)["error"].lower()


def test_missing_lon_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400


def test_non_numeric_lat_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "abc", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400
    assert "lat" in _body(resp)["error"].lower()


def test_non_numeric_lon_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074", "lon": "xyz"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400
    assert "lon" in _body(resp)["error"].lower()


def test_lat_out_of_range_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Latitude must be in [-90, 90]."""
    event = _make_event(params={"lat": "91.0", "lon": "0.0"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400


def test_lon_out_of_range_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Longitude must be in [-180, 180]."""
    event = _make_event(params={"lat": "0.0", "lon": "181.0"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400


def test_valid_coords_pass_validation(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# Temperature unit parameter validation
# ---------------------------------------------------------------------------


def test_invalid_units_param_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278", "units": "kelvin"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400
    assert "kelvin" in _body(resp)["error"].lower() or "unit" in _body(resp)["error"].lower()


def test_missing_units_defaults_to_celsius(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 200
    assert _body(resp)["unit"] == "celsius"


def test_explicit_celsius_unit(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278", "units": "celsius"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 200
    assert _body(resp)["unit"] == "celsius"


def test_fahrenheit_unit_accepted(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278", "units": "fahrenheit"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 200
    assert _body(resp)["unit"] == "fahrenheit"


# ---------------------------------------------------------------------------
# GET /weather/current happy path
# ---------------------------------------------------------------------------


def test_current_returns_200_with_conditions(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirements 2.1, 2.2, 2.3, 2.4"""
    conditions = _make_conditions(temp=15.0)
    mock_weather_svc.get_current_conditions.return_value = conditions

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["temperature"] == 15.0
    assert body["humidity"] == 65
    assert body["condition"] == "Partly cloudy"
    assert body["unit"] == "celsius"


def test_current_response_includes_timestamp(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 2.5 — last retrieved time is included."""
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    assert "timestamp" in _body(resp)


def test_current_response_includes_wind_and_humidity(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 2.3 — wind speed and humidity present."""
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))
    assert "wind_speed" in body
    assert "humidity" in body


def test_current_celsius_temperatures_not_converted(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """When units=celsius, raw (Celsius) values are returned unchanged."""
    conditions = _make_conditions(temp=0.0, feels_like=-5.0)
    mock_weather_svc.get_current_conditions.return_value = conditions

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278", "units": "celsius"})
    body = _body(weather_handler.handler(event, None))

    assert body["temperature"] == 0.0
    assert body["feels_like"] == -5.0


def test_current_fahrenheit_conversion_applied(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """When units=fahrenheit, C→F conversion is applied (Req 2.2, 6.1).

    0 °C = 32 °F; 100 °C = 212 °F.
    """
    conditions = _make_conditions(temp=0.0, feels_like=100.0)
    mock_weather_svc.get_current_conditions.return_value = conditions

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278", "units": "fahrenheit"})
    body = _body(weather_handler.handler(event, None))

    assert body["temperature"] == 32.0
    assert body["feels_like"] == 212.0
    assert body["unit"] == "fahrenheit"


def test_current_calls_weather_service_with_resolved_location(
    mock_weather_svc, mock_location_svc, mock_forecast_svc
):
    """The weather service receives the location resolved from coordinates."""
    location = _make_location()
    mock_location_svc.get_location_by_coordinates.return_value = location

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    weather_handler.handler(event, None)

    mock_weather_svc.get_current_conditions.assert_called_once_with(location)


# ---------------------------------------------------------------------------
# GET /weather/current — unresolvable location
# ---------------------------------------------------------------------------


def test_current_location_not_found_returns_404(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    mock_location_svc.get_location_by_coordinates.return_value = None

    event = _make_event(params={"lat": "0.0", "lon": "0.0"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# GET /weather/current — error handling
# ---------------------------------------------------------------------------


def test_current_weather_timeout_returns_504(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 2.6 — timeout → 504, retain previous data."""
    mock_weather_svc.get_current_conditions.side_effect = DomainTimeoutError("timed out")

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 504
    assert "timed out" in _body(resp)["error"].lower()


def test_current_weather_external_service_error_returns_502(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 2.7 — provider failure → 502."""
    mock_weather_svc.get_current_conditions.side_effect = ExternalServiceError("API down")

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 502


def test_current_location_timeout_returns_504(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Location resolution timeout also surfaces as 504."""
    mock_location_svc.get_location_by_coordinates.side_effect = DomainTimeoutError("timeout")

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 504


def test_current_location_external_error_returns_502(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    mock_location_svc.get_location_by_coordinates.side_effect = ExternalServiceError("geocode fail")

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 502


# ---------------------------------------------------------------------------
# POST /weather/refresh — happy path (Req 7.1, 7.2)
# ---------------------------------------------------------------------------


def test_refresh_returns_200_with_combined_payload(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 7.1 — refresh retrieves conditions + forecast.
                  Requirement 7.2 — combined payload returned in one response."""
    conditions = _make_conditions(temp=18.0)
    mock_weather_svc.refresh_current_conditions.return_value = conditions
    mock_forecast_svc.get_hourly_forecast.return_value = _make_hourly_entries(24)
    mock_forecast_svc.get_daily_forecast.return_value = _make_daily_entries(7)

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 200
    body = _body(resp)
    # Combined payload must include all three sections (Req 7.1)
    assert "current_conditions" in body
    assert "hourly" in body
    assert "daily" in body
    # Last-retrieved timestamp must be present (Req 7.2)
    assert "last_retrieved" in body


def test_refresh_current_conditions_in_payload(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """current_conditions section has expected weather fields."""
    conditions = _make_conditions(temp=18.0)
    mock_weather_svc.refresh_current_conditions.return_value = conditions

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))

    cc = body["current_conditions"]
    assert cc["temperature"] == 18.0
    assert cc["humidity"] == 65
    assert cc["condition"] == "Partly cloudy"
    assert cc["unit"] == "celsius"


def test_refresh_hourly_forecast_in_payload(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """hourly section contains the expected number of entries."""
    mock_forecast_svc.get_hourly_forecast.return_value = _make_hourly_entries(24)

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))

    assert len(body["hourly"]) == 24
    entry = body["hourly"][0]
    assert "timestamp" in entry
    assert "temperature" in entry
    assert "condition" in entry


def test_refresh_daily_forecast_in_payload(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """daily section contains the expected number of entries."""
    mock_forecast_svc.get_daily_forecast.return_value = _make_daily_entries(7)

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))

    assert len(body["daily"]) == 7
    entry = body["daily"][0]
    assert "date" in entry
    assert "high_temp" in entry
    assert "low_temp" in entry


def test_refresh_calls_refresh_method_not_get(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """The refresh route uses refresh_current_conditions, not get_current_conditions."""
    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    weather_handler.handler(event, None)

    mock_weather_svc.refresh_current_conditions.assert_called_once()
    mock_weather_svc.get_current_conditions.assert_not_called()


def test_refresh_calls_forecast_service_for_hourly_and_daily(
    mock_weather_svc, mock_location_svc, mock_forecast_svc
):
    """Validates: Requirement 7.1 — forecast service is called for both
    hourly and daily data during a refresh."""
    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    weather_handler.handler(event, None)

    mock_forecast_svc.get_hourly_forecast.assert_called_once()
    mock_forecast_svc.get_daily_forecast.assert_called_once()


def test_refresh_last_retrieved_is_iso_string(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 7.2 — last_retrieved is included and parseable."""
    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))

    last_retrieved = body["last_retrieved"]
    assert isinstance(last_retrieved, str)
    # Should parse without error
    parsed = datetime.fromisoformat(last_retrieved)
    assert parsed is not None


def test_refresh_returns_celsius_conditions(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Refresh current_conditions are always returned in Celsius."""
    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))
    assert body["current_conditions"]["unit"] == "celsius"


def test_refresh_missing_lat_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: coordinate validation applies to the refresh route too."""
    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400


def test_refresh_missing_lon_returns_400(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400


def test_refresh_coordinate_validation_same_rules(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Refresh uses the same coordinate validation as /current."""
    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "not-a-number", "lon": "0.0"})
    resp = weather_handler.handler(event, None)
    assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# POST /weather/refresh — error handling (Req 7.3, 7.4)
# ---------------------------------------------------------------------------


def test_refresh_conditions_timeout_returns_504(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 7.3 — refresh timeout → 504, retain old data."""
    mock_weather_svc.refresh_current_conditions.side_effect = DomainTimeoutError("timed out")

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 504
    error_msg = _body(resp)["error"].lower()
    assert "timed out" in error_msg or "timeout" in error_msg


def test_refresh_forecast_timeout_returns_504(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 7.3 — forecast timeout during refresh → 504."""
    mock_forecast_svc.get_hourly_forecast.side_effect = DomainTimeoutError("timed out")

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 504


def test_refresh_external_service_error_returns_502(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Validates: Requirement 7.4 — provider error on refresh → 502."""
    mock_weather_svc.refresh_current_conditions.side_effect = ExternalServiceError("provider down")

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 502


def test_refresh_forecast_external_service_error_returns_502(
    mock_weather_svc, mock_location_svc, mock_forecast_svc
):
    """Validates: Requirement 7.4 — forecast provider error on refresh → 502."""
    mock_forecast_svc.get_daily_forecast.side_effect = ExternalServiceError("provider down")

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 502


def test_refresh_timeout_error_message_mentions_retained_data(
    mock_weather_svc, mock_location_svc, mock_forecast_svc
):
    """Validates: Requirement 7.3 — error message indicates previous data retained."""
    mock_weather_svc.refresh_current_conditions.side_effect = DomainTimeoutError("timeout")

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))

    assert "retained" in body["error"].lower() or "previous" in body["error"].lower()


def test_refresh_service_error_message_mentions_retained_data(
    mock_weather_svc, mock_location_svc, mock_forecast_svc
):
    """Validates: Requirement 7.4 — error message indicates previous data retained."""
    mock_weather_svc.refresh_current_conditions.side_effect = ExternalServiceError("down")

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))

    assert "retained" in body["error"].lower() or "previous" in body["error"].lower()


def test_refresh_location_not_found_returns_404(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    mock_location_svc.get_location_by_coordinates.return_value = None

    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "0.0", "lon": "0.0"})
    resp = weather_handler.handler(event, None)

    assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# CORS headers
# ---------------------------------------------------------------------------


def test_cors_headers_present_on_200(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


def test_cors_headers_present_on_error(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={})  # missing coords → 400
    resp = weather_handler.handler(event, None)
    assert "Access-Control-Allow-Origin" in resp["headers"]


def test_cors_headers_present_on_refresh_response(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(path="/weather/refresh", method="POST",
                        params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    assert "Access-Control-Allow-Origin" in resp["headers"]


# ---------------------------------------------------------------------------
# Response structure — GET /weather/current
# ---------------------------------------------------------------------------


def test_response_body_is_valid_json(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    resp = weather_handler.handler(event, None)
    body = json.loads(resp["body"])
    assert isinstance(body, dict)


def test_response_contains_required_fields(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """All fields specified in design.md response format are present."""
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))
    required_fields = {
        "temperature", "feels_like", "humidity", "wind_speed",
        "wind_direction", "condition", "timestamp", "unit",
    }
    assert required_fields.issubset(body.keys())


def test_sunrise_and_sunset_serialised_as_iso_strings(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    """Datetime values are ISO-format strings, not datetime objects."""
    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))
    assert isinstance(body["sunrise"], str)
    assert isinstance(body["sunset"], str)


def test_none_sunrise_sunset_serialised_as_null(mock_weather_svc, mock_location_svc, mock_forecast_svc):
    conditions = _make_conditions()
    conditions = conditions.__class__(
        **{**conditions.__dict__, "sunrise": None, "sunset": None}
    )
    mock_weather_svc.get_current_conditions.return_value = conditions

    event = _make_event(params={"lat": "51.5074", "lon": "-0.1278"})
    body = _body(weather_handler.handler(event, None))
    assert body["sunrise"] is None
    assert body["sunset"] is None
