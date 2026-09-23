"""
Unit tests for FavoritesService (application layer).

All port dependencies are replaced with MagicMock / AsyncMock instances so
no real DynamoDB or external API calls are made.

Coverage
--------
- add_favorite: success, FavoriteLimitExceededError propagation,
  DuplicateFavoriteError propagation
- list_favorites: temperature enrichment (success), timeout per-location
  (Property 18), exception per-location (Property 18), empty list
- remove_favorite: delegation to port
- get_favorite_count: delegation to port
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.exceptions import (
    DatabaseError,
    DuplicateFavoriteError,
    ExternalServiceError,
    FavoriteLimitExceededError,
)
from src.domain.models import CurrentConditions, FavoriteLocation, Location
from src.application.favorites_service import FavoritesService


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


def _make_favorite(
    user_id: str = "user-1",
    loc_id: str = "london-uk",
    current_temperature: float | None = None,
) -> FavoriteLocation:
    return FavoriteLocation(
        user_id=user_id,
        location=_make_location(loc_id),
        added_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        last_accessed=datetime(2024, 1, 2, tzinfo=timezone.utc),
        current_temperature=current_temperature,
    )


def _make_conditions(temperature: float = 15.5) -> CurrentConditions:
    return CurrentConditions(
        temperature=temperature,
        feels_like=14.0,
        humidity=65,
        pressure=1013,
        wind_speed=12.3,
        wind_direction=180,
        condition="Partly cloudy",
        icon="cloud",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        sunrise=None,
        sunset=None,
        visibility=10000,
    )


def _make_service(
    favorites_port: MagicMock | None = None,
    weather_port: MagicMock | None = None,
) -> FavoritesService:
    return FavoritesService(
        favorites_port=favorites_port or MagicMock(),
        weather_port=weather_port or MagicMock(),
    )


# ---------------------------------------------------------------------------
# add_favorite
# ---------------------------------------------------------------------------


class TestAddFavorite:
    def test_success_returns_favorite_location(self):
        expected = _make_favorite()
        favorites_port = MagicMock()
        favorites_port.add_favorite = AsyncMock(return_value=expected)

        svc = _make_service(favorites_port=favorites_port)
        result = asyncio.run(svc.add_favorite("user-1", _make_location()))

        assert result is expected
        favorites_port.add_favorite.assert_awaited_once_with("user-1", _make_location())

    def test_limit_exceeded_propagates(self):
        favorites_port = MagicMock()
        favorites_port.add_favorite = AsyncMock(
            side_effect=FavoriteLimitExceededError("limit reached")
        )

        svc = _make_service(favorites_port=favorites_port)
        with pytest.raises(FavoriteLimitExceededError):
            asyncio.run(svc.add_favorite("user-1", _make_location()))

    def test_duplicate_propagates(self):
        favorites_port = MagicMock()
        favorites_port.add_favorite = AsyncMock(
            side_effect=DuplicateFavoriteError("already exists")
        )

        svc = _make_service(favorites_port=favorites_port)
        with pytest.raises(DuplicateFavoriteError):
            asyncio.run(svc.add_favorite("user-1", _make_location()))

    def test_database_error_propagates(self):
        favorites_port = MagicMock()
        favorites_port.add_favorite = AsyncMock(
            side_effect=DatabaseError("db failure")
        )

        svc = _make_service(favorites_port=favorites_port)
        with pytest.raises(DatabaseError):
            asyncio.run(svc.add_favorite("user-1", _make_location()))


# ---------------------------------------------------------------------------
# remove_favorite
# ---------------------------------------------------------------------------


class TestRemoveFavorite:
    def test_delegates_to_port(self):
        favorites_port = MagicMock()
        favorites_port.remove_favorite = AsyncMock(return_value=None)

        svc = _make_service(favorites_port=favorites_port)
        asyncio.run(svc.remove_favorite("user-1", "london-uk"))

        favorites_port.remove_favorite.assert_awaited_once_with("user-1", "london-uk")

    def test_database_error_propagates(self):
        favorites_port = MagicMock()
        favorites_port.remove_favorite = AsyncMock(
            side_effect=DatabaseError("db failure")
        )

        svc = _make_service(favorites_port=favorites_port)
        with pytest.raises(DatabaseError):
            asyncio.run(svc.remove_favorite("user-1", "london-uk"))


# ---------------------------------------------------------------------------
# get_favorite_count
# ---------------------------------------------------------------------------


class TestGetFavoriteCount:
    def test_delegates_to_port(self):
        favorites_port = MagicMock()
        favorites_port.get_favorite_count = AsyncMock(return_value=7)

        svc = _make_service(favorites_port=favorites_port)
        result = asyncio.run(svc.get_favorite_count("user-1"))

        assert result == 7
        favorites_port.get_favorite_count.assert_awaited_once_with("user-1")

    def test_database_error_propagates(self):
        favorites_port = MagicMock()
        favorites_port.get_favorite_count = AsyncMock(
            side_effect=DatabaseError("db failure")
        )

        svc = _make_service(favorites_port=favorites_port)
        with pytest.raises(DatabaseError):
            asyncio.run(svc.get_favorite_count("user-1"))


# ---------------------------------------------------------------------------
# list_favorites — temperature enrichment
# ---------------------------------------------------------------------------


class TestListFavoritesEnrichment:
    def test_empty_list_returns_empty(self):
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(return_value=[])

        svc = _make_service(favorites_port=favorites_port)
        result = asyncio.run(svc.list_favorites("user-1"))

        assert result == []

    def test_temperature_enrichment_on_success(self):
        """list_favorites populates current_temperature from weather conditions."""
        fav = _make_favorite(current_temperature=None)
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(return_value=[fav])

        weather_port = MagicMock()
        weather_port.get_current_conditions = AsyncMock(
            return_value=_make_conditions(temperature=18.0)
        )

        svc = _make_service(favorites_port=favorites_port, weather_port=weather_port)
        result = asyncio.run(svc.list_favorites("user-1"))

        assert len(result) == 1
        assert result[0].current_temperature == 18.0

    def test_multiple_favorites_all_enriched(self):
        """Each favorite gets its own temperature fetched."""
        favs = [
            _make_favorite(loc_id="london-uk"),
            _make_favorite(loc_id="paris-fr"),
        ]
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(return_value=favs)

        conditions_by_call = [
            _make_conditions(temperature=10.0),
            _make_conditions(temperature=20.0),
        ]
        weather_port = MagicMock()
        weather_port.get_current_conditions = AsyncMock(
            side_effect=conditions_by_call
        )

        svc = _make_service(favorites_port=favorites_port, weather_port=weather_port)
        result = asyncio.run(svc.list_favorites("user-1"))

        temps = {r.location.id: r.current_temperature for r in result}
        assert temps["london-uk"] == 10.0
        assert temps["paris-fr"] == 20.0

    def test_temp_fetch_timeout_sets_none_and_retains_entry(self):
        """Property 18: timeout during temperature fetch → current_temperature=None,
        favorite still included in list."""
        fav = _make_favorite()
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(return_value=[fav])

        async def _slow(*args, **kwargs):
            await asyncio.sleep(100)

        weather_port = MagicMock()
        weather_port.get_current_conditions = _slow

        # Patch the timeout constant to 0.05s so the test runs fast
        with patch(
            "src.application.favorites_service._TEMP_FETCH_TIMEOUT", 0.05
        ):
            svc = _make_service(favorites_port=favorites_port, weather_port=weather_port)
            result = asyncio.run(svc.list_favorites("user-1"))

        assert len(result) == 1
        assert result[0].current_temperature is None

    def test_temp_fetch_external_error_sets_none_and_retains_entry(self):
        """Property 18: ExternalServiceError during temperature fetch →
        current_temperature=None, favorite still included."""
        fav = _make_favorite()
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(return_value=[fav])

        weather_port = MagicMock()
        weather_port.get_current_conditions = AsyncMock(
            side_effect=ExternalServiceError("weather API down")
        )

        svc = _make_service(favorites_port=favorites_port, weather_port=weather_port)
        result = asyncio.run(svc.list_favorites("user-1"))

        assert len(result) == 1
        assert result[0].current_temperature is None

    def test_temp_fetch_generic_error_sets_none_and_retains_entry(self):
        """Any unexpected exception during temperature fetch should be caught,
        leaving current_temperature=None and retaining the favorite."""
        fav = _make_favorite()
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(return_value=[fav])

        weather_port = MagicMock()
        weather_port.get_current_conditions = AsyncMock(
            side_effect=RuntimeError("unexpected error")
        )

        svc = _make_service(favorites_port=favorites_port, weather_port=weather_port)
        result = asyncio.run(svc.list_favorites("user-1"))

        assert len(result) == 1
        assert result[0].current_temperature is None

    def test_partial_failure_enriches_successful_and_nulls_failed(self):
        """When one temp fetch succeeds and another fails, only the successful
        one has current_temperature set; both entries are returned."""
        fav_a = _make_favorite(loc_id="london-uk")
        fav_b = _make_favorite(loc_id="paris-fr")
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(return_value=[fav_a, fav_b])

        call_count = 0

        async def _weather_side_effect(location, **kwargs):
            nonlocal call_count
            call_count += 1
            if location.id == "london-uk":
                return _make_conditions(temperature=12.0)
            raise ExternalServiceError("no data for paris")

        weather_port = MagicMock()
        weather_port.get_current_conditions = _weather_side_effect

        svc = _make_service(favorites_port=favorites_port, weather_port=weather_port)
        result = asyncio.run(svc.list_favorites("user-1"))

        assert len(result) == 2
        temps = {r.location.id: r.current_temperature for r in result}
        assert temps["london-uk"] == 12.0
        assert temps["paris-fr"] is None

    def test_list_order_preserved_after_enrichment(self):
        """The order of favorites from the port is preserved in the result."""
        favs = [
            _make_favorite(loc_id=f"city-{i}")
            for i in range(5)
        ]
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(return_value=favs)

        weather_port = MagicMock()
        weather_port.get_current_conditions = AsyncMock(
            return_value=_make_conditions(temperature=10.0)
        )

        svc = _make_service(favorites_port=favorites_port, weather_port=weather_port)
        result = asyncio.run(svc.list_favorites("user-1"))

        assert [r.location.id for r in result] == [f"city-{i}" for i in range(5)]

    def test_database_error_from_port_propagates(self):
        """If the underlying port list operation fails, the error propagates."""
        favorites_port = MagicMock()
        favorites_port.list_favorites = AsyncMock(
            side_effect=DatabaseError("db failure")
        )

        svc = _make_service(favorites_port=favorites_port)
        with pytest.raises(DatabaseError):
            asyncio.run(svc.list_favorites("user-1"))
