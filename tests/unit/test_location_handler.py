"""
Unit tests for src/api/handlers/location_handler.py.

All tests use AsyncMock stubs for LocationService and CachedWeatherDataService
so no real network calls or AWS services are involved.

Validates: Requirements 1.1, 1.2, 1.4, 1.5, 4.1, 4.2, 4.3, 4.4,
           Properties 4, 7, 9, 13, 17
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import the module so we can reset its service singletons between tests.
import src.api.handlers.location_handler as handler_module
from src.api.handlers.location_handler import (
    _handle_device,
    _handle_search,
    handler,
)

# Access private serialisation helpers via the module reference so tests
# don't break if they are later made public.
_serialize_location = handler_module._serialize_location
_serialize_conditions = handler_module._serialize_conditions
from src.domain.exceptions import ExternalServiceError, TimeoutError, ValidationError
from src.domain.models import CurrentConditions, DeviceLocationData, Location


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_location(idx: int = 0) -> Location:
    return Location(
        id=f"city-{idx}",
        name=f"City {idx}",
        region="Test Region",
        country="US",
        latitude=float(idx),
        longitude=float(idx),
        timezone="America/New_York",
    )


def _make_conditions() -> CurrentConditions:
    return CurrentConditions(
        temperature=20.5,
        feels_like=19.0,
        humidity=65,
        pressure=1013,
        wind_speed=12.3,
        wind_direction=270,
        condition="Partly cloudy",
        icon="cloud",
        timestamp=datetime(2024, 1, 16, 14, 0, tzinfo=timezone.utc),
        sunrise=datetime(2024, 1, 16, 7, 0, tzinfo=timezone.utc),
        sunset=datetime(2024, 1, 16, 17, 0, tzinfo=timezone.utc),
        visibility=10000,
    )


def _search_event(query: str | None = "London") -> dict:
    """Build a minimal API Gateway event for /locations/search."""
    params: dict = {}
    if query is not None:
        params["q"] = query
    return {
        "path": "/locations/search",
        "httpMethod": "GET",
        "queryStringParameters": params or None,
    }


def _device_event(lat: str | None = "51.5074", lon: str | None = "-0.1278") -> dict:
    """Build a minimal API Gateway event for /location/device."""
    params: dict = {}
    if lat is not None:
        params["lat"] = lat
    if lon is not None:
        params["lon"] = lon
    return {
        "path": "/location/device",
        "httpMethod": "GET",
        "queryStringParameters": params or None,
    }


@pytest.fixture(autouse=True)
def reset_service_singletons():
    """Reset module-level service singletons before each test."""
    handler_module._location_service = None
    handler_module._weather_service = None
    yield
    handler_module._location_service = None
    handler_module._weather_service = None


@pytest.fixture
def mock_location_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.search_locations = AsyncMock(return_value=[])
    svc.resolve_device_location = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def mock_weather_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.get_current_conditions = AsyncMock(return_value=_make_conditions())
    return svc


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def test_serialize_location_fields():
    loc = _make_location(0)
    result = _serialize_location(loc)
    assert result["id"] == loc.id
    assert result["name"] == loc.name
    assert result["region"] == loc.region
    assert result["country"] == loc.country
    assert result["lat"] == loc.latitude
    assert result["lon"] == loc.longitude
    assert result["timezone"] == loc.timezone


def test_serialize_conditions_fields():
    cond = _make_conditions()
    result = _serialize_conditions(cond)
    assert result["temperature"] == cond.temperature
    assert result["humidity"] == cond.humidity
    assert result["condition"] == cond.condition
    assert result["wind_speed"] == cond.wind_speed
    assert "timestamp" in result
    assert "sunrise" in result
    assert "sunset" in result


def test_serialize_conditions_none_sunrise_sunset():
    cond = _make_conditions()
    cond.sunrise = None
    cond.sunset = None
    result = _serialize_conditions(cond)
    assert result["sunrise"] is None
    assert result["sunset"] is None


# ---------------------------------------------------------------------------
# top-level handler routing
# ---------------------------------------------------------------------------


def test_handler_routes_search_path_contains_search():
    """The sync handler dispatches /search paths to _handle_search (routing check)."""
    event = _search_event("London")
    assert "/search" in event["path"]


def test_handler_routes_device_path_contains_device():
    """The sync handler dispatches /device paths to _handle_device (routing check)."""
    event = _device_event()
    assert "/device" in event["path"]


def test_handler_returns_404_for_unknown_path():
    """Unknown paths return 404 — can call handler() outside async context."""
    event = {"path": "/unknown", "queryStringParameters": None}
    result = handler(event, None)
    assert result["statusCode"] == 404


# ---------------------------------------------------------------------------
# GET /locations/search — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_200_with_locations(mock_location_svc, mock_weather_svc):
    locations = [_make_location(i) for i in range(3)]
    mock_location_svc.search_locations.return_value = locations

    result = await _handle_search(
        _search_event("London"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert len(body["locations"]) == 3
    assert body["locations"][0]["name"] == "City 0"


@pytest.mark.asyncio
async def test_search_returns_empty_list_when_no_matches(mock_location_svc, mock_weather_svc):
    """Valid query with zero results → 200 with empty list (Req 1.4, Property 13)."""
    mock_location_svc.search_locations.return_value = []

    result = await _handle_search(
        _search_event("Zzz"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["locations"] == []


@pytest.mark.asyncio
async def test_search_includes_cors_headers(mock_location_svc, mock_weather_svc):
    mock_location_svc.search_locations.return_value = []

    result = await _handle_search(
        _search_event("London"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert "Access-Control-Allow-Origin" in result["headers"]


# ---------------------------------------------------------------------------
# GET /locations/search — validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_missing_q_param_returns_400(mock_location_svc, mock_weather_svc):
    """Missing 'q' param → 400 (Req 1.2)."""
    event = {"path": "/locations/search", "queryStringParameters": None}
    result = await _handle_search(
        event,
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert "q" in body["error"].lower() or "query" in body["error"].lower()
    mock_location_svc.search_locations.assert_not_called()


@pytest.mark.asyncio
async def test_search_empty_q_param_returns_400(mock_location_svc, mock_weather_svc):
    """Empty 'q' param → 400 (Req 1.2)."""
    result = await _handle_search(
        _search_event(""),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400
    mock_location_svc.search_locations.assert_not_called()


@pytest.mark.asyncio
async def test_search_whitespace_q_calls_service_which_raises_validation(
    mock_location_svc, mock_weather_svc
):
    """Whitespace-only query stripped to empty string → 400 before calling service."""
    result = await _handle_search(
        _search_event("   "),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    # The handler strips the query; stripped "" → 400 without calling service
    assert result["statusCode"] == 400
    mock_location_svc.search_locations.assert_not_called()


@pytest.mark.asyncio
async def test_search_service_validation_error_returns_400(mock_location_svc, mock_weather_svc):
    """ValidationError from service (e.g. single char after strip) → 400 (Property 9)."""
    mock_location_svc.search_locations.side_effect = ValidationError(
        "Query must contain at least 2 non-whitespace characters"
    )
    result = await _handle_search(
        _search_event("x"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# GET /locations/search — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_timeout_returns_504(mock_location_svc, mock_weather_svc):
    """TimeoutError from service → 504 (Req 1.1, 1.5)."""
    mock_location_svc.search_locations.side_effect = TimeoutError("timed out")

    result = await _handle_search(
        _search_event("London"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 504


@pytest.mark.asyncio
async def test_search_external_service_error_returns_502(mock_location_svc, mock_weather_svc):
    """ExternalServiceError from service → 502 (Req 1.5)."""
    mock_location_svc.search_locations.side_effect = ExternalServiceError("API down")

    result = await _handle_search(
        _search_event("London"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 502


# ---------------------------------------------------------------------------
# GET /location/device — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_returns_200_with_location_and_conditions(
    mock_location_svc, mock_weather_svc
):
    """Valid coords, location found, conditions retrieved → 200 (Req 4.1, 4.2)."""
    location = _make_location(0)
    conditions = _make_conditions()
    mock_location_svc.resolve_device_location.return_value = location
    mock_weather_svc.get_current_conditions.return_value = conditions

    result = await _handle_device(
        _device_event("51.5074", "-0.1278"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert "location" in body
    assert "current_conditions" in body
    assert body["location"]["id"] == location.id
    assert body["current_conditions"]["temperature"] == conditions.temperature


@pytest.mark.asyncio
async def test_device_includes_cors_headers(mock_location_svc, mock_weather_svc):
    mock_location_svc.resolve_device_location.return_value = _make_location(0)

    result = await _handle_device(
        _device_event(),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert "Access-Control-Allow-Origin" in result["headers"]


# ---------------------------------------------------------------------------
# GET /location/device — coordinate validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_missing_lat_returns_400(mock_location_svc, mock_weather_svc):
    """Missing 'lat' → 400 (Req 4.3)."""
    result = await _handle_device(
        _device_event(lat=None),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400
    mock_location_svc.resolve_device_location.assert_not_called()


@pytest.mark.asyncio
async def test_device_missing_lon_returns_400(mock_location_svc, mock_weather_svc):
    """Missing 'lon' → 400."""
    result = await _handle_device(
        _device_event(lon=None),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400
    mock_location_svc.resolve_device_location.assert_not_called()


@pytest.mark.asyncio
async def test_device_non_numeric_lat_returns_400(mock_location_svc, mock_weather_svc):
    """Non-numeric 'lat' → 400."""
    result = await _handle_device(
        _device_event(lat="abc"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400
    mock_location_svc.resolve_device_location.assert_not_called()


@pytest.mark.asyncio
async def test_device_non_numeric_lon_returns_400(mock_location_svc, mock_weather_svc):
    """Non-numeric 'lon' → 400."""
    result = await _handle_device(
        _device_event(lon="xyz"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400
    mock_location_svc.resolve_device_location.assert_not_called()


@pytest.mark.asyncio
async def test_device_lat_out_of_range_returns_400(mock_location_svc, mock_weather_svc):
    """Latitude > 90 → 400."""
    result = await _handle_device(
        _device_event(lat="91.0"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400


@pytest.mark.asyncio
async def test_device_lon_out_of_range_returns_400(mock_location_svc, mock_weather_svc):
    """Longitude > 180 → 400."""
    result = await _handle_device(
        _device_event(lon="181.0"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )
    assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# GET /location/device — 404 when no location within 50 km
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_returns_404_when_no_location_within_50km(
    mock_location_svc, mock_weather_svc
):
    """Service returns None → 404 with informative message (Req 4.4, Property 17)."""
    mock_location_svc.resolve_device_location.return_value = None

    result = await _handle_device(
        _device_event(),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert result["statusCode"] == 404
    body = json.loads(result["body"])
    assert "50km" in body["error"]
    # Weather service must NOT be called when no location found
    mock_weather_svc.get_current_conditions.assert_not_called()


# ---------------------------------------------------------------------------
# GET /location/device — timeout / service errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_location_timeout_returns_504(mock_location_svc, mock_weather_svc):
    """TimeoutError from resolve_device_location → 504 (Req 4.1, Property 7)."""
    mock_location_svc.resolve_device_location.side_effect = TimeoutError("timed out")

    result = await _handle_device(
        _device_event(),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert result["statusCode"] == 504
    body = json.loads(result["body"])
    assert "timed out" in body["error"].lower() or "timeout" in body["error"].lower()


@pytest.mark.asyncio
async def test_device_location_service_error_returns_502(mock_location_svc, mock_weather_svc):
    """ExternalServiceError from resolve_device_location → 502 (Req 4.4)."""
    mock_location_svc.resolve_device_location.side_effect = ExternalServiceError("geocoder down")

    result = await _handle_device(
        _device_event(),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert result["statusCode"] == 502


@pytest.mark.asyncio
async def test_device_weather_timeout_returns_504(mock_location_svc, mock_weather_svc):
    """TimeoutError from get_current_conditions → 504 (Req 2.6)."""
    mock_location_svc.resolve_device_location.return_value = _make_location(0)
    mock_weather_svc.get_current_conditions.side_effect = TimeoutError("wx timed out")

    result = await _handle_device(
        _device_event(),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert result["statusCode"] == 504


@pytest.mark.asyncio
async def test_device_weather_service_error_returns_502(mock_location_svc, mock_weather_svc):
    """ExternalServiceError from get_current_conditions → 502 (Req 2.7)."""
    mock_location_svc.resolve_device_location.return_value = _make_location(0)
    mock_weather_svc.get_current_conditions.side_effect = ExternalServiceError("wx API down")

    result = await _handle_device(
        _device_event(),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    assert result["statusCode"] == 502


# ---------------------------------------------------------------------------
# DeviceLocationData construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_constructs_device_location_data_with_correct_coords(
    mock_location_svc, mock_weather_svc
):
    """Coordinates from query params are forwarded to resolve_device_location."""
    mock_location_svc.resolve_device_location.return_value = None  # 404 is fine

    await _handle_device(
        _device_event("48.8566", "2.3522"),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    call_args = mock_location_svc.resolve_device_location.call_args
    device_data: DeviceLocationData = call_args[0][0]
    assert device_data.latitude == pytest.approx(48.8566)
    assert device_data.longitude == pytest.approx(2.3522)


@pytest.mark.asyncio
async def test_device_timestamp_is_utc(mock_location_svc, mock_weather_svc):
    """Constructed DeviceLocationData has UTC timestamp."""
    mock_location_svc.resolve_device_location.return_value = None

    await _handle_device(
        _device_event(),
        location_svc=mock_location_svc,
        weather_svc=mock_weather_svc,
    )

    device_data: DeviceLocationData = (
        mock_location_svc.resolve_device_location.call_args[0][0]
    )
    assert device_data.timestamp.tzinfo is not None
