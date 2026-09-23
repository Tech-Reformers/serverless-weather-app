"""
Application configuration via environment variables.

Usage
-----
Import :func:`get_settings` anywhere in the application to obtain the
global :class:`Settings` singleton.  The singleton is initialised lazily on
first access and reused across all subsequent calls — ideal for warm Lambda
invocations.

    from src.config import get_settings

    settings = get_settings()
    table_name = settings.favorites_table_name

Local development
-----------------
Copy ``.env.example`` to ``.env`` and populate the values, then load the file
before importing this module (e.g. via ``python-dotenv`` or your shell):

    export $(grep -v '^#' .env | xargs)

Environment variable reference
-------------------------------
See ``.env.example`` at the project root for the full list of recognised
variables and their default values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of all runtime configuration values.

    Attributes are read from environment variables once at construction time.
    Use :func:`get_settings` to obtain the shared singleton rather than
    constructing this class directly.

    DynamoDB
    --------
    favorites_table_name    : Table that stores user favourite locations.
    cache_table_name        : Table used as the shared weather-data cache.
    preferences_table_name  : Table that stores per-user unit preferences.

    Secrets Manager
    ---------------
    weather_api_secret_arn  : ARN of the secret holding external API keys.
                              Empty string when running fully offline / in
                              local development with plain-text key variables.

    External API
    ------------
    weather_api_url         : Base URL for the weather data provider.
    geocoding_api_url       : Base URL for the geocoding provider.
    weather_api_key         : Plain-text API key (local dev only).
    geocoding_api_key       : Plain-text geocoding key (local dev only).

    Service limits and timeouts
    ---------------------------
    max_favorites                   : Hard cap on favourites per user (50).
    default_temp_unit               : Fallback temperature unit ("celsius").
    external_call_timeout_seconds   : Timeout for outbound HTTP calls (10 s).
    refresh_timeout_seconds         : Timeout for forced weather refresh (5 s).
    search_timeout_seconds          : Timeout for location search (5 s).
    device_location_timeout_seconds : Timeout for device location (30 s).
    cache_ttl_current_minutes       : TTL for current-conditions cache (15 min).
    cache_ttl_forecast_minutes      : TTL for forecast cache (60 min).

    AWS / observability
    -------------------
    aws_region              : AWS region (set automatically inside Lambda).
    log_level               : Python logging level string ("INFO").
    powertools_service_name : AWS Lambda Powertools service name.
    xray_tracing_name       : X-Ray segment/trace name.
    """

    # -- DynamoDB ----------------------------------------------------------
    favorites_table_name: str
    cache_table_name: str
    preferences_table_name: str

    # -- Secrets Manager ---------------------------------------------------
    weather_api_secret_arn: str

    # -- External APIs -----------------------------------------------------
    weather_api_url: str
    geocoding_api_url: str
    weather_api_key: str
    geocoding_api_key: str

    # -- Service configuration ---------------------------------------------
    max_favorites: int
    default_temp_unit: str
    external_call_timeout_seconds: float
    refresh_timeout_seconds: float
    search_timeout_seconds: float
    device_location_timeout_seconds: float
    cache_ttl_current_minutes: int
    cache_ttl_forecast_minutes: int

    # -- AWS / observability -----------------------------------------------
    aws_region: str
    log_level: str
    powertools_service_name: str
    xray_tracing_name: str

    # ------------------------------------------------------------------ #
    # Factory                                                              #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_env(cls) -> "Settings":
        """Construct a :class:`Settings` instance from the current environment.

        All values have sensible defaults so the application starts without
        requiring every variable to be present (useful for unit tests and
        local development).  The one exception is ``weather_api_secret_arn``:
        it defaults to an empty string, and the :class:`SecretsManagerAdapter`
        will raise an ``EnvironmentError`` at runtime if it is empty *and*
        no override is passed directly.
        """
        return cls(
            # DynamoDB
            favorites_table_name=os.environ.get(
                "FAVORITES_TABLE_NAME", "weather-app-favorites"
            ),
            cache_table_name=os.environ.get(
                "CACHE_TABLE_NAME", "weather-app-cache"
            ),
            preferences_table_name=os.environ.get(
                "PREFERENCES_TABLE_NAME", "weather-app-preferences"
            ),
            # Secrets Manager
            weather_api_secret_arn=os.environ.get("WEATHER_API_SECRET_ARN", ""),
            # External APIs
            weather_api_url=os.environ.get(
                "WEATHER_API_URL", "https://api.openweathermap.org/data/2.5"
            ),
            geocoding_api_url=os.environ.get(
                "GEOCODING_API_URL", "https://api.openweathermap.org/geo/1.0"
            ),
            weather_api_key=os.environ.get("WEATHER_API_KEY", ""),
            geocoding_api_key=os.environ.get(
                "GEOCODING_API_KEY",
                os.environ.get("WEATHER_API_KEY", ""),  # fall back to WEATHER_API_KEY
            ),
            # Service limits / timeouts
            max_favorites=int(os.environ.get("MAX_FAVORITES", "50")),
            default_temp_unit=os.environ.get("DEFAULT_TEMP_UNIT", "celsius"),
            external_call_timeout_seconds=float(
                os.environ.get("EXTERNAL_CALL_TIMEOUT_SECONDS", "10")
            ),
            refresh_timeout_seconds=float(
                os.environ.get("REFRESH_TIMEOUT_SECONDS", "5")
            ),
            search_timeout_seconds=float(
                os.environ.get("SEARCH_TIMEOUT_SECONDS", "5")
            ),
            device_location_timeout_seconds=float(
                os.environ.get("DEVICE_LOCATION_TIMEOUT_SECONDS", "30")
            ),
            cache_ttl_current_minutes=int(
                os.environ.get("CACHE_TTL_CURRENT_MINUTES", "15")
            ),
            cache_ttl_forecast_minutes=int(
                os.environ.get("CACHE_TTL_FORECAST_MINUTES", "60")
            ),
            # AWS / observability
            aws_region=os.environ.get(
                "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            powertools_service_name=os.environ.get(
                "POWERTOOLS_SERVICE_NAME", "weather-app"
            ),
            xray_tracing_name=os.environ.get(
                "AWS_XRAY_TRACING_NAME", "weather-app"
            ),
        )


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Return the global :class:`Settings` singleton (lazy initialisation).

    The singleton is created on first call and reused on all subsequent
    calls, which means environment variables are read exactly once per
    Lambda cold-start — subsequent warm invocations pay no overhead.

    Call :func:`reset_settings` in tests to force re-initialisation with
    a different environment.
    """
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """Discard the cached singleton so the next :func:`get_settings` call
    re-reads the environment.

    Intended for use in tests that need to change environment variables
    between test cases:

        monkeypatch.setenv("MAX_FAVORITES", "5")
        reset_settings()
        assert get_settings().max_favorites == 5
    """
    global _settings
    _settings = None
