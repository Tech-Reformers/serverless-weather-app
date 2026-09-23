"""
In-process LRU cache for Lambda warm-container reuse.

Primary use case: user preferences (temperature unit preference) so that
every Lambda invocation in the same container can avoid a DynamoDB
round-trip.

Features
--------
- Bounded capacity with true LRU eviction (oldest-used entry removed first)
- Per-entry TTL (default 60 seconds)
- Thread-safe: a single ``threading.Lock`` guards all mutations so the
  cache is safe to use if Lambda ever runs multiple threads concurrently
- Cache statistics: hits, misses, evictions, and current size

Module-level singleton
----------------------
``get_preference_cache()`` returns a shared :class:`LRUCache` instance that
persists across invocations in a warm Lambda container.  The first call
creates the cache; subsequent calls return the same object.

Usage::

    from src.infrastructure.cache.memory_cache import get_preference_cache

    cache = get_preference_cache()
    unit = cache.get(user_id)
    if unit is None:
        unit = await dynamodb_service.get_user_preference(user_id)
        cache.set(user_id, unit)
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Internal entry wrapper
# ---------------------------------------------------------------------------


class _Entry:
    """Holds a cached value together with its expiry timestamp."""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at  # monotonic seconds

    def is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------


class LRUCache:
    """Thread-safe in-process LRU cache for Lambda warm-container reuse.

    Entries are stored in an :class:`~collections.OrderedDict` ordered by
    most-recently used (MRU).  When the cache is full and a new entry
    arrives, the least-recently used (LRU) entry — at the *beginning* of the
    dict — is evicted.

    Parameters
    ----------
    max_size:
        Maximum number of entries.  Defaults to 100.
    default_ttl_seconds:
        Default time-to-live in seconds.  Individual calls to
        :meth:`set` may override this per entry.  Defaults to 60.0.

    Thread safety
    -------------
    All public methods acquire a single :class:`threading.Lock` before
    mutating the underlying data structure.  This is sufficient for Lambda
    environments (which may use thread pools in some runtimes) without
    significant contention.

    Cache statistics
    ----------------
    :meth:`get_stats` returns a snapshot of ``hits``, ``misses``,
    ``evictions``, ``size``, and ``max_size``.  Statistics are reset when
    :meth:`clear` is called.
    """

    def __init__(
        self,
        max_size: int = 100,
        default_ttl_seconds: float = 60.0,
    ) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        if default_ttl_seconds <= 0:
            raise ValueError(f"default_ttl_seconds must be > 0, got {default_ttl_seconds}")

        self._max_size = max_size
        self._default_ttl = default_ttl_seconds
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()

        # Counters
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value for *key*, or ``None`` if absent / expired.

        On a cache hit the entry is promoted to the MRU position.
        On a cache miss or expiry ``None`` is returned and the stats are
        updated accordingly (expired entries are lazily removed).

        Args:
            key: Cache key to look up.

        Returns:
            The stored value, or ``None``.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None

            if entry.is_expired():
                # Lazy expiry: remove the stale entry.
                del self._store[key]
                self._misses += 1
                return None

            # Promote to MRU position.
            self._store.move_to_end(key)
            self._hits += 1
            return entry.value

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Store *value* under *key* with an optional TTL override.

        If the cache is already at :attr:`max_size` the least-recently used
        entry is evicted to make room (eviction counter is incremented).

        If *key* already exists its value and TTL are updated and the entry
        is promoted to the MRU position without consuming extra capacity.

        Args:
            key: Cache key.
            value: Value to store.  Any Python object is accepted.
            ttl_seconds: Per-entry TTL in seconds.  Uses
                :attr:`default_ttl_seconds` when ``None``.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        if ttl <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl}")

        expires_at = time.monotonic() + ttl

        with self._lock:
            if key in self._store:
                # Update in place and promote to MRU.
                self._store[key] = _Entry(value, expires_at)
                self._store.move_to_end(key)
            else:
                # Evict LRU entry if at capacity.
                if len(self._store) >= self._max_size:
                    self._store.popitem(last=False)  # remove LRU (front)
                    self._evictions += 1
                self._store[key] = _Entry(value, expires_at)

    def delete(self, key: str) -> None:
        """Remove the entry for *key* if it exists.

        Args:
            key: Cache key to remove.  No-op when the key is absent.
        """
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Remove all entries and reset all statistics counters.

        After calling this the cache behaves exactly as it did when first
        constructed.
        """
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def get_stats(self) -> dict:
        """Return a snapshot of cache statistics.

        Returns:
            A dict with keys ``hits``, ``misses``, ``evictions``,
            ``size``, and ``max_size``.

        Example::

            >>> cache.get_stats()
            {'hits': 5, 'misses': 2, 'evictions': 0, 'size': 3, 'max_size': 100}
        """
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "size": len(self._store),
                "max_size": self._max_size,
            }

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        """Current number of entries (including unexpired ones).

        Note: this is a live count and does not remove stale entries.
        Use :meth:`get` to trigger lazy expiry.
        """
        with self._lock:
            return len(self._store)

    @property
    def max_size(self) -> int:
        """The maximum number of entries this cache can hold."""
        return self._max_size

    @property
    def default_ttl_seconds(self) -> float:
        """The default TTL (seconds) applied when no per-entry TTL is given."""
        return self._default_ttl


# ---------------------------------------------------------------------------
# Module-level singleton for Lambda warm-container reuse
# ---------------------------------------------------------------------------

_preference_cache: Optional[LRUCache] = None
_cache_lock = threading.Lock()


def get_preference_cache() -> LRUCache:
    """Return the shared preferences :class:`LRUCache` singleton.

    The instance is created on first call and reused across subsequent
    invocations in the same warm Lambda container.  A 1-minute TTL keeps
    preference data fresh without requiring frequent DynamoDB reads.

    Returns:
        The module-level :class:`LRUCache` with a 60-second TTL and
        capacity for 100 users.
    """
    global _preference_cache
    if _preference_cache is None:
        with _cache_lock:
            # Double-checked locking: another thread may have initialised
            # the cache while we were waiting for the lock.
            if _preference_cache is None:
                _preference_cache = LRUCache(max_size=100, default_ttl_seconds=60.0)
    return _preference_cache
