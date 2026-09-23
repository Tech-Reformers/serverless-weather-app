"""
Application service: CachedForecastService.

Wraps a ForecastServicePort with a DynamoDB caching layer so that
repeat requests for the same location reuse stored forecast data
until the TTL expires, reducing external API calls and latency.

Cache TTLs (from CacheStrategy):
  - Hourly forecast: 60 minutes (FORECAST_TTL_MINUTES)
  - Daily forecast:  60 minutes (FORECAST_TTL_MINUTES)

Timeout: both operations enforce a 10-second deadline as required by
Requirements 3.1 and 3.2 (Property 22).

On forecast failure (timeout or provider error) this service raises the
exception rather than swallowing it.  The API layer is responsible for
showing the forecast-section error while retaining current conditions
(Requirements 3.5, 3.6, Property 16).
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

from src.domain.exceptions import CacheError, ExternalServiceError, TimeoutError
from src.domain.models import DailyForecast, HourlyForecast, Location
from src.domain.services import ForecastServicePort
from src.infrastructure.cache.cache_strategy import CacheStrategy
from src.infrastructure.cache.dynamodb_cache import DynamoDBWeatherCache

logger = logging.getLogger(__name__)

_FORECAST_TIMEOUT_SECONDS: float = 10.0


class CachedForecastService:
    """Application service for weather forecasts with DynamoDB caching.

    Implements the same interface as :class:`~src.domain.services.ForecastServicePort`
    but is not declared as a subclass so that it can be composed with any
    concrete port implementation without inheritance coupling.

    Args:
        forecast_port: The underlying forecast data source (e.g. an adapter
            that calls the external Weather API).
        cache: DynamoDB cache adapter for reading/writing stored forecasts.
        cache_strategy: TTL logic and cache-key generation helper.
    """

    def __init__(
        self,
        forecast_port: ForecastServicePort,
        cache: DynamoDBWeatherCache,
        cache_strategy: CacheStrategy,
    ) -> None:
        self._port = forecast_port
        self._cache = cache
        self._strategy = cache_strategy

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get_hourly_forecast(
        self,
        location: Location,
        hours: int = 24,
    ) -> List[HourlyForecast]:
        """Return the next *hours* hours of forecast data.

        Checks the DynamoDB cache first; fetches from the underlying port
        on a cache miss, then persists the result for future calls.

        Args:
            location: The target location.
            hours: Number of hourly entries to return (default 24,
                Requirements 3.1, 3.3, Property 11).

        Returns:
            A list of :class:`~src.domain.models.HourlyForecast` entries
            sorted in ascending chronological order (Property 11).

        Raises:
            TimeoutError: If the external fetch exceeds 10 seconds
                (Requirements 3.5, Property 22).
            ExternalServiceError: If the provider returns a failure
                response (Requirements 3.6, Property 16).
        """
        cache_key = CacheStrategy.get_cache_key(location, "hourly")

        # --- cache read ---
        cached = await self._read_cache_forecast(cache_key, "hourly")
        if cached is not None:
            logger.debug("Cache hit for hourly forecast key=%s", cache_key)
            return _sorted_hourly(cached[:hours])

        # --- fetch from port with timeout ---
        logger.debug("Cache miss for hourly forecast key=%s, fetching from port", cache_key)
        entries = await self._fetch_hourly_with_timeout(location, hours)

        # --- populate cache (best-effort; errors are tolerated) ---
        await self._write_cache_forecast(cache_key, "hourly", entries)

        return _sorted_hourly(entries)

    async def get_daily_forecast(
        self,
        location: Location,
        days: int = 7,
    ) -> List[DailyForecast]:
        """Return the next *days* days of forecast data.

        Checks the DynamoDB cache first; fetches from the underlying port
        on a cache miss, then persists the result for future calls.

        Args:
            location: The target location.
            days: Number of daily entries to return (default 7,
                Requirements 3.2, 3.4, Property 11).

        Returns:
            A list of :class:`~src.domain.models.DailyForecast` entries
            sorted in ascending chronological order (Property 11).

        Raises:
            TimeoutError: If the external fetch exceeds 10 seconds
                (Requirements 3.5, Property 22).
            ExternalServiceError: If the provider returns a failure
                response (Requirements 3.6, Property 16).
        """
        cache_key = CacheStrategy.get_cache_key(location, "daily")

        # --- cache read ---
        cached = await self._read_cache_forecast(cache_key, "daily")
        if cached is not None:
            logger.debug("Cache hit for daily forecast key=%s", cache_key)
            return _sorted_daily(cached[:days])

        # --- fetch from port with timeout ---
        logger.debug("Cache miss for daily forecast key=%s, fetching from port", cache_key)
        entries = await self._fetch_daily_with_timeout(location, days)

        # --- populate cache (best-effort; errors are tolerated) ---
        await self._write_cache_forecast(cache_key, "daily", entries)

        return _sorted_daily(entries)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _read_cache_forecast(
        self,
        cache_key: str,
        forecast_type: str,
    ) -> list | None:
        """Read a forecast list from DynamoDB cache.

        Returns the cached list when fresh, ``None`` on a miss, a stale
        entry, or a cache error (cache errors are tolerated so that the
        service degrades gracefully).
        """
        try:
            raw = await self._cache.get_forecast(cache_key, forecast_type)
        except CacheError as exc:
            logger.warning(
                "Cache read error for key=%s type=%s — falling through to port: %s",
                cache_key,
                forecast_type,
                exc,
            )
            return None

        if raw is None:
            return None

        # Build a minimal entry dict so CacheStrategy can validate freshness.
        # DynamoDBWeatherCache already enforces TTL via the epoch ``ttl``
        # attribute, so if we got a non-None result it is already fresh.
        return raw

    async def _write_cache_forecast(
        self,
        cache_key: str,
        forecast_type: str,
        data: list,
    ) -> None:
        """Write a forecast list to DynamoDB cache (best-effort)."""
        try:
            await self._cache.put_forecast(
                cache_key,
                forecast_type,
                data,
                ttl_minutes=CacheStrategy.FORECAST_TTL_MINUTES,
            )
        except CacheError as exc:
            logger.warning(
                "Cache write error for key=%s type=%s (non-fatal): %s",
                cache_key,
                forecast_type,
                exc,
            )

    async def _fetch_hourly_with_timeout(
        self,
        location: Location,
        hours: int,
    ) -> List[HourlyForecast]:
        """Invoke the port with a 10-second timeout, mapping asyncio.TimeoutError."""
        try:
            return await asyncio.wait_for(
                self._port.get_hourly_forecast(location, hours),
                timeout=_FORECAST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Hourly forecast request for location '{location.id}' "
                f"exceeded {_FORECAST_TIMEOUT_SECONDS}s timeout."
            ) from exc

    async def _fetch_daily_with_timeout(
        self,
        location: Location,
        days: int,
    ) -> List[DailyForecast]:
        """Invoke the port with a 10-second timeout, mapping asyncio.TimeoutError."""
        try:
            return await asyncio.wait_for(
                self._port.get_daily_forecast(location, days),
                timeout=_FORECAST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Daily forecast request for location '{location.id}' "
                f"exceeded {_FORECAST_TIMEOUT_SECONDS}s timeout."
            ) from exc


# ------------------------------------------------------------------
# Pure ordering helpers (Property 11)
# ------------------------------------------------------------------


def _sorted_hourly(entries: List[HourlyForecast]) -> List[HourlyForecast]:
    """Return *entries* sorted by timestamp ascending."""
    return sorted(entries, key=lambda e: e.timestamp)


def _sorted_daily(entries: List[DailyForecast]) -> List[DailyForecast]:
    """Return *entries* sorted by date ascending."""
    return sorted(entries, key=lambda e: e.date)
