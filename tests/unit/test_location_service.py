"""
Unit tests for LocationService (src/application/location_service.py).

All tests use an AsyncMock stand-in for LocationServicePort so no real
network calls are made.

Validates: Requirements 1.1, 1.2, 1.4, 1.5, 4.1, 4.2, 4.3, 4.4,
           Properties 7, 9, 13, 17
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.application.location_service import LocationService, _MAX_SEARCH_RESULTS
from src.domain.exceptions import ExternalServiceError, TimeoutError, ValidationError
from src.domain.models import DeviceLocationData, Location


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_location(idx: int = 0) -> Location:
    return Location(
        id=f"city-{idx}-us",
        name=f"City {idx}",
        region="Test Region",
        country="US",
        latitude=float(idx),
        longitude=float(idx),
        timezone="America/New_York",
    )


def _make_device_data(lat: float = 51.5074, lon: float = -0.1278) -> DeviceLocationData:
    return DeviceLocationData(
        latitude=lat,
        longitude=lon,
        accuracy=10.0,
        timestamp=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def mock_port() -> AsyncMock:
    """A fresh AsyncMock satisfying LocationServicePort."""
    return AsyncMock()


@pytest.fixture
def service(mock_port: AsyncMock) -> LocationService:
    return LocationService(location_port=mock_port)


# ---------------------------------------------------------------------------
# search_locations — validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_rejects_empty_query(service: LocationService, mock_port: AsyncMock):
    """Empty string → ValidationError; port must NOT be called (Property 9, Req 1.2)."""
    with pytest.raises(ValidationError):
        await service.search_locations("")

    mock_port.search_locations.assert_not_called()


@pytest.mark.asyncio
async def test_search_rejects_single_char_query(service: LocationService, mock_port: AsyncMock):
    """Single non-whitespace char → ValidationError; port not called (Req 1.2)."""
    with pytest.raises(ValidationError):
        await service.search_locations("a")

    mock_port.search_locations.assert_not_called()


@pytest.mark.asyncio
async def test_search_rejects_whitespace_only_query(service: LocationService, mock_port: AsyncMock):
    """Whitespace-only query → ValidationError; port not called (Req 1.2)."""
    with pytest.raises(ValidationError):
        await service.search_locations("   ")

    mock_port.search_locations.assert_not_called()


@pytest.mark.asyncio
async def test_search_rejects_single_char_surrounded_by_whitespace(
    service: LocationService, mock_port: AsyncMock
):
    """One non-whitespace char padded with spaces → still rejected (Req 1.2)."""
    with pytest.raises(ValidationError):
        await service.search_locations("  x  ")

    mock_port.search_locations.assert_not_called()


# ---------------------------------------------------------------------------
# search_locations — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_results_for_valid_query(
    service: LocationService, mock_port: AsyncMock
):
    """Valid 2-char query → port called, results returned (Req 1.1)."""
    locations = [_make_location(i) for i in range(3)]
    mock_port.search_locations.return_value = locations

    results = await service.search_locations("Lo")

    assert results == locations
    mock_port.search_locations.assert_called_once_with("Lo", _MAX_SEARCH_RESULTS)


@pytest.mark.asyncio
async def test_search_caps_results_at_10(service: LocationService, mock_port: AsyncMock):
    """Port returning >10 locations is sliced to 10 (Req 1.1)."""
    locations = [_make_location(i) for i in range(15)]
    mock_port.search_locations.return_value = locations

    results = await service.search_locations("London", limit=15)

    assert len(results) == 10


@pytest.mark.asyncio
async def test_search_passes_lower_limit_to_port(
    service: LocationService, mock_port: AsyncMock
):
    """When caller asks for fewer than 10, that limit is forwarded."""
    locations = [_make_location(i) for i in range(5)]
    mock_port.search_locations.return_value = locations

    results = await service.search_locations("Berlin", limit=5)

    assert len(results) == 5
    mock_port.search_locations.assert_called_once_with("Berlin", 5)


@pytest.mark.asyncio
async def test_search_returns_empty_list_when_no_matches(
    service: LocationService, mock_port: AsyncMock
):
    """Valid query with zero results → empty list, no exception (Req 1.4, Property 13)."""
    mock_port.search_locations.return_value = []

    results = await service.search_locations("Nowhere")

    assert results == []


# ---------------------------------------------------------------------------
# search_locations — timeout / errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_raises_timeout_error_on_slow_port(
    service: LocationService, mock_port: AsyncMock
):
    """Port that never resolves → TimeoutError after 5 s (Req 1.1, 1.5)."""
    async def hang(*_args, **_kwargs):
        await asyncio.sleep(999)

    mock_port.search_locations.side_effect = hang

    with patch("src.application.location_service._SEARCH_TIMEOUT", 0.05):
        with pytest.raises(TimeoutError):
            await service.search_locations("London")


@pytest.mark.asyncio
async def test_search_propagates_external_service_error(
    service: LocationService, mock_port: AsyncMock
):
    """ExternalServiceError from the port bubbles up unchanged."""
    mock_port.search_locations.side_effect = ExternalServiceError("API down")

    with pytest.raises(ExternalServiceError, match="API down"):
        await service.search_locations("Paris")


# ---------------------------------------------------------------------------
# resolve_device_location — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_device_location_returns_nearest(
    service: LocationService, mock_port: AsyncMock
):
    """Port returns a location → service returns it unchanged (Req 4.2)."""
    location = _make_location(0)
    mock_port.resolve_device_location.return_value = location

    result = await service.resolve_device_location(_make_device_data())

    assert result == location
    mock_port.resolve_device_location.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_device_location_returns_none_when_outside_50km(
    service: LocationService, mock_port: AsyncMock
):
    """Port returns None (no location within 50 km) → service returns None (Req 4.4, Property 17)."""
    mock_port.resolve_device_location.return_value = None

    result = await service.resolve_device_location(_make_device_data())

    assert result is None


# ---------------------------------------------------------------------------
# resolve_device_location — timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_device_location_raises_timeout_error(
    service: LocationService, mock_port: AsyncMock
):
    """Port that hangs → TimeoutError after 30 s (Req 4.1, Property 7)."""
    async def hang(*_args, **_kwargs):
        await asyncio.sleep(999)

    mock_port.resolve_device_location.side_effect = hang

    with patch("src.application.location_service._DEVICE_LOCATION_TIMEOUT", 0.05):
        with pytest.raises(TimeoutError):
            await service.resolve_device_location(_make_device_data())


@pytest.mark.asyncio
async def test_resolve_device_location_propagates_external_service_error(
    service: LocationService, mock_port: AsyncMock
):
    """ExternalServiceError from the port propagates unchanged."""
    mock_port.resolve_device_location.side_effect = ExternalServiceError("geocoder unavailable")

    with pytest.raises(ExternalServiceError, match="geocoder unavailable"):
        await service.resolve_device_location(_make_device_data())


# ---------------------------------------------------------------------------
# get_location_by_coordinates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_location_by_coordinates_returns_location(
    service: LocationService, mock_port: AsyncMock
):
    """Valid coordinates → port called, result returned."""
    location = _make_location(0)
    mock_port.get_location_by_coordinates.return_value = location

    result = await service.get_location_by_coordinates(51.5074, -0.1278)

    assert result == location
    mock_port.get_location_by_coordinates.assert_called_once_with(51.5074, -0.1278)


@pytest.mark.asyncio
async def test_get_location_by_coordinates_returns_none(
    service: LocationService, mock_port: AsyncMock
):
    """No matching location → None returned."""
    mock_port.get_location_by_coordinates.return_value = None

    result = await service.get_location_by_coordinates(0.0, 0.0)

    assert result is None


@pytest.mark.asyncio
async def test_get_location_by_coordinates_raises_timeout_error(
    service: LocationService, mock_port: AsyncMock
):
    """Port that hangs → TimeoutError (10-second limit)."""
    async def hang(*_args, **_kwargs):
        await asyncio.sleep(999)

    mock_port.get_location_by_coordinates.side_effect = hang

    with patch("src.application.location_service._COORDINATE_LOOKUP_TIMEOUT", 0.05):
        with pytest.raises(TimeoutError):
            await service.get_location_by_coordinates(51.5074, -0.1278)
