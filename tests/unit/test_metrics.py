"""
Unit tests for src/monitoring/metrics.py (CustomMetricsEmitter).

All tests use unittest.mock to patch boto3 so no real AWS calls are made.
Tests verify:
  - Correct metric names are used
  - Correct namespace ("WeatherApp")
  - Correct units and dimension keys/values
  - Emitter is a no-op when disabled
  - boto3 errors are swallowed (fire-and-forget)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.monitoring.metrics import (
    METRIC_API_LATENCY,
    METRIC_CACHE_HIT,
    METRIC_CACHE_MISS,
    METRIC_EXTERNAL_API,
    METRIC_FAVORITE_OP,
    METRIC_LOCATION_SEARCH,
    CustomMetricsEmitter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_emitter() -> tuple[CustomMetricsEmitter, MagicMock]:
    """Return an emitter with a mocked CloudWatch client."""
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client):
        emitter = CustomMetricsEmitter(enabled=True)
    return emitter, mock_client


def _last_metric(mock_client: MagicMock) -> dict:
    """Return the single MetricData item from the last put_metric_data call."""
    args, kwargs = mock_client.put_metric_data.call_args
    data = kwargs.get("MetricData") or args[1] if args else kwargs["MetricData"]
    return data[0]


# ---------------------------------------------------------------------------
# Namespace / disabled tests
# ---------------------------------------------------------------------------

class TestNamespace:
    def test_namespace_constant(self) -> None:
        assert CustomMetricsEmitter.NAMESPACE == "WeatherApp"

    def test_put_uses_correct_namespace(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_cache_hit()
        _, kwargs = mock_client.put_metric_data.call_args
        assert kwargs["Namespace"] == "WeatherApp"


class TestDisabled:
    def test_disabled_emitter_never_calls_boto3(self) -> None:
        with patch("boto3.client") as patched:
            emitter = CustomMetricsEmitter(enabled=False)
            emitter.emit_cache_hit()
            emitter.emit_cache_miss()
            emitter.emit_api_latency("/weather/current", 123.4)
            emitter.emit_external_api_call("openweather", True)
            emitter.emit_favorite_operation("add")
            emitter.emit_location_search(True)
        patched.assert_not_called()
        assert emitter._client is None


# ---------------------------------------------------------------------------
# Cache hit / miss
# ---------------------------------------------------------------------------

class TestCacheMetrics:
    def test_cache_hit_default_data_type(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_cache_hit()
        metric = _last_metric(mock_client)
        assert metric["MetricName"] == METRIC_CACHE_HIT
        assert metric["Unit"] == "Count"
        assert metric["Value"] == 1
        assert {"Name": "data_type", "Value": "current"} in metric["Dimensions"]

    def test_cache_hit_forecast_data_type(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_cache_hit("forecast")
        metric = _last_metric(mock_client)
        assert metric["MetricName"] == METRIC_CACHE_HIT
        assert {"Name": "data_type", "Value": "forecast"} in metric["Dimensions"]

    def test_cache_miss_default_data_type(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_cache_miss()
        metric = _last_metric(mock_client)
        assert metric["MetricName"] == METRIC_CACHE_MISS
        assert metric["Unit"] == "Count"
        assert {"Name": "data_type", "Value": "current"} in metric["Dimensions"]

    def test_cache_miss_forecast_data_type(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_cache_miss("forecast")
        metric = _last_metric(mock_client)
        assert {"Name": "data_type", "Value": "forecast"} in metric["Dimensions"]


# ---------------------------------------------------------------------------
# Location search
# ---------------------------------------------------------------------------

class TestLocationSearch:
    def test_has_results(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_location_search(has_results=True)
        metric = _last_metric(mock_client)
        assert metric["MetricName"] == METRIC_LOCATION_SEARCH
        assert {"Name": "result_count", "Value": "has_results"} in metric["Dimensions"]

    def test_empty_results(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_location_search(has_results=False)
        metric = _last_metric(mock_client)
        assert metric["MetricName"] == METRIC_LOCATION_SEARCH
        assert {"Name": "result_count", "Value": "empty"} in metric["Dimensions"]


# ---------------------------------------------------------------------------
# API latency
# ---------------------------------------------------------------------------

class TestApiLatency:
    def test_metric_name_and_unit(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_api_latency("/weather/current", 245.7)
        metric = _last_metric(mock_client)
        assert metric["MetricName"] == METRIC_API_LATENCY
        assert metric["Unit"] == "Milliseconds"
        assert metric["Value"] == pytest.approx(245.7)

    def test_endpoint_dimension(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_api_latency("/favorites", 50.0)
        metric = _last_metric(mock_client)
        assert {"Name": "endpoint", "Value": "/favorites"} in metric["Dimensions"]

    def test_zero_latency_is_valid(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_api_latency("/preferences/units", 0.0)
        metric = _last_metric(mock_client)
        assert metric["Value"] == 0.0


# ---------------------------------------------------------------------------
# External API calls
# ---------------------------------------------------------------------------

class TestExternalApiCall:
    def test_success_status(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_external_api_call("openweather", success=True)
        metric = _last_metric(mock_client)
        assert metric["MetricName"] == METRIC_EXTERNAL_API
        assert {"Name": "provider", "Value": "openweather"} in metric["Dimensions"]
        assert {"Name": "status", "Value": "success"} in metric["Dimensions"]

    def test_error_status(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_external_api_call("weatherapi", success=False)
        metric = _last_metric(mock_client)
        assert {"Name": "status", "Value": "error"} in metric["Dimensions"]
        assert {"Name": "provider", "Value": "weatherapi"} in metric["Dimensions"]

    def test_count_unit(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_external_api_call("openweather", success=True)
        metric = _last_metric(mock_client)
        assert metric["Unit"] == "Count"
        assert metric["Value"] == 1


# ---------------------------------------------------------------------------
# Favorite operations
# ---------------------------------------------------------------------------

class TestFavoriteOperation:
    @pytest.mark.parametrize("operation", ["add", "remove", "list"])
    def test_operation_dimension(self, operation: str) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_favorite_operation(operation)
        metric = _last_metric(mock_client)
        assert metric["MetricName"] == METRIC_FAVORITE_OP
        assert {"Name": "operation", "Value": operation} in metric["Dimensions"]

    def test_count_unit(self) -> None:
        emitter, mock_client = _make_emitter()
        emitter.emit_favorite_operation("add")
        metric = _last_metric(mock_client)
        assert metric["Unit"] == "Count"


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------

class TestErrorResilience:
    def test_boto3_error_is_swallowed(self) -> None:
        """A CloudWatch PutMetricData failure must never raise to the caller."""
        emitter, mock_client = _make_emitter()
        mock_client.put_metric_data.side_effect = Exception("network error")

        # Should not raise
        emitter.emit_cache_hit()
        emitter.emit_api_latency("/weather/current", 100.0)

    def test_multiple_metrics_independent(self) -> None:
        """Each emit call triggers exactly one put_metric_data invocation."""
        emitter, mock_client = _make_emitter()
        emitter.emit_cache_hit()
        emitter.emit_cache_miss()
        emitter.emit_api_latency("/test", 10.0)
        assert mock_client.put_metric_data.call_count == 3
