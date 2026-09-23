"""
Unit tests for CacheStrategy.

Validates:
  - should_use_cache with fresh, stale, missing, and malformed entries
  - get_cache_key format
  - TTL constant values
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.models import Location
from src.infrastructure.cache.cache_strategy import CacheStrategy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def strategy() -> CacheStrategy:
    return CacheStrategy()


@pytest.fixture()
def location() -> Location:
    return Location(
        id="london-uk",
        name="London",
        region="England",
        country="UK",
        latitude=51.5074,
        longitude=-0.1278,
        timezone="Europe/London",
    )


def _fresh_entry(age_minutes: float = 0.0) -> dict:
    """Build a cache entry dict with a timestamp *age_minutes* old."""
    ts = datetime.now(tz=timezone.utc) - timedelta(minutes=age_minutes)
    return {"timestamp": ts.isoformat(), "data": {}}


# ---------------------------------------------------------------------------
# TTL constants
# ---------------------------------------------------------------------------


def test_current_ttl_constant() -> None:
    assert CacheStrategy.CURRENT_TTL_MINUTES == 15


def test_forecast_ttl_constant() -> None:
    assert CacheStrategy.FORECAST_TTL_MINUTES == 60


# ---------------------------------------------------------------------------
# should_use_cache — happy path
# ---------------------------------------------------------------------------


def test_fresh_entry_is_valid(strategy: CacheStrategy, location: Location) -> None:
    entry = _fresh_entry(age_minutes=1)
    assert strategy.should_use_cache(entry, location) is True


def test_just_under_ttl_is_valid(strategy: CacheStrategy, location: Location) -> None:
    entry = _fresh_entry(age_minutes=14.9)
    assert strategy.should_use_cache(entry, location) is True


def test_entry_at_ttl_boundary_is_stale(strategy: CacheStrategy, location: Location) -> None:
    # Exactly at TTL: age == ttl is NOT < ttl, so should be False
    entry = _fresh_entry(age_minutes=15.0)
    assert strategy.should_use_cache(entry, location) is False


def test_stale_entry_is_rejected(strategy: CacheStrategy, location: Location) -> None:
    entry = _fresh_entry(age_minutes=30)
    assert strategy.should_use_cache(entry, location) is False


def test_custom_ttl_override(strategy: CacheStrategy, location: Location) -> None:
    entry = _fresh_entry(age_minutes=50)
    # With default 15-minute TTL this would be stale; with 60-minute TTL it's fresh.
    assert strategy.should_use_cache(entry, location, ttl_minutes=60) is True


def test_custom_ttl_stale(strategy: CacheStrategy, location: Location) -> None:
    entry = _fresh_entry(age_minutes=61)
    assert strategy.should_use_cache(entry, location, ttl_minutes=60) is False


# ---------------------------------------------------------------------------
# should_use_cache — edge / error cases
# ---------------------------------------------------------------------------


def test_empty_dict_returns_false(strategy: CacheStrategy, location: Location) -> None:
    assert strategy.should_use_cache({}, location) is False


def test_none_timestamp_returns_false(strategy: CacheStrategy, location: Location) -> None:
    assert strategy.should_use_cache({"timestamp": None}, location) is False


def test_missing_timestamp_key_returns_false(strategy: CacheStrategy, location: Location) -> None:
    assert strategy.should_use_cache({"data": {}}, location) is False


def test_malformed_timestamp_returns_false(strategy: CacheStrategy, location: Location) -> None:
    assert strategy.should_use_cache({"timestamp": "not-a-date"}, location) is False


def test_naive_datetime_object_still_works(strategy: CacheStrategy, location: Location) -> None:
    # Naive datetime should be treated as UTC.
    ts = datetime.utcnow() - timedelta(minutes=1)
    entry = {"timestamp": ts}  # datetime, not string
    assert strategy.should_use_cache(entry, location) is True


# ---------------------------------------------------------------------------
# get_cache_key
# ---------------------------------------------------------------------------


def test_cache_key_format(location: Location) -> None:
    key = CacheStrategy.get_cache_key(location, "current")
    assert key == "51.5074_-0.1278_current"


def test_cache_key_forecast_types(location: Location) -> None:
    assert CacheStrategy.get_cache_key(location, "hourly") == "51.5074_-0.1278_hourly"
    assert CacheStrategy.get_cache_key(location, "daily") == "51.5074_-0.1278_daily"


def test_cache_key_different_locations() -> None:
    loc1 = Location("a", "A", "R", "C", 40.0, -74.0, "America/New_York")
    loc2 = Location("b", "B", "R", "C", 35.0, 139.0, "Asia/Tokyo")
    assert CacheStrategy.get_cache_key(loc1, "current") != CacheStrategy.get_cache_key(loc2, "current")
