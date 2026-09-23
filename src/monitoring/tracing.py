"""
X-Ray distributed tracing utilities for the Serverless Weather App.

Design reference: .kiro/specs/weather-app/design.md §AWS Service Mappings
  - X-Ray tracing enabled for all Lambda functions
  - 1% sampling rate (Property 25)
  - Custom annotations and metadata for operational visibility

Usage:
    # In Lambda handler (cold-start path):
    from src.monitoring.tracing import configure_tracing
    configure_tracing("weather-app-weather")

    # Around a specific operation:
    from src.monitoring.tracing import trace_segment, add_annotation, add_metadata

    with trace_segment("fetch-external-api", provider="openweather"):
        data = await weather_api.get(...)
        add_metadata("response_size", len(data))
"""
from __future__ import annotations

import contextlib
import logging
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import: aws-xray-sdk is an optional runtime dep; the module still
# imports cleanly in environments where the SDK is absent (e.g., local unit
# tests that fully mock it).  Real Lambda environments always have the SDK.
# ---------------------------------------------------------------------------
try:
    from aws_xray_sdk.core import patch_all, xray_recorder  # type: ignore[import]

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SDK_AVAILABLE = False
    xray_recorder = None  # type: ignore[assignment]
    patch_all = None  # type: ignore[assignment]


def configure_tracing(service_name: str = "weather-app") -> None:
    """
    Configure the X-Ray SDK for the current Lambda function.

    Should be called once per cold start, outside the handler body, so
    subsequent warm invocations skip the initialisation cost.

    - Sets sampling_rate to 0.01 (1 %) per design.md §AWS Service Mappings
    - Patches boto3, requests, and httpx automatically via ``patch_all``
    - Sets the service name annotation on all segments

    Args:
        service_name: Value written as the ``service`` annotation on every
                      X-Ray segment.  Defaults to ``"weather-app"``.
    """
    if not _SDK_AVAILABLE:
        logger.debug(
            "aws-xray-sdk not available — tracing disabled",
            extra={"service_name": service_name},
        )
        return

    xray_recorder.configure(
        service=service_name,
        sampling=True,
        # 1 % sampling rate — design.md §AWS Service Mappings / Property 25
        sampling_rules={
            "default": {
                "fixed_target": 0,
                "rate": 0.01,
            }
        },
        # Lambda manages the segment lifecycle; SDK must not create its own
        context_missing="LOG_ERROR",
        plugins=("ElasticBeanstalkPlugin",) if False else (),  # no EB plugin needed
    )

    # Patch supported libraries so their calls are traced automatically.
    # patch_all covers boto3/botocore, requests, httpx (when installed).
    patch_all()

    logger.debug(
        "X-Ray tracing configured",
        extra={"service_name": service_name, "sampling_rate": 0.01},
    )


@contextmanager
def trace_segment(name: str, **annotations: str) -> Iterator[None]:
    """
    Context manager that wraps a block of code in a named X-Ray subsegment.

    Any keyword arguments are added as custom annotations on the subsegment,
    making them searchable in the X-Ray console.

    Example::

        with trace_segment("dynamodb-get-item", table="weather-app-cache"):
            item = table.get_item(...)

    Args:
        name:        Subsegment name shown in the X-Ray service map.
        **annotations: Key-value string annotations attached to the subsegment.
    """
    if not _SDK_AVAILABLE or xray_recorder is None:
        # No-op in environments without the SDK (local dev / tests that don't
        # mock it).
        yield
        return

    # Guard against xray_recorder setup errors (e.g., no active segment in
    # non-Lambda environments).  User exceptions raised inside the ``with``
    # block are *not* caught here — they propagate normally after end_subsegment.
    try:
        subsegment = xray_recorder.begin_subsegment(name)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        logger.debug("trace_segment: begin_subsegment failed: %s", exc)
        yield
        return

    try:
        if subsegment is not None:
            for key, value in annotations.items():
                subsegment.put_annotation(key, value)
        yield
    finally:
        with contextlib.suppress(Exception):
            xray_recorder.end_subsegment()


def add_annotation(key: str, value: str) -> None:
    """
    Add a searchable annotation to the *current* X-Ray segment or subsegment.

    Annotations appear as filterable key-value pairs in the X-Ray console and
    can be used in filter expressions.  Values must be strings, numbers, or
    booleans; this helper accepts strings only to keep the interface simple.

    Args:
        key:   Annotation name (alphanumeric + underscores).
        value: String annotation value.
    """
    if not _SDK_AVAILABLE or xray_recorder is None:
        return

    try:
        xray_recorder.current_segment().put_annotation(key, value)
    except Exception as exc:  # noqa: BLE001
        # Tracing failures must never break the business logic.
        logger.debug("add_annotation failed: %s", exc)


def add_metadata(key: str, value: object, namespace: str = "weather-app") -> None:
    """
    Add arbitrary metadata to the *current* X-Ray segment or subsegment.

    Metadata is not indexed or searchable, but it is visible in the trace
    details pane in the X-Ray console.  Use it for rich debugging context
    (e.g., response payloads, cache hit/miss reasons).

    Args:
        key:       Metadata key.
        value:     Any JSON-serialisable object.
        namespace: Metadata namespace (default: ``"weather-app"``).
    """
    if not _SDK_AVAILABLE or xray_recorder is None:
        return

    try:
        xray_recorder.current_segment().put_metadata(key, value, namespace)
    except Exception as exc:  # noqa: BLE001
        logger.debug("add_metadata failed: %s", exc)
