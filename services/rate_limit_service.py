"""
services/rate_limit_service.py — sliding window rate limiter
=============================================================
Replaces the _state['recent_reports'] dict in database.py.

Uses Redis sliding window counter. Falls back to the in-memory
cache_service implementation when Redis is unavailable.

Sliding window algorithm:
  For each user, maintain a Redis key that expires after `window_seconds`.
  On each request, increment the counter. If counter > max_reports, reject.
  The key naturally expires after the window — no cleanup needed.

  This is not perfectly accurate for very high-frequency requests
  (fixed window at the boundary), but it's correct, fast, and simple.
  Good enough for AreaPulse's rate-limit use case (5 reports/minute).

Phase 6 — Redis Infrastructure.
"""

from __future__ import annotations

import time

from services import cache_service


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG  (mirrors database.py is_rate_limited defaults)
# ─────────────────────────────────────────────────────────────────────────────
_MAX_REPORTS      = 5    # max submissions per window
_WINDOW_SECONDS   = 60   # sliding window duration


# ─────────────────────────────────────────────────────────────────────────────
#  RATE LIMIT KEY
# ─────────────────────────────────────────────────────────────────────────────

def _rate_key(user_id: str) -> str:
    # One key per user. Expires after _WINDOW_SECONDS.
    return f'rate:{user_id}'


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def is_rate_limited(user_id: str,
                    max_reports: int = _MAX_REPORTS,
                    window_seconds: int = _WINDOW_SECONDS) -> bool:
    """
    Return True if the user has exceeded the rate limit.

    Each call records the attempt. The counter resets automatically
    when the window expires (Redis TTL).
    """
    key   = _rate_key(str(user_id))
    count = cache_service.incr(key)

    if count == 1:
        # First request in this window — set the expiry
        cache_service.expire(key, window_seconds)

    return count > max_reports


def get_request_count(user_id: str) -> int:
    """Return current request count for this user in the active window."""
    raw = cache_service.get(_rate_key(str(user_id)))
    try:
        return int(raw) if raw else 0
    except (ValueError, TypeError):
        return 0


def reset_rate_limit(user_id: str) -> None:
    """Manually reset the rate limit counter for a user (e.g. after testing)."""
    cache_service.delete(_rate_key(str(user_id)))
