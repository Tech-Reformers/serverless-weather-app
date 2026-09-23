"""
Unit tests for LRUCache (src/infrastructure/cache/memory_cache.py).

Coverage:
  - Basic get/set round-trip
  - TTL expiry: expired entry returns None
  - LRU eviction when at capacity
  - Thread safety: concurrent get/set does not corrupt state
  - Cache statistics accuracy (hits, misses, evictions, size)
  - clear() resets both data and statistics
  - delete() removes a specific key
  - get_preference_cache() returns singleton
  - per-entry TTL override
"""
from __future__ import annotations

import threading
import time
from typing import List

import pytest

from src.infrastructure.cache.memory_cache import LRUCache, get_preference_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cache(max_size: int = 10, ttl: float = 60.0) -> LRUCache:
    return LRUCache(max_size=max_size, default_ttl_seconds=ttl)


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_invalid_max_size_raises() -> None:
    with pytest.raises(ValueError, match="max_size"):
        LRUCache(max_size=0)


def test_invalid_ttl_raises() -> None:
    with pytest.raises(ValueError, match="ttl"):
        LRUCache(default_ttl_seconds=0)


def test_negative_ttl_raises() -> None:
    with pytest.raises(ValueError):
        LRUCache(default_ttl_seconds=-1.0)


# ---------------------------------------------------------------------------
# Basic get / set
# ---------------------------------------------------------------------------


def test_set_and_get_returns_value() -> None:
    cache = make_cache()
    cache.set("k", "v")
    assert cache.get("k") == "v"


def test_get_missing_key_returns_none() -> None:
    cache = make_cache()
    assert cache.get("nonexistent") is None


def test_set_overwrites_existing_value() -> None:
    cache = make_cache()
    cache.set("k", "first")
    cache.set("k", "second")
    assert cache.get("k") == "second"


def test_set_various_value_types() -> None:
    cache = make_cache()
    cache.set("int", 42)
    cache.set("list", [1, 2, 3])
    cache.set("dict", {"a": 1})
    cache.set("none", None)

    assert cache.get("int") == 42
    assert cache.get("list") == [1, 2, 3]
    assert cache.get("dict") == {"a": 1}
    # None is a valid stored value; the key exists but value is None.
    # Our implementation stores None legitimately.
    assert cache.get("none") is None  # indistinguishable from missing — acceptable


def test_size_reflects_unique_keys() -> None:
    cache = make_cache()
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("a", 99)  # update, not a new entry
    assert cache.size == 2


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


def test_expired_entry_returns_none() -> None:
    cache = LRUCache(max_size=10, default_ttl_seconds=0.05)  # 50 ms
    cache.set("key", "value")
    time.sleep(0.1)  # wait for expiry
    assert cache.get("key") is None


def test_non_expired_entry_survives() -> None:
    cache = LRUCache(max_size=10, default_ttl_seconds=5.0)
    cache.set("key", "value")
    time.sleep(0.01)
    assert cache.get("key") == "value"


def test_per_entry_ttl_override_short() -> None:
    cache = LRUCache(max_size=10, default_ttl_seconds=60.0)
    cache.set("quick", "expires-fast", ttl_seconds=0.05)
    time.sleep(0.1)
    assert cache.get("quick") is None


def test_per_entry_ttl_override_longer_than_default() -> None:
    cache = LRUCache(max_size=10, default_ttl_seconds=0.05)
    cache.set("slow", "survives", ttl_seconds=5.0)
    time.sleep(0.1)
    assert cache.get("slow") == "survives"


def test_set_invalid_ttl_raises() -> None:
    cache = make_cache()
    with pytest.raises(ValueError):
        cache.set("k", "v", ttl_seconds=0)


def test_expired_entry_removed_from_size() -> None:
    cache = LRUCache(max_size=10, default_ttl_seconds=0.05)
    cache.set("temp", "val")
    time.sleep(0.1)
    cache.get("temp")  # triggers lazy removal
    assert cache.size == 0


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


def test_lru_eviction_removes_oldest_used() -> None:
    cache = make_cache(max_size=3)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    # Access "a" to make it MRU; "b" is now LRU.
    cache.get("a")

    # Adding "d" should evict "b" (LRU).
    cache.set("d", 4)

    assert cache.get("b") is None  # evicted
    assert cache.get("a") == 1
    assert cache.get("c") == 3
    assert cache.get("d") == 4


def test_eviction_increments_evictions_stat() -> None:
    cache = make_cache(max_size=2)
    cache.set("x", 1)
    cache.set("y", 2)
    cache.set("z", 3)  # evicts "x"

    stats = cache.get_stats()
    assert stats["evictions"] == 1


def test_no_eviction_when_updating_existing_key() -> None:
    cache = make_cache(max_size=2)
    cache.set("x", 1)
    cache.set("y", 2)
    cache.set("x", 99)  # update, NOT a new entry — no eviction

    stats = cache.get_stats()
    assert stats["evictions"] == 0
    assert cache.size == 2
    assert cache.get("x") == 99
    assert cache.get("y") == 2


def test_lru_order_maintained_across_gets() -> None:
    """After multiple gets the eviction picks the truly least-recently-used."""
    cache = make_cache(max_size=3)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    # Touch in order: b, c, a — so "a" is MRU, "b" is LRU.
    cache.get("b")
    cache.get("c")
    cache.get("a")

    cache.set("d", 4)  # should evict "b"

    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None
    assert cache.get("d") is not None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_existing_key() -> None:
    cache = make_cache()
    cache.set("k", "v")
    cache.delete("k")
    assert cache.get("k") is None
    assert cache.size == 0


def test_delete_nonexistent_key_is_noop() -> None:
    cache = make_cache()
    cache.delete("ghost")  # should not raise


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_removes_all_entries() -> None:
    cache = make_cache()
    cache.set("a", 1)
    cache.set("b", 2)
    cache.clear()
    assert cache.size == 0
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_clear_resets_statistics() -> None:
    cache = make_cache(max_size=2)
    cache.set("x", 1)
    cache.set("y", 2)
    cache.set("z", 3)  # evicts one → evictions = 1
    cache.get("y")     # hit
    cache.get("missing")  # miss

    cache.clear()
    stats = cache.get_stats()
    assert stats == {"hits": 0, "misses": 0, "evictions": 0, "size": 0, "max_size": 2}


# ---------------------------------------------------------------------------
# Cache statistics
# ---------------------------------------------------------------------------


def test_stats_initial_state() -> None:
    cache = make_cache(max_size=5)
    stats = cache.get_stats()
    assert stats == {"hits": 0, "misses": 0, "evictions": 0, "size": 0, "max_size": 5}


def test_stats_hit_incremented_on_cache_hit() -> None:
    cache = make_cache()
    cache.set("k", "v")
    cache.get("k")
    assert cache.get_stats()["hits"] == 1


def test_stats_miss_incremented_on_missing_key() -> None:
    cache = make_cache()
    cache.get("ghost")
    assert cache.get_stats()["misses"] == 1


def test_stats_miss_incremented_on_expired_entry() -> None:
    cache = LRUCache(max_size=10, default_ttl_seconds=0.05)
    cache.set("k", "v")
    time.sleep(0.1)
    cache.get("k")  # expired → miss
    assert cache.get_stats()["misses"] == 1
    assert cache.get_stats()["hits"] == 0


def test_stats_combined() -> None:
    cache = make_cache(max_size=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)   # evicts "a"
    cache.get("b")      # hit
    cache.get("a")      # miss (evicted)
    cache.get("c")      # hit

    stats = cache.get_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["evictions"] == 1
    assert stats["size"] == 2


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_writes_do_not_corrupt_state() -> None:
    """Multiple threads writing disjoint keys must all be stored correctly."""
    cache = make_cache(max_size=200)
    n_threads = 20
    writes_per_thread = 10
    errors: List[Exception] = []

    def writer(thread_id: int) -> None:
        try:
            for i in range(writes_per_thread):
                cache.set(f"t{thread_id}_k{i}", thread_id * 100 + i)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    assert cache.size == n_threads * writes_per_thread


def test_concurrent_reads_do_not_raise() -> None:
    """Multiple threads reading the same key simultaneously must not raise."""
    cache = make_cache()
    cache.set("shared", "value")
    errors: List[Exception] = []

    def reader() -> None:
        try:
            for _ in range(50):
                cache.get("shared")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"


def test_concurrent_mixed_operations() -> None:
    """Writers and readers running in parallel must not corrupt the cache."""
    cache = make_cache(max_size=50)
    errors: List[Exception] = []

    def worker(wid: int) -> None:
        try:
            for i in range(20):
                cache.set(f"key-{wid}-{i}", wid * i)
                cache.get(f"key-{wid}-{i}")
                if i % 5 == 0:
                    cache.get_stats()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def test_get_preference_cache_returns_lru_cache() -> None:
    cache = get_preference_cache()
    assert isinstance(cache, LRUCache)


def test_get_preference_cache_returns_same_instance() -> None:
    c1 = get_preference_cache()
    c2 = get_preference_cache()
    assert c1 is c2


def test_preference_cache_default_config() -> None:
    cache = get_preference_cache()
    assert cache.max_size == 100
    assert cache.default_ttl_seconds == 60.0
