"""
WeatherAPIAdapter — infrastructure adapter for the external weather data provider.

Implements WeatherDataServicePort using httpx for async HTTP calls with:
  - 10-second per-request timeout (Requirements 2.6, 3.5)
  - Exponential-backoff retry (max 3 attempts, 1 s base delay) for transient
    failures (Requirements 2.7, 3.6)
  - API key loaded from the WEATHER_API_KEY environment variable; the helper
    can be swapped for a SecretsManager lookup without touching the adapter
    logic.

The base URL of the provider is read from WEATHER_API_URL (default points at
the OpenWeatherMap "current weather" endpoint pattern so that real keys work
with no extra config).  Mock/stub URLs can be injected via that variable in
tests.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import httpx

from src.domain.exceptions import ExternalServiceError, TimeoutError
from src.domain.models import CurrentConditions, Location
from src.domain.services import WeatherDataServicePort

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

_DEFAULT_WEATHER_API_URL = "https://api.openweathermap.org/data/2.5"
_TIMEOUT_SECONDS = 10.0
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0  # seconds


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------


class WeatherAPIAdapter(WeatherDataServicePort):
    """Adapter that calls an external weather provider to fulfil
    WeatherDataServicePort.

    Args:
        api_url: Base URL of the weather REST API.  Defaults to the
            WEATHER_API_URL environment variable (or the OpenWeatherMap
            endpoint if the variable is unset).
        api_key: API key for the provider.  When supplied it takes priority
            over the environment variable, making the adapter fully injectable
            for testing.
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
            api_url or os.environ.get("WEATHER_API_URL", _DEFAULT_WEATHER_API_URL)
        ).rstrip("/")
        self._api_key = api_key  # None → resolved lazily via _resolve_api_key()
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

    # ------------------------------------------------------------------
    # WeatherDataServicePort implementation
    # ------------------------------------------------------------------

    async def get_current_conditions(
        self,
        location: Location,
        force_refresh: bool = False,  # noqa: ARG002  (cache handled at a higher layer)
    ) -> CurrentConditions:
        """Retrieve current conditions from the external provider.

        Retry logic is applied transparently.  ``force_refresh`` is accepted
        for interface compliance; cache bypassing is the responsibility of the
        caching layer that wraps this adapter.

        Raises:
            TimeoutError: If no response is received within 10 seconds after
                all retry attempts are exhausted.
            ExternalServiceError: If the provider returns a non-2xx response
                after all retries.
        """
        return await self._with_retry(self._fetch_current_conditions, location)

    async def refresh_current_conditions(self, location: Location) -> CurrentConditions:
        """Force-refresh current conditions (bypasses any caching layer).

        Delegates to :meth:`get_current_conditions` with ``force_refresh=True``.

        Raises:
            TimeoutError: On request timeout after retries.
            ExternalServiceError: On provider error after retries.
        """
        return await self.get_current_conditions(location, force_refresh=True)

    # ------------------------------------------------------------------
    # Internal fetch helpers (single attempt, no retry)
    # ------------------------------------------------------------------

    async def _fetch_current_conditions(self, location: Location) -> CurrentConditions:
        """Single attempt to fetch current conditions for *location*."""
        api_key = self._resolve_api_key()
        url = f"{self._api_url}/weather"
        params = {
            "lat": location.latitude,
            "lon": location.longitude,
            "appid": api_key,
            "units": "metric",  # always fetch in °C; callers convert as needed
        }

        try:
            async with self._make_client() as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Weather API request timed out after {self._timeout}s for "
                f"location {location.id!r}"
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalServiceError(
                f"Weather API request failed for location {location.id!r}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise ExternalServiceError(
                f"Weather API returned HTTP {response.status_code} for location "
                f"{location.id!r}: {response.text[:200]}"
            )

        return self._map_current_conditions(response.json())

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
    def _map_current_conditions(data: dict) -> CurrentConditions:
        """Map a raw OpenWeatherMap /weather response to CurrentConditions."""
        main = data.get("main", {})
        wind = data.get("wind", {})
        weather_list = data.get("weather", [{}])
        weather = weather_list[0] if weather_list else {}
        sys = data.get("sys", {})

        timestamp = datetime.fromtimestamp(data.get("dt", 0), tz=timezone.utc)
        sunrise_ts = sys.get("sunrise")
        sunset_ts = sys.get("sunset")

        return CurrentConditions(
            temperature=float(main.get("temp", 0.0)),
            feels_like=float(main.get("feels_like", 0.0)),
            humidity=int(main.get("humidity", 0)),
            pressure=int(main.get("pressure", 0)),
            wind_speed=float(wind.get("speed", 0.0)),
            wind_direction=int(wind.get("deg", 0)),
            condition=weather.get("description", ""),
            icon=weather.get("icon", ""),
            timestamp=timestamp,
            sunrise=datetime.fromtimestamp(sunrise_ts, tz=timezone.utc) if sunrise_ts else None,
            sunset=datetime.fromtimestamp(sunset_ts, tz=timezone.utc) if sunset_ts else None,
            visibility=int(data.get("visibility", 0)),
        )

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    async def _with_retry(self, operation, *args) -> CurrentConditions:
        """Execute *operation* with exponential-backoff retry.

        Retries on TimeoutError and ExternalServiceError up to
        ``_max_retries`` times.  The final exception is re-raised once
        all attempts are exhausted.
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
                        "Weather API attempt %d/%d failed (%s). Retrying in %.1fs.",
                        attempt + 1,
                        self._max_retries,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise last_exc

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
        """Return the API key from the WEATHER_API_KEY environment variable.

        Extendable: swap this method's body with an async call to a
        SecretsAdapter when running on Lambda.

        Raises:
            ExternalServiceError: If no key is configured.
        """
        key = os.environ.get("WEATHER_API_KEY", "")
        if not key:
            raise ExternalServiceError(
                "Weather API key not configured. "
                "Set the WEATHER_API_KEY environment variable."
            )
        return key
