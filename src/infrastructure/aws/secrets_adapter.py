"""
AWS Secrets Manager adapter for external API key retrieval.

Security contract
-----------------
- Secret *values* are NEVER written to logs, traces, or error messages.
- Only the secret ARN and key *names* (not values) appear in log output.
- An in-process cache (5-minute TTL) avoids repeated Secrets Manager round
  trips within a warm Lambda container, without accumulating stale keys
  across secret rotations.

Rotation handling
-----------------
AWS Secrets Manager rotates the secret on a 90-day schedule (Property 27).
When the cached version becomes invalid the SDK raises ``InvalidRequestException``
(version staging label no longer exists) or ``ResourceNotFoundException``
(secret deleted/recreated during rotation).  The adapter catches these on the
first call, invalidates the cache, and retries once with a fresh ``GetSecretValue``
call to pick up the new version.

Expected secret format (JSON string in Secrets Manager)
---------------------------------------------------------
{
    "openweather_api_key": "<value>",
    "weatherapi_key": "<value>",
    "geocoding_api_key": "<value>"
}

Configuration
-------------
WEATHER_API_SECRET_ARN  – ARN (or name) of the Secrets Manager secret.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from src.domain.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)

# Required keys that must be present in the secret JSON payload.
_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"openweather_api_key", "weatherapi_key", "geocoding_api_key"}
)

_ENV_SECRET_ARN = "WEATHER_API_SECRET_ARN"

# Botocore error codes that indicate a rotation/version-mismatch event.
_ROTATION_ERROR_CODES: frozenset[str] = frozenset(
    {"InvalidRequestException", "ResourceNotFoundException"}
)


class SecretsManagerAdapter:
    """Retrieve external weather-service API keys from AWS Secrets Manager.

    Instantiate once per Lambda handler module (module-level singleton) so
    that the in-process cache survives across warm invocations:

        _secrets = SecretsManagerAdapter()

        def handler(event, context):
            key = _secrets.get_openweather_key()
            ...

    Thread safety
    -------------
    Lambda functions are single-threaded by design.  The cache is not
    protected by a lock; concurrent access in multi-threaded contexts is
    outside the intended usage.
    """

    def __init__(
        self,
        *,
        secret_arn: Optional[str] = None,
        cache_ttl_seconds: int = 300,
    ) -> None:
        """
        Parameters
        ----------
        secret_arn:
            Override for the secret ARN/name.  Defaults to the value of the
            ``WEATHER_API_SECRET_ARN`` environment variable.
        cache_ttl_seconds:
            How long (seconds) to keep the in-process cache before re-fetching.
            Defaults to 300 s (5 minutes).  Set to 0 to disable caching.
        """
        self._secret_arn: str = secret_arn or os.environ.get(_ENV_SECRET_ARN, "")
        if not self._secret_arn:
            raise EnvironmentError(
                f"Secret ARN not configured. "
                f"Set the {_ENV_SECRET_ARN!r} environment variable."
            )

        self._client = boto3.client("secretsmanager")
        self._cache_ttl_seconds: int = cache_ttl_seconds

        self._cached_secret: Optional[dict] = None
        self._cache_timestamp: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_api_keys(self) -> dict:
        """Return all API keys as a mapping.

        Returns
        -------
        dict
            Keys: ``openweather_api_key``, ``weatherapi_key``,
            ``geocoding_api_key``.

        Raises
        ------
        ExternalServiceError
            If the secret cannot be retrieved or is malformed.
        """
        return self._load_secret()

    def get_openweather_key(self) -> str:
        """Return the OpenWeather API key.

        Raises
        ------
        ExternalServiceError
            If the secret cannot be retrieved or the key is absent.
        """
        return self._get_key("openweather_api_key")

    def get_weatherapi_key(self) -> str:
        """Return the WeatherAPI key.

        Raises
        ------
        ExternalServiceError
            If the secret cannot be retrieved or the key is absent.
        """
        return self._get_key("weatherapi_key")

    def get_geocoding_key(self) -> str:
        """Return the geocoding API key.

        Raises
        ------
        ExternalServiceError
            If the secret cannot be retrieved or the key is absent.
        """
        return self._get_key("geocoding_api_key")

    def invalidate_cache(self) -> None:
        """Force the in-process cache to be discarded.

        The next call to any ``get_*`` method will re-fetch the secret from
        Secrets Manager.  Call this from a rotation Lambda or when an
        ``InvalidRequestException`` is received outside normal flow.
        """
        logger.info(
            "Invalidating Secrets Manager cache",
            extra={"secret_arn": self._secret_arn},
        )
        self._cached_secret = None
        self._cache_timestamp = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_key(self, key_name: str) -> str:
        """Retrieve a single named key from the secret payload."""
        secret = self._load_secret()
        if key_name not in secret:
            raise ExternalServiceError(
                f"Secret is missing required key {key_name!r}. "
                f"Secret ARN: {self._secret_arn}"
            )
        return secret[key_name]  # type: ignore[return-value]

    def _is_cache_valid(self) -> bool:
        """Return True if a non-expired cached secret is available."""
        if self._cached_secret is None or self._cache_timestamp is None:
            return False
        if self._cache_ttl_seconds <= 0:
            return False
        age = (datetime.now(tz=timezone.utc) - self._cache_timestamp).total_seconds()
        return age < self._cache_ttl_seconds

    def _load_secret(self, *, _retry: bool = True) -> dict:
        """Return the secret payload, using the in-process cache when valid.

        Parameters
        ----------
        _retry:
            Internal flag.  When ``True`` the method will invalidate the
            cache and retry once if a rotation-related ``ClientError`` is
            raised.  Set to ``False`` on the recursive retry to avoid loops.
        """
        if self._is_cache_valid():
            logger.debug(
                "Returning cached secret",
                extra={"secret_arn": self._secret_arn},
            )
            assert self._cached_secret is not None  # narrowing for type checker
            return self._cached_secret

        logger.info(
            "Fetching secret from Secrets Manager",
            extra={"secret_arn": self._secret_arn},
        )

        try:
            response = self._client.get_secret_value(SecretId=self._secret_arn)
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]  # type: ignore[index]

            if error_code in _ROTATION_ERROR_CODES and _retry:
                # The cached version label is no longer valid (rotation in
                # progress or secret recreated).  Discard stale state and
                # fetch the current version once more.
                logger.warning(
                    "Secrets Manager returned %s — rotation likely in progress. "
                    "Invalidating cache and retrying once.",
                    error_code,
                    extra={"secret_arn": self._secret_arn},
                )
                self.invalidate_cache()
                return self._load_secret(_retry=False)

            # Non-recoverable error or second consecutive failure.
            logger.error(
                "Failed to retrieve secret from Secrets Manager: %s",
                error_code,
                extra={"secret_arn": self._secret_arn},
                # Deliberately NOT logging exc_info to avoid exposing any
                # partial secret data that might appear in the exception chain.
            )
            raise ExternalServiceError(
                f"Unable to retrieve API keys from Secrets Manager "
                f"(error code: {error_code}). "
                f"Secret ARN: {self._secret_arn}"
            ) from exc

        secret_payload = self._parse_secret(response)
        self._cached_secret = secret_payload
        self._cache_timestamp = datetime.now(tz=timezone.utc)
        logger.info(
            "Secret fetched and cached successfully. Keys present: %s",
            sorted(secret_payload.keys()),
            extra={"secret_arn": self._secret_arn},
        )
        return secret_payload

    def _parse_secret(self, response: dict) -> dict:
        """Parse the raw Secrets Manager response into a key/value mapping.

        Raises
        ------
        ExternalServiceError
            If the secret string cannot be decoded as JSON or is missing
            required keys.
        """
        secret_string = response.get("SecretString")
        if secret_string is None:
            raise ExternalServiceError(
                "Secrets Manager returned a binary secret; only JSON string "
                f"secrets are supported. Secret ARN: {self._secret_arn}"
            )

        try:
            payload: dict = json.loads(secret_string)
        except json.JSONDecodeError as exc:
            raise ExternalServiceError(
                f"Secret payload is not valid JSON. "
                f"Secret ARN: {self._secret_arn}"
            ) from exc

        missing = _REQUIRED_KEYS - payload.keys()
        if missing:
            raise ExternalServiceError(
                f"Secret payload is missing required key(s): {sorted(missing)}. "
                f"Secret ARN: {self._secret_arn}"
            )

        return payload
