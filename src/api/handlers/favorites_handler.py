"""
Lambda handler for the Favorites API endpoints.

Routes all three favorites operations through a single Lambda function:

    GET    /favorites                      → list user's favorites
    POST   /favorites                      → add a favorite location
    DELETE /favorites/{location_id}        → remove a favorite location

The handler uses a module-level lazy-initialized :class:`FavoritesService`
so that the DynamoDB and weather adapters are wired up once per container
(warm Lambda) rather than on every invocation.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8

CORS headers are included in every response.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

from src.api.middleware.cache_headers import add_no_store_headers
from src.application.favorites_service import FavoritesService
from src.domain.exceptions import (
    DatabaseError,
    DuplicateFavoriteError,
    FavoriteLimitExceededError,
    ValidationError,
)
from src.domain.models import FavoriteLocation, Location

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CORS headers included in every response
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Lazy service initialisation
# ---------------------------------------------------------------------------

_service: Optional[FavoritesService] = None


def _get_service() -> FavoritesService:
    global _service
    if _service is None:
        from src.application.container import ServiceContainer
        _service = ServiceContainer.get_instance().favorites_service
    return _service


def _set_service(svc: Optional[FavoritesService]) -> None:
    """Replace the module-level service instance (test helper)."""
    global _service
    _service = svc


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _response(status_code: int, body: Any) -> dict:
    """Build an API Gateway proxy-compatible response dict."""
    return {
        "statusCode": status_code,
        "headers": _CORS_HEADERS,
        "body": json.dumps(body, default=str),
    }


def _serialize_favorite(fav: FavoriteLocation) -> dict:
    """Convert a :class:`FavoriteLocation` to a JSON-serialisable dict."""
    return {
        "user_id": fav.user_id,
        "location": {
            "id": fav.location.id,
            "name": fav.location.name,
            "region": fav.location.region,
            "country": fav.location.country,
            "latitude": fav.location.latitude,
            "longitude": fav.location.longitude,
            "timezone": fav.location.timezone,
        },
        "added_at": fav.added_at.isoformat() if isinstance(fav.added_at, datetime) else fav.added_at,
        "last_accessed": fav.last_accessed.isoformat() if isinstance(fav.last_accessed, datetime) else fav.last_accessed,
        "current_temperature": fav.current_temperature,
    }


# ---------------------------------------------------------------------------
# Individual route handlers (async)
# ---------------------------------------------------------------------------


async def _list_favorites(event: dict) -> dict:
    """Handle GET /favorites.

    Query parameters:
        user_id (str, optional): Defaults to ``"default"``.

    Returns:
        200 with ``{"favorites": [...]}`` on success.
        500 on DatabaseError or unexpected error.
    """
    query_params = event.get("queryStringParameters") or {}
    user_id = query_params.get("user_id", "default")

    svc = _get_service()
    try:
        favorites = await svc.list_favorites(user_id)
        return _response(200, {"favorites": [_serialize_favorite(f) for f in favorites]})
    except DatabaseError as exc:
        logger.error("DatabaseError listing favorites for user '%s': %s", user_id, exc, exc_info=True)
        return _response(500, {"error": "A database error occurred. Please try again later."})
    except Exception as exc:
        logger.error("Unexpected error listing favorites for user '%s': %s", user_id, exc, exc_info=True)
        return _response(500, {"error": "An unexpected error occurred."})


async def _add_favorite(event: dict) -> dict:
    """Handle POST /favorites.

    Body (JSON):
        user_id (str, optional): Defaults to ``"default"``.
        id (str, required): Location identifier.
        name (str, required): Human-readable location name.
        latitude (float, required): Geographic latitude.
        longitude (float, required): Geographic longitude.
        region (str, optional): Administrative region.
        country (str, optional): Country name.
        timezone (str, optional): IANA timezone string.

    Returns:
        201 with the serialised favorite on success.
        400 on missing required fields or ValidationError.
        409 on DuplicateFavoriteError (Requirements 5.7).
        422 on FavoriteLimitExceededError (Requirements 5.2).
        500 on DatabaseError or unexpected error.
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body must be valid JSON."})

    # Extract user_id from body or query params, defaulting to "default"
    query_params = event.get("queryStringParameters") or {}
    user_id = body.get("user_id") or query_params.get("user_id", "default")

    # Validate required location fields
    missing = [f for f in ("id", "name", "latitude", "longitude") if f not in body]
    if missing:
        return _response(
            400,
            {"error": f"Missing required field(s): {', '.join(missing)}."},
        )

    # Validate latitude / longitude are numeric
    try:
        latitude = float(body["latitude"])
        longitude = float(body["longitude"])
    except (TypeError, ValueError):
        return _response(400, {"error": "'latitude' and 'longitude' must be numeric."})

    location = Location(
        id=str(body["id"]),
        name=str(body["name"]),
        region=str(body.get("region", "")),
        country=str(body.get("country", "")),
        latitude=latitude,
        longitude=longitude,
        timezone=str(body.get("timezone", "UTC")),
    )

    svc = _get_service()
    try:
        favorite = await svc.add_favorite(user_id, location)
        # POST is a mutation — no-store (design.md §API Gateway Caching, task 10.1)
        return add_no_store_headers(_response(201, _serialize_favorite(favorite)))
    except FavoriteLimitExceededError as exc:
        return _response(422, {"error": str(exc) or "Favorite limit of 50 locations has been reached."})
    except DuplicateFavoriteError as exc:
        return _response(409, {"error": str(exc) or "Location is already in your favorites."})
    except ValidationError as exc:
        return _response(400, {"error": str(exc)})
    except DatabaseError as exc:
        logger.error("DatabaseError adding favorite for user '%s': %s", user_id, exc, exc_info=True)
        return _response(500, {"error": "A database error occurred. Please try again later."})
    except Exception as exc:
        logger.error("Unexpected error adding favorite for user '%s': %s", user_id, exc, exc_info=True)
        return _response(500, {"error": "An unexpected error occurred."})


async def _remove_favorite(event: dict) -> dict:
    """Handle DELETE /favorites/{location_id}.

    Path parameters:
        location_id (str, required): ID of the location to remove.

    Query parameters:
        user_id (str, optional): Defaults to ``"default"``.

    Returns:
        204 (empty body) on success.
        400 if location_id is missing.
        500 on DatabaseError or unexpected error.
    """
    path_params = event.get("pathParameters") or {}
    location_id = path_params.get("location_id")
    if not location_id:
        return _response(400, {"error": "Missing path parameter: location_id."})

    query_params = event.get("queryStringParameters") or {}
    user_id = query_params.get("user_id", "default")

    svc = _get_service()
    try:
        await svc.remove_favorite(user_id, location_id)
        # DELETE is a mutation — no-store (design.md §API Gateway Caching, task 10.1)
        return add_no_store_headers({
            "statusCode": 204,
            "headers": _CORS_HEADERS,
            "body": "",
        })
    except DatabaseError as exc:
        logger.error(
            "DatabaseError removing favorite '%s' for user '%s': %s",
            location_id, user_id, exc, exc_info=True,
        )
        return _response(500, {"error": "A database error occurred. Please try again later."})
    except Exception as exc:
        logger.error(
            "Unexpected error removing favorite '%s' for user '%s': %s",
            location_id, user_id, exc, exc_info=True,
        )
        return _response(500, {"error": "An unexpected error occurred."})


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """AWS Lambda handler — routes by HTTP method.

    Supports:
        GET    → list_favorites
        POST   → add_favorite
        DELETE → remove_favorite
        OPTIONS → 200 (CORS pre-flight)

    All route handlers are ``async``; this sync entrypoint drives them via
    :func:`asyncio.run`.
    """
    method = (event.get("httpMethod") or "").upper()

    if method == "OPTIONS":
        return _response(200, {})

    if method == "GET":
        return asyncio.run(_list_favorites(event))
    if method == "POST":
        return asyncio.run(_add_favorite(event))
    if method == "DELETE":
        return asyncio.run(_remove_favorite(event))

    return _response(405, {"error": "Method not allowed."})
