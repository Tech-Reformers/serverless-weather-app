"""
CustomMetricsEmitter — emits business-level CloudWatch metrics from Lambda.

Uses boto3 PutMetricData to push metrics to the "WeatherApp" namespace.
The emitter is disabled automatically in test environments (pass enabled=False)
so that no real AWS calls are made during unit tests.

Metrics emitted
───────────────
  WeatherCacheHit        Count   dimension: data_type (current|forecast)
  WeatherCacheMiss       Count   dimension: data_type (current|forecast)
  LocationSearchCount    Count   dimension: result_count (empty|has_results)
  FavoriteOperationCount Count   dimension: operation   (add|remove|list)
  ApiLatency             Milliseconds  dimension: endpoint
  ExternalApiCallCount   Count   dimension: provider, status (success|error)

Design reference: .kiro/specs/weather-app/design.md
Validates: Requirements 8.5 (CloudWatch Logging Completeness)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

import boto3

logger = logging.getLogger(__name__)

# Metric names — kept as constants so callers can reference them without
# relying on magic strings.
METRIC_CACHE_HIT = "WeatherCacheHit"
METRIC_CACHE_MISS = "WeatherCacheMiss"
METRIC_LOCATION_SEARCH = "LocationSearchCount"
METRIC_FAVORITE_OP = "FavoriteOperationCount"
METRIC_API_LATENCY = "ApiLatency"
METRIC_EXTERNAL_API = "ExternalApiCallCount"


class CustomMetricsEmitter:
    """
    Emits custom CloudWatch metrics from Lambda functions.

    Parameters
    ----------
    enabled:
        Set to False in unit tests or offline environments so that no real
        boto3 calls are attempted.
    region_name:
        Optional AWS region override; defaults to the Lambda execution region.
    """

    NAMESPACE = "WeatherApp"

    def __init__(
        self,
        enabled: bool = True,
        region_name: str | None = None,
    ) -> None:
        self._enabled = enabled
        self._client: Any = None  # lazy-init so imports remain fast
        self._region = region_name
        if enabled:
            kwargs: dict[str, Any] = {}
            if region_name:
                kwargs["region_name"] = region_name
            self._client = boto3.client("cloudwatch", **kwargs)

    # ------------------------------------------------------------------
    # Public emit methods
    # ------------------------------------------------------------------

    def emit_cache_hit(self, data_type: str = "current") -> None:
        """Record a DynamoDB / in-process cache hit.

        Parameters
        ----------
        data_type:
            One of ``"current"`` (current conditions) or ``"forecast"``.
        """
        self._put(
            metric_name=METRIC_CACHE_HIT,
            value=1,
            unit="Count",
            dimensions=[{"Name": "data_type", "Value": data_type}],
        )

    def emit_cache_miss(self, data_type: str = "current") -> None:
        """Record a cache miss (external API call will follow).

        Parameters
        ----------
        data_type:
            One of ``"current"`` or ``"forecast"``.
        """
        self._put(
            metric_name=METRIC_CACHE_MISS,
            value=1,
            unit="Count",
            dimensions=[{"Name": "data_type", "Value": data_type}],
        )

    def emit_location_search(self, has_results: bool) -> None:
        """Record a location search result.

        Parameters
        ----------
        has_results:
            True when at least one location was returned.
        """
        result_count = "has_results" if has_results else "empty"
        self._put(
            metric_name=METRIC_LOCATION_SEARCH,
            value=1,
            unit="Count",
            dimensions=[{"Name": "result_count", "Value": result_count}],
        )

    def emit_api_latency(self, endpoint: str, latency_ms: float) -> None:
        """Record end-to-end latency for an API endpoint.

        Parameters
        ----------
        endpoint:
            Short path label, e.g. ``"/weather/current"``.
        latency_ms:
            Elapsed time in milliseconds.
        """
        self._put(
            metric_name=METRIC_API_LATENCY,
            value=latency_ms,
            unit="Milliseconds",
            dimensions=[{"Name": "endpoint", "Value": endpoint}],
        )

    def emit_external_api_call(self, provider: str, success: bool) -> None:
        """Record an outbound call to an external weather/geocoding provider.

        Parameters
        ----------
        provider:
            Provider label, e.g. ``"openweather"`` or ``"weatherapi"``.
        success:
            True on HTTP 2xx; False on any error or timeout.
        """
        status = "success" if success else "error"
        self._put(
            metric_name=METRIC_EXTERNAL_API,
            value=1,
            unit="Count",
            dimensions=[
                {"Name": "provider", "Value": provider},
                {"Name": "status", "Value": status},
            ],
        )

    def emit_favorite_operation(self, operation: str) -> None:
        """Record a favorites CRUD operation.

        Parameters
        ----------
        operation:
            One of ``"add"``, ``"remove"``, or ``"list"``.
        """
        self._put(
            metric_name=METRIC_FAVORITE_OP,
            value=1,
            unit="Count",
            dimensions=[{"Name": "operation", "Value": operation}],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _put(
        self,
        metric_name: str,
        value: float,
        unit: str,
        dimensions: Sequence[dict[str, str]],
    ) -> None:
        """Push a single data point to CloudWatch.

        Silently skips if *enabled* is False.  Any boto3 error is logged
        at WARNING level and swallowed so that a metrics failure never
        disrupts the primary request path.
        """
        if not self._enabled or self._client is None:
            return

        metric_data = {
            "MetricName": metric_name,
            "Value": value,
            "Unit": unit,
            "Timestamp": datetime.now(tz=timezone.utc),
            "Dimensions": list(dimensions),
        }

        try:
            self._client.put_metric_data(
                Namespace=self.NAMESPACE,
                MetricData=[metric_data],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to emit CloudWatch metric %s: %s",
                metric_name,
                exc,
            )
