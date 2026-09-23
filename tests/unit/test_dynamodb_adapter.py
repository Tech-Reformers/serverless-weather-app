"""
Unit tests for DynamoDBFavoritesService and DynamoDBUnitService.

All DynamoDB calls are intercepted with pytest-mock so no real AWS
credentials or network access are required.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.domain.exceptions import (
    DatabaseError,
    DuplicateFavoriteError,
    FavoriteLimitExceededError,
    ValidationError,
)
from src.domain.models import FavoriteLocation, Location, TempUnit
from src.infrastructure.aws.dynamodb_adapter import (
    DynamoDBFavoritesService,
    DynamoDBUnitService,
    _COUNTER_SORT_KEY,
    _MAX_FAVORITES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_location(loc_id: str = "london-uk") -> Location:
    return Location(
        id=loc_id,
        name="London",
        region="England",
        country="UK",
        latitude=51.5074,
        longitude=-0.1278,
        timezone="Europe/London",
    )


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "mock error"}},
        "operation",
    )


def _make_dynamodb_resource(table_mock: MagicMock) -> MagicMock:
    resource = MagicMock()
    resource.Table.return_value = table_mock
    return resource


# ---------------------------------------------------------------------------
# DynamoDBFavoritesService — add_favorite
# ---------------------------------------------------------------------------


class TestAddFavorite:
    def _service(self, table):
        return DynamoDBFavoritesService(
            table_name="test-favorites",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def test_success_returns_favorite_location(self):
        table = MagicMock()
        table.put_item.return_value = {}
        table.update_item.return_value = {}

        service = self._service(table)
        loc = _make_location()
        result = asyncio.run(service.add_favorite("user-1", loc))

        assert isinstance(result, FavoriteLocation)
        assert result.user_id == "user-1"
        assert result.location == loc
        assert result.current_temperature is None

    def test_duplicate_raises_duplicate_favorite_error(self):
        table = MagicMock()
        table.put_item.side_effect = _client_error("ConditionalCheckFailedException")

        service = self._service(table)
        with pytest.raises(DuplicateFavoriteError):
            asyncio.run(service.add_favorite("user-1", _make_location()))

    def test_limit_exceeded_raises_favorite_limit_error(self):
        table = MagicMock()
        table.put_item.return_value = {}
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        table.delete_item.return_value = {}  # rollback

        service = self._service(table)
        with pytest.raises(FavoriteLimitExceededError):
            asyncio.run(service.add_favorite("user-1", _make_location()))

        # Rollback delete must have been called
        table.delete_item.assert_called_once()

    def test_dynamodb_error_on_put_raises_database_error(self):
        table = MagicMock()
        table.put_item.side_effect = _client_error("InternalServerError")

        service = self._service(table)
        with pytest.raises(DatabaseError):
            asyncio.run(service.add_favorite("user-1", _make_location()))

    def test_dynamodb_error_on_counter_update_raises_database_error(self):
        table = MagicMock()
        table.put_item.return_value = {}
        table.update_item.side_effect = _client_error("ProvisionedThroughputExceededException")
        table.delete_item.return_value = {}

        service = self._service(table)
        with pytest.raises(DatabaseError):
            asyncio.run(service.add_favorite("user-1", _make_location()))


# ---------------------------------------------------------------------------
# DynamoDBFavoritesService — remove_favorite
# ---------------------------------------------------------------------------


class TestRemoveFavorite:
    def _service(self, table):
        return DynamoDBFavoritesService(
            table_name="test-favorites",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def test_existing_item_deleted_and_counter_decremented(self):
        table = MagicMock()
        table.delete_item.return_value = {"Attributes": {"location_id": "london-uk"}}
        table.update_item.return_value = {}

        service = self._service(table)
        asyncio.run(service.remove_favorite("user-1", "london-uk"))

        table.delete_item.assert_called_once()
        table.update_item.assert_called_once()

    def test_missing_item_does_not_decrement_counter(self):
        table = MagicMock()
        table.delete_item.return_value = {}  # No "Attributes" key

        service = self._service(table)
        asyncio.run(service.remove_favorite("user-1", "nonexistent"))

        table.update_item.assert_not_called()

    def test_dynamodb_error_raises_database_error(self):
        table = MagicMock()
        table.delete_item.side_effect = _client_error("InternalServerError")

        service = self._service(table)
        with pytest.raises(DatabaseError):
            asyncio.run(service.remove_favorite("user-1", "london-uk"))


# ---------------------------------------------------------------------------
# DynamoDBFavoritesService — list_favorites
# ---------------------------------------------------------------------------


class TestListFavorites:
    def _service(self, table):
        return DynamoDBFavoritesService(
            table_name="test-favorites",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def _make_item(self, loc_id: str, added_at: str) -> dict:
        return {
            "user_id": "user-1",
            "location_id": loc_id,
            "location_name": "Test City",
            "location_region": "Region",
            "location_country": "Country",
            "latitude": "10.0",
            "longitude": "20.0",
            "timezone": "UTC",
            "added_at": added_at,
            "last_accessed": added_at,
        }

    def test_returns_empty_list_for_no_items(self):
        table = MagicMock()
        table.query.return_value = {"Items": []}

        service = self._service(table)
        result = asyncio.run(service.list_favorites("user-1"))

        assert result == []

    def test_counter_item_filtered_out(self):
        counter_item = {
            "user_id": "user-1",
            "location_id": _COUNTER_SORT_KEY,
            "count": 2,
        }
        real_item = self._make_item("london-uk", "2024-01-01T00:00:00+00:00")

        table = MagicMock()
        table.query.return_value = {"Items": [counter_item, real_item]}

        service = self._service(table)
        result = asyncio.run(service.list_favorites("user-1"))

        assert len(result) == 1
        assert result[0].location.id == "london-uk"

    def test_results_sorted_by_added_at(self):
        items = [
            self._make_item("paris-fr", "2024-01-03T00:00:00+00:00"),
            self._make_item("london-uk", "2024-01-01T00:00:00+00:00"),
            self._make_item("berlin-de", "2024-01-02T00:00:00+00:00"),
        ]

        table = MagicMock()
        table.query.return_value = {"Items": items}

        service = self._service(table)
        result = asyncio.run(service.list_favorites("user-1"))

        assert [f.location.id for f in result] == ["london-uk", "berlin-de", "paris-fr"]

    def test_dynamodb_error_raises_database_error(self):
        table = MagicMock()
        table.query.side_effect = _client_error("InternalServerError")

        service = self._service(table)
        with pytest.raises(DatabaseError):
            asyncio.run(service.list_favorites("user-1"))


# ---------------------------------------------------------------------------
# DynamoDBFavoritesService — get_favorite_count
# ---------------------------------------------------------------------------


class TestGetFavoriteCount:
    def _service(self, table):
        return DynamoDBFavoritesService(
            table_name="test-favorites",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def test_returns_zero_when_counter_not_found(self):
        table = MagicMock()
        table.get_item.return_value = {}

        service = self._service(table)
        assert asyncio.run(service.get_favorite_count("user-1")) == 0

    def test_returns_count_from_counter_item(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"count": 7}}

        service = self._service(table)
        assert asyncio.run(service.get_favorite_count("user-1")) == 7

    def test_dynamodb_error_raises_database_error(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("InternalServerError")

        service = self._service(table)
        with pytest.raises(DatabaseError):
            asyncio.run(service.get_favorite_count("user-1"))


# ---------------------------------------------------------------------------
# DynamoDBUnitService — convert_temperature
# ---------------------------------------------------------------------------


class TestConvertTemperature:
    def _service(self):
        table = MagicMock()
        return DynamoDBUnitService(
            table_name="test-preferences",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def test_celsius_to_fahrenheit(self):
        svc = self._service()
        result = asyncio.run(
            svc.convert_temperature(0.0, TempUnit.CELSIUS, TempUnit.FAHRENHEIT)
        )
        assert result == pytest.approx(32.0)

    def test_fahrenheit_to_celsius(self):
        svc = self._service()
        result = asyncio.run(
            svc.convert_temperature(32.0, TempUnit.FAHRENHEIT, TempUnit.CELSIUS)
        )
        assert result == pytest.approx(0.0)

    def test_boiling_point_preserved(self):
        svc = self._service()
        f = asyncio.run(svc.convert_temperature(100.0, TempUnit.CELSIUS, TempUnit.FAHRENHEIT))
        assert f == pytest.approx(212.0)

        c = asyncio.run(svc.convert_temperature(212.0, TempUnit.FAHRENHEIT, TempUnit.CELSIUS))
        assert c == pytest.approx(100.0)

    def test_same_unit_returns_unchanged(self):
        svc = self._service()
        result = asyncio.run(
            svc.convert_temperature(25.0, TempUnit.CELSIUS, TempUnit.CELSIUS)
        )
        assert result == pytest.approx(25.0)

    def test_invalid_unit_raises_validation_error(self):
        svc = self._service()
        with pytest.raises(ValidationError):
            asyncio.run(svc.convert_temperature(25.0, "kelvin", TempUnit.CELSIUS))  # type: ignore


# ---------------------------------------------------------------------------
# DynamoDBUnitService — get_user_preference
# ---------------------------------------------------------------------------


class TestGetUserPreference:
    def _service(self, table):
        return DynamoDBUnitService(
            table_name="test-preferences",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def test_returns_celsius_when_no_item_found(self):
        table = MagicMock()
        table.get_item.return_value = {}

        svc = self._service(table)
        result = asyncio.run(svc.get_user_preference("user-1"))
        assert result == TempUnit.CELSIUS

    def test_returns_stored_fahrenheit_preference(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"temperature_unit": "fahrenheit"}}

        svc = self._service(table)
        result = asyncio.run(svc.get_user_preference("user-1"))
        assert result == TempUnit.FAHRENHEIT

    def test_corrupt_value_falls_back_to_celsius(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"temperature_unit": "kelvin"}}

        svc = self._service(table)
        result = asyncio.run(svc.get_user_preference("user-1"))
        assert result == TempUnit.CELSIUS

    def test_dynamodb_error_raises_database_error(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("InternalServerError")

        svc = self._service(table)
        with pytest.raises(DatabaseError):
            asyncio.run(svc.get_user_preference("user-1"))


# ---------------------------------------------------------------------------
# DynamoDBUnitService — set_user_preference
# ---------------------------------------------------------------------------


class TestSetUserPreference:
    def _service(self, table):
        return DynamoDBUnitService(
            table_name="test-preferences",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def test_persists_unit_preference(self):
        table = MagicMock()
        table.put_item.return_value = {}

        svc = self._service(table)
        asyncio.run(svc.set_user_preference("user-1", TempUnit.FAHRENHEIT))

        call_kwargs = table.put_item.call_args[1]
        assert call_kwargs["Item"]["temperature_unit"] == "fahrenheit"
        assert call_kwargs["Item"]["user_id"] == "user-1"
        assert "ttl" in call_kwargs["Item"]

    def test_invalid_unit_raises_validation_error(self):
        table = MagicMock()
        svc = self._service(table)
        with pytest.raises(ValidationError):
            asyncio.run(svc.set_user_preference("user-1", "kelvin"))  # type: ignore

    def test_dynamodb_error_raises_database_error(self):
        table = MagicMock()
        table.put_item.side_effect = _client_error("InternalServerError")

        svc = self._service(table)
        with pytest.raises(DatabaseError):
            asyncio.run(svc.set_user_preference("user-1", TempUnit.CELSIUS))


# ---------------------------------------------------------------------------
# Task 10.3 — DynamoDB optimisation tests
# ---------------------------------------------------------------------------

from src.infrastructure.aws.dynamodb_adapter import (
    DaxEnabledDynamoDBAdapter,
    _FAVORITES_PROJECTION,
    _FAVORITES_PROJECTION_NAMES,
)


class TestListFavoritesProjection:
    """Verify that list_favorites uses a ProjectionExpression to limit
    the attributes fetched from DynamoDB (Requirement 5.4, 5.5 — keeps
    reads lean before per-favorite temperature enrichment)."""

    def _service(self, table):
        return DynamoDBFavoritesService(
            table_name="test-favorites",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def _make_item(self, loc_id: str, added_at: str) -> dict:
        return {
            "user_id": "user-1",
            "location_id": loc_id,
            "location_name": "Test City",
            "location_region": "Region",
            "location_country": "Country",
            "latitude": "10.0",
            "longitude": "20.0",
            "timezone": "UTC",
            "added_at": added_at,
            "last_accessed": added_at,
        }

    def test_query_includes_projection_expression(self):
        """Query call must pass ProjectionExpression so DynamoDB doesn't
        return all attributes (reduces RCU and network payload)."""
        table = MagicMock()
        table.query.return_value = {"Items": []}

        service = self._service(table)
        asyncio.run(service.list_favorites("user-1"))

        call_kwargs = table.query.call_args[1]
        assert "ProjectionExpression" in call_kwargs, (
            "list_favorites must include a ProjectionExpression"
        )

    def test_projection_expression_value_matches_constant(self):
        """The projection string passed to DynamoDB must equal the
        _FAVORITES_PROJECTION module constant."""
        table = MagicMock()
        table.query.return_value = {"Items": []}

        service = self._service(table)
        asyncio.run(service.list_favorites("user-1"))

        call_kwargs = table.query.call_args[1]
        assert call_kwargs["ProjectionExpression"] == _FAVORITES_PROJECTION

    def test_expression_attribute_names_includes_timezone_alias(self):
        """``timezone`` is a DynamoDB reserved word; the query must alias
        it via ExpressionAttributeNames to avoid a parser error."""
        table = MagicMock()
        table.query.return_value = {"Items": []}

        service = self._service(table)
        asyncio.run(service.list_favorites("user-1"))

        call_kwargs = table.query.call_args[1]
        assert "ExpressionAttributeNames" in call_kwargs, (
            "ExpressionAttributeNames must be present for the timezone alias"
        )
        attr_names = call_kwargs["ExpressionAttributeNames"]
        assert "#tz" in attr_names
        assert attr_names["#tz"] == "timezone"

    def test_projection_covers_all_required_fields(self):
        """Projection must include every field needed to build FavoriteLocation."""
        required = {
            "user_id",
            "location_id",
            "location_name",
            "location_region",
            "location_country",
            "latitude",
            "longitude",
            "added_at",
            "last_accessed",
        }
        # Expand aliases in the projection string for comparison.
        resolved = _FAVORITES_PROJECTION
        for alias, real in _FAVORITES_PROJECTION_NAMES.items():
            resolved = resolved.replace(alias, real)

        projected_fields = {f.strip() for f in resolved.split(",")}
        assert required.issubset(projected_fields), (
            f"Missing fields in projection: {required - projected_fields}"
        )

    def test_functional_result_unchanged_with_projection(self):
        """Returned FavoriteLocation objects must be identical regardless of
        the projection; the projection is a transport optimisation only."""
        items = [
            self._make_item("london-uk", "2024-01-01T00:00:00+00:00"),
            self._make_item("paris-fr", "2024-01-02T00:00:00+00:00"),
        ]
        table = MagicMock()
        table.query.return_value = {"Items": items}

        service = self._service(table)
        result = asyncio.run(service.list_favorites("user-1"))

        assert len(result) == 2
        assert result[0].location.id == "london-uk"
        assert result[1].location.id == "paris-fr"


class TestGetOrInitCount:
    """Verify the _get_or_init_count helper returns accurate counts and
    uses strong consistency (ConsistentRead=True)."""

    def _service(self, table):
        return DynamoDBFavoritesService(
            table_name="test-favorites",
            dynamodb_resource=_make_dynamodb_resource(table),
        )

    def test_returns_zero_when_no_counter_item(self):
        table = MagicMock()
        table.get_item.return_value = {}

        service = self._service(table)
        result = asyncio.run(service._get_or_init_count("user-1"))
        assert result == 0

    def test_returns_stored_count(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"count": 42}}

        service = self._service(table)
        result = asyncio.run(service._get_or_init_count("user-1"))
        assert result == 42

    def test_uses_consistent_read(self):
        """Counter checks must use ConsistentRead=True to avoid stale cap
        enforcement decisions under concurrent writes."""
        table = MagicMock()
        table.get_item.return_value = {}

        service = self._service(table)
        asyncio.run(service._get_or_init_count("user-1"))

        call_kwargs = table.get_item.call_args[1]
        assert call_kwargs.get("ConsistentRead") is True

    def test_dynamodb_error_raises_database_error(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("InternalServerError")

        service = self._service(table)
        with pytest.raises(DatabaseError):
            asyncio.run(service._get_or_init_count("user-1"))


class TestDaxEnabledDynamoDBAdapter:
    """Verify the DAX adapter stub falls back to standard DynamoDB when no
    endpoint is provided (the common case in tests and local dev)."""

    def test_no_endpoint_creates_standard_favorites_service(self):
        table = MagicMock()
        adapter = DaxEnabledDynamoDBAdapter(
            table_name="test-favorites",
            dynamodb_resource=_make_dynamodb_resource(table),
        )
        # Should behave identically to DynamoDBFavoritesService
        assert isinstance(adapter, DynamoDBFavoritesService)

    def test_no_endpoint_add_favorite_works(self):
        table = MagicMock()
        table.put_item.return_value = {}
        table.update_item.return_value = {}

        adapter = DaxEnabledDynamoDBAdapter(
            table_name="test-favorites",
            dynamodb_resource=_make_dynamodb_resource(table),
        )
        loc = _make_location()
        result = asyncio.run(adapter.add_favorite("user-1", loc))
        assert isinstance(result, FavoriteLocation)

    def test_dax_endpoint_raises_import_error_without_package(self):
        """When amazondax is not installed, a clear ImportError should be raised."""
        import sys
        # Temporarily hide amazondax from the import system
        original = sys.modules.get("amazondax", None)
        sys.modules["amazondax"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(ImportError, match="amazondax"):
                DaxEnabledDynamoDBAdapter(
                    table_name="test-favorites",
                    dax_endpoint="daxs://test.abc.dax-clusters.us-east-1.amazonaws.com",
                )
        finally:
            if original is None:
                del sys.modules["amazondax"]
            else:
                sys.modules["amazondax"] = original
