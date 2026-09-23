"""
Request validation utilities for Lambda / API Gateway handlers.

Provides lightweight, dependency-free helpers that inspect the API Gateway
proxy event dict and return early error responses when required parameters
are absent or malformed.

Public API
----------
``validate_request(event, required_params=None)``
    Check that all required query-string parameters are present and non-blank.

``parse_float_param(params, key)``
    Safely coerce a string query-string value to ``float`` (or ``None``).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_response(message: str, status_code: int = 400) -> dict[str, Any]:
    """Build a minimal 400-class API Gateway response for validation failures."""
    body = {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": message,
            "details": {},
            "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# validate_request
# ---------------------------------------------------------------------------


def validate_request(
    event: dict[str, Any],
    required_params: Optional[list[str]] = None,
) -> tuple[bool, Optional[dict[str, Any]]]:
    """Validate that all *required_params* are present and non-blank.

    Checks ``event["queryStringParameters"]`` for each name in
    *required_params*.  A parameter is considered missing or invalid when:
    * the key is absent from the query-string dict, or
    * the key is present but its value is ``None``, an empty string, or
      a whitespace-only string.

    Args:
        event: API Gateway proxy event dict.
        required_params: List of required query-string parameter names.
            Defaults to ``[]`` (no validation performed).

    Returns:
        ``(True, None)`` when all required parameters are present and valid.
        ``(False, error_response)`` where ``error_response`` is a 400 API
        Gateway response dict when validation fails.

    Example::

        ok, err = validate_request(event, ["lat", "lon"])
        if not ok:
            return err
    """
    if not required_params:
        return True, None

    params: dict[str, Any] = event.get("queryStringParameters") or {}

    for name in required_params:
        value = params.get(name)
        if value is None or str(value).strip() == "":
            return False, _error_response(
                f"Missing or blank required query parameter: '{name}'"
            )

    return True, None


# ---------------------------------------------------------------------------
# parse_float_param
# ---------------------------------------------------------------------------


def parse_float_param(
    params: Optional[dict[str, Any]],
    key: str,
) -> Optional[float]:
    """Safely parse a float from a query-string parameter dict.

    Returns ``None`` when:
    * *params* is ``None`` or the key is absent,
    * the value is ``None`` or an empty string,
    * the value cannot be converted to a float.

    Args:
        params: Query-string parameter dict (may be ``None``).
        key: Parameter name to look up.

    Returns:
        The parsed ``float``, or ``None`` on any failure.

    Example::

        lat = parse_float_param(event.get("queryStringParameters"), "lat")
        if lat is None:
            return _response(400, {"error": "Invalid 'lat' parameter"})
    """
    if params is None:
        return None

    raw = params.get(key)
    if raw is None or str(raw).strip() == "":
        return None

    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
