"""
Unit tests for the forecast Lambda handler.

All forecast service calls are replaced with mocks so tests run without
AWS credentials, network access, or a real DynamoDB table.

Validates:
  - 200 response with serialised hourly entries (Requirements 3.1, 3.3)
  - 200 response with serialised daily entries (Requirements 3.2, 3.4)
  - 400 when lat/lon are missing or non-numeric
  - 504 when the service raises TimeoutError (Requirements 3.5, Property 22)
  - 502 when the service raises ExternalServiceError (Requirements 3.6, Property 16)
  - Unknown path returns 404
  - OPTIONS pre-flight returns 200
  - CORS headers present on every response
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.handlers.forecast_handler import _set_forecast_service, handler
from src.domain.exceptions import ExternalServiceError
from src.domain.exceptions import TimeoutError as DomainTimeoutError
from src.domain.models import DailyForecast, HourlyForecast


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_hourly(offset_hours: int = 0) -> HourlyForecast:
    ts = _NOW.replace(hour=offset_hours % 24)
    return HourlyForecast(
        timestamp=ts,
        temperature=20.0 + offset_hours,
        feels_like=19.0,
        humidity=60,
        condition="Partly cloudy",
        icon="cloudy",
        precipitation_probability=10,
        wind_speed=5.0,
    )


def _make_daily(offset_days: int = 0) -> DailyForecast:
    ts = _NOW.replace(day=1 + offset_days)
    return DailyForecast(
        date=ts,
        high_temp=25.0,
        low_temp=15.0,
        condition="Sunny",
        icon="sunny",
        precipitation_probability=5,
        sunrise=ts.replace(hour=6),
        sunset=ts.replace(hour=20),
    )


def _hourly_entries(n: int = 24) -> list[HourlyForecast]:
    return [_make_hourly(i) for i in range(n)]


def _daily_entries(n: int = 7) -> list[DailyForecast]:
    return [_make_daily(i) for i in range(n)]


def _event(path: str, lat: str | None = "51.5", lon: str | None = "-0.1") -> dict:
    params: dict = {}
    if lat is not None:
        params["lat"] = lat
    if lon is not None:
        params["lon"] = lon
    return {
        "path": path,
        "httpMethod": "GET",
        "queryStringParameters": params or None,
    }


def _mock_service(
    hourly: list | None = None,
    daily: list | None = None,
    hourly_exc: Exception | None = None,
    daily_exc: Exception | None = None,
) -> MagicMock:
    svc = MagicMock()
    if hourly_exc:
        svc.get_hourly_forecast = AsyncMock(side_effect=hourly_exc)
    else:
        svc.get_hourly_forecast = AsyncMock(return_value=hourly or _hourly_entries())
    if daily_exc:
        svc.get_daily_forecast = AsyncMock(side_effect=daily_exc)
    else:
        svc.get_daily_forecast = AsyncMock(return_value=daily or _daily_entries())
    return svc


# ---------------------------------------------------------------------------
# Hourly endpoint – happy path
# ---------------------------------------------------------------------------


def test_hourly_returns_200_with_24_entries():
    """GET /weather/forecast/hourly returns 200 and 24 hourly entries.

    Validates: Requirements 3.1, 3.3
    """
    _set_forecast_service(_mock_service())
    resp = handler(_event("/weather/forecast/hourly"), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "hourly" in body
    assert len(body["hourly"]) == 24


def test_hourly_entry_fields():
    """Each hourly entry contains all required fields."""
    _set_forecast_service(_mock_service(hourly=_hourly_entries(1)))
    resp = handler(_event("/weather/forecast/hourly"), None)

    entry = json.loads(resp["body"])["hourly"][0]
    required = {
        "timestamp", "temperature", "feels_like", "humidity",
        "condition", "icon", "precipitation_probability", "wind_speed",
    }
    assert required.issubset(entry.keys())


def test_hourly_entries_in_chronological_order():
    """Hourly entries are returned in the order supplied by the service (Property 11).

    The sorting contract (chronological order) is enforced by
    CachedForecastService, not the handler.  Here we verify the handler
    preserves and serialises the order the service returns.

    Validates: Requirements 3.3
    """
    entries = _hourly_entries(24)  # already chronological
    _set_forecast_service(_mock_service(hourly=entries))
    resp = handler(_event("/weather/forecast/hourly"), None)

    timestamps = [e["timestamp"] for e in json.loads(resp["body"])["hourly"]]
    # Handler must preserve the service's ordering without reordering
    expected = [e.timestamp.isoformat() for e in entries]
    assert timestamps == expected


# ---------------------------------------------------------------------------
# Daily endpoint – happy path
# ---------------------------------------------------------------------------


def test_daily_returns_200_with_7_entries():
    """GET /weather/forecast/daily returns 200 and 7 daily entries.

    Validates: Requirements 3.2, 3.4
    """
    _set_forecast_service(_mock_service())
    resp = handler(_event("/weather/forecast/daily"), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "daily" in body
    assert len(body["daily"]) == 7


def test_daily_entry_fields():
    """Each daily entry contains all required fields."""
    _set_forecast_service(_mock_service(daily=_daily_entries(1)))
    resp = handler(_event("/weather/forecast/daily"), None)

    entry = json.loads(resp["body"])["daily"][0]
    required = {
        "date", "high_temp", "low_temp", "condition", "icon",
        "precipitation_probability", "sunrise", "sunset",
    }
    assert required.issubset(entry.keys())


def test_daily_entries_in_chronological_order():
    """Daily entries are returned in the order supplied by the service (Property 11).

    Sorting is enforced by CachedForecastService; the handler must preserve
    that ordering through serialisation.

    Validates: Requirements 3.4
    """
    entries = _daily_entries(7)  # already chronological
    _set_forecast_service(_mock_service(daily=entries))
    resp = handler(_event("/weather/forecast/daily"), None)

    dates = [e["date"] for e in json.loads(resp["body"])["daily"]]
    expected = [e.date.isoformat() for e in entries]
    assert dates == expected


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/weather/forecast/hourly", "/weather/forecast/daily"])
def test_missing_lat_returns_400(path: str):
    """Missing lat parameter returns 400.

    Validates: coordinate validation for both endpoints
    """
    _set_forecast_service(_mock_service())
    resp = handler(_event(path, lat=None, lon="-0.1"), None)

    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert "lat" in body["error"]


@pytest.mark.parametrize("path", ["/weather/forecast/hourly", "/weather/forecast/daily"])
def test_missing_lon_returns_400(path: str):
    """Missing lon parameter returns 400."""
    _set_forecast_service(_mock_service())
    resp = handler(_event(path, lat="51.5", lon=None), None)

    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert "lon" in body["error"]


@pytest.mark.parametrize("path", ["/weather/forecast/hourly", "/weather/forecast/daily"])
def test_invalid_lat_returns_400(path: str):
    """Non-numeric lat returns 400."""
    _set_forecast_service(_mock_service())
    resp = handler(_event(path, lat="not-a-float", lon="-0.1"), None)

    assert resp["statusCode"] == 400
    assert "lat" in json.loads(resp["body"])["error"]


@pytest.mark.parametrize("path", ["/weather/forecast/hourly", "/weather/forecast/daily"])
def test_invalid_lon_returns_400(path: str):
    """Non-numeric lon returns 400."""
    _set_forecast_service(_mock_service())
    resp = handler(_event(path, lat="51.5", lon="bad"), None)

    assert resp["statusCode"] == 400
    assert "lon" in json.loads(resp["body"])["error"]


@pytest.mark.parametrize("path", ["/weather/forecast/hourly", "/weather/forecast/daily"])
def test_missing_both_params_returns_400_with_both_errors(path: str):
    """Both missing params are reported in the 400 response."""
    _set_forecast_service(_mock_service())
    event = {"path": path, "httpMethod": "GET", "queryStringParameters": None}
    resp = handler(event, None)

    assert resp["statusCode"] == 400
    error_msg = json.loads(resp["body"])["error"]
    assert "lat" in error_msg
    assert "lon" in error_msg


# ---------------------------------------------------------------------------
# Timeout error handling (Requirements 3.5, Property 22)
# ---------------------------------------------------------------------------


def test_hourly_timeout_returns_504():
    """TimeoutError from the forecast service returns 504 with descriptive message.

    Validates: Requirements 3.5, Property 22
    """
    _set_forecast_service(_mock_service(hourly_exc=DomainTimeoutError("timed out")))
    resp = handler(_event("/weather/forecast/hourly"), None)

    assert resp["statusCode"] == 504
    body = json.loads(resp["body"])
    assert "timed out" in body["error"].lower() or "timeout" in body["error"].lower()
    # Must mention that current conditions are still available (Req 3.5)
    assert "current conditions" in body["detail"].lower()


def test_daily_timeout_returns_504():
    """TimeoutError from the forecast service returns 504 for daily endpoint.

    Validates: Requirements 3.5, Property 22
    """
    _set_forecast_service(_mock_service(daily_exc=DomainTimeoutError("timed out")))
    resp = handler(_event("/weather/forecast/daily"), None)

    assert resp["statusCode"] == 504
    body = json.loads(resp["body"])
    assert "current conditions" in body["detail"].lower()


# ---------------------------------------------------------------------------
# External service error handling (Requirements 3.6, Property 16)
# ---------------------------------------------------------------------------


def test_hourly_external_service_error_returns_502():
    """ExternalServiceError from the forecast service returns 502.

    Validates: Requirements 3.6, Property 16
    """
    _set_forecast_service(
        _mock_service(hourly_exc=ExternalServiceError("provider down"))
    )
    resp = handler(_event("/weather/forecast/hourly"), None)

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert "unavailable" in body["error"].lower()
    assert "current conditions" in body["detail"].lower()


def test_daily_external_service_error_returns_502():
    """ExternalServiceError from the forecast service returns 502.

    Validates: Requirements 3.6, Property 16
    """
    _set_forecast_service(
        _mock_service(daily_exc=ExternalServiceError("provider error"))
    )
    resp = handler(_event("/weather/forecast/daily"), None)

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert "unavailable" in body["error"].lower()


# ---------------------------------------------------------------------------
# Routing edge cases
# ---------------------------------------------------------------------------


def test_unknown_path_returns_404():
    """An unrecognised path returns 404."""
    _set_forecast_service(_mock_service())
    resp = handler({"path": "/weather/forecast/unknown", "httpMethod": "GET"}, None)
    assert resp["statusCode"] == 404


def test_options_preflight_returns_200():
    """OPTIONS request returns 200 for CORS pre-flight."""
    _set_forecast_service(_mock_service())
    resp = handler({"path": "/weather/forecast/hourly", "httpMethod": "OPTIONS"}, None)
    assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# CORS headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/weather/forecast/hourly", "/weather/forecast/daily"])
def test_cors_headers_present(path: str):
    """CORS headers are included in every successful response."""
    _set_forecast_service(_mock_service())
    resp = handler(_event(path), None)

    assert "Access-Control-Allow-Origin" in resp["headers"]
    assert "Access-Control-Allow-Methods" in resp["headers"]


def test_cors_headers_present_on_error():
    """CORS headers are included in error responses."""
    _set_forecast_service(_mock_service(hourly_exc=DomainTimeoutError("t/o")))
    resp = handler(_event("/weather/forecast/hourly"), None)

    assert "Access-Control-Allow-Origin" in resp["headers"]


# ---------------------------------------------------------------------------
# Service receives correct location coordinates
# ---------------------------------------------------------------------------


def test_service_called_with_correct_coordinates():
    """The forecast service receives a Location built from the query params."""
    svc = _mock_service()
    _set_forecast_service(svc)

    handler(_event("/weather/forecast/hourly", lat="48.8566", lon="2.3522"), None)

    call_args = svc.get_hourly_forecast.call_args
    location_arg = call_args[0][0]  # first positional arg
    assert location_arg.latitude == pytest.approx(48.8566)
    assert location_arg.longitude == pytest.approx(2.3522)


def test_daily_service_called_with_correct_coordinates():
    """The forecast service receives a Location built from the query params (daily)."""
    svc = _mock_service()
    _set_forecast_service(svc)

    handler(_event("/weather/forecast/daily", lat="-33.8688", lon="151.2093"), None)

    call_args = svc.get_daily_forecast.call_args
    location_arg = call_args[0][0]
    assert location_arg.latitude == pytest.approx(-33.8688)
    assert location_arg.longitude == pytest.approx(151.2093)
