"""
Unit tests for CachedForecastService.

All I/O (DynamoDB cache, external forecast port) is replaced with mocks so
tests run without AWS credentials or network access.

Validates:
  - Cache hit returns cached forecast without calling port
  - Cache miss fetches from port and caches result
  - Hourly results are in chronological order (Property 11, Requirements 3.3)
  - Daily results are in chronological order (Property 11, Requirements 3.4)
  - Timeout raises TimeoutError (Requirements 3.5, Property 22)
  - Port error (ExternalServiceError) is propagated, not swallowed
    (Requirements 3.5, 3.6, Property 16)
  - Cache read error is tolerated and falls through to port
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.application.forecast_service import CachedForecastService
from src.domain.exceptions import CacheError, ExternalServiceError, TimeoutError
from src.domain.models import DailyForecast, HourlyForecast, Location
from src.infrastructure.cache.cache_strategy import CacheStrategy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def location() -> Location:
    return Location(
        id="london-uk",
        name="London",
        region="England",
        country="UK",
        latitude=51.5074,
        longitude=-0.1278,
        timezone="Europe/London",
    )


def _make_hourly_entries(count: int = 24, reverse: bool = False) -> list[HourlyForecast]:
    """Build *count* hourly forecast entries, optionally in reverse order."""
    now = datetime.now(tz=timezone.utc)
    entries = [
        HourlyForecast(
            timestamp=now + timedelta(hours=i),
            temperature=20.0 + i,
            feels_like=19.0 + i,
            humidity=60,
            condition="Clear",
            icon="01d",
            precipitation_probability=0,
            wind_speed=5.0,
        )
        for i in range(count)
    ]
    if reverse:
        entries.reverse()
    return entries


def _make_daily_entries(count: int = 7, reverse: bool = False) -> list[DailyForecast]:
    """Build *count* daily forecast entries, optionally in reverse order."""
    now = datetime.now(tz=timezone.utc)
    entries = [
        DailyForecast(
            date=now + timedelta(days=i),
            high_temp=25.0 + i,
            low_temp=15.0 + i,
            condition="Sunny",
            icon="01d",
            precipitation_probability=5,
            sunrise=now + timedelta(days=i, hours=6),
            sunset=now + timedelta(days=i, hours=20),
        )
        for i in range(count)
    ]
    if reverse:
        entries.reverse()
    return entries


def _make_service(
    *,
    cache_hourly: list | None = None,
    cache_daily: list | None = None,
    cache_error: bool = False,
    port_hourly: list | None = None,
    port_daily: list | None = None,
    port_error: Exception | None = None,
    port_timeout: bool = False,
) -> CachedForecastService:
    """Construct a CachedForecastService with fully mocked dependencies.

    Args:
        cache_hourly: Value returned by cache.get_forecast for "hourly".
            ``None`` simulates a cache miss.
        cache_daily: Value returned by cache.get_forecast for "daily".
        cache_error: When True, cache.get_forecast raises CacheError.
        port_hourly: Value returned by port.get_hourly_forecast.
        port_daily: Value returned by port.get_daily_forecast.
        port_error: Exception raised by the port (instead of returning data).
        port_timeout: When True, the port sleeps forever (triggering timeout).
    """
    # Build mock port
    port = MagicMock()

    if port_timeout:
        async def _sleep_forever(*args, **kwargs):
            await asyncio.sleep(9999)
        port.get_hourly_forecast = _sleep_forever
        port.get_daily_forecast = _sleep_forever
    elif port_error is not None:
        port.get_hourly_forecast = AsyncMock(side_effect=port_error)
        port.get_daily_forecast = AsyncMock(side_effect=port_error)
    else:
        port.get_hourly_forecast = AsyncMock(return_value=port_hourly or [])
        port.get_daily_forecast = AsyncMock(return_value=port_daily or [])

    # Build mock cache
    cache = MagicMock()
    cache.put_forecast = AsyncMock()

    if cache_error:
        cache.get_forecast = AsyncMock(side_effect=CacheError("simulated cache error"))
    else:
        async def _get_forecast(key: str, forecast_type: str):
            if forecast_type == "hourly":
                return cache_hourly
            return cache_daily
        cache.get_forecast = _get_forecast

    return CachedForecastService(
        forecast_port=port,
        cache=cache,
        cache_strategy=CacheStrategy(),
    )


# ---------------------------------------------------------------------------
# Hourly forecast — cache hit
# ---------------------------------------------------------------------------


async def test_hourly_cache_hit_returns_cached_without_port_call(location: Location) -> None:
    """When cache has fresh hourly data, the port should not be called."""
    cached = _make_hourly_entries(24)
    svc = _make_service(cache_hourly=cached)

    result = await svc.get_hourly_forecast(location)

    assert result == sorted(cached, key=lambda e: e.timestamp)
    # port was never awaited
    svc._port.get_hourly_forecast.assert_not_called()  # type: ignore[attr-defined]


async def test_hourly_cache_hit_does_not_write_cache(location: Location) -> None:
    """A cache hit must not trigger a redundant cache write."""
    cached = _make_hourly_entries(24)
    svc = _make_service(cache_hourly=cached)

    await svc.get_hourly_forecast(location)

    svc._cache.put_forecast.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Hourly forecast — cache miss
# ---------------------------------------------------------------------------


async def test_hourly_cache_miss_fetches_from_port(location: Location) -> None:
    """On a cache miss the service should call the port."""
    port_entries = _make_hourly_entries(24)
    svc = _make_service(cache_hourly=None, port_hourly=port_entries)

    result = await svc.get_hourly_forecast(location)

    assert result == sorted(port_entries, key=lambda e: e.timestamp)
    svc._port.get_hourly_forecast.assert_called_once()  # type: ignore[attr-defined]


async def test_hourly_cache_miss_writes_result_to_cache(location: Location) -> None:
    """After a successful port fetch the result should be persisted."""
    port_entries = _make_hourly_entries(24)
    svc = _make_service(cache_hourly=None, port_hourly=port_entries)

    await svc.get_hourly_forecast(location)

    svc._cache.put_forecast.assert_called_once()  # type: ignore[attr-defined]
    call_args = svc._cache.put_forecast.call_args  # type: ignore[attr-defined]
    assert call_args.kwargs.get("forecast_type") == "hourly" or call_args.args[1] == "hourly"


# ---------------------------------------------------------------------------
# Daily forecast — cache hit
# ---------------------------------------------------------------------------


async def test_daily_cache_hit_returns_cached_without_port_call(location: Location) -> None:
    """When cache has fresh daily data, the port should not be called."""
    cached = _make_daily_entries(7)
    svc = _make_service(cache_daily=cached)

    result = await svc.get_daily_forecast(location)

    assert result == sorted(cached, key=lambda e: e.date)
    svc._port.get_daily_forecast.assert_not_called()  # type: ignore[attr-defined]


async def test_daily_cache_hit_does_not_write_cache(location: Location) -> None:
    cached = _make_daily_entries(7)
    svc = _make_service(cache_daily=cached)

    await svc.get_daily_forecast(location)

    svc._cache.put_forecast.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Daily forecast — cache miss
# ---------------------------------------------------------------------------


async def test_daily_cache_miss_fetches_from_port(location: Location) -> None:
    port_entries = _make_daily_entries(7)
    svc = _make_service(cache_daily=None, port_daily=port_entries)

    result = await svc.get_daily_forecast(location)

    assert result == sorted(port_entries, key=lambda e: e.date)
    svc._port.get_daily_forecast.assert_called_once()  # type: ignore[attr-defined]


async def test_daily_cache_miss_writes_result_to_cache(location: Location) -> None:
    port_entries = _make_daily_entries(7)
    svc = _make_service(cache_daily=None, port_daily=port_entries)

    await svc.get_daily_forecast(location)

    svc._cache.put_forecast.assert_called_once()  # type: ignore[attr-defined]
    call_args = svc._cache.put_forecast.call_args  # type: ignore[attr-defined]
    assert call_args.kwargs.get("forecast_type") == "daily" or call_args.args[1] == "daily"


# ---------------------------------------------------------------------------
# Property 11 — chronological order
# ---------------------------------------------------------------------------


async def test_hourly_results_are_in_chronological_order(location: Location) -> None:
    """Hourly entries returned from cache in reverse order must be re-sorted.

    Validates: Property 11, Requirements 3.3
    """
    # Provide reversed entries via cache
    out_of_order = _make_hourly_entries(24, reverse=True)
    svc = _make_service(cache_hourly=out_of_order)

    result = await svc.get_hourly_forecast(location)

    timestamps = [e.timestamp for e in result]
    assert timestamps == sorted(timestamps), "Hourly entries must be chronological"


async def test_daily_results_are_in_chronological_order(location: Location) -> None:
    """Daily entries returned from cache in reverse order must be re-sorted.

    Validates: Property 11, Requirements 3.4
    """
    out_of_order = _make_daily_entries(7, reverse=True)
    svc = _make_service(cache_daily=out_of_order)

    result = await svc.get_daily_forecast(location)

    dates = [e.date for e in result]
    assert dates == sorted(dates), "Daily entries must be chronological"


async def test_hourly_port_results_are_in_chronological_order(location: Location) -> None:
    """Hourly entries fetched from port out-of-order must be re-sorted.

    Validates: Property 11
    """
    out_of_order = _make_hourly_entries(24, reverse=True)
    svc = _make_service(cache_hourly=None, port_hourly=out_of_order)

    result = await svc.get_hourly_forecast(location)

    timestamps = [e.timestamp for e in result]
    assert timestamps == sorted(timestamps)


async def test_daily_port_results_are_in_chronological_order(location: Location) -> None:
    """Daily entries fetched from port out-of-order must be re-sorted.

    Validates: Property 11
    """
    out_of_order = _make_daily_entries(7, reverse=True)
    svc = _make_service(cache_daily=None, port_daily=out_of_order)

    result = await svc.get_daily_forecast(location)

    dates = [e.date for e in result]
    assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# Timeout — Requirements 3.5, Property 22
# ---------------------------------------------------------------------------


async def test_hourly_timeout_raises_timeout_error(location: Location) -> None:
    """A port that never responds must produce TimeoutError within ~10 s.

    The test patches the timeout constant to 0.05 s so the suite stays fast.
    Validates: Requirements 3.5, Property 22
    """
    svc = _make_service(cache_hourly=None, port_timeout=True)

    with patch("src.application.forecast_service._FORECAST_TIMEOUT_SECONDS", 0.05):
        with pytest.raises(TimeoutError):
            await svc.get_hourly_forecast(location)


async def test_daily_timeout_raises_timeout_error(location: Location) -> None:
    """Validates: Requirements 3.5, Property 22"""
    svc = _make_service(cache_daily=None, port_timeout=True)

    with patch("src.application.forecast_service._FORECAST_TIMEOUT_SECONDS", 0.05):
        with pytest.raises(TimeoutError):
            await svc.get_daily_forecast(location)


# ---------------------------------------------------------------------------
# Port errors — Requirements 3.6, Property 16
# ---------------------------------------------------------------------------


async def test_hourly_external_service_error_propagates(location: Location) -> None:
    """ExternalServiceError from port must not be swallowed.

    Validates: Requirements 3.6, Property 16
    """
    svc = _make_service(
        cache_hourly=None,
        port_error=ExternalServiceError("provider down"),
    )

    with pytest.raises(ExternalServiceError):
        await svc.get_hourly_forecast(location)


async def test_daily_external_service_error_propagates(location: Location) -> None:
    """Validates: Requirements 3.6, Property 16"""
    svc = _make_service(
        cache_daily=None,
        port_error=ExternalServiceError("provider down"),
    )

    with pytest.raises(ExternalServiceError):
        await svc.get_daily_forecast(location)


async def test_hourly_port_error_does_not_write_cache(location: Location) -> None:
    """When the port raises, nothing should be written to the cache."""
    svc = _make_service(
        cache_hourly=None,
        port_error=ExternalServiceError("error"),
    )

    with pytest.raises(ExternalServiceError):
        await svc.get_hourly_forecast(location)

    svc._cache.put_forecast.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Cache errors tolerated — fall through to port
# ---------------------------------------------------------------------------


async def test_hourly_cache_read_error_falls_through_to_port(location: Location) -> None:
    """A CacheError on read should be logged and the port called instead."""
    port_entries = _make_hourly_entries(24)
    svc = _make_service(cache_error=True, port_hourly=port_entries)

    result = await svc.get_hourly_forecast(location)

    assert len(result) == 24
    svc._port.get_hourly_forecast.assert_called_once()  # type: ignore[attr-defined]


async def test_daily_cache_read_error_falls_through_to_port(location: Location) -> None:
    """A CacheError on read should be logged and the port called instead."""
    port_entries = _make_daily_entries(7)
    svc = _make_service(cache_error=True, port_daily=port_entries)

    result = await svc.get_daily_forecast(location)

    assert len(result) == 7
    svc._port.get_daily_forecast.assert_called_once()  # type: ignore[attr-defined]


async def test_cache_write_error_does_not_bubble_up(location: Location) -> None:
    """A CacheError on write must not propagate — data was fetched successfully."""
    port_entries = _make_hourly_entries(24)
    svc = _make_service(cache_hourly=None, port_hourly=port_entries)

    # Override put_forecast to raise after a successful get (cache miss path)
    svc._cache.put_forecast = AsyncMock(side_effect=CacheError("write error"))  # type: ignore[attr-defined]

    # Should not raise — CacheError on write is tolerated
    result = await svc.get_hourly_forecast(location)
    assert len(result) == 24
