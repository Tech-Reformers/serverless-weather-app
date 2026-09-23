"""
LocationService — application-layer service for location search and device
location resolution.

This service wraps a :class:`~src.domain.services.LocationServicePort`
(provided via constructor injection) and adds:

* Query-length validation before any external call (Requirements 1.2,
  Property 9).
* A 5-second asyncio timeout for location searches (Requirements 1.1, 1.5).
* A 30-second asyncio timeout for device location resolution (Requirement 4.1,
  Property 7).
* A 10-second asyncio timeout for coordinate-based lookups (Requirement 1.5).
* Hard cap of 10 results per search (Requirement 1.1).
* Returns an empty list — never raises — when a valid query matches nothing
  (Requirement 1.4, Property 13).
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from src.domain.exceptions import TimeoutError, ValidationError
from src.domain.models import DeviceLocationData, Location
from src.domain.services import LocationServicePort
from src.domain.validators import validate_query_length

logger = logging.getLogger(__name__)

# Timeout constants (seconds)
_SEARCH_TIMEOUT = 5.0
_DEVICE_LOCATION_TIMEOUT = 30.0
_COORDINATE_LOOKUP_TIMEOUT = 10.0

# Result cap
_MAX_SEARCH_RESULTS = 10


class LocationService:
    """Application service that adds validation and timeouts around a
    :class:`~src.domain.services.LocationServicePort`.

    Args:
        location_port: The infrastructure adapter that performs the actual
            geocoding API calls.
    """

    def __init__(self, location_port: LocationServicePort) -> None:
        self._port = location_port

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def search_locations(
        self, query: str, limit: int = _MAX_SEARCH_RESULTS
    ) -> List[Location]:
        """Search for locations by name.

        Validates the query locally (no external call for short queries) then
        delegates to the port with a 5-second timeout.

        Args:
            query: Search string.  Must have ≥ 2 non-whitespace characters.
            limit: Maximum results to return (capped at 10).

        Returns:
            Up to *limit* (max 10) matching :class:`~src.domain.models.Location`
            objects.  Returns an empty list when no matches are found
            (Requirements 1.4, Property 13).

        Raises:
            ValidationError: If *query* has fewer than 2 non-whitespace
                characters.  No external call is made (Property 9).
            TimeoutError: If the port does not respond within 5 seconds
                (Requirements 1.1, 1.5).
            ExternalServiceError: If the geocoding provider returns a failure.
        """
        if not validate_query_length(query):
            raise ValidationError(
                "Location search query must contain at least 2 non-whitespace "
                f"characters; received: {query!r}"
            )

        effective_limit = min(limit, _MAX_SEARCH_RESULTS)

        try:
            results: List[Location] = await asyncio.wait_for(
                self._port.search_locations(query, effective_limit),
                timeout=_SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Location search timed out after {_SEARCH_TIMEOUT}s for query: {query!r}"
            ) from exc

        # Guard: ensure the port never exceeds the limit (defensive slice)
        return results[:effective_limit]

    async def resolve_device_location(
        self, device_data: DeviceLocationData
    ) -> Optional[Location]:
        """Resolve device GPS coordinates to the nearest weather location.

        Delegates to the port with a 30-second timeout.  Locations must be
        within 50 km of the device position (enforced by the port/adapter
        layer per Requirements 4.2, 4.4).

        Args:
            device_data: Raw coordinates and accuracy reported by the device.

        Returns:
            The nearest :class:`~src.domain.models.Location` within 50 km, or
            ``None`` if no location is found within the radius (Property 17).

        Raises:
            TimeoutError: If the port does not respond within 30 seconds
                (Requirements 4.1, Property 7).
            ExternalServiceError: If the geocoding provider is unavailable.
        """
        try:
            return await asyncio.wait_for(
                self._port.resolve_device_location(device_data),
                timeout=_DEVICE_LOCATION_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Device location resolution timed out after {_DEVICE_LOCATION_TIMEOUT}s"
            ) from exc

    async def get_location_by_coordinates(
        self, lat: float, lon: float
    ) -> Optional[Location]:
        """Resolve a location from geographic coordinates.

        Delegates to the port with a 10-second timeout.

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.

        Returns:
            The nearest named :class:`~src.domain.models.Location`, or
            ``None`` if the coordinates cannot be resolved.

        Raises:
            TimeoutError: If the port does not respond within 10 seconds.
            ExternalServiceError: If the geocoding provider fails.
        """
        try:
            return await asyncio.wait_for(
                self._port.get_location_by_coordinates(lat, lon),
                timeout=_COORDINATE_LOOKUP_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Coordinate lookup timed out after {_COORDINATE_LOOKUP_TIMEOUT}s "
                f"for ({lat}, {lon})"
            ) from exc
