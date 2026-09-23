"""
GeocodingAdapter — infrastructure adapter for the external geocoding / reverse-
geocoding provider.

Implements LocationServicePort using httpx for async HTTP calls with:
  - 10-second per-request timeout (Requirements 1.5, 4.1)
  - Exponential-backoff retry (max 3 attempts, 1 s base delay)
  - Query-length validation before any network call (Requirements 1.2, Property 9)
  - 50 km radius filter for device location resolution (Requirements 4.2, 4.4)

The base URL is read from GEOCODING_API_URL (defaults to the OpenWeatherMap
Geocoding API).  Substitute any compatible reverse-geocoding endpoint via that
variable; the adapter maps a simple JSON contract described inline.

API key is read from GEOCODING_API_KEY; if unset falls back to WEATHER_API_KEY
(many providers share one key for both endpoints).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

import httpx

from src.domain.exceptions import ExternalServiceError, TimeoutError, ValidationError
from src.domain.models import DeviceLocationData, Location
from src.domain.services import LocationServicePort

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

_DEFAULT_GEOCODING_API_URL = "https://api.openweathermap.org/geo/1.0"
_TIMEOUT_SECONDS = 10.0
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0  # seconds
_DEVICE_LOCATION_RADIUS_KM = 50.0
_EARTH_RADIUS_KM = 6_371.0


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------


class GeocodingAdapter(LocationServicePort):
    """Adapter that calls an external geocoding provider to fulfil
    LocationServicePort.

    Args:
        api_url: Base URL of the geocoding REST API.  Defaults to the value
            of GEOCODING_API_URL (or the OpenWeatherMap Geocoding endpoint).
        api_key: Provider API key.  When supplied it takes priority over the
            environment variable, making the adapter fully injectable for tests.
        timeout: Per-request timeout in seconds (default 10).
        max_retries: Maximum number of attempts per operation (default 3).
        retry_base_delay: Base delay (seconds) for exponential backoff.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = _TIMEOUT_SECONDS,
        max_retries: int = _MAX_RETRY_ATTEMPTS,
        retry_base_delay: float = _RETRY_BASE_DELAY,
    ) -> None:
        self._api_url = (
            api_url or os.environ.get("GEOCODING_API_URL", _DEFAULT_GEOCODING_API_URL)
        ).rstrip("/")
        self._api_key = api_key  # None → resolved lazily via _resolve_api_key()
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

    # ------------------------------------------------------------------
    # LocationServicePort implementation
    # ------------------------------------------------------------------

    async def search_locations(self, query: str, limit: int = 10) -> List[Location]:
        """Search for locations by name.

        Validates the query locally before making any network call
        (Requirements 1.2, Property 9).

        Args:
            query: Search string.  Must contain ≥ 2 non-whitespace characters.
            limit: Maximum results (default 10).

        Returns:
            List of up to *limit* matching Location objects.  Empty list when
            no matches exist (Requirements 1.4, Property 13).

        Raises:
            ValidationError: If *query* has fewer than 2 non-whitespace chars.
            ExternalServiceError: On provider error after retries.
            TimeoutError: On request timeout after retries.
        """
        self._validate_query(query)
        return await self._with_retry(self._fetch_search_locations, query, limit)

    async def get_location_by_coordinates(self, lat: float, lon: float) -> Optional[Location]:
        """Reverse-geocode a coordinate pair to the nearest named location.

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.

        Returns:
            Nearest Location, or None if the provider returns no results.

        Raises:
            ExternalServiceError: On provider error after retries.
            TimeoutError: On request timeout after retries.
        """
        results: List[Location] = await self._with_retry(
            self._fetch_reverse_geocode, lat, lon
        )
        return results[0] if results else None

    async def resolve_device_location(
        self, device_data: DeviceLocationData
    ) -> Optional[Location]:
        """Resolve device GPS data to the nearest weather location within 50 km.

        Performs a reverse-geocode of the device coordinates and filters the
        results to only those within 50 km of the reported position.

        Args:
            device_data: Coordinates and accuracy reported by the device.

        Returns:
            The nearest Location within 50 km, or None if none is found
            (Requirements 4.4, Property 17).

        Raises:
            TimeoutError: If resolution exceeds the configured timeout after
                all retries (Requirements 4.1, Property 7).
            ExternalServiceError: If the geocoding service is unavailable.
        """
        candidates: List[Location] = await self._with_retry(
            self._fetch_reverse_geocode, device_data.latitude, device_data.longitude
        )

        # Filter to the 50 km radius and return the closest.
        nearby = [
            loc
            for loc in candidates
            if _haversine_km(
                device_data.latitude,
                device_data.longitude,
                loc.latitude,
                loc.longitude,
            )
            <= _DEVICE_LOCATION_RADIUS_KM
        ]

        if not nearby:
            return None

        return min(
            nearby,
            key=lambda loc: _haversine_km(
                device_data.latitude,
                device_data.longitude,
                loc.latitude,
                loc.longitude,
            ),
        )

    # ------------------------------------------------------------------
    # Internal fetch helpers (single attempt, no retry)
    # ------------------------------------------------------------------

    async def _fetch_search_locations(self, query: str, limit: int) -> List[Location]:
        """Single attempt: forward-geocode *query* to a list of Locations."""
        api_key = self._resolve_api_key()
        url = f"{self._api_url}/direct"
        params = {
            "q": query,
            "limit": limit,
            "appid": api_key,
        }

        response = await self._get(url, params)
        raw_locations: list = response.json() if isinstance(response.json(), list) else []
        return [self._map_location(item) for item in raw_locations]

    async def _fetch_reverse_geocode(self, lat: float, lon: float) -> List[Location]:
        """Single attempt: reverse-geocode *(lat, lon)* to Locations."""
        api_key = self._resolve_api_key()
        url = f"{self._api_url}/reverse"
        params = {
            "lat": lat,
            "lon": lon,
            "limit": 5,  # fetch a few so the 50 km filter has candidates
            "appid": api_key,
        }

        response = await self._get(url, params)
        raw_locations: list = response.json() if isinstance(response.json(), list) else []
        return [self._map_location(item) for item in raw_locations]

    async def _get(self, url: str, params: dict) -> httpx.Response:
        """Perform a GET request, translating httpx exceptions to domain errors."""
        try:
            async with self._make_client() as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Geocoding API request timed out after {self._timeout}s"
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalServiceError(
                f"Geocoding API request failed: {exc}"
            ) from exc

        if response.status_code != 200:
            raise ExternalServiceError(
                f"Geocoding API returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response

    # ------------------------------------------------------------------
    # HTTP client factory (overridable in tests)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _make_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield an httpx.AsyncClient.  Override in tests to inject a mock."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            yield client

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _map_location(data: dict) -> Location:
        """Map a single OpenWeatherMap Geocoding API entry to a Location.

        The OWM Geocoding API returns objects like::

            {
              "name": "London",
              "local_names": { ... },
              "lat": 51.5074,
              "lon": -0.1278,
              "country": "GB",
              "state": "England"
            }

        We derive a stable ``id`` from ``"{name}-{country}"`` (lower-cased,
        spaces replaced with hyphens).
        """
        name: str = data.get("name", "")
        country: str = data.get("country", "")
        state: str = data.get("state", "")
        lat: float = float(data.get("lat", 0.0))
        lon: float = float(data.get("lon", 0.0))

        location_id = f"{name}-{country}".lower().replace(" ", "-")

        return Location(
            id=location_id,
            name=name,
            region=state,
            country=country,
            latitude=lat,
            longitude=lon,
            timezone=data.get("timezone", ""),
        )

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    async def _with_retry(self, operation, *args):
        """Execute *operation* with exponential-backoff retry.

        Retries on TimeoutError and ExternalServiceError up to
        ``_max_retries`` times.  Raises the last exception once exhausted.
        """
        last_exc: Exception = ExternalServiceError("No attempts made")

        for attempt in range(self._max_retries):
            try:
                return await operation(*args)
            except (TimeoutError, ExternalServiceError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.warning(
                        "Geocoding API attempt %d/%d failed (%s). Retrying in %.1fs.",
                        attempt + 1,
                        self._max_retries,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise last_exc

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_query(query: str) -> None:
        """Raise ValidationError if *query* has fewer than 2 non-whitespace chars.

        No network call is made in this case (Requirements 1.2, Property 9).
        """
        non_ws_chars = [c for c in query if not c.isspace()]
        if len(non_ws_chars) < 2:
            raise ValidationError(
                "Location search query must contain at least 2 non-whitespace "
                f"characters; received: {query!r}"
            )

    # ------------------------------------------------------------------
    # API key helpers
    # ------------------------------------------------------------------

    def _resolve_api_key(self) -> str:
        """Return the API key, preferring the injected value over the env var."""
        if self._api_key:
            return self._api_key
        return self._get_api_key()

    @staticmethod
    def _get_api_key() -> str:
        """Return the API key from the environment.

        Resolution order:
        1. GEOCODING_API_KEY environment variable.
        2. WEATHER_API_KEY environment variable (shared key for OWM endpoints).

        Raises:
            ExternalServiceError: If no key is configured.
        """
        key = os.environ.get("GEOCODING_API_KEY") or os.environ.get("WEATHER_API_KEY", "")
        if not key:
            raise ExternalServiceError(
                "Geocoding API key not configured. "
                "Set GEOCODING_API_KEY (or WEATHER_API_KEY) environment variable."
            )
        return key


# ---------------------------------------------------------------------------
# Utility: Haversine distance
# ---------------------------------------------------------------------------


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in kilometres between two points.

    Uses the Haversine formula.
    """
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_KM * c
