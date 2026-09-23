"""
Unit tests for DynamoDBWeatherCache.

All DynamoDB I/O is intercepted via a stub resource / table so tests run
without AWS credentials or LocalStack.

Validates:
  - get_current_conditions: cache hit, cache miss, TTL expiry, DynamoDB error
  - put_current_conditions: correct item shape written, error propagation
  - get_forecast (hourly / daily): cache hit, miss, TTL expiry, error
  - put_forecast: correct item shape, unknown type raises CacheError
  - invalidate: all three sort-key variants deleted, error propagation
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.domain.exceptions import CacheError
from src.domain.models import CurrentConditions, DailyForecast, HourlyForecast
from src.infrastructure.cache.dynamodb_cache import DynamoDBWeatherCache


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _make_stub_resource(table: MagicMock) -> MagicMock:
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


def _make_table() -> MagicMock:
    return MagicMock()


def _future_epoch(minutes: int = 60) -> int:
    return int((datetime.now(tz=timezone.utc) + timedelta(minutes=minutes)).timestamp())


def _past_epoch(minutes: int = 60) -> int:
    return int((datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)).timestamp())


def _sample_conditions() -> CurrentConditions:
    return CurrentConditions(
        temperature=15.5,
        feels_like=13.0,
        humidity=65,
        pressure=1013,
        wind_speed=12.3,
        wind_direction=270,
        condition="Partly cloudy",
        icon="03d",
        timestamp=datetime(2024, 1, 16, 14, 0, 0, tzinfo=timezone.utc),
        sunrise=datetime(2024, 1, 16, 7, 0, 0, tzinfo=timezone.utc),
        sunset=datetime(2024, 1, 16, 17, 0, 0, tzinfo=timezone.utc),
        visibility=10000,
    )


def _sample_hourly() -> HourlyForecast:
    return HourlyForecast(
        timestamp=datetime(2024, 1, 16, 15, 0, 0, tzinfo=timezone.utc),
        temperature=14.0,
        feels_like=12.0,
        humidity=70,
        condition="Cloudy",
        icon="04d",
        precipitation_probability=20,
        wind_speed=10.0,
    )


def _sample_daily() -> DailyForecast:
    return DailyForecast(
        date=datetime(2024, 1, 16, tzinfo=timezone.utc),
        high_temp=18.0,
        low_temp=10.0,
        condition="Sunny",
        icon="01d",
        precipitation_probability=5,
        sunrise=datetime(2024, 1, 16, 7, 0, 0, tzinfo=timezone.utc),
        sunset=datetime(2024, 1, 16, 17, 0, 0, tzinfo=timezone.utc),
    )


def _serialised_conditions(conditions: CurrentConditions) -> dict[str, Any]:
    return {
        "temperature": str(conditions.temperature),
        "feels_like": str(conditions.feels_like),
        "humidity": str(conditions.humidity),
        "pressure": str(conditions.pressure),
        "wind_speed": str(conditions.wind_speed),
        "wind_direction": str(conditions.wind_direction),
        "condition": conditions.condition,
        "icon": conditions.icon,
        "timestamp": conditions.timestamp.isoformat(),
        "sunrise": conditions.sunrise.isoformat() if conditions.sunrise else None,
        "sunset": conditions.sunset.isoformat() if conditions.sunset else None,
        "visibility": str(conditions.visibility),
    }


# ---------------------------------------------------------------------------
# get_current_conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_conditions_hit() -> None:
    conditions = _sample_conditions()
    table = _make_table()
    table.get_item.return_value = {
        "Item": {
            "cache_key": "51.5_-0.1_current",
            "cache_type": "current",
            "data": _serialised_conditions(conditions),
            "timestamp": conditions.timestamp.isoformat(),
            "ttl": _future_epoch(15),
        }
    }
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    result = await cache.get_current_conditions("51.5_-0.1_current")

    assert result is not None
    assert result.temperature == conditions.temperature
    assert result.humidity == conditions.humidity
    assert result.condition == conditions.condition


@pytest.mark.asyncio
async def test_get_current_conditions_miss() -> None:
    table = _make_table()
    table.get_item.return_value = {}  # no "Item" key
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    result = await cache.get_current_conditions("missing_key")
    assert result is None


@pytest.mark.asyncio
async def test_get_current_conditions_expired_ttl() -> None:
    conditions = _sample_conditions()
    table = _make_table()
    table.get_item.return_value = {
        "Item": {
            "cache_key": "51.5_-0.1_current",
            "cache_type": "current",
            "data": _serialised_conditions(conditions),
            "timestamp": conditions.timestamp.isoformat(),
            "ttl": _past_epoch(5),  # already expired
        }
    }
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    result = await cache.get_current_conditions("51.5_-0.1_current")
    assert result is None


@pytest.mark.asyncio
async def test_get_current_conditions_dynamodb_error() -> None:
    table = _make_table()
    table.get_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "oops"}}, "GetItem"
    )
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    with pytest.raises(CacheError):
        await cache.get_current_conditions("some_key")


# ---------------------------------------------------------------------------
# put_current_conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_current_conditions_writes_correct_shape() -> None:
    table = _make_table()
    table.put_item.return_value = {}
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    conditions = _sample_conditions()

    await cache.put_current_conditions("51.5_-0.1_current", conditions)

    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["cache_key"] == "51.5_-0.1_current"
    assert item["cache_type"] == "current"
    assert "timestamp" in item
    assert "ttl" in item
    assert int(item["ttl"]) > int(datetime.now(tz=timezone.utc).timestamp())
    assert item["data"]["condition"] == conditions.condition


@pytest.mark.asyncio
async def test_put_current_conditions_custom_ttl() -> None:
    table = _make_table()
    table.put_item.return_value = {}
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    conditions = _sample_conditions()
    now_before = int(datetime.now(tz=timezone.utc).timestamp())

    await cache.put_current_conditions("key", conditions, ttl_minutes=30)

    item = table.put_item.call_args.kwargs["Item"]
    expected_min = now_before + 30 * 60
    assert int(item["ttl"]) >= expected_min


@pytest.mark.asyncio
async def test_put_current_conditions_dynamodb_error() -> None:
    table = _make_table()
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceeded", "Message": "throttled"}}, "PutItem"
    )
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    with pytest.raises(CacheError):
        await cache.put_current_conditions("key", _sample_conditions())


# ---------------------------------------------------------------------------
# get_forecast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_hourly_forecast_hit() -> None:
    entry = _sample_hourly()
    table = _make_table()
    table.get_item.return_value = {
        "Item": {
            "cache_key": "51.5_-0.1_hourly",
            "cache_type": "hourly",
            "data": [
                {
                    "timestamp": entry.timestamp.isoformat(),
                    "temperature": str(entry.temperature),
                    "feels_like": str(entry.feels_like),
                    "humidity": str(entry.humidity),
                    "condition": entry.condition,
                    "icon": entry.icon,
                    "precipitation_probability": str(entry.precipitation_probability),
                    "wind_speed": str(entry.wind_speed),
                }
            ],
            "timestamp": entry.timestamp.isoformat(),
            "ttl": _future_epoch(60),
        }
    }
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    result = await cache.get_forecast("51.5_-0.1_hourly", "hourly")

    assert result is not None
    assert len(result) == 1
    assert isinstance(result[0], HourlyForecast)
    assert result[0].temperature == entry.temperature


@pytest.mark.asyncio
async def test_get_daily_forecast_hit() -> None:
    entry = _sample_daily()
    table = _make_table()
    table.get_item.return_value = {
        "Item": {
            "cache_key": "51.5_-0.1_daily",
            "cache_type": "daily",
            "data": [
                {
                    "date": entry.date.isoformat(),
                    "high_temp": str(entry.high_temp),
                    "low_temp": str(entry.low_temp),
                    "condition": entry.condition,
                    "icon": entry.icon,
                    "precipitation_probability": str(entry.precipitation_probability),
                    "sunrise": entry.sunrise.isoformat(),
                    "sunset": entry.sunset.isoformat(),
                }
            ],
            "timestamp": entry.date.isoformat(),
            "ttl": _future_epoch(60),
        }
    }
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    result = await cache.get_forecast("51.5_-0.1_daily", "daily")

    assert result is not None
    assert len(result) == 1
    assert isinstance(result[0], DailyForecast)
    assert result[0].high_temp == entry.high_temp


@pytest.mark.asyncio
async def test_get_forecast_miss() -> None:
    table = _make_table()
    table.get_item.return_value = {}
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    assert await cache.get_forecast("missing", "hourly") is None


@pytest.mark.asyncio
async def test_get_forecast_expired_ttl() -> None:
    table = _make_table()
    table.get_item.return_value = {
        "Item": {
            "cache_key": "key",
            "cache_type": "hourly",
            "data": [],
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "ttl": _past_epoch(10),
        }
    }
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    assert await cache.get_forecast("key", "hourly") is None


@pytest.mark.asyncio
async def test_get_forecast_dynamodb_error() -> None:
    table = _make_table()
    table.get_item.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}, "GetItem"
    )
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    with pytest.raises(CacheError):
        await cache.get_forecast("key", "hourly")


@pytest.mark.asyncio
async def test_get_forecast_unknown_type_returns_none() -> None:
    table = _make_table()
    table.get_item.return_value = {
        "Item": {
            "cache_key": "key",
            "cache_type": "weekly",
            "data": [{"bogus": "data"}],
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "ttl": _future_epoch(60),
        }
    }
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    # Unknown type: no deserialiser → returns None
    result = await cache.get_forecast("key", "weekly")
    assert result is None


# ---------------------------------------------------------------------------
# put_forecast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_hourly_forecast_writes_correct_shape() -> None:
    table = _make_table()
    table.put_item.return_value = {}
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    entries = [_sample_hourly()]

    await cache.put_forecast("key", "hourly", entries)

    item = table.put_item.call_args.kwargs["Item"]
    assert item["cache_type"] == "hourly"
    assert len(item["data"]) == 1
    assert "temperature" in item["data"][0]


@pytest.mark.asyncio
async def test_put_daily_forecast_writes_correct_shape() -> None:
    table = _make_table()
    table.put_item.return_value = {}
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))

    await cache.put_forecast("key", "daily", [_sample_daily()])

    item = table.put_item.call_args.kwargs["Item"]
    assert item["cache_type"] == "daily"
    assert "high_temp" in item["data"][0]


@pytest.mark.asyncio
async def test_put_forecast_unknown_type_raises_cache_error() -> None:
    table = _make_table()
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    with pytest.raises(CacheError, match="Unknown forecast_type"):
        await cache.put_forecast("key", "weekly", [])


@pytest.mark.asyncio
async def test_put_forecast_dynamodb_error() -> None:
    table = _make_table()
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "error"}}, "PutItem"
    )
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    with pytest.raises(CacheError):
        await cache.put_forecast("key", "hourly", [_sample_hourly()])


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_deletes_all_three_types() -> None:
    table = _make_table()
    table.delete_item.return_value = {}
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))

    await cache.invalidate("51.5_-0.1")

    assert table.delete_item.call_count == 3
    deleted_types = {
        call.kwargs["Key"]["cache_type"] for call in table.delete_item.call_args_list
    }
    assert deleted_types == {"current", "hourly", "daily"}


@pytest.mark.asyncio
async def test_invalidate_raises_cache_error_on_failure() -> None:
    table = _make_table()
    table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "fail"}}, "DeleteItem"
    )
    cache = DynamoDBWeatherCache(table_name="test-table", dynamodb_resource=_make_stub_resource(table))
    with pytest.raises(CacheError):
        await cache.invalidate("key")


# ---------------------------------------------------------------------------
# Constructor — table name resolution
# ---------------------------------------------------------------------------


def test_default_table_name_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_TABLE_NAME", "my-custom-cache-table")
    table = _make_table()
    cache = DynamoDBWeatherCache(dynamodb_resource=_make_stub_resource(table))
    assert cache._table_name == "my-custom-cache-table"


def test_explicit_table_name_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_TABLE_NAME", "env-table")
    table = _make_table()
    cache = DynamoDBWeatherCache(table_name="explicit-table", dynamodb_resource=_make_stub_resource(table))
    assert cache._table_name == "explicit-table"


def test_fallback_table_name_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CACHE_TABLE_NAME", raising=False)
    table = _make_table()
    cache = DynamoDBWeatherCache(dynamodb_resource=_make_stub_resource(table))
    assert cache._table_name == "weather-app-cache"
