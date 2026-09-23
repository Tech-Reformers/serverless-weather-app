"""
Domain layer for the Serverless Weather App.

This package exposes the core domain models, service port interfaces
(hexagonal-architecture ports), and the application's exception hierarchy.
Nothing in this package depends on AWS services, external APIs, or any
infrastructure concern — it is pure Python business logic.

Public API
----------
Models:
    TempUnit, Location, CurrentConditions, HourlyForecast, DailyForecast,
    FavoriteLocation, DeviceLocationData

Service ports (abstract interfaces):
    LocationServicePort, WeatherDataServicePort, ForecastServicePort,
    FavoritesServicePort, UnitConversionServicePort

Exceptions:
    WeatherAppError, ValidationError, ExternalServiceError, CacheError,
    DatabaseError, FavoriteLimitExceededError, DuplicateFavoriteError,
    TimeoutError
"""

# --- Models ------------------------------------------------------------------
from .models import (
    CurrentConditions,
    DailyForecast,
    DeviceLocationData,
    FavoriteLocation,
    HourlyForecast,
    Location,
    TempUnit,
)

# --- Service ports -----------------------------------------------------------
from .services import (
    FavoritesServicePort,
    ForecastServicePort,
    LocationServicePort,
    UnitConversionServicePort,
    WeatherDataServicePort,
)

# --- Exceptions --------------------------------------------------------------
from .exceptions import (
    CacheError,
    DatabaseError,
    DuplicateFavoriteError,
    ExternalServiceError,
    FavoriteLimitExceededError,
    TimeoutError,
    ValidationError,
    WeatherAppError,
)

__all__ = [
    # Models
    "TempUnit",
    "Location",
    "CurrentConditions",
    "HourlyForecast",
    "DailyForecast",
    "FavoriteLocation",
    "DeviceLocationData",
    # Service ports
    "LocationServicePort",
    "WeatherDataServicePort",
    "ForecastServicePort",
    "FavoritesServicePort",
    "UnitConversionServicePort",
    # Exceptions
    "WeatherAppError",
    "ValidationError",
    "ExternalServiceError",
    "CacheError",
    "DatabaseError",
    "FavoriteLimitExceededError",
    "DuplicateFavoriteError",
    "TimeoutError",
]
