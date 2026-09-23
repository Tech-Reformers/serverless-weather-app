"""
CachedWeatherDataService — application service for current weather conditions.

Implements WeatherDataServicePort with a multi-layer cache strategy:

  1. On a normal request (force_refresh=False):
       - Build cache key via CacheStrategy.get_cache_key
       - Attempt to read from DynamoDBWeatherCache
       - If the entry exists and is still within TTL → return it
       - Otherwise fetch live from the underlying WeatherDataServicePort
         (10-second asyncio timeout, Requirements 2.6)
       - Cache the live result on success (15-minute TTL, Requirements 2.5)

  2. On a forced refresh (force_refresh=True / refresh_current_conditions):
       - Bypass cache entirely
       - Fetch live with a 5-second asyncio timeout (Requirements 7.3,
         Property 15 and 24)
       - Cache the live result on success

  3. CacheError is tolerated — the service logs a warning and continues to
     the live fetch rather than surfacing cache failures to the caller.

  4. asyncio.TimeoutError is re-raised as the domain TimeoutError so that
     callers receive a typed, catchable exception (Requirements 2.6, 7.3).

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 7.1, 7.2, 7.3, 7.4
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.domain.exceptions import CacheError
from src.domain.exceptions import TimeoutError as DomainTimeoutError
from src.domain.models import CurrentConditions, Location
from src.domain.services import WeatherDataServicePort
from src.infrastructure.cache.cache_strategy import CacheStrategy
from src.infrastructure.cache.dynamodb_cache import DynamoDBWeatherCache

logger = logging.getLogger(__name__)

# Timeouts used per-operation (seconds).
_NORMAL_TIMEOUT: float = 10.0   # Requirements 2.6
_REFRESH_TIMEOUT: float = 5.0   # Requirements 7.3


class CachedWeatherDataService(WeatherDataServicePort):
    """Weather data service with DynamoDB cache fallback.

    Args:
        weather_port: The upstream WeatherDataServicePort implementation
            that performs real external API calls (e.g. WeatherAPIAdapter).
        cache: The DynamoDBWeatherCache adapter used for reads and writes.
        cache_strategy: CacheStrategy instance that provides TTL constants
            and cache-key generation.
    """

    def __init__(
        self,
        weather_port: WeatherDataServicePort,
        cache: DynamoDBWeatherCache,
        cache_strategy: Optional[CacheStrategy] = None,
    ) -> None:
        self._port = weather_port
        self._cache = cache
        self._strategy = cache_strategy or CacheStrategy()

    # ------------------------------------------------------------------
    # WeatherDataServicePort implementation
    # ------------------------------------------------------------------

    async def get_current_conditions(
        self,
        location: Location,
        force_refresh: bool = False,
    ) -> CurrentConditions:
        """Return current weather conditions for *location*.

        Checks the cache first unless *force_refresh* is ``True``.
        On a cache miss the live API is called and the result is cached.

        Args:
            location: The target location.
            force_refresh: When ``True`` the cache is bypassed and a
                5-second timeout is applied (Requirements 7.3).

        Returns:
            A :class:`~src.domain.models.CurrentConditions` instance.

        Raises:
            TimeoutError: If the live fetch exceeds the operation timeout.
            ExternalServiceError: If the upstream API returns a failure.
        """
        timeout = _REFRESH_TIMEOUT if force_refresh else _NORMAL_TIMEOUT

        if not force_refresh:
            cached = await self._read_cache(location)
            if cached is not None:
                logger.debug(
                    "Cache hit for location=%s", location.id
                )
                return cached

        # Live fetch with timeout.
        conditions = await self._fetch_with_timeout(location, timeout)

        # Cache the successful result (non-blocking error handling).
        await self._write_cache(location, conditions)

        return conditions

    async def refresh_current_conditions(self, location: Location) -> CurrentConditions:
        """Force-refresh current conditions, bypassing all caches.

        Applies a 5-second timeout (Requirements 7.3, Properties 15, 24).

        Args:
            location: The target location.

        Returns:
            Freshly retrieved :class:`~src.domain.models.CurrentConditions`.

        Raises:
            TimeoutError: If the refresh exceeds 5 seconds.
            ExternalServiceError: If the upstream API fails.
        """
        return await self.get_current_conditions(location, force_refresh=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read_cache(self, location: Location) -> Optional[CurrentConditions]:
        """Attempt to return a fresh cached entry; return None on miss or error."""
        cache_key = CacheStrategy.get_cache_key(location, "current")
        try:
            conditions = await self._cache.get_current_conditions(cache_key)
        except CacheError as exc:
            logger.warning(
                "Cache read failed for location=%s, falling back to live fetch: %s",
                location.id,
                exc,
            )
            return None

        if conditions is None:
            logger.debug("Cache miss for location=%s", location.id)
            return None

        # DynamoDBWeatherCache already enforces TTL internally; if we received
        # a result it is still within the TTL window.
        return conditions

    async def _fetch_with_timeout(
        self,
        location: Location,
        timeout: float,
    ) -> CurrentConditions:
        """Call the underlying port with an asyncio timeout.

        Args:
            location: Target location.
            timeout: Maximum seconds to wait.

        Returns:
            Current conditions from the live API.

        Raises:
            TimeoutError: Wraps asyncio.TimeoutError.
            ExternalServiceError: Propagated from the port on API failure.
        """
        try:
            return await asyncio.wait_for(
                self._port.get_current_conditions(location),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise DomainTimeoutError(
                f"Weather data retrieval timed out after {timeout}s for "
                f"location {location.id!r}"
            ) from exc

    async def _write_cache(
        self,
        location: Location,
        conditions: CurrentConditions,
    ) -> None:
        """Write *conditions* to the cache, tolerating write failures.

        A CacheError here must not prevent the caller from receiving the
        freshly retrieved data — we log a warning and continue.
        """
        cache_key = CacheStrategy.get_cache_key(location, "current")
        try:
            await self._cache.put_current_conditions(
                cache_key,
                conditions,
                ttl_minutes=CacheStrategy.CURRENT_TTL_MINUTES,
            )
            logger.debug("Cached current conditions for location=%s", location.id)
        except CacheError as exc:
            logger.warning(
                "Cache write failed for location=%s (data still returned): %s",
                location.id,
                exc,
            )
