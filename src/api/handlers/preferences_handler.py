"""
Lambda handler for the Preferences API endpoints.

Routes both unit-preference operations through a single Lambda function:

    GET /preferences/units     → retrieve user's current temperature unit
    PUT /preferences/units     → update user's temperature unit preference

The handler uses a module-level lazy-initialised :class:`UnitConversionService`
so that the DynamoDB adapter is wired up once per container (warm Lambda) rather
than on every invocation.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7

CORS headers are included in every response.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from src.api.middleware.cache_headers import add_no_store_headers, add_private_cache_headers
from src.application.unit_service import UnitConversionService
from src.domain.exceptions import DatabaseError, ValidationError
from src.domain.models import TempUnit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CORS headers included in every response
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key",
    "Access-Control-Allow-Methods": "GET,PUT,OPTIONS",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Lazy service initialisation
# ---------------------------------------------------------------------------

_service: Optional[UnitConversionService] = None


def _get_service() -> UnitConversionService:
    global _service
    if _service is None:
        from src.application.container import ServiceContainer
        _service = ServiceContainer.get_instance().unit_service
    return _service


def _set_service(svc: Optional[UnitConversionService]) -> None:
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


# ---------------------------------------------------------------------------
# Individual route handlers (async)
# ---------------------------------------------------------------------------


async def _handle_get_units(event: dict) -> dict:
    """Handle GET /preferences/units.

    Query parameters:
        user_id (str, optional): Defaults to ``"default"``.

    Returns:
        200 with ``{"unit": "celsius"|"fahrenheit"}`` on success.
        500 on DatabaseError or unexpected error.

    Requirements: 6.5, 6.7 — applies persisted preference, defaults to Celsius.
    """
    query_params = event.get("queryStringParameters") or {}
    user_id = query_params.get("user_id", "default")

    svc = _get_service()
    try:
        unit = await svc.get_user_preference(user_id)
        # User-specific data — 1-minute private cache (design.md §API Gateway Caching, task 10.1)
        return add_private_cache_headers(_response(200, {"unit": unit.value}))
    except DatabaseError as exc:
        logger.error(
            "DatabaseError retrieving preference for user '%s': %s",
            user_id, exc, exc_info=True,
        )
        return _response(500, {"error": "A database error occurred while retrieving your preference."})
    except Exception as exc:
        logger.error(
            "Unexpected error retrieving preference for user '%s': %s",
            user_id, exc, exc_info=True,
        )
        return _response(500, {"error": "An unexpected error occurred."})


async def _handle_put_units(event: dict) -> dict:
    """Handle PUT /preferences/units.

    Body (JSON):
        unit (str, required): ``"celsius"`` or ``"fahrenheit"``.
        user_id (str, optional): Defaults to query param or ``"default"``.

    Returns:
        200 with ``{"unit": "<saved unit>", "message": "Preference saved"}``
            on success.
        400 on missing/invalid ``unit`` field or invalid JSON.
        500 on DatabaseError with a message explaining that the preference
            will be retained for the current session only (Req 6.6, Property 19).

    Requirements: 6.3, 6.4, 6.6 — validates unit, persists, degrades gracefully.
    """
    # Parse request body
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body must be valid JSON."})

    # Resolve user_id: body > query params > default
    query_params = event.get("queryStringParameters") or {}
    user_id = body.get("user_id") or query_params.get("user_id", "default")

    # Validate 'unit' field presence
    raw_unit = body.get("unit")
    if raw_unit is None:
        return _response(400, {"error": "Missing required field: 'unit'."})

    # Validate 'unit' value is one of the allowed TempUnit values (Req 6.3)
    if raw_unit not in (TempUnit.CELSIUS.value, TempUnit.FAHRENHEIT.value):
        return _response(
            400,
            {
                "error": (
                    f"Invalid temperature unit: '{raw_unit}'. "
                    "Allowed values: 'celsius', 'fahrenheit'."
                )
            },
        )

    unit = TempUnit(raw_unit)

    svc = _get_service()
    try:
        await svc.set_user_preference(user_id, unit)
        # PUT is a mutation — no-store (design.md §API Gateway Caching, task 10.1)
        return add_no_store_headers(_response(200, {"unit": unit.value, "message": "Preference saved"}))
    except ValidationError as exc:
        # set_user_preference may raise ValidationError for invalid unit strings
        return _response(400, {"error": str(exc)})
    except DatabaseError as exc:
        logger.error(
            "DatabaseError saving preference for user '%s': %s",
            user_id, exc, exc_info=True,
        )
        # Req 6.6 / Property 19: inform caller that preference is session-only
        return _response(
            500,
            {
                "error": (
                    "Your preference could not be saved. "
                    "The selected unit will be retained for the current session only."
                )
            },
        )
    except Exception as exc:
        logger.error(
            "Unexpected error saving preference for user '%s': %s",
            user_id, exc, exc_info=True,
        )
        return _response(500, {"error": "An unexpected error occurred."})


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """AWS Lambda handler — routes by HTTP method.

    Supports:
        GET     → get temperature unit preference
        PUT     → set temperature unit preference
        OPTIONS → 200 (CORS pre-flight)

    All route handlers are ``async``; this sync entrypoint drives them via
    :func:`asyncio.run`.
    """
    method = (event.get("httpMethod") or "").upper()

    if method == "OPTIONS":
        return _response(200, {})

    if method == "GET":
        return asyncio.run(_handle_get_units(event))
    if method == "PUT":
        return asyncio.run(_handle_put_units(event))

    return _response(405, {"error": "Method not allowed."})
