"""
Cache-control header middleware for API Gateway Lambda responses.

This module provides constants and helpers for adding HTTP cache-related
headers to Lambda proxy responses, aligning with the multi-layer caching
strategy described in design.md §Multi-Layer Caching Approach.

Endpoint caching policy (design.md §API Gateway Caching):
  - GET /locations/search              → 5-minute public cache, keyed on ``q``
  - GET /weather/current               → 5-minute public cache, keyed on ``lat``, ``lon``, ``units``
  - GET /weather/forecast/hourly       → 5-minute public cache, keyed on ``lat``, ``lon``
  - GET /weather/forecast/daily        → 5-minute public cache, keyed on ``lat``, ``lon``
  - GET /preferences/units             → 1-minute private cache, keyed on ``user_id``
  - POST /weather/refresh              → no-store (always fresh, Req 7.1)
  - POST/DELETE /favorites             → no-store (mutations)

The ``Vary: Accept-Encoding`` header is added on all responses so that
CDN/reverse-proxy nodes respect content negotiation for compressed payloads.

Requirements: Performance optimization (task 10.1)
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Cache-Control value constants
# ---------------------------------------------------------------------------

#: 5-minute shared cache — for weather data and location search results.
CACHE_CONTROL_5MIN = "max-age=300, public"

#: 1-minute private cache — for user-specific data (unit preferences).
CACHE_CONTROL_1MIN = "max-age=60, private"

#: No-store — for mutations and force-refresh endpoints.
CACHE_CONTROL_NO_STORE = "no-store"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_cache_headers(
    response: dict,
    cache_ttl_seconds: int = 300,
    *,
    private: bool = False,
    no_store: bool = False,
) -> dict:
    """Add ``Cache-Control`` and ``Vary`` headers to an API Gateway response.

    The function merges cache headers into the existing ``headers`` dict (or
    creates one) and returns the mutated response object.  Callers can pass
    the response dict returned by their ``_response()`` helper directly.

    Args:
        response:
            API Gateway proxy response dict.  Must contain a ``"headers"`` key
            (a plain ``dict``) or none at all — one will be created if absent.
        cache_ttl_seconds:
            Seconds before cached content is considered stale.  Ignored when
            *no_store* is ``True``.  Defaults to 300 (5 minutes).
        private:
            When ``True`` the directive is ``private`` (user-specific data)
            rather than ``public`` (shared across users).  Ignored when
            *no_store* is ``True``.
        no_store:
            When ``True`` emit ``Cache-Control: no-store`` regardless of other
            parameters.  Use for mutations and force-refresh endpoints.

    Returns:
        The same *response* dict with cache-related headers merged in.

    Examples::

        # 5-minute shared cache (default)
        return add_cache_headers(_response(200, payload))

        # 1-minute user-specific cache
        return add_cache_headers(_response(200, payload), cache_ttl_seconds=60, private=True)

        # Disable caching entirely
        return add_cache_headers(_response(200, payload), no_store=True)
    """
    headers: dict[str, str] = response.setdefault("headers", {})

    if no_store:
        cache_control = CACHE_CONTROL_NO_STORE
    elif private:
        cache_control = f"max-age={cache_ttl_seconds}, private"
    else:
        cache_control = f"max-age={cache_ttl_seconds}, public"

    headers["Cache-Control"] = cache_control
    headers["Vary"] = "Accept-Encoding"

    return response


def add_public_cache_headers(response: dict, ttl_seconds: int = 300) -> dict:
    """Convenience wrapper: add public cache headers with *ttl_seconds* TTL.

    Equivalent to ``add_cache_headers(response, ttl_seconds, private=False)``.
    """
    return add_cache_headers(response, cache_ttl_seconds=ttl_seconds, private=False)


def add_private_cache_headers(response: dict, ttl_seconds: int = 60) -> dict:
    """Convenience wrapper: add private cache headers with *ttl_seconds* TTL.

    Equivalent to ``add_cache_headers(response, ttl_seconds, private=True)``.
    """
    return add_cache_headers(response, cache_ttl_seconds=ttl_seconds, private=True)


def add_no_store_headers(response: dict) -> dict:
    """Convenience wrapper: mark response as non-cacheable (``no-store``).

    Use for mutation endpoints (POST /favorites, DELETE /favorites/{id},
    POST /weather/refresh).
    """
    return add_cache_headers(response, no_store=True)
