"""
DynamoDB-backed weather cache adapter.

Provides read/write/invalidate operations for current-conditions and
forecast data stored in the ``weather-app-cache`` DynamoDB table.  All
DynamoDB I/O is synchronous boto3 wrapped in ``asyncio.get_event_loop
().run_in_executor`` so the adapter can be awaited from async code
without blocking the event loop.

Table schema
------------
Partition key  ``cache_key``   (String)
Sort key       ``cache_type``  (String) — "current" | "hourly" | "daily"
Attribute      ``data``        (Map)    — serialised domain object
Attribute      ``timestamp``   (String) — ISO-8601 UTC write time
Attribute      ``ttl``         (Number) — epoch seconds for DynamoDB TTL

Environment variables
---------------------
CACHE_TABLE_NAME
    Name of the DynamoDB table (default: ``"weather-app-cache"``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.domain.exceptions import CacheError
from src.domain.models import (
    CurrentConditions,
    DailyForecast,
    HourlyForecast,
    Location,
)

logger = logging.getLogger(__name__)

_DEFAULT_TABLE = "weather-app-cache"

# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _datetime_to_iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime to an ISO-8601 string, or return None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _iso_to_datetime(value: Optional[str]) -> Optional[datetime]:
    """Deserialise an ISO-8601 string to a datetime, or return None."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _serialise_current_conditions(conditions: CurrentConditions) -> dict[str, Any]:
    return {
        "temperature": str(conditions.temperature),
        "feels_like": str(conditions.feels_like),
        "humidity": str(conditions.humidity),
        "pressure": str(conditions.pressure),
        "wind_speed": str(conditions.wind_speed),
        "wind_direction": str(conditions.wind_direction),
        "condition": conditions.condition,
        "icon": conditions.icon,
        "timestamp": _datetime_to_iso(conditions.timestamp),
        "sunrise": _datetime_to_iso(conditions.sunrise),
        "sunset": _datetime_to_iso(conditions.sunset),
        "visibility": str(conditions.visibility),
    }


def _deserialise_current_conditions(data: dict[str, Any]) -> CurrentConditions:
    return CurrentConditions(
        temperature=float(data["temperature"]),
        feels_like=float(data["feels_like"]),
        humidity=int(data["humidity"]),
        pressure=int(data["pressure"]),
        wind_speed=float(data["wind_speed"]),
        wind_direction=int(data["wind_direction"]),
        condition=str(data["condition"]),
        icon=str(data["icon"]),
        timestamp=_iso_to_datetime(data.get("timestamp")) or datetime.now(tz=timezone.utc),
        sunrise=_iso_to_datetime(data.get("sunrise")),
        sunset=_iso_to_datetime(data.get("sunset")),
        visibility=int(data["visibility"]),
    )


def _serialise_hourly_forecast(entry: HourlyForecast) -> dict[str, Any]:
    return {
        "timestamp": _datetime_to_iso(entry.timestamp),
        "temperature": str(entry.temperature),
        "feels_like": str(entry.feels_like),
        "humidity": str(entry.humidity),
        "condition": entry.condition,
        "icon": entry.icon,
        "precipitation_probability": str(entry.precipitation_probability),
        "wind_speed": str(entry.wind_speed),
    }


def _deserialise_hourly_forecast(data: dict[str, Any]) -> HourlyForecast:
    return HourlyForecast(
        timestamp=_iso_to_datetime(data.get("timestamp")) or datetime.now(tz=timezone.utc),
        temperature=float(data["temperature"]),
        feels_like=float(data["feels_like"]),
        humidity=int(data["humidity"]),
        condition=str(data["condition"]),
        icon=str(data["icon"]),
        precipitation_probability=int(data["precipitation_probability"]),
        wind_speed=float(data["wind_speed"]),
    )


def _serialise_daily_forecast(entry: DailyForecast) -> dict[str, Any]:
    return {
        "date": _datetime_to_iso(entry.date),
        "high_temp": str(entry.high_temp),
        "low_temp": str(entry.low_temp),
        "condition": entry.condition,
        "icon": entry.icon,
        "precipitation_probability": str(entry.precipitation_probability),
        "sunrise": _datetime_to_iso(entry.sunrise),
        "sunset": _datetime_to_iso(entry.sunset),
    }


def _deserialise_daily_forecast(data: dict[str, Any]) -> DailyForecast:
    return DailyForecast(
        date=_iso_to_datetime(data.get("date")) or datetime.now(tz=timezone.utc),
        high_temp=float(data["high_temp"]),
        low_temp=float(data["low_temp"]),
        condition=str(data["condition"]),
        icon=str(data["icon"]),
        precipitation_probability=int(data["precipitation_probability"]),
        sunrise=_iso_to_datetime(data.get("sunrise")) or datetime.now(tz=timezone.utc),
        sunset=_iso_to_datetime(data.get("sunset")) or datetime.now(tz=timezone.utc),
    )


def _compute_ttl_epoch(ttl_minutes: int) -> int:
    """Return a Unix epoch timestamp *ttl_minutes* from now."""
    expiry = datetime.now(tz=timezone.utc) + timedelta(minutes=ttl_minutes)
    return int(expiry.timestamp())


# ---------------------------------------------------------------------------
# DynamoDB cache adapter
# ---------------------------------------------------------------------------


class DynamoDBWeatherCache:
    """DynamoDB-backed cache for weather data.

    All public methods are coroutines (``async def``) and wrap synchronous
    boto3 calls with ``run_in_executor`` to avoid blocking the event loop.

    Args:
        table_name: Override the table name (reads ``CACHE_TABLE_NAME``
            environment variable when ``None``; falls back to
            ``"weather-app-cache"``).
        dynamodb_resource: Injected boto3 DynamoDB resource (useful for
            tests).  A new resource is created when ``None``.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        dynamodb_resource: Any = None,
    ) -> None:
        self._table_name = table_name or os.environ.get("CACHE_TABLE_NAME", _DEFAULT_TABLE)
        self._resource = dynamodb_resource or boto3.resource("dynamodb")
        self._table = self._resource.Table(self._table_name)

    # ------------------------------------------------------------------
    # Internal execution helper
    # ------------------------------------------------------------------

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking boto3 call in the default thread-pool executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------
    # Current conditions
    # ------------------------------------------------------------------

    async def get_current_conditions(
        self,
        cache_key: str,
    ) -> Optional[CurrentConditions]:
        """Retrieve current conditions from cache.

        Returns ``None`` when the entry is absent or its DynamoDB TTL has
        already been exceeded (the TTL attribute is checked independently
        of DynamoDB's own background expiry so that stale entries are
        never served — **Property 3**).

        Args:
            cache_key: The composite cache key for this location/type pair.

        Returns:
            A :class:`~src.domain.models.CurrentConditions` instance, or
            ``None`` when the cache is cold or stale.

        Raises:
            CacheError: On DynamoDB read failure.
        """
        try:
            response = await self._run(
                self._table.get_item,
                Key={"cache_key": cache_key, "cache_type": "current"},
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("DynamoDB get_item failed for key=%s: %s", cache_key, exc)
            raise CacheError(f"Failed to read current conditions from cache: {exc}") from exc

        item = response.get("Item")
        if not item:
            return None

        # Honour TTL: reject entries that have logically expired even if
        # DynamoDB has not yet physically removed them.
        ttl_epoch = item.get("ttl")
        if ttl_epoch is not None:
            now_epoch = int(datetime.now(tz=timezone.utc).timestamp())
            if now_epoch >= int(ttl_epoch):
                logger.debug("Cache entry expired for key=%s (TTL=%s)", cache_key, ttl_epoch)
                return None

        try:
            return _deserialise_current_conditions(item["data"])
        except (KeyError, ValueError) as exc:
            logger.warning("Failed to deserialise current conditions for key=%s: %s", cache_key, exc)
            return None

    async def put_current_conditions(
        self,
        cache_key: str,
        conditions: CurrentConditions,
        ttl_minutes: int = 15,
    ) -> None:
        """Write current conditions to the cache with a TTL.

        Args:
            cache_key: The composite cache key for this entry.
            conditions: The domain object to persist.
            ttl_minutes: Time-to-live in minutes (default 15 per
                Requirements 2.5 and Property 3).

        Raises:
            CacheError: On DynamoDB write failure.
        """
        now_iso = _datetime_to_iso(datetime.now(tz=timezone.utc))
        item = {
            "cache_key": cache_key,
            "cache_type": "current",
            "data": _serialise_current_conditions(conditions),
            "timestamp": now_iso,
            "ttl": _compute_ttl_epoch(ttl_minutes),
        }
        try:
            await self._run(self._table.put_item, Item=item)
        except (BotoCoreError, ClientError) as exc:
            logger.error("DynamoDB put_item failed for key=%s: %s", cache_key, exc)
            raise CacheError(f"Failed to write current conditions to cache: {exc}") from exc

    # ------------------------------------------------------------------
    # Forecast (hourly / daily)
    # ------------------------------------------------------------------

    async def get_forecast(
        self,
        cache_key: str,
        forecast_type: str,
    ) -> Optional[List]:
        """Retrieve a forecast list from cache.

        Args:
            cache_key: The composite cache key for this location/type pair.
            forecast_type: ``"hourly"`` or ``"daily"``.

        Returns:
            A list of :class:`~src.domain.models.HourlyForecast` or
            :class:`~src.domain.models.DailyForecast` objects, or ``None``
            when the cache is cold or stale.

        Raises:
            CacheError: On DynamoDB read failure.
        """
        try:
            response = await self._run(
                self._table.get_item,
                Key={"cache_key": cache_key, "cache_type": forecast_type},
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error(
                "DynamoDB get_item failed for key=%s type=%s: %s",
                cache_key,
                forecast_type,
                exc,
            )
            raise CacheError(f"Failed to read {forecast_type} forecast from cache: {exc}") from exc

        item = response.get("Item")
        if not item:
            return None

        # Honour TTL regardless of DynamoDB background expiry (Property 3).
        ttl_epoch = item.get("ttl")
        if ttl_epoch is not None:
            now_epoch = int(datetime.now(tz=timezone.utc).timestamp())
            if now_epoch >= int(ttl_epoch):
                logger.debug(
                    "Cache entry expired for key=%s type=%s (TTL=%s)",
                    cache_key,
                    forecast_type,
                    ttl_epoch,
                )
                return None

        raw_list: list[dict[str, Any]] = item.get("data", [])
        try:
            if forecast_type == "hourly":
                return [_deserialise_hourly_forecast(entry) for entry in raw_list]
            elif forecast_type == "daily":
                return [_deserialise_daily_forecast(entry) for entry in raw_list]
            else:
                logger.warning("Unknown forecast_type=%s for key=%s", forecast_type, cache_key)
                return None
        except (KeyError, ValueError) as exc:
            logger.warning(
                "Failed to deserialise %s forecast for key=%s: %s",
                forecast_type,
                cache_key,
                exc,
            )
            return None

    async def put_forecast(
        self,
        cache_key: str,
        forecast_type: str,
        data: list,
        ttl_minutes: int = 60,
    ) -> None:
        """Write a forecast list to the cache with a TTL.

        Args:
            cache_key: The composite cache key for this entry.
            forecast_type: ``"hourly"`` or ``"daily"``.
            data: List of :class:`~src.domain.models.HourlyForecast` or
                :class:`~src.domain.models.DailyForecast` objects.
            ttl_minutes: Time-to-live in minutes (default 60 per
                Requirements 3.1, 3.2 and Property 3).

        Raises:
            CacheError: On DynamoDB write failure.
        """
        if forecast_type == "hourly":
            serialised = [_serialise_hourly_forecast(entry) for entry in data]
        elif forecast_type == "daily":
            serialised = [_serialise_daily_forecast(entry) for entry in data]
        else:
            raise CacheError(f"Unknown forecast_type '{forecast_type}'")

        now_iso = _datetime_to_iso(datetime.now(tz=timezone.utc))
        item = {
            "cache_key": cache_key,
            "cache_type": forecast_type,
            "data": serialised,
            "timestamp": now_iso,
            "ttl": _compute_ttl_epoch(ttl_minutes),
        }
        try:
            await self._run(self._table.put_item, Item=item)
        except (BotoCoreError, ClientError) as exc:
            logger.error(
                "DynamoDB put_item failed for key=%s type=%s: %s",
                cache_key,
                forecast_type,
                exc,
            )
            raise CacheError(f"Failed to write {forecast_type} forecast to cache: {exc}") from exc

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    async def invalidate(self, cache_key: str) -> None:
        """Delete all cache entries that share *cache_key*.

        Removes every sort-key variant (``"current"``, ``"hourly"``,
        ``"daily"``) so that the next read is guaranteed to bypass the
        cache (Requirements 7.1 force-refresh path).

        Args:
            cache_key: The composite cache key whose entries should be
                purged.

        Raises:
            CacheError: If any DynamoDB delete operation fails.
        """
        cache_types = ("current", "hourly", "daily")
        errors: list[str] = []

        for cache_type in cache_types:
            try:
                await self._run(
                    self._table.delete_item,
                    Key={"cache_key": cache_key, "cache_type": cache_type},
                )
            except (BotoCoreError, ClientError) as exc:
                logger.error(
                    "DynamoDB delete_item failed for key=%s type=%s: %s",
                    cache_key,
                    cache_type,
                    exc,
                )
                errors.append(f"{cache_type}: {exc}")

        if errors:
            raise CacheError(
                f"Failed to invalidate cache entries for key={cache_key}: {'; '.join(errors)}"
            )
