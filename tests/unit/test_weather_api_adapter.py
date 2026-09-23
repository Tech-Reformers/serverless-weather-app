"""
Unit tests for WeatherAPIAdapter.

httpx transport is injected by overriding _make_client() on each adapter
instance so there is no recursion and no monkey-patching of the httpx module.
asyncio.sleep is patched to keep retry tests fast.

Validates: Requirements 2.1, 2.6, 2.7
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from src.domain.exceptions import ExternalServiceError, TimeoutError
from src.domain.models import Location
from src.infrastructure.external.weather_api_adapter import WeatherAPIAdapter


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_location(lat: float = 51.5074, lon: float = -0.1278) -> Location:
    return Location(
        id="london-gb",
        name="London",
        region="England",
        country="GB",
        latitude=lat,
        longitude=lon,
        timezone="Europe/London",
    )


def _owm_weather_payload(temp: float = 15.5) -> dict:
    """Minimal OpenWeatherMap /weather response."""
    return {
        "dt": 1705410000,
        "main": {
            "temp": temp,
            "feels_like": 13.2,
            "humidity": 65,
            "pressure": 1013,
        },
        "wind": {"speed": 5.3, "deg": 220},
        "weather": [{"description": "light rain", "icon": "10d"}],
        "sys": {"sunrise": 1705388400, "sunset": 1705421200},
        "visibility": 9000,
    }


class _MockTransport(httpx.AsyncBaseTransport):
    """httpx transport that always returns a fixed response."""

    def __init__(self, status_code: int = 200, body=None, raise_exc=None) -> None:
        self._status_code = status_code
        self._body = json.dumps(body or {}).encode()
        self._raise_exc = raise_exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._raise_exc is not None:
            raise self._raise_exc
        return httpx.Response(
            status_code=self._status_code,
            headers={"Content-Type": "application/json"},
            content=self._body,
        )


def _adapter_with_transport(transport: httpx.AsyncBaseTransport, **kwargs) -> WeatherAPIAdapter:
    """Build a WeatherAPIAdapter that uses *transport* for all HTTP calls."""
    adapter = WeatherAPIAdapter(
        api_url="https://fake.weather.api",
        api_key="test-key",
        retry_base_delay=0.0,
        **kwargs,
    )

    # Override _make_client to inject our transport without patching httpx globally.
    @asynccontextmanager
    async def _patched_make_client():
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    adapter._make_client = _patched_make_client  # type: ignore[method-assign]
    return adapter


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_current_conditions_returns_correct_data():
    """Successful 200 response is mapped to CurrentConditions correctly.

    Validates: Requirements 2.1
    """
    adapter = _adapter_with_transport(_MockTransport(200, _owm_weather_payload(15.5)))
    conditions = await adapter.get_current_conditions(_make_location())

    assert conditions.temperature == 15.5
    assert conditions.feels_like == 13.2
    assert conditions.humidity == 65
    assert conditions.pressure == 1013
    assert conditions.wind_speed == 5.3
    assert conditions.wind_direction == 220
    assert conditions.condition == "light rain"
    assert conditions.icon == "10d"
    assert conditions.visibility == 9000
    assert isinstance(conditions.timestamp, datetime)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refresh_current_conditions_delegates_to_get():
    """refresh_current_conditions returns the same result as get_current_conditions.

    Validates: Requirements 2.1, 7.1
    """
    adapter = _adapter_with_transport(_MockTransport(200, _owm_weather_payload(20.0)))
    conditions = await adapter.refresh_current_conditions(_make_location())
    assert conditions.temperature == 20.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sunrise_sunset_none_when_absent():
    """Sunrise/sunset are None when the provider omits them."""
    payload = _owm_weather_payload()
    del payload["sys"]["sunrise"]
    del payload["sys"]["sunset"]
    adapter = _adapter_with_transport(_MockTransport(200, payload))
    conditions = await adapter.get_current_conditions(_make_location())
    assert conditions.sunrise is None
    assert conditions.sunset is None


# ---------------------------------------------------------------------------
# Tests — error handling (Requirements 2.6, 2.7)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raises_external_service_error_on_non_200(monkeypatch):
    """Non-200 response raises ExternalServiceError after retries.

    Validates: Requirements 2.7
    """
    adapter = _adapter_with_transport(
        _MockTransport(503, {"message": "Service Unavailable"}),
        max_retries=1,
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    with pytest.raises(ExternalServiceError, match="HTTP 503"):
        await adapter.get_current_conditions(_make_location())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raises_timeout_error_on_httpx_timeout(monkeypatch):
    """TimeoutException from httpx is translated to domain TimeoutError.

    Validates: Requirements 2.6
    """
    adapter = _adapter_with_transport(
        _MockTransport(raise_exc=httpx.ReadTimeout("timed out")),
        max_retries=1,
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    with pytest.raises(TimeoutError):
        await adapter.get_current_conditions(_make_location())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_attempts_exhausted_before_raising(monkeypatch):
    """All retry attempts are tried before the final exception is raised.

    Validates: Requirements 2.7
    """
    call_count = 0

    class _CountingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                status_code=500,
                headers={"Content-Type": "application/json"},
                content=b'{"message": "error"}',
            )

    adapter = _adapter_with_transport(_CountingTransport(), max_retries=3)
    mock_sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    with pytest.raises(ExternalServiceError):
        await adapter.get_current_conditions(_make_location())

    assert call_count == 3  # all 3 attempts made
    assert mock_sleep.call_count == 2  # sleep between attempts 1→2 and 2→3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exponential_backoff_delays(monkeypatch):
    """Sleep delays follow 2^n * base_delay pattern (1 s base → 1 s, 2 s).

    Validates: exponential-backoff implementation
    """
    class _FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(500, headers={"Content-Type": "application/json"}, content=b"{}")

    adapter = WeatherAPIAdapter(
        api_url="https://fake.weather.api",
        api_key="test-key",
        max_retries=3,
        retry_base_delay=1.0,
    )

    @asynccontextmanager
    async def _patched_make_client():
        async with httpx.AsyncClient(transport=_FailTransport()) as client:
            yield client

    adapter._make_client = _patched_make_client  # type: ignore[method-assign]

    sleep_calls: list = []

    async def _capture_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr("asyncio.sleep", _capture_sleep)

    with pytest.raises(ExternalServiceError):
        await adapter.get_current_conditions(_make_location())

    assert sleep_calls == [1.0, 2.0]  # 1*2^0, 1*2^1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raises_external_service_error_when_no_api_key(monkeypatch):
    """Missing API key raises ExternalServiceError before any network call.

    Validates: secure key handling
    """
    monkeypatch.delenv("WEATHER_API_KEY", raising=False)
    # Do NOT inject api_key — force resolution from env
    adapter = WeatherAPIAdapter(api_url="https://fake.weather.api")

    with pytest.raises(ExternalServiceError, match="API key not configured"):
        await adapter.get_current_conditions(_make_location())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_key_loaded_from_env(monkeypatch):
    """API key is read from WEATHER_API_KEY when not provided explicitly.

    Validates: WEATHER_API_KEY env-var support
    """
    monkeypatch.setenv("WEATHER_API_KEY", "env-key")
    adapter = WeatherAPIAdapter(
        api_url="https://fake.weather.api",
        retry_base_delay=0.0,
    )

    @asynccontextmanager
    async def _patched_make_client():
        async with httpx.AsyncClient(transport=_MockTransport(200, _owm_weather_payload())) as client:
            yield client

    adapter._make_client = _patched_make_client  # type: ignore[method-assign]

    conditions = await adapter.get_current_conditions(_make_location())
    assert conditions.temperature == 15.5
