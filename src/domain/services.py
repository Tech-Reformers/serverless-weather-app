"""
Service port interfaces (abstract base classes) for the Serverless Weather App.

These classes define the hexagonal-architecture *ports* — the contracts that
application services and infrastructure adapters must satisfy.  No concrete
I/O or AWS-specific code belongs here; only abstract method signatures and
their documentation.

Port naming convention: ``<Domain>ServicePort``

Raised exceptions are documented per method.  All exceptions are defined in
:mod:`src.domain.exceptions`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from .exceptions import (
    DatabaseError,
    DuplicateFavoriteError,
    ExternalServiceError,
    FavoriteLimitExceededError,
    TimeoutError,
    ValidationError,
)
from .models import (
    CurrentConditions,
    DailyForecast,
    DeviceLocationData,
    FavoriteLocation,
    HourlyForecast,
    Location,
    TempUnit,
)


# ---------------------------------------------------------------------------
# Location Service Port
# ---------------------------------------------------------------------------


class LocationServicePort(ABC):
    """Port for resolving, searching, and managing geographic locations.

    Implementations must integrate with an external geocoding provider (e.g.
    OpenWeather Geocoding API) and honour the timeout and result-limit
    constraints defined in Requirements 1.1, 1.2, 1.4, 1.5, 4.1–4.4.
    """

    @abstractmethod
    async def search_locations(
        self,
        query: str,
        limit: int = 10,
    ) -> List[Location]:
        """Search for locations by name query.

        Args:
            query: Search string.  Must contain at least 2 non-whitespace
                characters (Requirements 1.2, Property 9).
            limit: Maximum number of results to return (default 10, per
                Requirements 1.1).

        Returns:
            A list of up to *limit* matching :class:`~.models.Location`
            objects, ordered by relevance.  An empty list is returned when
            the query is valid but matches no locations (Requirements 1.4,
            Property 13).

        Raises:
            ValidationError: If *query* has fewer than 2 non-whitespace
                characters.  No external API call must be made in this case
                (Property 9).
            ExternalServiceError: If the geocoding API returns a failure
                response.
            TimeoutError: If the geocoding API does not respond within 10
                seconds (Requirements 1.5).
        """

    @abstractmethod
    async def get_location_by_coordinates(
        self,
        lat: float,
        lon: float,
    ) -> Optional[Location]:
        """Resolve a :class:`~.models.Location` from geographic coordinates.

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.

        Returns:
            The nearest named :class:`~.models.Location`, or ``None`` if no
            location can be resolved for the given coordinates.

        Raises:
            ExternalServiceError: If the reverse-geocoding API fails.
            TimeoutError: If the request exceeds the configured timeout.
        """

    @abstractmethod
    async def resolve_device_location(
        self,
        device_data: DeviceLocationData,
    ) -> Optional[Location]:
        """Resolve device GPS coordinates to the nearest weather location.

        The implementation must search for locations within 50 km of the
        reported device position (Requirements 4.2).

        Args:
            device_data: Raw coordinates and accuracy reported by the device.

        Returns:
            The nearest :class:`~.models.Location` within 50 km, or ``None``
            if no location exists within that radius (Requirements 4.4,
            Property 17).

        Raises:
            TimeoutError: If device location resolution exceeds 30 seconds
                (Requirements 4.1, Property 7).
            ExternalServiceError: If the geocoding service is unavailable.
        """


# ---------------------------------------------------------------------------
# Weather Data Service Port
# ---------------------------------------------------------------------------


class WeatherDataServicePort(ABC):
    """Port for retrieving and caching current weather conditions.

    Implementations must apply the multi-layer caching strategy described in
    the design (DynamoDB TTL = 15 minutes) and fall back to the external API
    when the cache is cold or stale (Requirements 2.1–2.8, Property 3).
    """

    @abstractmethod
    async def get_current_conditions(
        self,
        location: Location,
        force_refresh: bool = False,
    ) -> CurrentConditions:
        """Get current weather conditions for a location, using cache when
        available.

        Args:
            location: The target location.
            force_refresh: When ``True``, bypass all caches and fetch fresh
                data from the external API (Requirements 7.1).

        Returns:
            A :class:`~.models.CurrentConditions` instance for the location.

        Raises:
            ExternalServiceError: If the weather API returns a failure
                response (Requirements 2.7).
            TimeoutError: If the request does not complete within 10 seconds
                (Requirements 2.6).
        """

    @abstractmethod
    async def refresh_current_conditions(
        self,
        location: Location,
    ) -> CurrentConditions:
        """Force-refresh current conditions, bypassing all caches.

        Equivalent to calling :meth:`get_current_conditions` with
        ``force_refresh=True``.  Provided as an explicit method so that
        refresh-specific instrumentation and timeout handling can be applied
        independently (Requirements 7.1–7.4, Property 15, Property 24).

        Args:
            location: The target location.

        Returns:
            Freshly retrieved :class:`~.models.CurrentConditions`.

        Raises:
            ExternalServiceError: If the weather API fails.
            TimeoutError: If the refresh does not complete within 5 seconds
                (Requirements 7.3).
        """


# ---------------------------------------------------------------------------
# Forecast Service Port
# ---------------------------------------------------------------------------


class ForecastServicePort(ABC):
    """Port for retrieving hourly and daily weather forecasts.

    Implementations must cache forecast data with a 1-hour TTL and retrieve
    fresh data from the external API when the cache is stale
    (Requirements 3.1–3.6, Properties 3, 11, 16, 22).
    """

    @abstractmethod
    async def get_hourly_forecast(
        self,
        location: Location,
        hours: int = 24,
    ) -> List[HourlyForecast]:
        """Retrieve an hourly forecast for the next *hours* hours.

        Args:
            location: The target location.
            hours: Number of hourly entries to return (default 24, per
                Requirements 3.1).

        Returns:
            A list of exactly *hours* :class:`~.models.HourlyForecast`
            entries in chronological order (Property 11, Requirements 3.3).

        Raises:
            ExternalServiceError: If the forecast API returns a failure
                response (Requirements 3.6).
            TimeoutError: If the request does not complete within 10 seconds
                (Requirements 3.5, Property 22).
        """

    @abstractmethod
    async def get_daily_forecast(
        self,
        location: Location,
        days: int = 7,
    ) -> List[DailyForecast]:
        """Retrieve a daily forecast for the next *days* days.

        Args:
            location: The target location.
            days: Number of daily entries to return (default 7, per
                Requirements 3.2).

        Returns:
            A list of exactly *days* :class:`~.models.DailyForecast` entries
            in chronological order (Property 11, Requirements 3.4).

        Raises:
            ExternalServiceError: If the forecast API returns a failure
                response (Requirements 3.6).
            TimeoutError: If the request does not complete within 10 seconds
                (Requirements 3.5, Property 22).
        """


# ---------------------------------------------------------------------------
# Favorites Service Port
# ---------------------------------------------------------------------------


class FavoritesServicePort(ABC):
    """Port for managing a user's saved favourite locations.

    Implementations must enforce the 50-location hard cap (Requirements 5.1,
    5.2, Property 1), prevent duplicates (Requirements 5.7, Property 14),
    and persist data across sessions (Requirements 5.8).
    """

    @abstractmethod
    async def add_favorite(
        self,
        user_id: str,
        location: Location,
    ) -> FavoriteLocation:
        """Add a location to the user's favourites list.

        Args:
            user_id: Unique identifier of the owning user.
            location: The location to save.

        Returns:
            The newly created :class:`~.models.FavoriteLocation`.

        Raises:
            FavoriteLimitExceededError: If the user already has 50 saved
                locations (Requirements 5.2, Property 1).  The existing list
                must remain unchanged.
            DuplicateFavoriteError: If *location* is already in the user's
                favourites (Requirements 5.7, Property 14).  The list must
                retain a single entry and remain unchanged.
            DatabaseError: If the DynamoDB write operation fails.
        """

    @abstractmethod
    async def remove_favorite(
        self,
        user_id: str,
        location_id: str,
    ) -> None:
        """Remove a favourite location from the user's list.

        Args:
            user_id: Unique identifier of the owning user.
            location_id: ID of the location to remove (Requirements 5.3).

        Raises:
            DatabaseError: If the DynamoDB delete operation fails.
        """

    @abstractmethod
    async def list_favorites(
        self,
        user_id: str,
    ) -> List[FavoriteLocation]:
        """Return all of the user's favourite locations.

        Each entry includes the current temperature if it can be fetched
        within 5 seconds; otherwise ``current_temperature`` is ``None``
        (Requirements 5.4, 5.5, Property 18, Property 23).

        Args:
            user_id: Unique identifier of the user.

        Returns:
            List of :class:`~.models.FavoriteLocation` objects, ordered by
            the time they were added.

        Raises:
            DatabaseError: If the DynamoDB read operation fails.
        """

    @abstractmethod
    async def get_favorite_count(
        self,
        user_id: str,
    ) -> int:
        """Return the number of favourite locations saved by the user.

        Used to enforce the 50-location cap before attempting a write
        (Requirements 5.1, Property 1).

        Args:
            user_id: Unique identifier of the user.

        Returns:
            Integer count (0–50).

        Raises:
            DatabaseError: If the DynamoDB read operation fails.
        """


# ---------------------------------------------------------------------------
# Unit Conversion Service Port
# ---------------------------------------------------------------------------


class UnitConversionServicePort(ABC):
    """Port for temperature unit conversion and user preference persistence.

    Implementations must restrict the valid unit set to Celsius and
    Fahrenheit (Requirements 6.3, Property 2) and persist the preference
    across sessions (Requirements 6.4, 6.5).
    """

    @abstractmethod
    async def convert_temperature(
        self,
        value: float,
        from_unit: TempUnit,
        to_unit: TempUnit,
    ) -> float:
        """Convert a temperature value between units.

        The conversion must preserve physical relationships — e.g. the
        freezing point of water is always 0 °C / 32 °F (Requirements 6.1,
        Property 10).

        Args:
            value: The temperature value to convert.
            from_unit: The unit *value* is currently expressed in.
            to_unit: The target unit.

        Returns:
            The converted temperature as a float.

        Raises:
            ValidationError: If either *from_unit* or *to_unit* is not a
                valid :class:`~.models.TempUnit` member (Property 2).
        """

    @abstractmethod
    async def get_user_preference(
        self,
        user_id: str,
    ) -> TempUnit:
        """Retrieve the persisted temperature unit preference for a user.

        If no preference has been stored, the implementation must return
        :attr:`~.models.TempUnit.CELSIUS` as the default (Requirements 6.7).

        Args:
            user_id: Unique identifier of the user.

        Returns:
            The user's :class:`~.models.TempUnit` preference.

        Raises:
            DatabaseError: If the DynamoDB read operation fails.
        """

    @abstractmethod
    async def set_user_preference(
        self,
        user_id: str,
        unit: TempUnit,
    ) -> None:
        """Persist the user's temperature unit preference.

        Args:
            user_id: Unique identifier of the user.
            unit: The :class:`~.models.TempUnit` to persist.

        Raises:
            ValidationError: If *unit* is not a valid
                :class:`~.models.TempUnit` member (Requirements 6.3).
            DatabaseError: If the DynamoDB write fails.  Callers must retain
                the selected unit for the current session and surface a save
                error to the user (Requirements 6.6, Property 19).
        """
