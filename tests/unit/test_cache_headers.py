"""
Unit tests for src.api.middleware.cache_headers

Validates that add_cache_headers and its convenience wrappers attach the
correct Cache-Control and Vary headers to API Gateway proxy responses.

Task 10.1 — Configure API Gateway caching
"""
from __future__ import annotations

import json
import pytest

from src.api.middleware.cache_headers import (
    CACHE_CONTROL_1MIN,
    CACHE_CONTROL_5MIN,
    CACHE_CONTROL_NO_STORE,
    add_cache_headers,
    add_no_store_headers,
    add_private_cache_headers,
    add_public_cache_headers,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bare_response(status: int = 200, body: dict | None = None) -> dict:
    """Build a minimal API Gateway proxy response (no headers yet)."""
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body or {}),
    }


# ---------------------------------------------------------------------------
# add_cache_headers — core function
# ---------------------------------------------------------------------------


class TestAddCacheHeaders:
    def test_default_is_5min_public(self):
        response = add_cache_headers(_bare_response())
        assert response["headers"]["Cache-Control"] == "max-age=300, public"

    def test_vary_accept_encoding_always_set(self):
        response = add_cache_headers(_bare_response())
        assert response["headers"]["Vary"] == "Accept-Encoding"

    def test_custom_ttl_public(self):
        response = add_cache_headers(_bare_response(), cache_ttl_seconds=120)
        assert response["headers"]["Cache-Control"] == "max-age=120, public"

    def test_private_flag(self):
        response = add_cache_headers(_bare_response(), cache_ttl_seconds=60, private=True)
        assert response["headers"]["Cache-Control"] == "max-age=60, private"

    def test_no_store_overrides_ttl_and_private(self):
        # no_store takes priority regardless of other params
        response = add_cache_headers(
            _bare_response(), cache_ttl_seconds=300, private=True, no_store=True
        )
        assert response["headers"]["Cache-Control"] == CACHE_CONTROL_NO_STORE

    def test_no_store_still_sets_vary(self):
        response = add_cache_headers(_bare_response(), no_store=True)
        assert response["headers"]["Vary"] == "Accept-Encoding"

    def test_existing_headers_preserved(self):
        response = add_cache_headers(_bare_response())
        assert response["headers"]["Content-Type"] == "application/json"

    def test_returns_same_response_object(self):
        original = _bare_response()
        returned = add_cache_headers(original)
        assert returned is original

    def test_status_code_unchanged(self):
        response = add_cache_headers(_bare_response(status=404))
        assert response["statusCode"] == 404

    def test_body_unchanged(self):
        body = {"key": "value"}
        response = _bare_response(body=body)
        original_body = response["body"]
        add_cache_headers(response)
        assert response["body"] == original_body

    def test_creates_headers_dict_if_absent(self):
        """Response without a 'headers' key should still work."""
        response = {"statusCode": 200, "body": "{}"}
        add_cache_headers(response)
        assert "Cache-Control" in response["headers"]
        assert "Vary" in response["headers"]

    def test_overwrites_existing_cache_control(self):
        response = _bare_response()
        response["headers"]["Cache-Control"] = "old-value"
        add_cache_headers(response, cache_ttl_seconds=600)
        assert response["headers"]["Cache-Control"] == "max-age=600, public"


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


class TestAddPublicCacheHeaders:
    def test_default_5min(self):
        response = add_public_cache_headers(_bare_response())
        assert response["headers"]["Cache-Control"] == "max-age=300, public"

    def test_vary_set(self):
        response = add_public_cache_headers(_bare_response())
        assert response["headers"]["Vary"] == "Accept-Encoding"

    def test_custom_ttl(self):
        response = add_public_cache_headers(_bare_response(), ttl_seconds=600)
        assert response["headers"]["Cache-Control"] == "max-age=600, public"


class TestAddPrivateCacheHeaders:
    def test_default_1min(self):
        response = add_private_cache_headers(_bare_response())
        assert response["headers"]["Cache-Control"] == "max-age=60, private"

    def test_vary_set(self):
        response = add_private_cache_headers(_bare_response())
        assert response["headers"]["Vary"] == "Accept-Encoding"

    def test_custom_ttl(self):
        response = add_private_cache_headers(_bare_response(), ttl_seconds=120)
        assert response["headers"]["Cache-Control"] == "max-age=120, private"


class TestAddNoStoreHeaders:
    def test_no_store_value(self):
        response = add_no_store_headers(_bare_response())
        assert response["headers"]["Cache-Control"] == CACHE_CONTROL_NO_STORE

    def test_vary_set(self):
        response = add_no_store_headers(_bare_response())
        assert response["headers"]["Vary"] == "Accept-Encoding"


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    def test_cache_control_5min_value(self):
        assert CACHE_CONTROL_5MIN == "max-age=300, public"

    def test_cache_control_1min_value(self):
        assert CACHE_CONTROL_1MIN == "max-age=60, private"

    def test_cache_control_no_store_value(self):
        assert CACHE_CONTROL_NO_STORE == "no-store"


# ---------------------------------------------------------------------------
# Policy mapping — verify each endpoint type gets the right header
# ---------------------------------------------------------------------------


class TestEndpointCachePolicies:
    """Simulate the header that each endpoint type emits (task 10.1)."""

    def test_location_search_5min_public(self):
        # GET /locations/search — 5-minute public cache
        response = add_public_cache_headers(_bare_response(), ttl_seconds=300)
        assert "max-age=300" in response["headers"]["Cache-Control"]
        assert "public" in response["headers"]["Cache-Control"]

    def test_weather_current_5min_public(self):
        # GET /weather/current — 5-minute public cache
        response = add_public_cache_headers(_bare_response(), ttl_seconds=300)
        assert "max-age=300" in response["headers"]["Cache-Control"]

    def test_forecast_hourly_5min_public(self):
        # GET /weather/forecast/hourly — 5-minute public cache
        response = add_public_cache_headers(_bare_response(), ttl_seconds=300)
        assert "max-age=300" in response["headers"]["Cache-Control"]

    def test_forecast_daily_5min_public(self):
        # GET /weather/forecast/daily — 5-minute public cache
        response = add_public_cache_headers(_bare_response(), ttl_seconds=300)
        assert "max-age=300" in response["headers"]["Cache-Control"]

    def test_preferences_units_1min_private(self):
        # GET /preferences/units — 1-minute private cache (user-specific)
        response = add_private_cache_headers(_bare_response(), ttl_seconds=60)
        assert "max-age=60" in response["headers"]["Cache-Control"]
        assert "private" in response["headers"]["Cache-Control"]

    def test_weather_refresh_no_store(self):
        # POST /weather/refresh — force fresh, no cache
        response = add_no_store_headers(_bare_response())
        assert response["headers"]["Cache-Control"] == CACHE_CONTROL_NO_STORE

    def test_favorites_post_no_store(self):
        # POST /favorites — mutation, no cache
        response = add_no_store_headers(_bare_response(status=201))
        assert response["headers"]["Cache-Control"] == CACHE_CONTROL_NO_STORE

    def test_favorites_delete_no_store(self):
        # DELETE /favorites/{id} — mutation, no cache
        response = add_no_store_headers({"statusCode": 204, "body": ""})
        assert response["headers"]["Cache-Control"] == CACHE_CONTROL_NO_STORE

    def test_preferences_put_no_store(self):
        # PUT /preferences/units — mutation, no cache
        response = add_no_store_headers(_bare_response())
        assert response["headers"]["Cache-Control"] == CACHE_CONTROL_NO_STORE
