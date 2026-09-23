"""
Cache strategy helpers for the Serverless Weather App.

Provides TTL validation and cache-key generation used by both the
DynamoDB cache adapter and higher-level application services.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.domain.models import Location


class CacheStrategy:
    """Encapsulates TTL constants and cache-key logic.

    All methods are pure / side-effect-free so they can be shared
    across the DynamoDB cache adapter and in-process caches without
    coupling to any I/O layer.

    Constants
    ---------
    CURRENT_TTL_MINUTES
        Maximum age for current-conditions entries (15 minutes).
        (Requirements 2.5, Property 3)
    FORECAST_TTL_MINUTES
        Maximum age for hourly / daily forecast entries (60 minutes).
        (Requirements 3.1, 3.2, Property 3)
    """

    CURRENT_TTL_MINUTES: int = 15
    FORECAST_TTL_MINUTES: int = 60

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def should_use_cache(
        self,
        cache_entry: dict[str, Any],
        location: Location,  # noqa: ARG002  (reserved for future geo checks)
        ttl_minutes: int | None = None,
    ) -> bool:
        """Return ``True`` when *cache_entry* is present and still fresh.

        The entry is considered stale when its ``timestamp`` field is
        older than *ttl_minutes* (defaults to
        :attr:`CURRENT_TTL_MINUTES`).  This enforces **Property 3**:
        the system shall never serve data older than its configured TTL.

        Args:
            cache_entry: Dict as returned by the DynamoDB adapter.
                Expected to contain a ``"timestamp"`` key with either a
                :class:`~datetime.datetime` value or an ISO-8601 string.
            location: The location the entry belongs to (reserved for
                future spatial-validity checks; unused now).
            ttl_minutes: Override TTL; falls back to
                :attr:`CURRENT_TTL_MINUTES` when ``None``.

        Returns:
            ``True`` if the entry exists and has not exceeded its TTL,
            ``False`` otherwise.
        """
        if not cache_entry:
            return False

        raw_timestamp = cache_entry.get("timestamp")
        if raw_timestamp is None:
            return False

        entry_time: datetime
        if isinstance(raw_timestamp, datetime):
            entry_time = raw_timestamp
        else:
            try:
                entry_time = datetime.fromisoformat(str(raw_timestamp))
            except (ValueError, TypeError):
                return False

        # Normalise to UTC-aware for safe arithmetic.
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)

        effective_ttl = ttl_minutes if ttl_minutes is not None else self.CURRENT_TTL_MINUTES
        now = datetime.now(tz=timezone.utc)
        age_minutes = (now - entry_time).total_seconds() / 60
        return age_minutes < effective_ttl

    @staticmethod
    def get_cache_key(location: Location, data_type: str) -> str:
        """Build a deterministic cache key for a location + data-type pair.

        Args:
            location: The weather location.
            data_type: One of ``"current"``, ``"hourly"``, ``"daily"``, etc.

        Returns:
            A string of the form ``"<lat>_<lon>_<data_type>"``.

        Example::

            key = CacheStrategy.get_cache_key(location, "current")
            # "51.5074_-0.1278_current"
        """
        return f"{location.latitude}_{location.longitude}_{data_type}"
