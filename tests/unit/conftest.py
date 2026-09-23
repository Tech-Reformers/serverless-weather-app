"""
Shared pytest fixtures for the unit test suite.

This conftest.py ensures every test in ``tests/unit/`` runs with a
consistent, minimal set of environment variables so that modules that read
from ``os.environ`` at import time or at construction time never raise
``KeyError`` / ``EnvironmentError`` due to missing variables.

The fixtures are session-scoped for efficiency (env vars are cheap to set)
but individual tests can still override specific variables using pytest's
built-in ``monkeypatch`` fixture, which is function-scoped and undoes changes
automatically after each test.

Usage in individual tests
-------------------------
If a test needs a different value for a specific variable:

    def test_custom_limit(monkeypatch):
        monkeypatch.setenv("MAX_FAVORITES", "5")
        reset_settings()
        settings = get_settings()
        assert settings.max_favorites == 5

The ``reset_settings()`` call is needed whenever ``src.config.get_settings``
is used, because it caches a singleton.  Tests that do NOT use
``get_settings`` directly can simply use ``monkeypatch.setenv`` without
resetting the singleton.
"""
from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Default test values
# These mirror .env.example and the CDK stack defaults so tests reflect a
# realistic but safe local configuration.
# ---------------------------------------------------------------------------

_TEST_ENV: dict[str, str] = {
    # DynamoDB table names
    "FAVORITES_TABLE_NAME": "weather-app-favorites-test",
    "CACHE_TABLE_NAME": "weather-app-cache-test",
    "PREFERENCES_TABLE_NAME": "weather-app-preferences-test",
    # Secrets Manager — a fake ARN that won't hit AWS in unit tests
    "WEATHER_API_SECRET_ARN": (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:weather-app/test-keys"
    ),
    # External API endpoints
    "WEATHER_API_URL": "https://api.openweathermap.org/data/2.5",
    "GEOCODING_API_URL": "https://api.openweathermap.org/geo/1.0",
    # Plain-text API keys — dummy values safe for unit tests
    "WEATHER_API_KEY": "test-weather-api-key",
    "GEOCODING_API_KEY": "test-geocoding-api-key",
    # Service configuration
    "MAX_FAVORITES": "50",
    "DEFAULT_TEMP_UNIT": "celsius",
    "EXTERNAL_CALL_TIMEOUT_SECONDS": "10",
    "REFRESH_TIMEOUT_SECONDS": "5",
    "SEARCH_TIMEOUT_SECONDS": "5",
    "DEVICE_LOCATION_TIMEOUT_SECONDS": "30",
    "CACHE_TTL_CURRENT_MINUTES": "15",
    "CACHE_TTL_FORECAST_MINUTES": "60",
    # AWS
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    # Observability
    "LOG_LEVEL": "WARNING",  # quieter output during tests
    "POWERTOOLS_SERVICE_NAME": "weather-app-test",
    "AWS_XRAY_TRACING_NAME": "weather-app-test",
}


@pytest.fixture(autouse=True, scope="session")
def set_test_environment() -> None:
    """Set all required environment variables for the duration of the test session.

    Only variables that are NOT already set in the environment are written,
    so a developer can pre-set values (e.g. in CI) and have them honoured.
    This fixture is autouse=True so it applies to every test without requiring
    an explicit import.
    """
    original: dict[str, str | None] = {}

    for key, value in _TEST_ENV.items():
        original[key] = os.environ.get(key)
        if key not in os.environ:
            os.environ[key] = value

    yield  # type: ignore[misc]

    # Restore the original state after the session
    for key, prev_value in original.items():
        if prev_value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev_value


@pytest.fixture(autouse=True)
def reset_settings_singleton() -> None:
    """Reset the Settings singleton before and after every test.

    This prevents state leakage when one test sets environment variables via
    ``monkeypatch`` and a cached singleton from a previous test would
    otherwise shadow the new values.
    """
    # Import here to avoid circular issues at collection time
    from src.config import reset_settings

    reset_settings()
    yield
    reset_settings()
