"""
Dependency injection container for the Serverless Weather App.

Wires the full application service graph so that Lambda handlers and tests
can obtain correctly-configured service objects without knowing how the
individual adapters are constructed.

Design goals
------------
* **Lazy initialisation** — each service is created on first access via
  :func:`functools.cached_property`, so importing the module or calling
  ``ServiceContainer.get_instance()`` never touches AWS APIs.  This makes
  the container importable in unit tests without live credentials.
* **Warm-container reuse** — Lambda invocations that share a process
  (i.e. warm starts) reuse the singleton and the already-constructed
  service objects, which avoids redundant boto3 client creation.
* **Test isolation** — ``ServiceContainer.reset()`` discards the singleton
  so that each test can construct a fresh container or inject its own
  instance.

Wiring order (low → high)
--------------------------
1. Infrastructure adapters with no app-layer dependencies
   ``SecretsManagerAdapter``, ``DynamoDBWeatherCache``, ``CacheStrategy``,
   ``WeatherAPIAdapter``, ``GeocodingAdapter``, ``DynamoDBFavoritesService``,
   ``DynamoDBUnitService``
2. Application services that compose infrastructure adapters
   ``LocationService``, ``CachedWeatherDataService``, ``CachedForecastService``,
   ``FavoritesService``, ``UnitConversionService``

Usage in Lambda handlers
------------------------
::

    from src.application.container import ServiceContainer

    def handler(event, context):
        container = ServiceContainer.get_instance()
        result = await container.location_service.search_locations(query)
        ...
"""
from __future__ import annotations

import os
from functools import cached_property
from typing import Optional

from src.application.favorites_service import FavoritesService
from src.application.forecast_service import CachedForecastService
from src.application.location_service import LocationService
from src.application.unit_service import UnitConversionService
from src.application.weather_service import CachedWeatherDataService
from src.infrastructure.aws.dynamodb_adapter import (
    DynamoDBFavoritesService,
    DynamoDBUnitService,
)
from src.infrastructure.aws.secrets_adapter import SecretsManagerAdapter
from src.infrastructure.cache.cache_strategy import CacheStrategy
from src.infrastructure.cache.dynamodb_cache import DynamoDBWeatherCache
from src.infrastructure.external.geocoding_adapter import GeocodingAdapter
from src.infrastructure.external.weather_api_adapter import WeatherAPIAdapter
from src.infrastructure.external.forecast_api_adapter import ForecastAPIAdapter


class ServiceContainer:
    """Singleton container that creates and caches all application services.

    Each service is created lazily on first access and cached for Lambda
    warm-container reuse.

    Usage in Lambda handlers::

        container = ServiceContainer.get_instance()
        location_service = container.location_service
    """

    _instance: Optional["ServiceContainer"] = None

    @classmethod
    def get_instance(cls) -> "ServiceContainer":
        """Return the process-wide singleton, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Discard the singleton — for testing only.

        Calling this in production code will cause the next
        ``get_instance()`` call to rebuild the entire service graph,
        including fresh boto3 clients.
        """
        cls._instance = None

    # ------------------------------------------------------------------
    # Infrastructure layer — adapters with no app-layer dependencies
    # ------------------------------------------------------------------

    @cached_property
    def secrets_adapter(self) -> SecretsManagerAdapter:
        """AWS Secrets Manager adapter for API key retrieval.

        Reads the secret ARN from the ``WEATHER_API_SECRET_ARN`` environment
        variable.  If the variable is absent the adapter raises
        ``EnvironmentError`` on first key access (not on construction),
        keeping the container importable in environments without AWS creds.
        """
        return SecretsManagerAdapter(
            secret_arn=os.environ.get("WEATHER_API_SECRET_ARN"),
        )

    @cached_property
    def cache(self) -> DynamoDBWeatherCache:
        """DynamoDB-backed weather data cache.

        Table name is resolved from the ``CACHE_TABLE_NAME`` environment
        variable (default: ``"weather-app-cache"``).
        """
        return DynamoDBWeatherCache(
            table_name=os.environ.get("CACHE_TABLE_NAME"),
        )

    @cached_property
    def cache_strategy(self) -> CacheStrategy:
        """Stateless helper that provides TTL constants and cache-key logic."""
        return CacheStrategy()

    @cached_property
    def weather_api_adapter(self) -> WeatherAPIAdapter:
        """HTTP adapter for the external weather data provider.

        API key is fetched from Secrets Manager when ``WEATHER_API_SECRET_ARN``
        is set (production/Lambda), falling back to the ``WEATHER_API_KEY``
        env var for local development.
        """
        api_key = os.environ.get("WEATHER_API_KEY")
        if not api_key and os.environ.get("WEATHER_API_SECRET_ARN"):
            api_key = self.secrets_adapter.get_openweather_key()
        return WeatherAPIAdapter(
            api_url=os.environ.get("WEATHER_API_URL"),
            api_key=api_key,
        )

    @cached_property
    def forecast_api_adapter(self) -> ForecastAPIAdapter:
        """HTTP adapter for the OpenWeatherMap /forecast endpoint.

        API key is fetched from Secrets Manager when WEATHER_API_SECRET_ARN
        is set, falling back to WEATHER_API_KEY env var for local dev.
        """
        api_key = os.environ.get("WEATHER_API_KEY")
        if not api_key and os.environ.get("WEATHER_API_SECRET_ARN"):
            api_key = self.secrets_adapter.get_openweather_key()
        return ForecastAPIAdapter(
            api_url=os.environ.get("WEATHER_API_URL"),
            api_key=api_key,
        )

    @cached_property
    def geocoding_adapter(self) -> GeocodingAdapter:
        """HTTP adapter for the external geocoding provider.

        API key is fetched from Secrets Manager when ``WEATHER_API_SECRET_ARN``
        is set (production/Lambda), falling back to env vars for local dev.
        OpenWeatherMap uses the same key for geocoding and weather data.
        """
        api_key = os.environ.get("GEOCODING_API_KEY") or os.environ.get("WEATHER_API_KEY")
        if not api_key and os.environ.get("WEATHER_API_SECRET_ARN"):
            api_key = self.secrets_adapter.get_geocoding_key()
        return GeocodingAdapter(
            api_url=os.environ.get("GEOCODING_API_URL"),
            api_key=api_key,
        )

    @cached_property
    def favorites_port(self) -> DynamoDBFavoritesService:
        """DynamoDB adapter for the favourites persistence port.

        Table name is resolved from ``FAVORITES_TABLE_NAME``
        (default: ``"weather-app-favorites"``).
        """
        return DynamoDBFavoritesService(
            table_name=os.environ.get("FAVORITES_TABLE_NAME"),
        )

    @cached_property
    def unit_port(self) -> DynamoDBUnitService:
        """DynamoDB adapter for the unit-conversion (preferences) port.

        Table name is resolved from ``PREFERENCES_TABLE_NAME``
        (default: ``"weather-app-preferences"``).
        """
        return DynamoDBUnitService(
            table_name=os.environ.get("PREFERENCES_TABLE_NAME"),
        )

    # ------------------------------------------------------------------
    # Application layer — services that compose infrastructure adapters
    # ------------------------------------------------------------------

    @cached_property
    def location_service(self) -> LocationService:
        """Application service for location search and device location.

        Wraps ``GeocodingAdapter`` with validation, result-capping, and
        timeout enforcement (Requirements 1.1, 1.2, 1.4, 1.5, 4.1–4.4).
        """
        return LocationService(location_port=self.geocoding_adapter)

    @cached_property
    def weather_service(self) -> CachedWeatherDataService:
        """Application service for current weather conditions with caching.

        Composes ``WeatherAPIAdapter`` (live fetch) with
        ``DynamoDBWeatherCache`` (15-minute TTL) and ``CacheStrategy``
        (Requirements 2.1–2.8, 7.1–7.4).
        """
        return CachedWeatherDataService(
            weather_port=self.weather_api_adapter,
            cache=self.cache,
            cache_strategy=self.cache_strategy,
        )

    @cached_property
    def forecast_service(self) -> CachedForecastService:
        """Application service for hourly/daily forecasts with caching.

        Composes ``WeatherAPIAdapter`` (which also implements
        ``ForecastServicePort`` via ``WeatherAPIAdapter``) with the shared
        ``DynamoDBWeatherCache`` and ``CacheStrategy`` (Requirements 3.1–3.6).

        Note: ``WeatherAPIAdapter`` currently exposes only
        ``get_current_conditions`` / ``refresh_current_conditions``.  Until
        a dedicated ``ForecastAPIAdapter`` is added the forecast port is
        wired to the same weather API adapter, which satisfies the interface
        contract because ``CachedForecastService`` only calls the methods
        defined on ``ForecastServicePort``.
        """
        return CachedForecastService(
            forecast_port=self.forecast_api_adapter,
            cache=self.cache,
            cache_strategy=self.cache_strategy,
        )

    @cached_property
    def favorites_service(self) -> FavoritesService:
        """Application service for favourites management with temperature enrichment.

        Composes ``DynamoDBFavoritesService`` (persistence) with
        ``CachedWeatherDataService`` (temperature fetches) so that
        ``list_favorites`` enriches each entry concurrently with a
        5-second per-location timeout (Requirements 5.1–5.8, Properties
        1, 14, 18, 23).
        """
        return FavoritesService(
            favorites_port=self.favorites_port,
            weather_port=self.weather_service,
        )

    @cached_property
    def unit_service(self) -> UnitConversionService:
        """Application service for temperature unit conversion and preference persistence.

        Wraps ``DynamoDBUnitService`` with strict unit validation and
        in-process, no-I/O temperature conversion (Requirements 6.1–6.7,
        Properties 2, 10, 19).
        """
        return UnitConversionService(unit_port=self.unit_port)
