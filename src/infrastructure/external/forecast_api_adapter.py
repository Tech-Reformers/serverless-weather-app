"""
ForecastAPIAdapter — infrastructure adapter for weather forecast data.

Implements ForecastServicePort using the OpenWeatherMap 5-day/3-hour
forecast endpoint (/forecast), mapping the response to HourlyForecast and
DailyForecast domain objects.

Endpoint used: GET /forecast?lat={lat}&lon={lon}&appid={key}&units=metric&cnt=40
  - Returns up to 40 entries spaced 3 hours apart (= 5 days of data)
  - We take the first 24 entries for hourly (24 * 3h = 72h window) and
    group by calendar day for the 7-day daily summary.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, List, Optional

import httpx

from src.domain.exceptions import ExternalServiceError, TimeoutError
from src.domain.models import DailyForecast, HourlyForecast, Location
from src.domain.services import ForecastServicePort

logger = logging.getLogger(__name__)

_DEFAULT_WEATHER_API_URL = "https://api.openweathermap.org/data/2.5"
_TIMEOUT_SECONDS = 10.0
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0


class ForecastAPIAdapter(ForecastServicePort):
    """Adapter that calls the OpenWeatherMap /forecast endpoint."""

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
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

    async def get_hourly_forecast(
        self, location: Location, hours: int = 24
    ) -> List[HourlyForecast]:
        entries = await self._with_retry(self._fetch_forecast, location)
        return entries[:hours]

    async def get_daily_forecast(
        self, location: Location, days: int = 7
    ) -> List[DailyForecast]:
        entries = await self._with_retry(self._fetch_forecast, location)
        return self._aggregate_daily(entries)[:days]

    async def _fetch_forecast(self, location: Location) -> List[HourlyForecast]:
        """Fetch all forecast entries from the OWM /forecast endpoint."""
        api_key = self._resolve_api_key()
        url = f"{self._api_url}/forecast"
        params = {
            "lat": location.latitude,
            "lon": location.longitude,
            "appid": api_key,
            "units": "metric",
            "cnt": 40,
        }
        try:
            async with self._make_client() as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"Forecast API timed out for {location.id!r}") from exc
        except httpx.RequestError as exc:
            raise ExternalServiceError(f"Forecast API request failed: {exc}") from exc

        if response.status_code != 200:
            raise ExternalServiceError(
                f"Forecast API returned HTTP {response.status_code}: {response.text[:200]}"
            )

        data = response.json()
        entries = []
        for item in data.get("list", []):
            ts = datetime.fromtimestamp(item["dt"], tz=timezone.utc)
            main = item.get("main", {})
            weather = item.get("weather", [{}])[0]
            wind = item.get("wind", {})
            pop = int(item.get("pop", 0) * 100)
            entries.append(HourlyForecast(
                timestamp=ts,
                temperature=float(main.get("temp", 0.0)),
                feels_like=float(main.get("feels_like", 0.0)),
                humidity=int(main.get("humidity", 0)),
                condition=weather.get("description", ""),
                icon=weather.get("icon", ""),
                precipitation_probability=pop,
                wind_speed=float(wind.get("speed", 0.0)),
            ))
        return entries

    @staticmethod
    def _aggregate_daily(entries: List[HourlyForecast]) -> List[DailyForecast]:
        """Group 3-hourly entries into daily summaries."""
        from collections import defaultdict
        days: dict = defaultdict(list)
        for entry in entries:
            day_key = entry.timestamp.date()
            days[day_key].append(entry)

        result = []
        for day_date in sorted(days.keys()):
            day_entries = days[day_date]
            temps = [e.temperature for e in day_entries]
            pops = [e.precipitation_probability for e in day_entries]
            # Use the midday entry icon if available, else first entry
            midday = next(
                (e for e in day_entries if 11 <= e.timestamp.hour <= 13),
                day_entries[0],
            )
            result.append(DailyForecast(
                date=datetime.combine(day_date, datetime.min.time(), tzinfo=timezone.utc),
                high_temp=max(temps),
                low_temp=min(temps),
                condition=midday.condition,
                icon=midday.icon,
                precipitation_probability=max(pops),
                sunrise=datetime.combine(day_date, datetime.min.time(), tzinfo=timezone.utc),
                sunset=datetime.combine(day_date, datetime.min.time(), tzinfo=timezone.utc),
            ))
        return result

    @asynccontextmanager
    async def _make_client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            yield client

    async def _with_retry(self, operation, *args):
        last_exc: Exception = ExternalServiceError("No attempts made")
        for attempt in range(self._max_retries):
            try:
                return await operation(*args)
            except (TimeoutError, ExternalServiceError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.warning("Forecast API attempt %d/%d failed. Retrying in %.1fs.", attempt + 1, self._max_retries, delay)
                    await asyncio.sleep(delay)
        raise last_exc

    def _resolve_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        key = os.environ.get("WEATHER_API_KEY", "")
        if not key:
            raise ExternalServiceError("Weather API key not configured.")
        return key
