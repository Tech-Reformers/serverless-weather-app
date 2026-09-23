"""
Lambda handler for weather endpoints.

Routes:
    GET  /weather/current?lat={lat}&lon={lon}&units={celsius|fahrenheit}
    POST /weather/refresh?lat={lat}&lon={lon}

Both routes:
    1. Validate ``lat`` and ``lon`` query parameters (required, valid floats,
       within geographic bounds) — 400 on failure.
    2. Resolve a :class:`~src.domain.models.Location` via
       :class:`~src.application.location_service.LocationService`.
    3. Delegate to :class:`~src.application.weather_service.CachedWeatherDataService`.
    4. Optionally convert temperature values to Fahrenheit when ``units`` is
       supplied (GET /weather/current only; default is Celsius).

POST /weather/refresh additionally:
    - Bypasses all caches for both current conditions AND forecasts (Req 7.1).
    - Fetches updated current conditions, hourly forecast (24 h), and daily
      forecast (7 days) concurrently under a shared 5-second timeout (Req 7.3).
    - Returns a combined payload: current_conditions + hourly + daily +
      last_retrieved (Req 7.2).
    - On timeout → 504 retaining previously displayed data (Req 7.3).
    - On service error → 502 retaining previously displayed data (Req 7.4).

Error mapping:
    ValidationError     → 400
    TimeoutError        → 504 (data could not be retrieved in time, Req 2.6 / 7.3)
    ExternalServiceError→ 502 (upstream failure, Req 2.7)
    All others          → 500

CORS headers are included on every response.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 7.1, 7.2, 7.3, 7.4
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, List, Optional

from src.application.forecast_service import CachedForecastService
from src.application.location_service import LocationService
from src.application.weather_service import CachedWeatherDataService
from src.domain.exceptions import ExternalServiceError
from src.domain.exceptions import TimeoutError as DomainTimeoutError
from src.domain.exceptions import ValidationError as DomainValidationError
from src.domain.validators import ValidationError as ValidatorError
from src.domain.models import CurrentConditions, DailyForecast, HourlyForecast, Location, TempUnit
from src.domain.validators import validate_coordinates, validate_temp_unit
from src.api.middleware.cache_headers import add_no_store_headers, add_public_cache_headers
from src.infrastructure.cache.cache_strategy import CacheStrategy
from src.infrastructure.cache.dynamodb_cache import DynamoDBWeatherCache
from src.infrastructure.external.weather_api_adapter import WeatherAPIAdapter
from src.infrastructure.external.geocoding_adapter import GeocodingAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CORS headers included on every response (Requirements for browser clients)
# ---------------------------------------------------------------------------
_CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Lazy-initialised singletons (reused across warm Lambda invocations)
# ---------------------------------------------------------------------------
_weather_service: Optional[CachedWeatherDataService] = None
_location_service: Optional[LocationService] = None
_forecast_service: Optional[CachedForecastService] = None


def _get_weather_service() -> CachedWeatherDataService:
    """Return the shared :class:`CachedWeatherDataService`, initialising it on
    first call.

    Lazy initialisation avoids AWS SDK calls during the import phase (cold
    start), which reduces the effective cold-start latency.
    """
    global _weather_service
    if _weather_service is None:
        cache = DynamoDBWeatherCache()
        weather_port = WeatherAPIAdapter()
        _weather_service = CachedWeatherDataService(
            weather_port=weather_port,
            cache=cache,
            cache_strategy=CacheStrategy(),
        )
    return _weather_service


def _get_location_service() -> LocationService:
    """Return the shared :class:`LocationService`, initialising it on first
    call."""
    global _location_service
    if _location_service is None:
        geocoding_port = GeocodingAdapter()
        _location_service = LocationService(location_port=geocoding_port)
    return _location_service


def _get_forecast_service() -> CachedForecastService:
    """Return the shared :class:`CachedForecastService`, initialising it on
    first call."""
    global _forecast_service
    if _forecast_service is None:
        cache = DynamoDBWeatherCache()
        weather_port = WeatherAPIAdapter()
        _forecast_service = CachedForecastService(
            forecast_port=weather_port,
            cache=cache,
            cache_strategy=CacheStrategy(),
        )
    return _forecast_service


# ---------------------------------------------------------------------------
# Public Lambda entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """Top-level Lambda handler for all ``/weather/*`` routes.

    Dispatches based on path and HTTP method:

    * ``GET  /weather/current`` → :func:`_handle_current`
    * ``POST /weather/refresh`` → :func:`_handle_refresh`

    Args:
        event: API Gateway proxy event.
        context: Lambda context object (unused).

    Returns:
        API Gateway proxy response dict.
    """
    path: str = event.get("path", "")
    method: str = event.get("httpMethod", "GET").upper()

    # OPTIONS pre-flight — return CORS headers immediately.
    if method == "OPTIONS":
        return _response(200, {})

    if "/current" in path and method == "GET":
        return asyncio.run(_handle_current(event))
    if "/refresh" in path and method == "POST":
        return asyncio.run(_handle_refresh(event))

    return _response(404, {"error": "Not found"})


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _handle_current(event: dict) -> dict:
    """Handle ``GET /weather/current``.

    Query parameters:
        lat (required): Latitude as a float string.
        lon (required): Longitude as a float string.
        units (optional): ``"celsius"`` or ``"fahrenheit"``; defaults to
            ``"celsius"`` per Requirement 6.7.

    Returns:
        200 with serialised :class:`~src.domain.models.CurrentConditions`, or
        an appropriate error response.
    """
    params = event.get("queryStringParameters") or {}

    # --- Coordinate validation ---
    try:
        lat, lon = _parse_coordinates(params)
    except (DomainValidationError, ValidatorError) as exc:
        return _response(400, {"error": str(exc)})

    # --- Temperature unit (optional, default CELSIUS) ---
    try:
        unit = _parse_units(params)
    except (DomainValidationError, ValidatorError) as exc:
        return _response(400, {"error": str(exc)})

    # --- Resolve location ---
    try:
        location = await _resolve_location(lat, lon)
    except DomainTimeoutError as exc:
        logger.warning("Location resolution timed out: %s", exc)
        return _response(504, {"error": "Location resolution timed out. Please try again."})
    except ExternalServiceError as exc:
        logger.error("Location service error: %s", exc)
        return _response(502, {"error": "Location service is currently unavailable."})

    if location is None:
        return _response(404, {"error": f"No location found for coordinates ({lat}, {lon})."})

    # --- Fetch current conditions ---
    try:
        conditions = await _get_weather_service().get_current_conditions(location)
    except DomainTimeoutError as exc:
        logger.warning("Weather retrieval timed out for location=%s: %s", location.id, exc)
        return _response(
            504,
            {
                "error": (
                    "Weather data retrieval timed out. "
                    "Previously displayed conditions are still valid."
                )
            },
        )
    except ExternalServiceError as exc:
        logger.error("Weather API error for location=%s: %s", location.id, exc)
        return _response(
            502,
            {
                "error": (
                    "Weather data could not be retrieved from the provider. "
                    "Previously displayed conditions are still valid."
                )
            },
        )

    # --- Unit conversion (Requirement 2.2 / 6.1) ---
    payload = _serialise_conditions(conditions, unit)
    # Cache current conditions for 5 minutes (design.md §API Gateway Caching, task 10.1)
    return add_public_cache_headers(_response(200, payload))


async def _handle_refresh(event: dict) -> dict:
    """Handle ``POST /weather/refresh``.

    Bypasses all caches for current conditions AND forecasts (Req 7.1) and
    applies the 5-second refresh timeout (Req 7.3).  Fetches current
    conditions, hourly forecast (24 h), and daily forecast (7 days)
    concurrently so that all three complete within the single timeout window.

    Returns a combined payload containing ``current_conditions``, ``hourly``,
    ``daily``, and ``last_retrieved`` so callers can update everything in one
    round-trip (Req 7.2).

    Query parameters:
        lat (required): Latitude as a float string.
        lon (required): Longitude as a float string.

    Returns:
        200 with combined refresh payload, or an error response.
    """
    params = event.get("queryStringParameters") or {}

    # --- Coordinate validation ---
    try:
        lat, lon = _parse_coordinates(params)
    except (DomainValidationError, ValidatorError) as exc:
        return _response(400, {"error": str(exc)})

    # --- Resolve location ---
    try:
        location = await _resolve_location(lat, lon)
    except DomainTimeoutError as exc:
        logger.warning("Location resolution timed out during refresh: %s", exc)
        return _response(504, {"error": "Location resolution timed out. Please try again."})
    except ExternalServiceError as exc:
        logger.error("Location service error during refresh: %s", exc)
        return _response(502, {"error": "Location service is currently unavailable."})

    if location is None:
        return _response(404, {"error": f"No location found for coordinates ({lat}, {lon})."})

    # --- Concurrently force-refresh conditions + hourly + daily (Req 7.1) ---
    # All three are launched under a shared 5-second timeout (Req 7.3) so
    # that the combined refresh stays within the wall-clock budget.
    try:
        conditions, hourly_entries, daily_entries = await asyncio.wait_for(
            asyncio.gather(
                _get_weather_service().refresh_current_conditions(location),
                _get_forecast_service().get_hourly_forecast(location, hours=24),
                _get_forecast_service().get_daily_forecast(location, days=7),
            ),
            timeout=5.0,
        )
    except asyncio.TimeoutError as exc:
        logger.warning(
            "Refresh timed out for location=%s",
            location.id if location else f"({lat},{lon})",
        )
        return _response(
            504,
            {
                "error": (
                    "Refresh timed out. Previously displayed data is retained."
                )
            },
        )
    except DomainTimeoutError as exc:
        logger.warning("Refresh timed out for location=%s: %s", location.id, exc)
        return _response(
            504,
            {
                "error": (
                    "Refresh timed out. Previously displayed data is retained."
                )
            },
        )
    except ExternalServiceError as exc:
        logger.error("Weather API error during refresh for location=%s: %s", location.id, exc)
        return _response(
            502,
            {
                "error": (
                    "Weather data could not be updated. "
                    "Previously displayed data is retained."
                )
            },
        )

    # --- Build combined response (Req 7.2) ---
    last_retrieved = datetime.now(tz=timezone.utc).isoformat()
    payload = {
        "current_conditions": _serialise_conditions(conditions, TempUnit.CELSIUS),
        "hourly": [_serialise_hourly(e) for e in hourly_entries],
        "daily": [_serialise_daily(e) for e in daily_entries],
        "last_retrieved": last_retrieved,
    }
    # Refresh endpoint always bypasses cache — no-store (design.md §API Gateway Caching, task 10.1)
    return add_no_store_headers(_response(200, payload))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_coordinates(params: dict) -> tuple[float, float]:
    """Extract and validate ``lat`` / ``lon`` from query-string parameters.

    Args:
        params: Parsed query-string dictionary.

    Returns:
        ``(lat, lon)`` as floats.

    Raises:
        ValidationError: If either parameter is missing, not parseable as a
            float, or outside the valid geographic range.
    """
    lat_raw = params.get("lat")
    lon_raw = params.get("lon")

    if lat_raw is None or lon_raw is None:
        missing = "lat" if lat_raw is None else "lon"
        raise DomainValidationError(
            f"Both 'lat' and 'lon' query parameters are required. Missing: '{missing}'."
        )

    try:
        lat = float(lat_raw)
    except (ValueError, TypeError):
        raise DomainValidationError(
            f"Invalid latitude value: {lat_raw!r}. Must be a number."
        )

    try:
        lon = float(lon_raw)
    except (ValueError, TypeError):
        raise DomainValidationError(
            f"Invalid longitude value: {lon_raw!r}. Must be a number."
        )

    if not validate_coordinates(lat, lon):
        raise DomainValidationError(
            f"Coordinates ({lat}, {lon}) are out of range. "
            "Latitude must be between -90 and 90; longitude between -180 and 180."
        )

    return lat, lon


def _parse_units(params: dict) -> TempUnit:
    """Extract and validate the optional ``units`` query parameter.

    Args:
        params: Parsed query-string dictionary.

    Returns:
        :class:`~src.domain.models.TempUnit`; defaults to
        :attr:`~src.domain.models.TempUnit.CELSIUS` when absent.

    Raises:
        ValidationError: If *units* is present but not a recognised unit.
    """
    raw = params.get("units")
    if raw is None:
        return TempUnit.CELSIUS
    return validate_temp_unit(raw)


async def _resolve_location(lat: float, lon: float) -> Optional[Location]:
    """Use :class:`~src.application.location_service.LocationService` to
    resolve a :class:`~src.domain.models.Location` from coordinates.

    Args:
        lat: Latitude.
        lon: Longitude.

    Returns:
        Resolved location or ``None``.

    Raises:
        DomainTimeoutError: Propagated from the location service.
        ExternalServiceError: Propagated from the location service.
    """
    return await _get_location_service().get_location_by_coordinates(lat, lon)


def _serialise_conditions(conditions: CurrentConditions, unit: TempUnit) -> dict:
    """Serialise a :class:`~src.domain.models.CurrentConditions` to a JSON-
    compatible dict, applying temperature unit conversion if required.

    The source data is assumed to be in Celsius (the internal representation
    used by the adapters).  Fahrenheit conversion is applied in-place when
    *unit* is :attr:`~src.domain.models.TempUnit.FAHRENHEIT`.

    Args:
        conditions: Weather conditions to serialise.
        unit: Desired output temperature unit.

    Returns:
        Dictionary ready for JSON encoding.
    """
    temperature = conditions.temperature
    feels_like = conditions.feels_like

    if unit == TempUnit.FAHRENHEIT:
        temperature = temperature * 9 / 5 + 32
        feels_like = feels_like * 9 / 5 + 32

    return {
        "temperature": round(temperature, 2),
        "feels_like": round(feels_like, 2),
        "humidity": conditions.humidity,
        "pressure": conditions.pressure,
        "wind_speed": conditions.wind_speed,
        "wind_direction": conditions.wind_direction,
        "condition": conditions.condition,
        "icon": conditions.icon,
        "visibility": conditions.visibility,
        "timestamp": conditions.timestamp.isoformat(),
        "sunrise": conditions.sunrise.isoformat() if conditions.sunrise else None,
        "sunset": conditions.sunset.isoformat() if conditions.sunset else None,
        "unit": unit.value,
    }


def _serialise_hourly(entry: HourlyForecast) -> dict:
    """Serialise a :class:`~src.domain.models.HourlyForecast` to a JSON-
    compatible dict.

    Args:
        entry: Hourly forecast entry to serialise.

    Returns:
        Dictionary ready for JSON encoding.
    """
    return {
        "timestamp": entry.timestamp.isoformat(),
        "temperature": entry.temperature,
        "feels_like": entry.feels_like,
        "humidity": entry.humidity,
        "condition": entry.condition,
        "icon": entry.icon,
        "precipitation_probability": entry.precipitation_probability,
        "wind_speed": entry.wind_speed,
    }


def _serialise_daily(entry: DailyForecast) -> dict:
    """Serialise a :class:`~src.domain.models.DailyForecast` to a JSON-
    compatible dict.

    Args:
        entry: Daily forecast entry to serialise.

    Returns:
        Dictionary ready for JSON encoding.
    """
    return {
        "date": entry.date.isoformat(),
        "high_temp": entry.high_temp,
        "low_temp": entry.low_temp,
        "condition": entry.condition,
        "icon": entry.icon,
        "precipitation_probability": entry.precipitation_probability,
        "sunrise": entry.sunrise.isoformat(),
        "sunset": entry.sunset.isoformat(),
    }


def _response(status_code: int, body: dict) -> dict:
    """Build an API Gateway proxy response dict.

    Args:
        status_code: HTTP status code.
        body: Response body as a Python dict (serialised to JSON).

    Returns:
        API Gateway proxy integration response dict.
    """
    return {
        "statusCode": status_code,
        "headers": _CORS_HEADERS,
        "body": json.dumps(body),
    }
