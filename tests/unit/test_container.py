"""
Unit tests for ServiceContainer (src/application/container.py).

Tests validate:
  - Singleton behaviour: get_instance() always returns the same object.
  - reset() allows a fresh container to be created.
  - Each @cached_property returns the correct concrete type on first access.
  - The container is importable and get_instance() callable without real AWS
    credentials (lazy initialisation — no boto3 / network calls at import or
    construction time).

All boto3/AWS clients are patched at the lowest practical level so that the
test suite runs offline and without any environment variables.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.application.container import ServiceContainer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Patch targets — we replace boto3.resource and boto3.client with MagicMocks
# so that DynamoDB / Secrets Manager adapters can be constructed without a
# live AWS environment.
_BOTO3_RESOURCE = "boto3.resource"
_BOTO3_CLIENT = "boto3.client"


def _make_container() -> ServiceContainer:
    """Return a fresh, uncached container instance (singleton is reset first)."""
    ServiceContainer.reset()
    return ServiceContainer.get_instance()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure the singleton is always reset before and after each test."""
    ServiceContainer.reset()
    yield
    ServiceContainer.reset()


# ---------------------------------------------------------------------------
# Singleton contract
# ---------------------------------------------------------------------------


def test_get_instance_returns_same_object_on_repeated_calls():
    """get_instance() must return the identical object every time.

    Validates singleton lifecycle across warm Lambda invocations.
    """
    instance_a = ServiceContainer.get_instance()
    instance_b = ServiceContainer.get_instance()
    assert instance_a is instance_b


def test_reset_allows_fresh_container_creation():
    """After reset(), get_instance() must return a different object.

    Validates that test isolation works correctly.
    """
    first = ServiceContainer.get_instance()
    ServiceContainer.reset()
    second = ServiceContainer.get_instance()
    assert first is not second


def test_reset_clears_class_level_instance():
    """After reset(), the internal _instance class variable must be None."""
    ServiceContainer.get_instance()
    assert ServiceContainer._instance is not None

    ServiceContainer.reset()
    assert ServiceContainer._instance is None


def test_get_instance_sets_class_level_instance():
    """Calling get_instance() populates _instance."""
    assert ServiceContainer._instance is None
    container = ServiceContainer.get_instance()
    assert ServiceContainer._instance is container


# ---------------------------------------------------------------------------
# Lazy initialisation — importable without AWS credentials
# ---------------------------------------------------------------------------


def test_get_instance_does_not_call_boto3_at_construction_time():
    """Constructing the container must not trigger any AWS API calls.

    The @cached_property accessors must remain unevaluated until first
    access so that the container is safe to import in offline / test
    environments.
    """
    # If any AWS call were made during get_instance() this would raise
    # inside the patched mock or trigger a real network call.
    with patch(_BOTO3_RESOURCE, return_value=MagicMock()) as mock_resource, \
         patch(_BOTO3_CLIENT, return_value=MagicMock()) as mock_client:
        ServiceContainer.get_instance()
        # Neither boto3.resource nor boto3.client should be called until a
        # cached_property is first accessed.
        mock_resource.assert_not_called()
        mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# cached_property — correct types
# ---------------------------------------------------------------------------


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_cache_strategy_type(_mock_client, _mock_resource):
    """cache_strategy returns a CacheStrategy instance."""
    from src.infrastructure.cache.cache_strategy import CacheStrategy

    container = ServiceContainer.get_instance()
    assert isinstance(container.cache_strategy, CacheStrategy)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_cache_type(_mock_client, _mock_resource):
    """cache returns a DynamoDBWeatherCache instance."""
    from src.infrastructure.cache.dynamodb_cache import DynamoDBWeatherCache

    container = ServiceContainer.get_instance()
    assert isinstance(container.cache, DynamoDBWeatherCache)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_weather_api_adapter_type(_mock_client, _mock_resource):
    """weather_api_adapter returns a WeatherAPIAdapter instance."""
    from src.infrastructure.external.weather_api_adapter import WeatherAPIAdapter

    container = ServiceContainer.get_instance()
    assert isinstance(container.weather_api_adapter, WeatherAPIAdapter)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_geocoding_adapter_type(_mock_client, _mock_resource):
    """geocoding_adapter returns a GeocodingAdapter instance."""
    from src.infrastructure.external.geocoding_adapter import GeocodingAdapter

    container = ServiceContainer.get_instance()
    assert isinstance(container.geocoding_adapter, GeocodingAdapter)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_favorites_port_type(_mock_client, _mock_resource):
    """favorites_port returns a DynamoDBFavoritesService instance."""
    from src.infrastructure.aws.dynamodb_adapter import DynamoDBFavoritesService

    container = ServiceContainer.get_instance()
    assert isinstance(container.favorites_port, DynamoDBFavoritesService)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_unit_port_type(_mock_client, _mock_resource):
    """unit_port returns a DynamoDBUnitService instance."""
    from src.infrastructure.aws.dynamodb_adapter import DynamoDBUnitService

    container = ServiceContainer.get_instance()
    assert isinstance(container.unit_port, DynamoDBUnitService)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_location_service_type(_mock_client, _mock_resource):
    """location_service returns a LocationService instance."""
    from src.application.location_service import LocationService

    container = ServiceContainer.get_instance()
    assert isinstance(container.location_service, LocationService)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_weather_service_type(_mock_client, _mock_resource):
    """weather_service returns a CachedWeatherDataService instance."""
    from src.application.weather_service import CachedWeatherDataService

    container = ServiceContainer.get_instance()
    assert isinstance(container.weather_service, CachedWeatherDataService)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_forecast_service_type(_mock_client, _mock_resource):
    """forecast_service returns a CachedForecastService instance."""
    from src.application.forecast_service import CachedForecastService

    container = ServiceContainer.get_instance()
    assert isinstance(container.forecast_service, CachedForecastService)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_favorites_service_type(_mock_client, _mock_resource):
    """favorites_service returns a FavoritesService instance."""
    from src.application.favorites_service import FavoritesService

    container = ServiceContainer.get_instance()
    assert isinstance(container.favorites_service, FavoritesService)


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_unit_service_type(_mock_client, _mock_resource):
    """unit_service returns a UnitConversionService instance."""
    from src.application.unit_service import UnitConversionService

    container = ServiceContainer.get_instance()
    assert isinstance(container.unit_service, UnitConversionService)


# ---------------------------------------------------------------------------
# Caching — same object returned on repeated property access
# ---------------------------------------------------------------------------


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_cached_property_returns_same_object_on_repeated_access(_mock_client, _mock_resource):
    """Each @cached_property must return the identical object every time.

    This ensures that warm Lambda containers reuse service instances rather
    than constructing new ones per invocation.
    """
    container = ServiceContainer.get_instance()

    # Access each property twice and confirm object identity
    assert container.cache_strategy is container.cache_strategy
    assert container.cache is container.cache
    assert container.weather_api_adapter is container.weather_api_adapter
    assert container.geocoding_adapter is container.geocoding_adapter
    assert container.favorites_port is container.favorites_port
    assert container.unit_port is container.unit_port
    assert container.location_service is container.location_service
    assert container.weather_service is container.weather_service
    assert container.forecast_service is container.forecast_service
    assert container.favorites_service is container.favorites_service
    assert container.unit_service is container.unit_service


# ---------------------------------------------------------------------------
# Wiring correctness — adapters are threaded into services correctly
# ---------------------------------------------------------------------------


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_location_service_receives_geocoding_adapter(_mock_client, _mock_resource):
    """LocationService must be wired with the container's geocoding_adapter."""
    container = ServiceContainer.get_instance()
    assert container.location_service._port is container.geocoding_adapter


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_weather_service_receives_weather_api_adapter(_mock_client, _mock_resource):
    """CachedWeatherDataService must use the container's weather_api_adapter."""
    container = ServiceContainer.get_instance()
    assert container.weather_service._port is container.weather_api_adapter


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_weather_service_receives_shared_cache(_mock_client, _mock_resource):
    """CachedWeatherDataService must share the container's single cache instance."""
    container = ServiceContainer.get_instance()
    assert container.weather_service._cache is container.cache


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_forecast_service_receives_shared_cache(_mock_client, _mock_resource):
    """CachedForecastService must share the container's single cache instance."""
    container = ServiceContainer.get_instance()
    assert container.forecast_service._cache is container.cache


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_favorites_service_receives_favorites_port(_mock_client, _mock_resource):
    """FavoritesService must be wired with the container's favorites_port."""
    container = ServiceContainer.get_instance()
    assert container.favorites_service._favorites is container.favorites_port


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_favorites_service_receives_weather_service(_mock_client, _mock_resource):
    """FavoritesService must use the container's weather_service for temperature enrichment."""
    container = ServiceContainer.get_instance()
    assert container.favorites_service._weather is container.weather_service


@patch(_BOTO3_RESOURCE, return_value=MagicMock())
@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_unit_service_receives_unit_port(_mock_client, _mock_resource):
    """UnitConversionService must be wired with the container's unit_port."""
    container = ServiceContainer.get_instance()
    assert container.unit_service._port is container.unit_port


# ---------------------------------------------------------------------------
# SecretsManagerAdapter — only constructed when accessed
# ---------------------------------------------------------------------------


def test_secrets_adapter_not_constructed_at_get_instance():
    """secrets_adapter must not be constructed during get_instance().

    SecretsManagerAdapter reads WEATHER_API_SECRET_ARN from the environment
    and would raise EnvironmentError if the variable is absent.  The fact
    that we can call get_instance() here without that variable confirms the
    property is truly lazy.
    """
    # No env var set, no patch — should not raise
    container = ServiceContainer.get_instance()
    # The cached_property itself (the descriptor) should not have been
    # evaluated yet.
    assert "secrets_adapter" not in container.__dict__


@patch(_BOTO3_CLIENT, return_value=MagicMock())
def test_secrets_adapter_type_when_arn_provided(mock_client, monkeypatch):
    """secrets_adapter returns SecretsManagerAdapter when ARN is configured."""
    from src.infrastructure.aws.secrets_adapter import SecretsManagerAdapter

    monkeypatch.setenv("WEATHER_API_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:test")

    container = ServiceContainer.get_instance()
    assert isinstance(container.secrets_adapter, SecretsManagerAdapter)
