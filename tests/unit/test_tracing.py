"""
Unit tests for src/monitoring/tracing.py

All tests mock the aws-xray-sdk so no real X-Ray daemon is needed.
The module-level ``_SDK_AVAILABLE`` flag and ``xray_recorder`` are patched
directly so the same code paths used in production are exercised.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODULE = "src.monitoring.tracing"


def _make_recorder() -> MagicMock:
    """Return a fresh mock that mimics xray_recorder's interface."""
    recorder = MagicMock()
    subsegment = MagicMock()
    recorder.begin_subsegment.return_value = subsegment
    recorder.current_segment.return_value = MagicMock()
    return recorder


# ---------------------------------------------------------------------------
# configure_tracing
# ---------------------------------------------------------------------------


class TestConfigureTracing:
    def test_does_not_raise_when_sdk_available(self) -> None:
        """configure_tracing should complete without raising."""
        recorder = _make_recorder()
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
            patch(f"{_MODULE}.patch_all") as mock_patch_all,
        ):
            from src.monitoring.tracing import configure_tracing

            configure_tracing("weather-app-test")

        recorder.configure.assert_called_once()
        mock_patch_all.assert_called_once()

    def test_sets_service_name(self) -> None:
        """configure_tracing should pass the service name to xray_recorder."""
        recorder = _make_recorder()
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
            patch(f"{_MODULE}.patch_all"),
        ):
            from src.monitoring.tracing import configure_tracing

            configure_tracing("my-service")

        call_kwargs = recorder.configure.call_args.kwargs
        assert call_kwargs.get("service") == "my-service"

    def test_sets_1_percent_sampling_rate(self) -> None:
        """configure_tracing must configure 1 % sampling (design.md Property 25)."""
        recorder = _make_recorder()
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
            patch(f"{_MODULE}.patch_all"),
        ):
            from src.monitoring.tracing import configure_tracing

            configure_tracing()

        call_kwargs = recorder.configure.call_args.kwargs
        sampling_rules = call_kwargs.get("sampling_rules", {})
        assert sampling_rules.get("default", {}).get("rate") == pytest.approx(0.01)

    def test_does_not_raise_when_sdk_unavailable(self) -> None:
        """configure_tracing should silently do nothing when SDK is absent."""
        with patch(f"{_MODULE}._SDK_AVAILABLE", False):
            from src.monitoring.tracing import configure_tracing

            # Must not raise
            configure_tracing("any-service")

    def test_patches_libraries(self) -> None:
        """patch_all should be called so boto3/requests/httpx are traced."""
        recorder = _make_recorder()
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
            patch(f"{_MODULE}.patch_all") as mock_patch_all,
        ):
            from src.monitoring.tracing import configure_tracing

            configure_tracing()

        mock_patch_all.assert_called_once_with()


# ---------------------------------------------------------------------------
# trace_segment
# ---------------------------------------------------------------------------


class TestTraceSegment:
    def test_creates_subsegment_with_correct_name(self) -> None:
        """trace_segment should open a subsegment with the given name."""
        recorder = _make_recorder()
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import trace_segment

            with trace_segment("my-operation"):
                pass

        recorder.begin_subsegment.assert_called_once_with("my-operation")

    def test_ends_subsegment_after_block(self) -> None:
        """trace_segment should always close the subsegment on exit."""
        recorder = _make_recorder()
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import trace_segment

            with trace_segment("op"):
                pass

        recorder.end_subsegment.assert_called_once()

    def test_ends_subsegment_on_exception(self) -> None:
        """trace_segment should close the subsegment even when an exception is raised."""
        recorder = _make_recorder()
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import trace_segment

            with pytest.raises(ValueError):
                with trace_segment("failing-op"):
                    raise ValueError("boom")

        recorder.end_subsegment.assert_called_once()

    def test_adds_annotations_from_kwargs(self) -> None:
        """trace_segment should attach keyword arguments as annotations."""
        recorder = _make_recorder()
        subsegment = recorder.begin_subsegment.return_value
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import trace_segment

            with trace_segment("db-op", table="weather-cache", action="get"):
                pass

        subsegment.put_annotation.assert_any_call("table", "weather-cache")
        subsegment.put_annotation.assert_any_call("action", "get")

    def test_no_op_when_sdk_unavailable(self) -> None:
        """trace_segment should behave as a no-op when SDK is absent."""
        with patch(f"{_MODULE}._SDK_AVAILABLE", False):
            from src.monitoring.tracing import trace_segment

            executed = False
            with trace_segment("silent-op"):
                executed = True

        assert executed, "Code inside trace_segment should still execute"


# ---------------------------------------------------------------------------
# add_annotation
# ---------------------------------------------------------------------------


class TestAddAnnotation:
    def test_calls_put_annotation_on_current_segment(self) -> None:
        """add_annotation should forward the key/value to the current segment."""
        recorder = _make_recorder()
        segment = recorder.current_segment.return_value
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import add_annotation

            add_annotation("user_id", "user-42")

        segment.put_annotation.assert_called_once_with("user_id", "user-42")

    def test_no_op_when_sdk_unavailable(self) -> None:
        """add_annotation should be a no-op when SDK is absent."""
        with patch(f"{_MODULE}._SDK_AVAILABLE", False):
            from src.monitoring.tracing import add_annotation

            # Must not raise
            add_annotation("key", "value")

    def test_swallows_recorder_errors(self) -> None:
        """add_annotation should not propagate xray_recorder failures."""
        recorder = _make_recorder()
        recorder.current_segment.side_effect = RuntimeError("no segment")
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import add_annotation

            # Must not raise
            add_annotation("k", "v")


# ---------------------------------------------------------------------------
# add_metadata
# ---------------------------------------------------------------------------


class TestAddMetadata:
    def test_calls_put_metadata_with_default_namespace(self) -> None:
        """add_metadata should use 'weather-app' as the default namespace."""
        recorder = _make_recorder()
        segment = recorder.current_segment.return_value
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import add_metadata

            add_metadata("cache_hit", True)

        segment.put_metadata.assert_called_once_with("cache_hit", True, "weather-app")

    def test_calls_put_metadata_with_custom_namespace(self) -> None:
        """add_metadata should forward a custom namespace."""
        recorder = _make_recorder()
        segment = recorder.current_segment.return_value
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import add_metadata

            add_metadata("response", {"status": 200}, namespace="http")

        segment.put_metadata.assert_called_once_with(
            "response", {"status": 200}, "http"
        )

    def test_no_op_when_sdk_unavailable(self) -> None:
        """add_metadata should be a no-op when SDK is absent."""
        with patch(f"{_MODULE}._SDK_AVAILABLE", False):
            from src.monitoring.tracing import add_metadata

            # Must not raise
            add_metadata("anything", {"complex": True})

    def test_swallows_recorder_errors(self) -> None:
        """add_metadata should not propagate xray_recorder failures."""
        recorder = _make_recorder()
        recorder.current_segment.side_effect = RuntimeError("no segment")
        with (
            patch(f"{_MODULE}._SDK_AVAILABLE", True),
            patch(f"{_MODULE}.xray_recorder", recorder),
        ):
            from src.monitoring.tracing import add_metadata

            # Must not raise
            add_metadata("k", "v")
