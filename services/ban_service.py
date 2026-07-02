"""
services/ban_service.py — user ban and strike management
=========================================================
Moves _banned_users / _strike_log out of ai_engine.py module-level dicts
into Redis (via cache_service). Bans now survive server restarts and are
shared across all Gunicorn workers.

Fallback: when Redis is unavailable, falls back to in-memory cache
(cache_service handles this transparently).

Phase 6 — Redis Infrastructure.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from services import cache_service

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BAN_THRESHOLD     = 3        # strikes before auto-ban
BAN_TTL_PERMANENT = 0        # 0 = no expiry (permanent ban)
BAN_TTL_TEMP      = 24 * 3600  # 24 hours for temporary bans
STRIKE_TTL        = 7 * 24 * 3600  # strikes expire after 7 days


# ─────────────────────────────────────────────────────────────────────────────
#  KEY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ban_key(user_id: str) -> str:
    return f'ban:{user_id}'

def _strikes_key(user_id: str) -> str:
    return f'strikes:{user_id}'


# ─────────────────────────────────────────────────────────────────────────────
#  BAN MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def ban_user(user_id: str, reason: str, permanent: bool = True) -> dict:
    """
    Ban a user. Stores ban record in Redis.
    Returns the ban record dict.
    """
    uid     = str(user_id)
    record  = {
        'reason':    reason,
        'at':        time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'permanent': permanent,
    }
    if permanent:
        cache_service.set_json(_ban_key(uid), record)
    else:
        cache_service.set_json(_ban_key(uid), record, ttl_seconds=BAN_TTL_TEMP)
    return record


def is_banned(user_id: str) -> dict:
    """
    Check if a user is banned.
    Returns {'banned': True, ...ban_info} or {'banned': False}.
    """
    info = cache_service.get_json(_ban_key(str(user_id)))
    if info:
        return {'banned': True, **info}
    return {'banned': False}


def unban_user(user_id: str) -> bool:
    """Remove a ban and clear all strikes for the user."""
    uid = str(user_id)
    cache_service.delete(_ban_key(uid), _strikes_key(uid))
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  STRIKE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def record_strike(user_id: str, reason: str) -> dict:
    """
    Record a strike against a user. Auto-bans when strike count >= BAN_THRESHOLD.
    Returns {'strikes': int, 'banned': bool, 'ban_info': dict|None}.
    """
    uid          = str(user_id)
    key          = _strikes_key(uid)

    # Load existing strikes
    existing: list = cache_service.get_json(key) or []
    if not isinstance(existing, list):
        existing = []

    # Add new strike
    existing.append({
        'reason': reason,
        'at':     time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })

    # Persist with TTL
    cache_service.set_json(key, existing, ttl_seconds=STRIKE_TTL)

    # Auto-ban on threshold
    banned = False
    ban_info = None
    if len(existing) >= BAN_THRESHOLD:
        ban_info = ban_user(uid, reason=f'auto-ban after {len(existing)} strikes', permanent=False)
        banned = True

    return {
        'strikes': len(existing),
        'banned':  banned,
        'ban_info': ban_info,
    }


def get_strikes(user_id: str) -> list:
    """Return the strike list for a user."""
    result = cache_service.get_json(_strikes_key(str(user_id)))
    return result if isinstance(result, list) else []


def clear_strikes(user_id: str) -> None:
    """Clear all strikes without unbanning."""
    cache_service.delete(_strikes_key(str(user_id)))
