"""
Unit tests for SecretsManagerAdapter.

All AWS SDK calls are mocked — no real AWS credentials or network access
are required.  Tests cover:

- Successful secret retrieval and per-key accessors
- In-process cache: hit on second call, TTL expiry, manual invalidation
- Rotation retry: InvalidRequestException / ResourceNotFoundException
  trigger a single retry that succeeds
- Non-retriable ClientErrors are surfaced as ExternalServiceError
- Malformed secrets (non-JSON, missing keys, binary payload)
- Missing ARN environment variable raises EnvironmentError
- Log output never contains secret values (Property 27)

Validates: Property 27 (Secrets Manager Security)
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError

from src.domain.exceptions import ExternalServiceError
from src.infrastructure.aws.secrets_adapter import SecretsManagerAdapter, _REQUIRED_KEYS

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_FAKE_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:weather-keys"

_VALID_PAYLOAD = {
    "openweather_api_key": "ow-secret-key-value",
    "weatherapi_key": "wa-secret-key-value",
    "geocoding_api_key": "geo-secret-key-value",
}

_VALID_SECRET_RESPONSE = {
    "SecretString": json.dumps(_VALID_PAYLOAD),
    "ARN": _FAKE_ARN,
}


def _client_error(code: str) -> ClientError:
    """Build a minimal botocore ClientError for a given error code."""
    return ClientError(
        {"Error": {"Code": code, "Message": f"Simulated {code}"}},
        "GetSecretValue",
    )


def _make_adapter(
    mock_client: MagicMock,
    *,
    cache_ttl_seconds: int = 300,
) -> SecretsManagerAdapter:
    """Construct an adapter that uses *mock_client* instead of a real boto3 client."""
    with patch("boto3.client", return_value=mock_client):
        return SecretsManagerAdapter(
            secret_arn=_FAKE_ARN,
            cache_ttl_seconds=cache_ttl_seconds,
        )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_raises_when_arn_missing_and_env_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEATHER_API_SECRET_ARN", raising=False)
        with patch("boto3.client"):
            with pytest.raises(EnvironmentError, match="WEATHER_API_SECRET_ARN"):
                SecretsManagerAdapter()

    def test_reads_arn_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEATHER_API_SECRET_ARN", _FAKE_ARN)
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE

        with patch("boto3.client", return_value=mock_client):
            adapter = SecretsManagerAdapter()

        assert adapter._secret_arn == _FAKE_ARN

    def test_explicit_arn_takes_precedence_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEATHER_API_SECRET_ARN", "env-arn")
        mock_client = MagicMock()
        with patch("boto3.client", return_value=mock_client):
            adapter = SecretsManagerAdapter(secret_arn=_FAKE_ARN)
        assert adapter._secret_arn == _FAKE_ARN


# ---------------------------------------------------------------------------
# Successful retrieval
# ---------------------------------------------------------------------------


class TestSuccessfulRetrieval:
    def test_get_api_keys_returns_all_keys(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        result = adapter.get_api_keys()

        assert result == _VALID_PAYLOAD

    def test_get_openweather_key(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        assert adapter.get_openweather_key() == _VALID_PAYLOAD["openweather_api_key"]

    def test_get_weatherapi_key(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        assert adapter.get_weatherapi_key() == _VALID_PAYLOAD["weatherapi_key"]

    def test_get_geocoding_key(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        assert adapter.get_geocoding_key() == _VALID_PAYLOAD["geocoding_api_key"]


# ---------------------------------------------------------------------------
# In-process cache behaviour
# ---------------------------------------------------------------------------


class TestCaching:
    def test_second_call_does_not_hit_secrets_manager(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        adapter.get_api_keys()
        adapter.get_api_keys()

        assert mock_client.get_secret_value.call_count == 1

    def test_cache_expires_after_ttl(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client, cache_ttl_seconds=60)

        adapter.get_api_keys()

        # Simulate the cache being 61 seconds old
        assert adapter._cache_timestamp is not None
        adapter._cache_timestamp = datetime.now(tz=timezone.utc) - timedelta(seconds=61)

        adapter.get_api_keys()

        assert mock_client.get_secret_value.call_count == 2

    def test_invalidate_cache_forces_refetch(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        adapter.get_api_keys()
        adapter.invalidate_cache()
        adapter.get_api_keys()

        assert mock_client.get_secret_value.call_count == 2

    def test_cache_disabled_when_ttl_is_zero(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client, cache_ttl_seconds=0)

        adapter.get_api_keys()
        adapter.get_api_keys()

        assert mock_client.get_secret_value.call_count == 2

    def test_invalidate_clears_cached_data(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        adapter.get_api_keys()
        assert adapter._cached_secret is not None

        adapter.invalidate_cache()

        assert adapter._cached_secret is None
        assert adapter._cache_timestamp is None


# ---------------------------------------------------------------------------
# Rotation handling
# ---------------------------------------------------------------------------


class TestRotationHandling:
    @pytest.mark.parametrize("error_code", ["InvalidRequestException", "ResourceNotFoundException"])
    def test_rotation_error_triggers_single_retry_and_succeeds(self, error_code: str) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = [
            _client_error(error_code),  # First call — rotation in progress
            _VALID_SECRET_RESPONSE,     # Retry — new version available
        ]
        adapter = _make_adapter(mock_client)

        result = adapter.get_api_keys()

        assert result == _VALID_PAYLOAD
        assert mock_client.get_secret_value.call_count == 2

    @pytest.mark.parametrize("error_code", ["InvalidRequestException", "ResourceNotFoundException"])
    def test_rotation_error_on_retry_raises_external_service_error(self, error_code: str) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = [
            _client_error(error_code),
            _client_error(error_code),  # Retry also fails
        ]
        adapter = _make_adapter(mock_client)

        with pytest.raises(ExternalServiceError):
            adapter.get_api_keys()

        assert mock_client.get_secret_value.call_count == 2

    def test_rotation_error_invalidates_cache_before_retry(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = [
            _client_error("InvalidRequestException"),
            _VALID_SECRET_RESPONSE,
        ]
        adapter = _make_adapter(mock_client)

        # Prime the cache with an *expired* entry so the SDK is called, which
        # then raises a rotation error and triggers the invalidate + retry path.
        adapter._cached_secret = {"stale": "data"}
        adapter._cache_timestamp = datetime.now(tz=timezone.utc) - timedelta(seconds=400)

        adapter.get_api_keys()

        # After successful retry the new secret should be cached
        assert adapter._cached_secret == _VALID_PAYLOAD


# ---------------------------------------------------------------------------
# Non-retriable errors
# ---------------------------------------------------------------------------


class TestNonRetriableErrors:
    @pytest.mark.parametrize(
        "error_code",
        ["AccessDeniedException", "DecryptionFailureException", "InternalServiceError"],
    )
    def test_non_rotation_client_error_raises_immediately(self, error_code: str) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = _client_error(error_code)
        adapter = _make_adapter(mock_client)

        with pytest.raises(ExternalServiceError, match=error_code):
            adapter.get_api_keys()

        # Must NOT retry — only one call expected
        assert mock_client.get_secret_value.call_count == 1

    def test_error_message_contains_arn_not_secret_value(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = _client_error("AccessDeniedException")
        adapter = _make_adapter(mock_client)

        with pytest.raises(ExternalServiceError) as exc_info:
            adapter.get_api_keys()

        error_msg = str(exc_info.value)
        assert _FAKE_ARN in error_msg
        # Must not contain any secret value
        for value in _VALID_PAYLOAD.values():
            assert value not in error_msg


# ---------------------------------------------------------------------------
# Malformed secret payloads
# ---------------------------------------------------------------------------


class TestMalformedPayloads:
    def test_non_json_secret_raises_external_service_error(self) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": "not-valid-json"}
        adapter = _make_adapter(mock_client)

        with pytest.raises(ExternalServiceError, match="not valid JSON"):
            adapter.get_api_keys()

    def test_missing_required_key_raises_external_service_error(self) -> None:
        incomplete = {"openweather_api_key": "val"}  # missing the other two
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": json.dumps(incomplete)}
        adapter = _make_adapter(mock_client)

        with pytest.raises(ExternalServiceError, match="missing required key"):
            adapter.get_api_keys()

    def test_binary_secret_raises_external_service_error(self) -> None:
        mock_client = MagicMock()
        # Secrets Manager returns SecretBinary instead of SecretString
        mock_client.get_secret_value.return_value = {"SecretBinary": b"\x00\x01"}
        adapter = _make_adapter(mock_client)

        with pytest.raises(ExternalServiceError, match="binary secret"):
            adapter.get_api_keys()

    def test_extra_keys_in_payload_are_accepted(self) -> None:
        extended = {**_VALID_PAYLOAD, "extra_key": "extra_value"}
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": json.dumps(extended)}
        adapter = _make_adapter(mock_client)

        result = adapter.get_api_keys()

        # Required keys must be present; extra keys pass through
        for key in _REQUIRED_KEYS:
            assert key in result


# ---------------------------------------------------------------------------
# Security: secret values must never appear in logs (Property 27)
# ---------------------------------------------------------------------------


class TestSecretNeverLogged:
    """Validates: Property 27 — Secrets Manager Security"""

    def test_secret_values_absent_from_log_output_on_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        with caplog.at_level(logging.DEBUG):
            adapter.get_api_keys()

        combined_logs = "\n".join(caplog.messages)
        for secret_value in _VALID_PAYLOAD.values():
            assert secret_value not in combined_logs, (
                f"Secret value {secret_value!r} leaked into log output"
            )

    def test_secret_values_absent_from_log_output_on_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = _client_error("AccessDeniedException")
        adapter = _make_adapter(mock_client)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ExternalServiceError):
                adapter.get_api_keys()

        combined_logs = "\n".join(caplog.messages)
        for secret_value in _VALID_PAYLOAD.values():
            assert secret_value not in combined_logs, (
                f"Secret value {secret_value!r} leaked into log output during error"
            )

    def test_arn_is_present_in_log_output(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ARN (not secret values) must appear in logs for auditability."""
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = _VALID_SECRET_RESPONSE
        adapter = _make_adapter(mock_client)

        with caplog.at_level(logging.INFO):
            adapter.get_api_keys()

        # At least one log record should reference the ARN for audit trail
        log_records_with_arn = [
            r for r in caplog.records if _FAKE_ARN in str(r.__dict__)
        ]
        assert log_records_with_arn, "ARN should appear in at least one log record"
