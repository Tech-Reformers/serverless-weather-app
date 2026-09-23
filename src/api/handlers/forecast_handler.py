"""
Lambda handler for weather forecast endpoints.

Handles two routes:
  GET /weather/forecast/hourly?lat={lat}&lon={lon}
  GET /weather/forecast/daily?lat={lat}&lon={lon}

Both endpoints accept ``lat`` and ``lon`` as query-string parameters,
build a :class:`~src.domain.models.Location` stub from the coordinates,
delegate to :class:`~src.application.forecast_service.CachedForecastService`,
and return serialised forecast data.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

from src.api.middleware.cache_headers import add_public_cache_headers
from src.application.forecast_service import CachedForecastService
from src.domain.exceptions import ExternalServiceError
from src.domain.exceptions import TimeoutError as DomainTimeoutError
from src.domain.models import DailyForecast, HourlyForecast, Location

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CORS headers included on every response (Requirements: cross-origin access)
# ---------------------------------------------------------------------------
_CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Lazy service initialisation
#
# The service graph is built once per warm Lambda container.  We defer
# construction until the first real invocation so that import-time errors do
# not prevent the module from loading during CDK synthesis or test collection.
# ---------------------------------------------------------------------------
_forecast_service: CachedForecastService | None = None


def _get_forecast_service() -> CachedForecastService:  # pragma: no cover – wired at runtime
    """Return (and lazily build) the singleton CachedForecastService."""
    global _forecast_service  # noqa: PLW0603
    if _forecast_service is None:
        # Import infrastructure only when actually needed so tests can inject
        # their own service instance via ``_set_forecast_service``.
        from src.infrastructure.cache.cache_strategy import CacheStrategy
        from src.infrastructure.cache.dynamodb_cache import DynamoDBWeatherCache
        from src.infrastructure.external.weather_api_adapter import WeatherAPIAdapter

        weather_adapter = WeatherAPIAdapter()
        cache = DynamoDBWeatherCache()
        strategy = CacheStrategy()
        _forecast_service = CachedForecastService(
            forecast_port=weather_adapter,
            cache=cache,
            cache_strategy=strategy,
        )
    return _forecast_service


def _set_forecast_service(service: CachedForecastService) -> None:
    """Override the module-level singleton.  Used in tests."""
    global _forecast_service  # noqa: PLW0603
    _forecast_service = service


# ---------------------------------------------------------------------------
# Public Lambda entry point
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Route the request to the appropriate forecast handler.

    Args:
        event: API Gateway proxy event.
        context: Lambda context (unused but required by Lambda).

    Returns:
        API Gateway proxy response dict.
    """
    path: str = event.get("path", "")
    http_method: str = event.get("httpMethod", "GET").upper()

    if http_method == "OPTIONS":
        return _response(200, {})

    if "hourly" in path:
        return asyncio.run(_handle_hourly(event))
    if "daily" in path:
        return asyncio.run(_handle_daily(event))

    return _response(404, {"error": "Not found"})


# ---------------------------------------------------------------------------
# Private route handlers
# ---------------------------------------------------------------------------


async def _handle_hourly(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /weather/forecast/hourly.

    Returns 200 with up to 24 serialised :class:`~src.domain.models.HourlyForecast`
    entries (Requirements 3.1, 3.3).

    Error responses:
      400 – missing or unparseable ``lat``/``lon`` parameters.
      504 – upstream timed out (Requirements 3.5, Property 22).
      502 – upstream returned a service failure (Requirements 3.6, Property 16).
    """
    coords = _parse_coordinates(event)
    if isinstance(coords, dict):
        # _parse_coordinates returned an error response
        return coords

    lat, lon = coords
    location = _build_location(lat, lon)

    try:
        service = _get_forecast_service()
        entries = await service.get_hourly_forecast(location, hours=24)
    except DomainTimeoutError:
        logger.warning(
            "Hourly forecast timeout for lat=%s lon=%s", lat, lon
        )
        return _response(
            504,
            {
                "error": "Forecast retrieval timed out",
                "detail": (
                    "The hourly forecast could not be retrieved within the allowed time. "
                    "Current conditions may still be available."
                ),
            },
        )
    except ExternalServiceError as exc:
        logger.warning(
            "Hourly forecast external service error for lat=%s lon=%s: %s",
            lat,
            lon,
            exc,
        )
        return _response(
            502,
            {
                "error": "Forecast unavailable",
                "detail": (
                    "The hourly forecast is currently unavailable due to a service error. "
                    "Current conditions may still be available."
                ),
            },
        )

    return add_public_cache_headers(
        _response(200, {"hourly": [_serialise_hourly(e) for e in entries]})
    )


async def _handle_daily(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /weather/forecast/daily.

    Returns 200 with up to 7 serialised :class:`~src.domain.models.DailyForecast`
    entries (Requirements 3.2, 3.4).

    Error responses:
      400 – missing or unparseable ``lat``/``lon`` parameters.
      504 – upstream timed out (Requirements 3.5, Property 22).
      502 – upstream returned a service failure (Requirements 3.6, Property 16).
    """
    coords = _parse_coordinates(event)
    if isinstance(coords, dict):
        return coords

    lat, lon = coords
    location = _build_location(lat, lon)

    try:
        service = _get_forecast_service()
        entries = await service.get_daily_forecast(location, days=7)
    except DomainTimeoutError:
        logger.warning(
            "Daily forecast timeout for lat=%s lon=%s", lat, lon
        )
        return _response(
            504,
            {
                "error": "Forecast retrieval timed out",
                "detail": (
                    "The daily forecast could not be retrieved within the allowed time. "
                    "Current conditions may still be available."
                ),
            },
        )
    except ExternalServiceError as exc:
        logger.warning(
            "Daily forecast external service error for lat=%s lon=%s: %s",
            lat,
            lon,
            exc,
        )
        return _response(
            502,
            {
                "error": "Forecast unavailable",
                "detail": (
                    "The daily forecast is currently unavailable due to a service error. "
                    "Current conditions may still be available."
                ),
            },
        )

    return add_public_cache_headers(
        _response(200, {"daily": [_serialise_daily(e) for e in entries]})
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_coordinates(
    event: dict[str, Any],
) -> tuple[float, float] | dict[str, Any]:
    """Extract and validate ``lat``/``lon`` from API Gateway query params.

    Returns:
        A ``(lat, lon)`` tuple on success, or a 400 error-response dict on
        failure.
    """
    params: dict[str, str] = event.get("queryStringParameters") or {}
    errors: list[str] = []

    lat_raw = params.get("lat")
    lon_raw = params.get("lon")

    lat: float | None = None
    lon: float | None = None

    if lat_raw is None:
        errors.append("Missing required query parameter: lat")
    else:
        try:
            lat = float(lat_raw)
        except ValueError:
            errors.append(f"Invalid value for 'lat': '{lat_raw}' is not a valid number")

    if lon_raw is None:
        errors.append("Missing required query parameter: lon")
    else:
        try:
            lon = float(lon_raw)
        except ValueError:
            errors.append(f"Invalid value for 'lon': '{lon_raw}' is not a valid number")

    if errors:
        return _response(400, {"error": "; ".join(errors)})

    return lat, lon  # type: ignore[return-value]


def _build_location(lat: float, lon: float) -> Location:
    """Build a coordinate-only :class:`~src.domain.models.Location` stub.

    A full reverse-geocoding lookup is deferred to the forecast service layer;
    for routing purposes a stub with a derived ``id`` is sufficient.
    """
    return Location(
        id=f"{lat}_{lon}",
        name="",
        region="",
        country="",
        latitude=lat,
        longitude=lon,
        timezone="UTC",
    )


def _serialise_hourly(entry: HourlyForecast) -> dict[str, Any]:
    """Convert an :class:`~src.domain.models.HourlyForecast` to a JSON-safe dict."""
    return {
        "timestamp": _iso(entry.timestamp),
        "temperature": entry.temperature,
        "feels_like": entry.feels_like,
        "humidity": entry.humidity,
        "condition": entry.condition,
        "icon": entry.icon,
        "precipitation_probability": entry.precipitation_probability,
        "wind_speed": entry.wind_speed,
    }


def _serialise_daily(entry: DailyForecast) -> dict[str, Any]:
    """Convert a :class:`~src.domain.models.DailyForecast` to a JSON-safe dict."""
    return {
        "date": _iso(entry.date),
        "high_temp": entry.high_temp,
        "low_temp": entry.low_temp,
        "condition": entry.condition,
        "icon": entry.icon,
        "precipitation_probability": entry.precipitation_probability,
        "sunrise": _iso(entry.sunrise),
        "sunset": _iso(entry.sunset),
    }


def _iso(value: datetime) -> str:
    """Render a :class:`~datetime.datetime` as an ISO-8601 string."""
    return value.isoformat()


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Wrap *body* in an API Gateway proxy response envelope."""
    return {
        "statusCode": status_code,
        "headers": _CORS_HEADERS,
        "body": json.dumps(body),
    }
