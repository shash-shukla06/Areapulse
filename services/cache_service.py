"""
services/cache_service.py — Redis client wrapper
=================================================
Provides a thin, consistent interface over Redis.
Falls back to an in-memory dict transparently when REDIS_URL is not set
— so the rest of the codebase works in dev/demo mode without Redis.

Why this wrapper exists instead of using redis.Redis directly:
  1. Fallback — in-memory dict means zero config for local dev
  2. Testability — swap in a mock cache in unit tests trivially
  3. Single place to handle connection errors, serialisation, key namespacing

Connection:
  Set REDIS_URL=redis://localhost:6379/0 (local)
  or  REDIS_URL=rediss://user:pass@xxx.upstash.io:6379 (Upstash serverless)

Phase 6 — Redis Infrastructure.
"""

from __future__ import annotations

import json
import os
import time
import threading
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────────────────────
#  REDIS CLIENT  (optional dependency)
# ─────────────────────────────────────────────────────────────────────────────

_redis_client = None
_redis_available = False

_REDIS_URL = os.environ.get('REDIS_URL', '').strip()

if _REDIS_URL:
    try:
        import redis as _redis_lib
        _redis_client = _redis_lib.from_url(
            _REDIS_URL,
            decode_responses=True,   # always get str back, not bytes
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
        )
        # Ping to verify connection is live
        _redis_client.ping()
        _redis_available = True
        print(f'[cache_service] Redis connected ✓  url={_REDIS_URL[:30]}...')
    except Exception as exc:
        print(f'[cache_service] Redis unavailable ({exc}) — falling back to in-memory')
        _redis_client = None
else:
    print('[cache_service] REDIS_URL not set — using in-memory fallback')


# ─────────────────────────────────────────────────────────────────────────────
#  IN-MEMORY FALLBACK
#  Mimics the Redis interface closely enough for local dev.
#  NOT suitable for production with multiple workers.
# ─────────────────────────────────────────────────────────────────────────────

class _MemoryCache:
    """Thread-safe in-memory dict that mimics basic Redis operations."""

    def __init__(self):
        self._store: dict = {}      # key → value
        self._expiry: dict = {}     # key → expiry Unix timestamp (float)
        self._lock = threading.Lock()

    def _is_expired(self, key: str) -> bool:
        exp = self._expiry.get(key)
        return exp is not None and time.time() > exp

    def _clean(self, key: str):
        if self._is_expired(key):
            self._store.pop(key, None)
            self._expiry.pop(key, None)

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            self._clean(key)
            return self._store.get(key)

    def set(self, key: str, value: str) -> bool:
        with self._lock:
            self._store[key] = str(value)
            return True

    def setex(self, key: str, ttl_seconds: int, value: str) -> bool:
        with self._lock:
            self._store[key] = str(value)
            self._expiry[key] = time.time() + ttl_seconds
            return True

    def delete(self, *keys) -> int:
        with self._lock:
            count = 0
            for key in keys:
                if key in self._store:
                    del self._store[key]
                    self._expiry.pop(key, None)
                    count += 1
            return count

    def exists(self, key: str) -> bool:
        with self._lock:
            self._clean(key)
            return key in self._store

    def incr(self, key: str) -> int:
        with self._lock:
            self._clean(key)
            val = int(self._store.get(key, 0)) + 1
            self._store[key] = str(val)
            return val

    def expire(self, key: str, ttl_seconds: int) -> bool:
        with self._lock:
            if key not in self._store:
                return False
            self._expiry[key] = time.time() + ttl_seconds
            return True

    def lpush(self, key: str, *values) -> int:
        with self._lock:
            lst = json.loads(self._store.get(key, '[]'))
            for v in reversed(values):
                lst.insert(0, v)
            self._store[key] = json.dumps(lst)
            return len(lst)

    def lrange(self, key: str, start: int, end: int) -> list:
        with self._lock:
            self._clean(key)
            lst = json.loads(self._store.get(key, '[]'))
            if end == -1:
                return lst[start:]
            return lst[start:end + 1]

    def llen(self, key: str) -> int:
        with self._lock:
            self._clean(key)
            return len(json.loads(self._store.get(key, '[]')))

    def keys(self, pattern: str = '*') -> list:
        """Very basic pattern matching — only supports trailing *."""
        with self._lock:
            prefix = pattern.rstrip('*') if '*' in pattern else pattern
            return [k for k in self._store if k.startswith(prefix) and not self._is_expired(k)]

    def ping(self) -> bool:
        return True


_memory_cache = _MemoryCache()

# The single cache instance used by all services
_cache = _redis_client if _redis_available else _memory_cache


def is_redis_available() -> bool:
    return _redis_available


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
#  All functions gracefully fall back to in-memory on Redis errors.
# ─────────────────────────────────────────────────────────────────────────────

_KEY_PREFIX = 'areapulse:'


def _k(key: str) -> str:
    """Namespace all keys to avoid collisions."""
    return f'{_KEY_PREFIX}{key}'


def get(key: str) -> Optional[str]:
    """Get a string value. Returns None if missing or expired."""
    try:
        return _cache.get(_k(key))
    except Exception as exc:
        print(f'[cache_service] get error: {exc}')
        return _memory_cache.get(_k(key))


def set(key: str, value: Any) -> bool:
    """Set a value with no expiry."""
    try:
        return bool(_cache.set(_k(key), str(value)))
    except Exception as exc:
        print(f'[cache_service] set error: {exc}')
        return _memory_cache.set(_k(key), str(value))


def setex(key: str, ttl_seconds: int, value: Any) -> bool:
    """Set a value with a TTL (expires after ttl_seconds)."""
    try:
        return bool(_cache.setex(_k(key), ttl_seconds, str(value)))
    except Exception as exc:
        print(f'[cache_service] setex error: {exc}')
        return _memory_cache.setex(_k(key), ttl_seconds, str(value))


def delete(*keys: str) -> int:
    """Delete one or more keys. Returns count of deleted keys."""
    namespaced = [_k(k) for k in keys]
    try:
        return _cache.delete(*namespaced)
    except Exception as exc:
        print(f'[cache_service] delete error: {exc}')
        return _memory_cache.delete(*namespaced)


def exists(key: str) -> bool:
    """Return True if key exists and is not expired."""
    try:
        return bool(_cache.exists(_k(key)))
    except Exception as exc:
        print(f'[cache_service] exists error: {exc}')
        return _memory_cache.exists(_k(key))


def incr(key: str) -> int:
    """Atomically increment an integer counter. Returns new value."""
    try:
        return int(_cache.incr(_k(key)))
    except Exception as exc:
        print(f'[cache_service] incr error: {exc}')
        return _memory_cache.incr(_k(key))


def expire(key: str, ttl_seconds: int) -> bool:
    """Set expiry on an existing key."""
    try:
        return bool(_cache.expire(_k(key), ttl_seconds))
    except Exception as exc:
        print(f'[cache_service] expire error: {exc}')
        return _memory_cache.expire(_k(key), ttl_seconds)


def get_json(key: str) -> Optional[Any]:
    """Get a value and JSON-decode it. Returns None if missing."""
    raw = get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def set_json(key: str, value: Any, ttl_seconds: Optional[int] = None) -> bool:
    """JSON-encode value and store it. Optionally with TTL."""
    serialised = json.dumps(value)
    if ttl_seconds:
        return setex(key, ttl_seconds, serialised)
    return set(key, serialised)


def lpush(key: str, *values: str) -> int:
    """Push values to the left of a list. Returns new list length."""
    try:
        return int(_cache.lpush(_k(key), *values))
    except Exception as exc:
        print(f'[cache_service] lpush error: {exc}')
        return _memory_cache.lpush(_k(key), *values)


def lrange(key: str, start: int = 0, end: int = -1) -> list:
    """Get a range of elements from a list."""
    try:
        return _cache.lrange(_k(key), start, end)
    except Exception as exc:
        print(f'[cache_service] lrange error: {exc}')
        return _memory_cache.lrange(_k(key), start, end)


def llen(key: str) -> int:
    """Return the length of a list."""
    try:
        return int(_cache.llen(_k(key)))
    except Exception as exc:
        print(f'[cache_service] llen error: {exc}')
        return _memory_cache.llen(_k(key))


def keys_matching(pattern: str) -> list:
    """Return all keys matching a pattern (use sparingly in production)."""
    try:
        raw = _cache.keys(f'{_KEY_PREFIX}{pattern}')
        prefix = _KEY_PREFIX
        return [k[len(prefix):] if k.startswith(prefix) else k for k in raw]
    except Exception as exc:
        print(f'[cache_service] keys error: {exc}')
        return _memory_cache.keys(f'{_KEY_PREFIX}{pattern}')
