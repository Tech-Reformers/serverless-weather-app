"""
Unit tests for structured logging — task 11.3.

Covers:
  configure_lambda_logging  — doesn't raise; sets function_name in context
  bind_request_context      — adds correlation_id (and optionally aws_request_id)
                              to log output
  clear_request_context     — removes correlation_id / aws_request_id from output
  JSON parsability          — every captured log line is valid JSON
  Field contract            — required CloudWatch fields present in every entry

Implementation note
-------------------
structlog is configured to use `stdlib.LoggerFactory`, which routes all output
through Python's stdlib `logging` module.  pytest captures stdlib log records
via the `caplog` fixture, not `capsys`.  We parse the JSON-formatted message
from each `LogRecord.message` (or `LogRecord.getMessage()`) to inspect fields.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import structlog

from src.monitoring.logging_config import (
    bind_request_context,
    clear_request_context,
    configure_lambda_logging,
    get_logger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reconfigure(log_level: int = logging.DEBUG) -> None:
    """Reset structlog defaults and re-run setup_logging."""
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    from src.api.middleware.logging import setup_logging
    setup_logging(log_level)


def _parse_records(caplog: Any) -> list[dict]:
    """Return JSON-parsed dicts from all captured log records."""
    results = []
    for record in caplog.records:
        msg = record.getMessage()
        try:
            results.append(json.loads(msg))
        except json.JSONDecodeError:
            pass
    return results


# ---------------------------------------------------------------------------
# configure_lambda_logging
# ---------------------------------------------------------------------------


class TestConfigureLambdaLogging:
    def setup_method(self):
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()

    def teardown_method(self):
        structlog.contextvars.clear_contextvars()

    def test_does_not_raise(self):
        configure_lambda_logging("weather-app-test", log_level="INFO")

    def test_accepts_debug_level(self):
        configure_lambda_logging("weather-app-test", log_level="DEBUG")

    def test_accepts_warning_level(self):
        configure_lambda_logging("weather-app-test", log_level="WARNING")

    def test_accepts_lowercase_level(self):
        configure_lambda_logging("weather-app-test", log_level="info")

    def test_invalid_level_falls_back_to_info(self):
        """An unrecognised level string should not raise."""
        configure_lambda_logging("weather-app-test", log_level="NONSENSE")

    def test_binds_function_name_to_context(self):
        """function_name should be stored in structlog contextvars."""
        configure_lambda_logging("my-test-function", log_level="DEBUG")
        ctx = structlog.contextvars.get_contextvars()
        assert ctx.get("function_name") == "my-test-function"

    def test_idempotent_second_call(self):
        """Calling twice must not raise."""
        configure_lambda_logging("fn-a", log_level="INFO")
        configure_lambda_logging("fn-a", log_level="INFO")


# ---------------------------------------------------------------------------
# bind_request_context
# ---------------------------------------------------------------------------


class TestBindRequestContext:
    def setup_method(self):
        _reconfigure()

    def teardown_method(self):
        structlog.contextvars.clear_contextvars()

    def test_correlation_id_appears_in_log_output(self, caplog):
        bind_request_context(correlation_id="test-corr-id-001")
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("test_event")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert entries[-1]["correlation_id"] == "test-corr-id-001"

    def test_aws_request_id_appears_when_provided(self, caplog):
        bind_request_context(
            correlation_id="corr-002",
            aws_request_id="aws-req-abc-123",
        )
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("test_event_with_aws_id")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert entries[-1]["aws_request_id"] == "aws-req-abc-123"

    def test_aws_request_id_absent_when_not_provided(self, caplog):
        bind_request_context(correlation_id="corr-003")
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("no_aws_id")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert "aws_request_id" not in entries[-1]

    def test_multiple_calls_overwrite_previous_context(self, caplog):
        bind_request_context(correlation_id="old-id")
        bind_request_context(correlation_id="new-id")
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("overwrite_test")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert entries[-1]["correlation_id"] == "new-id"


# ---------------------------------------------------------------------------
# clear_request_context
# ---------------------------------------------------------------------------


class TestClearRequestContext:
    def setup_method(self):
        _reconfigure()

    def teardown_method(self):
        structlog.contextvars.clear_contextvars()

    def test_correlation_id_absent_after_clear(self, caplog):
        bind_request_context(correlation_id="to-be-cleared")
        clear_request_context()
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("after_clear")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert "correlation_id" not in entries[-1]

    def test_aws_request_id_absent_after_clear(self, caplog):
        bind_request_context(correlation_id="x", aws_request_id="aws-123")
        clear_request_context()
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("after_clear_aws")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert "aws_request_id" not in entries[-1]

    def test_clear_without_bind_does_not_raise(self):
        """clear_request_context should be a no-op if nothing was bound."""
        clear_request_context()  # must not raise

    def test_function_name_preserved_after_clear(self, caplog):
        """function_name (cold-start context) must survive clear_request_context."""
        structlog.contextvars.bind_contextvars(function_name="persistent-fn")
        bind_request_context(correlation_id="ephemeral-id")
        clear_request_context()
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("after_clear_fn")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert entries[-1].get("function_name") == "persistent-fn"


# ---------------------------------------------------------------------------
# JSON parsability and field contract
# ---------------------------------------------------------------------------


class TestLogJsonFormat:
    def setup_method(self):
        _reconfigure()

    def teardown_method(self):
        structlog.contextvars.clear_contextvars()

    def test_log_output_is_valid_json(self, caplog):
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("json_check")

        assert caplog.records, "Expected at least one log record"
        for record in caplog.records:
            parsed = json.loads(record.getMessage())
            assert isinstance(parsed, dict)

    def test_timestamp_field_present(self, caplog):
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("ts_check")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert "timestamp" in entries[-1]

    def test_level_field_present(self, caplog):
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("level_check")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert "level" in entries[-1]

    def test_event_field_present(self, caplog):
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("my_event_name")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert entries[-1]["event"] == "my_event_name"

    def test_logger_field_present(self, caplog):
        logger = get_logger("my.module.name")
        with caplog.at_level(logging.DEBUG):
            logger.info("logger_field_check")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert entries[-1].get("logger") == "my.module.name"

    def test_level_info_value(self, caplog):
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("info_level")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert entries[-1]["level"] == "info"

    def test_level_warning_value(self, caplog):
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.warning("warning_level")

        entries = _parse_records(caplog)
        warning_entries = [e for e in entries if e.get("level") == "warning"]
        assert warning_entries, "Expected a warning-level log record"

    def test_extra_kwargs_appear_in_output(self, caplog):
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("with_extra", user_id="u-999", action="search")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        entry = entries[-1]
        assert entry.get("user_id") == "u-999"
        assert entry.get("action") == "search"

    def test_correlation_id_in_json_when_bound(self, caplog):
        bind_request_context(correlation_id="json-corr-check")
        logger = get_logger(__name__)
        with caplog.at_level(logging.DEBUG):
            logger.info("corr_id_json")

        entries = _parse_records(caplog)
        assert entries, "Expected at least one log record"
        assert entries[-1]["correlation_id"] == "json-corr-check"
