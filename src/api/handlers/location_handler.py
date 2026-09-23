"""
Lambda handler for location-related endpoints.

Handles:
  - GET /locations/search?q={query}            (Requirements 1.1, 1.2, 1.4, 1.5)
  - GET /location/device?lat={lat}&lon={lon}   (Requirements 4.1, 4.2, 4.3, 4.4)

Error mapping:
  ValidationError         → 400 Bad Request
  TimeoutError            → 504 Gateway Timeout
  ExternalServiceError    → 502 Bad Gateway
  Unexpected exceptions   → 500 Internal Server Error
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.application.location_service import LocationService
from src.application.weather_service import CachedWeatherDataService
from src.domain.exceptions import ExternalServiceError, TimeoutError, ValidationError
from src.domain.models import (
    CurrentConditions,
    DeviceLocationData,
    Location,
)
from src.domain.validators import validate_coordinates
from src.api.middleware.cache_headers import add_no_store_headers, add_public_cache_headers
from src.infrastructure.external.geocoding_adapter import GeocodingAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Correlation-ID",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
    "Content-Type": "application/json",
}


def _response(status: int, body: dict) -> dict:
    """Build a well-formed API Gateway proxy response."""
    return {
        "statusCode": status,
        "headers": _CORS_HEADERS,
        "body": json.dumps(body, default=str),
    }


def _serialize_location(loc: Location) -> dict:
    """Serialize a Location domain object to a JSON-safe dict.

    Uses ``lat``/``lon`` keys (shorter API form) and includes timezone.
    """
    return {
        "id": loc.id,
        "name": loc.name,
        "region": loc.region,
        "country": loc.country,
        "lat": loc.latitude,
        "lon": loc.longitude,
        "timezone": loc.timezone,
    }


def _serialize_conditions(cond: CurrentConditions) -> dict:
    """Serialize a CurrentConditions domain object to a JSON-safe dict."""
    return {
        "temperature": cond.temperature,
        "feels_like": cond.feels_like,
        "humidity": cond.humidity,
        "pressure": cond.pressure,
        "wind_speed": cond.wind_speed,
        "wind_direction": cond.wind_direction,
        "condition": cond.condition,
        "icon": cond.icon,
        "timestamp": cond.timestamp.isoformat() if cond.timestamp else None,
        "sunrise": cond.sunrise.isoformat() if cond.sunrise else None,
        "sunset": cond.sunset.isoformat() if cond.sunset else None,
        "visibility": cond.visibility,
    }


# ---------------------------------------------------------------------------
# Lazy service wiring
# ---------------------------------------------------------------------------

_location_service: Optional[LocationService] = None
_weather_service: Optional[CachedWeatherDataService] = None


def _get_location_service() -> LocationService:
    """Return the shared LocationService, constructing it on first call."""
    global _location_service
    if _location_service is None:
        adapter = GeocodingAdapter()
        _location_service = LocationService(location_port=adapter)
    return _location_service


def _get_weather_service() -> CachedWeatherDataService:
    """Return the shared CachedWeatherDataService, constructing it on first call."""
    global _weather_service
    if _weather_service is None:
        from src.infrastructure.external.weather_api_adapter import WeatherAPIAdapter
        from src.infrastructure.cache.dynamodb_cache import DynamoDBWeatherCache
        weather_adapter = WeatherAPIAdapter()
        cache = DynamoDBWeatherCache()
        _weather_service = CachedWeatherDataService(
            weather_port=weather_adapter,
            cache=cache,
        )
    return _weather_service


# ---------------------------------------------------------------------------
# Lambda entry-point
# ---------------------------------------------------------------------------


def handler(event: dict, context) -> dict:
    """Main Lambda handler — dispatches to search or device sub-handlers.

    Routing is based on the ``path`` field injected by API Gateway:
      - ``/locations/search`` → :func:`_handle_search`
      - ``/location/device``  → :func:`_handle_device`
    """
    path: str = event.get("path", "")
    logger.info("Location handler invoked: path=%s", path)

    if "/search" in path:
        location_svc = _get_location_service()
        weather_svc = _get_weather_service()
        return asyncio.run(
            _handle_search(event, location_svc=location_svc, weather_svc=weather_svc)
        )
    elif "/device" in path:
        location_svc = _get_location_service()
        weather_svc = _get_weather_service()
        return asyncio.run(
            _handle_device(event, location_svc=location_svc, weather_svc=weather_svc)
        )

    return _response(404, {"error": "Not found", "path": path})


# ---------------------------------------------------------------------------
# Sub-handlers (accept injected services for testability)
# ---------------------------------------------------------------------------


async def _handle_search(
    event: dict,
    *,
    location_svc: LocationService,
    weather_svc,  # CachedWeatherDataService — typed loosely to avoid import cycles
) -> dict:
    """Handle GET /locations/search?q={query}.

    Requirements 1.1, 1.2, 1.4, 1.5.

    Returns:
        200  {"locations": [{"id", "name", "region", "country", "lat", "lon",
                            "timezone"}, ...]}
        400  {"error": "<message>"}   — missing/invalid query (Req 1.2)
        504  {"error": "<message>"}   — TimeoutError (Req 1.5)
        502  {"error": "<message>"}   — ExternalServiceError
        500  {"error": "Internal server error"}
    """
    params: dict = event.get("queryStringParameters") or {}
    query: str = (params.get("q") or "").strip()

    # Quick-reject before calling the service (no external call, Req 1.2)
    if not query:
        return _response(
            400,
            {"error": "Missing or empty required query parameter 'q'"},
        )

    try:
        locations = await location_svc.search_locations(query)
    except ValidationError as exc:
        logger.info("Location search validation error: %s", exc)
        return _response(400, {"error": str(exc)})
    except TimeoutError as exc:
        logger.warning("Location search timed out: %s", exc)
        return _response(504, {"error": "Location search timed out. Please try again."})
    except ExternalServiceError as exc:
        logger.error("Location search external service error: %s", exc)
        return _response(502, {"error": "Location search service is temporarily unavailable."})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during location search: %s", exc)
        return _response(500, {"error": "Internal server error"})

    # Req 1.4: empty result set is valid — return [] with 200
    # Cache search results for 5 minutes (design.md §API Gateway Caching, task 10.1)
    return add_public_cache_headers(
        _response(200, {"locations": [_serialize_location(loc) for loc in locations]})
    )


async def _handle_device(
    event: dict,
    *,
    location_svc: LocationService,
    weather_svc,  # CachedWeatherDataService — typed loosely to avoid import cycles
) -> dict:
    """Handle GET /location/device?lat={lat}&lon={lon}.

    Resolves device GPS coordinates to the nearest location within 50 km and
    retrieves current weather conditions for it (Requirements 4.1, 4.2, 4.4).

    Returns:
        200  {"location": {...}, "current_conditions": {...}}
        400  {"error": "<message>"}   — missing/invalid/out-of-range params
        404  {"error": "No location found within 50km"}  (Req 4.4)
        504  {"error": "<message>"}   — TimeoutError (Req 4.1)
        502  {"error": "<message>"}   — ExternalServiceError
        500  {"error": "Internal server error"}
    """
    params: dict = event.get("queryStringParameters") or {}

    # ------------------------------------------------------------------
    # Parse and validate coordinates
    # ------------------------------------------------------------------
    try:
        lat = float(params["lat"])
        lon = float(params["lon"])
    except KeyError:
        return _response(
            400,
            {"error": "Missing required parameters: 'lat' and 'lon'"},
        )
    except (TypeError, ValueError):
        return _response(
            400,
            {"error": "Parameters 'lat' and 'lon' must be valid numbers"},
        )

    if not validate_coordinates(lat, lon):
        return _response(
            400,
            {
                "error": (
                    f"Coordinates out of range: lat={lat}, lon={lon}. "
                    "lat must be in [-90, 90] and lon in [-180, 180]."
                )
            },
        )

    device_data = DeviceLocationData(
        latitude=lat,
        longitude=lon,
        accuracy=0.0,  # accuracy unknown when inferred from API query params
        timestamp=datetime.now(tz=timezone.utc),
    )

    # ------------------------------------------------------------------
    # Resolve device location
    # ------------------------------------------------------------------
    try:
        location = await location_svc.resolve_device_location(device_data)
    except TimeoutError as exc:
        logger.warning("Device location resolution timed out: %s", exc)
        return _response(
            504,
            {"error": "Device location detection timed out. Please use manual search."},
        )
    except ExternalServiceError as exc:
        logger.error("Device location external service error: %s", exc)
        return _response(
            502,
            {"error": "Location service is temporarily unavailable."},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during device location resolution: %s", exc)
        return _response(500, {"error": "Internal server error"})

    if location is None:
        # Req 4.4: no location within 50 km
        return _response(
            404,
            {"error": "No location found within 50km. Please use manual search."},
        )

    # ------------------------------------------------------------------
    # Retrieve current conditions for the resolved location
    # ------------------------------------------------------------------
    try:
        conditions = await weather_svc.get_current_conditions(location)
    except TimeoutError as exc:
        logger.warning("Weather data timed out for device location: %s", exc)
        return _response(
            504,
            {"error": "Weather data retrieval timed out. Please try again."},
        )
    except ExternalServiceError as exc:
        logger.error("Weather service error for device location: %s", exc)
        return _response(
            502,
            {"error": "Weather service is temporarily unavailable."},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error retrieving weather for device location: %s", exc)
        return _response(500, {"error": "Internal server error"})

    return _response(
        200,
        {
            "location": _serialize_location(location),
            "current_conditions": _serialize_conditions(conditions),
        },
    )
