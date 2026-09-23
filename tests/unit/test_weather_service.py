"""
Unit tests for CachedWeatherDataService.

Tests validate the multi-layer cache strategy and timeout/error handling
for current weather conditions retrieval and refresh operations.

Validates: Requirements 2.1, 2.5, 2.6, 2.7, 7.1, 7.3, 7.4
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.application.weather_service import (
    CachedWeatherDataService,
    _NORMAL_TIMEOUT,
    _REFRESH_TIMEOUT,
)
from src.domain.exceptions import CacheError, ExternalServiceError
from src.domain.exceptions import TimeoutError as DomainTimeoutError
from src.domain.models import CurrentConditions, Location
from src.infrastructure.cache.cache_strategy import CacheStrategy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _make_conditions(temp: float = 15.5) -> CurrentConditions:
    return CurrentConditions(
        temperature=temp,
        feels_like=13.0,
        humidity=65,
        pressure=1013,
        wind_speed=5.3,
        wind_direction=220,
        condition="light rain",
        icon="10d",
        timestamp=datetime(2024, 1, 16, 14, 0, 0, tzinfo=timezone.utc),
        sunrise=None,
        sunset=None,
        visibility=9000,
    )


def _make_service(
    port_return=None,
    port_raise=None,
    cache_get_return=None,
    cache_get_raise=None,
    cache_put_raise=None,
) -> tuple[CachedWeatherDataService, AsyncMock, AsyncMock, AsyncMock]:
    """Build a CachedWeatherDataService with fully mocked dependencies.

    Returns:
        (service, mock_port, mock_cache_get, mock_cache_put)
    """
    mock_port = AsyncMock()
    if port_raise:
        mock_port.get_current_conditions.side_effect = port_raise
    else:
        mock_port.get_current_conditions.return_value = port_return or _make_conditions()

    mock_cache = AsyncMock()

    if cache_get_raise:
        mock_cache.get_current_conditions.side_effect = cache_get_raise
    else:
        mock_cache.get_current_conditions.return_value = cache_get_return  # None = cache miss

    if cache_put_raise:
        mock_cache.put_current_conditions.side_effect = cache_put_raise
    else:
        mock_cache.put_current_conditions.return_value = None

    strategy = CacheStrategy()
    service = CachedWeatherDataService(
        weather_port=mock_port,
        cache=mock_cache,
        cache_strategy=strategy,
    )
    return service, mock_port, mock_cache.get_current_conditions, mock_cache.put_current_conditions


# ---------------------------------------------------------------------------
# Cache hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_data_without_calling_port():
    """When the cache returns data, the port must not be called.

    Validates: Requirements 2.5 (cache serves fresh data)
    """
    cached_conditions = _make_conditions(temp=10.0)
    service, mock_port, _, _ = _make_service(cache_get_return=cached_conditions)

    result = await service.get_current_conditions(_make_location())

    assert result is cached_conditions
    mock_port.get_current_conditions.assert_not_called()


@pytest.mark.asyncio
async def test_cache_hit_returns_expected_temperature():
    """Temperature from cache is preserved exactly."""
    cached_conditions = _make_conditions(temp=22.5)
    service, _, _, _ = _make_service(cache_get_return=cached_conditions)

    result = await service.get_current_conditions(_make_location())

    assert result.temperature == 22.5


# ---------------------------------------------------------------------------
# Cache miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_calls_port():
    """When the cache returns None, the port must be called.

    Validates: Requirements 2.1
    """
    live_conditions = _make_conditions(temp=15.5)
    service, mock_port, _, _ = _make_service(
        port_return=live_conditions,
        cache_get_return=None,
    )

    result = await service.get_current_conditions(_make_location())

    assert result is live_conditions
    mock_port.get_current_conditions.assert_called_once()


@pytest.mark.asyncio
async def test_cache_miss_caches_port_result():
    """Successful live fetch must write result to cache.

    Validates: Requirements 2.5
    """
    live_conditions = _make_conditions()
    service, _, _, mock_cache_put = _make_service(
        port_return=live_conditions,
        cache_get_return=None,
    )

    await service.get_current_conditions(_make_location())

    mock_cache_put.assert_called_once()
    call_kwargs = mock_cache_put.call_args
    # Second positional arg (or kwarg) is the conditions being cached
    args, kwargs = call_kwargs
    stored_conditions = args[1] if len(args) > 1 else kwargs.get("conditions")
    assert stored_conditions is live_conditions


@pytest.mark.asyncio
async def test_cache_miss_uses_correct_ttl():
    """Cache write uses CURRENT_TTL_MINUTES (15) from CacheStrategy.

    Validates: Requirements 2.5 — 15-minute TTL for current conditions
    """
    service, _, _, mock_cache_put = _make_service(cache_get_return=None)

    await service.get_current_conditions(_make_location())

    _, kwargs = mock_cache_put.call_args
    # ttl_minutes passed as keyword arg
    assert kwargs.get("ttl_minutes") == CacheStrategy.CURRENT_TTL_MINUTES


# ---------------------------------------------------------------------------
# Force refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cache():
    """force_refresh=True must skip the cache read entirely.

    Validates: Requirements 7.1
    """
    live_conditions = _make_conditions(temp=18.0)
    service, mock_port, mock_cache_get, _ = _make_service(
        port_return=live_conditions,
        cache_get_return=_make_conditions(temp=10.0),  # stale-looking cached value
    )

    result = await service.get_current_conditions(_make_location(), force_refresh=True)

    # Cache must not be consulted
    mock_cache_get.assert_not_called()
    # Live data is returned
    assert result.temperature == 18.0
    mock_port.get_current_conditions.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_current_conditions_bypasses_cache():
    """refresh_current_conditions delegates to force_refresh path.

    Validates: Requirements 7.1
    """
    live_conditions = _make_conditions(temp=20.0)
    service, mock_port, mock_cache_get, _ = _make_service(
        port_return=live_conditions,
        cache_get_return=_make_conditions(temp=5.0),
    )

    result = await service.refresh_current_conditions(_make_location())

    mock_cache_get.assert_not_called()
    assert result.temperature == 20.0


@pytest.mark.asyncio
async def test_force_refresh_still_caches_result():
    """Successful force-refresh result is written to cache.

    Validates: Requirements 2.5 (cache refreshed after force-fetch)
    """
    live_conditions = _make_conditions()
    service, _, _, mock_cache_put = _make_service(
        port_return=live_conditions,
        cache_get_return=None,
    )

    await service.get_current_conditions(_make_location(), force_refresh=True)

    mock_cache_put.assert_called_once()


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_fetch_timeout_raises_domain_timeout_error():
    """asyncio.TimeoutError from the port is wrapped in domain TimeoutError.

    Validates: Requirements 2.6 — 10-second timeout for normal fetch
    """
    service, mock_port, _, _ = _make_service(cache_get_return=None)
    mock_port.get_current_conditions.side_effect = asyncio.TimeoutError()

    with pytest.raises(DomainTimeoutError):
        await service.get_current_conditions(_make_location())


@pytest.mark.asyncio
async def test_refresh_timeout_raises_domain_timeout_error():
    """asyncio.TimeoutError during force-refresh is wrapped in DomainTimeoutError.

    Validates: Requirements 7.3 — 5-second timeout for refresh
    """
    service, mock_port, _, _ = _make_service(cache_get_return=None)
    mock_port.get_current_conditions.side_effect = asyncio.TimeoutError()

    with pytest.raises(DomainTimeoutError):
        await service.refresh_current_conditions(_make_location())


@pytest.mark.asyncio
async def test_normal_timeout_applied_via_wait_for():
    """The service passes _NORMAL_TIMEOUT (10s) to asyncio.wait_for for normal fetches.

    Validates: Requirements 2.6
    """
    service, _, _, _ = _make_service(cache_get_return=None)

    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
        mock_wait_for.return_value = _make_conditions()
        await service.get_current_conditions(_make_location())

    _, kwargs = mock_wait_for.call_args
    assert kwargs.get("timeout") == _NORMAL_TIMEOUT


@pytest.mark.asyncio
async def test_refresh_timeout_applied_via_wait_for():
    """The service passes _REFRESH_TIMEOUT (5s) to asyncio.wait_for for refresh.

    Validates: Requirements 7.3
    """
    service, _, _, _ = _make_service(cache_get_return=None)

    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
        mock_wait_for.return_value = _make_conditions()
        await service.get_current_conditions(_make_location(), force_refresh=True)

    _, kwargs = mock_wait_for.call_args
    assert kwargs.get("timeout") == _REFRESH_TIMEOUT


# ---------------------------------------------------------------------------
# Port error propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_port_external_service_error_propagates():
    """ExternalServiceError from the port is not swallowed.

    Validates: Requirements 2.7
    """
    service, _, _, _ = _make_service(
        port_raise=ExternalServiceError("API unavailable"),
        cache_get_return=None,
    )

    with pytest.raises(ExternalServiceError, match="API unavailable"):
        await service.get_current_conditions(_make_location())


@pytest.mark.asyncio
async def test_port_error_does_not_cache():
    """On port failure, no cache write must occur.

    Validates: Requirements 2.7 — failed fetches must not pollute cache
    """
    service, _, _, mock_cache_put = _make_service(
        port_raise=ExternalServiceError("API down"),
        cache_get_return=None,
    )

    with pytest.raises(ExternalServiceError):
        await service.get_current_conditions(_make_location())

    mock_cache_put.assert_not_called()


# ---------------------------------------------------------------------------
# Cache error tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_read_error_is_tolerated_and_falls_back_to_port():
    """CacheError on read triggers fallback to the live fetch.

    Validates: graceful degradation on cache failure
    """
    live_conditions = _make_conditions(temp=15.5)
    service, mock_port, _, _ = _make_service(
        port_return=live_conditions,
        cache_get_raise=CacheError("DynamoDB read timeout"),
    )

    result = await service.get_current_conditions(_make_location())

    # Service still returns live data despite cache failure
    assert result is live_conditions
    mock_port.get_current_conditions.assert_called_once()


@pytest.mark.asyncio
async def test_cache_write_error_is_tolerated_data_still_returned():
    """CacheError on write must not prevent returning the live result.

    Validates: cache write errors are non-fatal (data returned to caller)
    """
    live_conditions = _make_conditions(temp=15.5)
    service, _, _, _ = _make_service(
        port_return=live_conditions,
        cache_get_return=None,
        cache_put_raise=CacheError("DynamoDB write timeout"),
    )

    # Must not raise; live data is still returned.
    result = await service.get_current_conditions(_make_location())

    assert result is live_conditions


@pytest.mark.asyncio
async def test_cache_write_error_after_force_refresh_is_tolerated():
    """Cache write errors during force-refresh do not propagate.

    Validates: Requirements 7.1 — refresh succeeds even if re-caching fails
    """
    live_conditions = _make_conditions()
    service, _, _, _ = _make_service(
        port_return=live_conditions,
        cache_get_return=None,
        cache_put_raise=CacheError("write failed"),
    )

    result = await service.get_current_conditions(_make_location(), force_refresh=True)

    assert result is live_conditions


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correct_cache_key_used_for_location():
    """The cache is queried with the key matching location lat/lon + 'current'.

    Validates: cache key format matches CacheStrategy.get_cache_key
    """
    location = _make_location()
    expected_key = CacheStrategy.get_cache_key(location, "current")

    service, _, mock_cache_get, _ = _make_service(cache_get_return=None)

    await service.get_current_conditions(location)

    mock_cache_get.assert_called_once_with(expected_key)
