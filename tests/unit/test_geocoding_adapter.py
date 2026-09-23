"""
Unit tests for GeocodingAdapter.

httpx transport is injected by overriding _make_client() on each adapter
instance.  asyncio.sleep is patched out to keep retry tests fast.

Validates: Requirements 1.1, 1.2, 1.4, 1.5, 4.1, 4.2, 4.4
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from src.domain.exceptions import ExternalServiceError, TimeoutError, ValidationError
from src.domain.models import DeviceLocationData, Location
from src.infrastructure.external.geocoding_adapter import (
    GeocodingAdapter,
    _haversine_km,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _owm_geocoding_entry(
    name: str = "London",
    country: str = "GB",
    state: str = "England",
    lat: float = 51.5074,
    lon: float = -0.1278,
) -> dict:
    return {"name": name, "country": country, "state": state, "lat": lat, "lon": lon}


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, status_code: int = 200, body=None, raise_exc=None) -> None:
        self._status_code = status_code
        self._body = json.dumps(body if body is not None else []).encode()
        self._raise_exc = raise_exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._raise_exc is not None:
            raise self._raise_exc
        return httpx.Response(
            status_code=self._status_code,
            headers={"Content-Type": "application/json"},
            content=self._body,
        )


def _adapter(transport: httpx.AsyncBaseTransport, **kwargs) -> GeocodingAdapter:
    """Build a GeocodingAdapter with an injected transport."""
    adapter = GeocodingAdapter(
        api_url="https://fake.geocoding.api",
        api_key="test-key",
        retry_base_delay=0.0,
        **kwargs,
    )

    @asynccontextmanager
    async def _patched_make_client():
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    adapter._make_client = _patched_make_client  # type: ignore[method-assign]
    return adapter


# ---------------------------------------------------------------------------
# Tests — search_locations (happy path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_locations_returns_mapped_list():
    """Valid query returns correctly mapped Location objects.

    Validates: Requirements 1.1
    """
    entries = [
        _owm_geocoding_entry("London", "GB", "England", 51.5074, -0.1278),
        _owm_geocoding_entry("London", "CA", "Ontario", 42.9849, -81.2453),
    ]
    adapter = _adapter(_MockTransport(200, entries))

    results = await adapter.search_locations("London", limit=10)

    assert len(results) == 2
    assert results[0].name == "London"
    assert results[0].country == "GB"
    assert results[0].region == "England"
    assert results[0].latitude == 51.5074
    assert results[0].longitude == -0.1278
    assert results[0].id == "london-gb"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_locations_empty_result():
    """Provider returning [] yields empty list — no error (Requirements 1.4).

    Validates: Requirements 1.4, Property 13
    """
    adapter = _adapter(_MockTransport(200, []))
    results = await adapter.search_locations("Xyz123Nonexistent")
    assert results == []


# ---------------------------------------------------------------------------
# Tests — search_locations (validation)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_too_short_raises_validation_error():
    """Single char query raises ValidationError — no API call made.

    Validates: Requirements 1.2, Property 9
    """
    adapter = _adapter(_MockTransport(200, []))
    with pytest.raises(ValidationError, match="at least 2 non-whitespace"):
        await adapter.search_locations("a")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_whitespace_only_raises_validation_error():
    """Whitespace-only query raises ValidationError (Requirements 1.2).

    Validates: Requirements 1.2, Property 9
    """
    adapter = _adapter(_MockTransport(200, []))
    with pytest.raises(ValidationError):
        await adapter.search_locations("   ")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_two_char_query_is_accepted():
    """Exactly 2 non-whitespace chars passes validation.

    Validates: Requirements 1.2
    """
    adapter = _adapter(_MockTransport(200, []))
    results = await adapter.search_locations("Lo")
    assert results == []


# ---------------------------------------------------------------------------
# Tests — get_location_by_coordinates
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_location_by_coordinates_returns_first_result():
    """Returns the first location from the reverse-geocode response."""
    entries = [_owm_geocoding_entry("London", "GB", "England", 51.5074, -0.1278)]
    adapter = _adapter(_MockTransport(200, entries))

    location = await adapter.get_location_by_coordinates(51.5074, -0.1278)

    assert location is not None
    assert location.name == "London"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_location_by_coordinates_returns_none_when_empty():
    """Returns None when the provider returns no results."""
    adapter = _adapter(_MockTransport(200, []))
    location = await adapter.get_location_by_coordinates(0.0, 0.0)
    assert location is None


# ---------------------------------------------------------------------------
# Tests — resolve_device_location
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_device_location_within_50km_returns_location():
    """Location within 50 km is returned correctly.

    Validates: Requirements 4.2
    """
    # Canary Wharf — ~7 km east of central London
    entries = [_owm_geocoding_entry("Canary Wharf", "GB", "England", 51.5054, -0.0235)]
    adapter = _adapter(_MockTransport(200, entries))

    device = DeviceLocationData(
        latitude=51.5074, longitude=-0.1278, accuracy=10.0, timestamp=datetime.now(tz=timezone.utc)
    )
    location = await adapter.resolve_device_location(device)

    assert location is not None
    assert location.name == "Canary Wharf"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_device_location_beyond_50km_returns_none():
    """Location beyond 50 km is filtered out; None returned (Requirements 4.4).

    Validates: Requirements 4.4, Property 17
    """
    # Paris (~340 km from London)
    entries = [_owm_geocoding_entry("Paris", "FR", "Île-de-France", 48.8566, 2.3522)]
    adapter = _adapter(_MockTransport(200, entries))

    device = DeviceLocationData(
        latitude=51.5074, longitude=-0.1278, accuracy=10.0, timestamp=datetime.now(tz=timezone.utc)
    )
    location = await adapter.resolve_device_location(device)
    assert location is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_device_location_selects_nearest_within_radius():
    """When multiple candidates are within 50 km, the nearest is returned."""
    entries = [
        # ~7 km away
        _owm_geocoding_entry("Canary Wharf", "GB", "England", 51.5054, -0.0235),
        # ~3 km away
        _owm_geocoding_entry("Southwark", "GB", "England", 51.5037, -0.1043),
    ]
    adapter = _adapter(_MockTransport(200, entries))

    device = DeviceLocationData(
        latitude=51.5074, longitude=-0.1278, accuracy=10.0, timestamp=datetime.now(tz=timezone.utc)
    )
    location = await adapter.resolve_device_location(device)

    assert location is not None
    assert location.name == "Southwark"


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_200_raises_external_service_error(monkeypatch):
    """HTTP 503 response raises ExternalServiceError.

    Validates: Requirements 1.5, 2.7
    """
    adapter = _adapter(_MockTransport(503, {"error": "down"}), max_retries=1)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    with pytest.raises(ExternalServiceError, match="HTTP 503"):
        await adapter.search_locations("London")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_timeout_raises_domain_timeout_error(monkeypatch):
    """httpx timeout translates to domain TimeoutError.

    Validates: Requirements 1.5, 4.1
    """
    adapter = _adapter(
        _MockTransport(raise_exc=httpx.ReadTimeout("timed out")),
        max_retries=1,
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    with pytest.raises(TimeoutError):
        await adapter.search_locations("London")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_count_on_persistent_failure(monkeypatch):
    """All configured retry attempts are exhausted before raising.

    Validates: retry logic
    """
    call_count = 0

    class _CountingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                status_code=500,
                headers={"Content-Type": "application/json"},
                content=b"[]",
            )

    adapter = GeocodingAdapter(
        api_url="https://fake.geocoding.api",
        api_key="test-key",
        max_retries=3,
        retry_base_delay=0.0,
    )

    @asynccontextmanager
    async def _patched_make_client():
        async with httpx.AsyncClient(transport=_CountingTransport()) as client:
            yield client

    adapter._make_client = _patched_make_client  # type: ignore[method-assign]

    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    with pytest.raises(ExternalServiceError):
        await adapter.search_locations("London")

    assert call_count == 3
    assert mock_sleep.call_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raises_external_service_error_when_no_api_key(monkeypatch):
    """Missing API key raises ExternalServiceError before any network call."""
    monkeypatch.delenv("GEOCODING_API_KEY", raising=False)
    monkeypatch.delenv("WEATHER_API_KEY", raising=False)
    adapter = GeocodingAdapter(api_url="https://fake.geocoding.api")

    with pytest.raises(ExternalServiceError, match="API key not configured"):
        await adapter.search_locations("London")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_key_falls_back_to_weather_api_key_env(monkeypatch):
    """GEOCODING_API_KEY absent → falls back to WEATHER_API_KEY.

    Validates: shared-key env-var support
    """
    monkeypatch.delenv("GEOCODING_API_KEY", raising=False)
    monkeypatch.setenv("WEATHER_API_KEY", "shared-key")

    adapter = GeocodingAdapter(
        api_url="https://fake.geocoding.api",
        retry_base_delay=0.0,
    )

    @asynccontextmanager
    async def _patched_make_client():
        async with httpx.AsyncClient(transport=_MockTransport(200, [])) as client:
            yield client

    adapter._make_client = _patched_make_client  # type: ignore[method-assign]

    results = await adapter.search_locations("London")
    assert results == []  # key resolved from env, call succeeded


# ---------------------------------------------------------------------------
# Tests — haversine utility
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_haversine_same_point_is_zero():
    assert _haversine_km(51.5074, -0.1278, 51.5074, -0.1278) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.unit
def test_haversine_london_to_paris_approx():
    """London ↔ Paris is ~340 km."""
    dist = _haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
    assert 335 < dist < 345


@pytest.mark.unit
def test_haversine_london_canary_wharf_within_50km():
    """Canary Wharf is well within 50 km of central London."""
    dist = _haversine_km(51.5074, -0.1278, 51.5054, -0.0235)
    assert dist < 50.0
