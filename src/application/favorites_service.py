"""
FavoritesService — application-layer service for managing favorite locations.

Wraps a :class:`~src.domain.services.FavoritesServicePort` (e.g.
:class:`~src.infrastructure.aws.dynamodb_adapter.DynamoDBFavoritesService`)
and a :class:`~src.domain.services.WeatherDataServicePort` to enrich favorite
locations with their current temperature when listing.

Responsibilities
----------------
* **Delegation**: ``add_favorite``, ``remove_favorite``, and
  ``get_favorite_count`` delegate directly to the persistence port and
  propagate all port exceptions unchanged.
* **Temperature enrichment**: ``list_favorites`` fetches current conditions for
  each favorite concurrently, applying a 5-second per-location timeout.  When
  a temperature retrieval fails or times out, ``current_temperature`` is left
  as ``None`` and the entry is still returned (Requirements 5.4, 5.5,
  Property 18).
* **Cap enforcement**: The 50-favorite hard cap is enforced by the port via
  :exc:`~src.domain.exceptions.FavoriteLimitExceededError` (Requirements 5.1,
  5.2).
* **Duplicate prevention**: The port raises
  :exc:`~src.domain.exceptions.DuplicateFavoriteError` for duplicate saves
  (Requirements 5.7, Property 14).
* **Persistence**: Fully delegated to the port (Requirement 5.8).
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

from src.domain.exceptions import ExternalServiceError
from src.domain.models import FavoriteLocation, Location
from src.domain.services import FavoritesServicePort, WeatherDataServicePort

logger = logging.getLogger(__name__)

# Per-location timeout for temperature enrichment (seconds)
_TEMP_FETCH_TIMEOUT = 5.0


class FavoritesService:
    """Application service that adds temperature enrichment to favorite
    location listings.

    Args:
        favorites_port: The persistence adapter implementing
            :class:`~src.domain.services.FavoritesServicePort`.
        weather_port: The weather adapter implementing
            :class:`~src.domain.services.WeatherDataServicePort`, used to
            fetch current temperatures for each favorite.
    """

    def __init__(
        self,
        favorites_port: FavoritesServicePort,
        weather_port: WeatherDataServicePort,
    ) -> None:
        self._favorites = favorites_port
        self._weather = weather_port

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def add_favorite(
        self,
        user_id: str,
        location: Location,
    ) -> FavoriteLocation:
        """Add *location* to *user_id*'s favourites.

        Delegates entirely to the persistence port.

        Args:
            user_id: Unique identifier of the owning user.
            location: The location to save.

        Returns:
            The newly created :class:`~src.domain.models.FavoriteLocation`.

        Raises:
            FavoriteLimitExceededError: If the user already has 50 saved
                locations (Requirements 5.2).  The existing list remains
                unchanged.
            DuplicateFavoriteError: If *location* is already in the user's
                favourites (Requirements 5.7).  The list retains a single
                entry unchanged.
            DatabaseError: If the underlying persistence operation fails.
        """
        return await self._favorites.add_favorite(user_id, location)

    async def remove_favorite(
        self,
        user_id: str,
        location_id: str,
    ) -> None:
        """Remove a favourite location from *user_id*'s list.

        Delegates entirely to the persistence port.

        Args:
            user_id: Unique identifier of the owning user.
            location_id: ID of the location to remove (Requirements 5.3).

        Raises:
            DatabaseError: If the underlying persistence operation fails.
        """
        await self._favorites.remove_favorite(user_id, location_id)

    async def list_favorites(
        self,
        user_id: str,
    ) -> List[FavoriteLocation]:
        """Return all of *user_id*'s favourite locations, enriched with
        current temperatures.

        For each favorite the service attempts to fetch the current temperature
        via the weather port with a 5-second timeout (Requirements 5.4, 5.5,
        Property 18):

        * On success: ``current_temperature`` is set to the fetched value.
        * On timeout or any weather error: ``current_temperature`` remains
          ``None`` and the favorite is still included in the returned list.

        All temperature fetches run concurrently via :func:`asyncio.gather`.

        Args:
            user_id: Unique identifier of the user.

        Returns:
            List of :class:`~src.domain.models.FavoriteLocation` objects,
            ordered by the time they were added, each optionally carrying
            ``current_temperature``.

        Raises:
            DatabaseError: If the DynamoDB read for the favorites list fails.
        """
        favorites = await self._favorites.list_favorites(user_id)

        if not favorites:
            return favorites

        # Enrich each favorite with its current temperature concurrently
        enriched = await asyncio.gather(
            *[self._enrich_temperature(fav) for fav in favorites]
        )
        return list(enriched)

    async def get_favorite_count(
        self,
        user_id: str,
    ) -> int:
        """Return the number of favourite locations saved by *user_id*.

        Delegates to the persistence port.

        Args:
            user_id: Unique identifier of the user.

        Returns:
            Integer count in the range 0–50.

        Raises:
            DatabaseError: If the underlying read operation fails.
        """
        return await self._favorites.get_favorite_count(user_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _enrich_temperature(
        self,
        favorite: FavoriteLocation,
    ) -> FavoriteLocation:
        """Attempt to fetch and attach the current temperature for *favorite*.

        If the weather service does not respond within ``_TEMP_FETCH_TIMEOUT``
        seconds or raises any exception, ``current_temperature`` is left as
        ``None`` and the original favorite is returned unchanged (Property 18).

        Args:
            favorite: The favorite location to enrich.

        Returns:
            The same :class:`~src.domain.models.FavoriteLocation` with
            ``current_temperature`` populated on success, or ``None`` on
            failure.
        """
        try:
            conditions = await asyncio.wait_for(
                self._weather.get_current_conditions(favorite.location),
                timeout=_TEMP_FETCH_TIMEOUT,
            )
            favorite.current_temperature = conditions.temperature
        except asyncio.TimeoutError:
            logger.warning(
                "Temperature fetch timed out after %ss for location '%s' "
                "(user '%s'); displaying as unavailable.",
                _TEMP_FETCH_TIMEOUT,
                favorite.location.id,
                favorite.user_id,
            )
        except Exception:
            logger.warning(
                "Temperature fetch failed for location '%s' (user '%s'); "
                "displaying as unavailable.",
                favorite.location.id,
                favorite.user_id,
                exc_info=True,
            )
        return favorite
