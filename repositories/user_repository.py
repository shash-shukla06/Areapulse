"""
repositories/user_repository.py — user account data access
===========================================================
Reads gov/NGO/citizen accounts from the database.
Falls back to the hardcoded GOV_ACCOUNTS / NGO_ACCOUNTS dicts
when running in memory mode (demo / local dev without Postgres).

This is the Phase 4 foundation for moving accounts out of app.py.
The immediate next step (Phase 4b or pre-Phase 5) is a seed function
that inserts the hardcoded demo accounts with bcrypt-hashed PINs
on first Postgres startup.

Phase 4 — JWT + Refresh Tokens + RBAC.
"""

from __future__ import annotations

import json
from typing import Optional, List

from database import _state


def get_gov_account(username: str) -> Optional[dict]:
    """
    Look up a government officer account by username.

    Returns a dict with keys matching GOV_ACCOUNTS structure:
      name, pin, authority, tags
    Returns None if not found.

    Priority: Postgres users table → fallback dict.
    """
    if _state.get('mode') == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT username, display_name, pin_hash, authority, tags
                           FROM users
                           WHERE username = %s AND role = 'gov_officer' AND is_active = TRUE""",
                        (username.lower(),),
                    )
                    row = cur.fetchone()
                    if row:
                        if hasattr(cur, 'description') and cur.description:
                            cols = [d.name for d in cur.description]
                            r = {cols[i]: row[i] for i in range(len(cols))}
                        else:
                            r = {
                                'username': row[0], 'display_name': row[1],
                                'pin_hash': row[2], 'authority': row[3], 'tags': row[4],
                            }
                        tags = r.get('tags') or []
                        if isinstance(tags, str):
                            try:
                                tags = json.loads(tags)
                            except Exception:
                                tags = []
                        return {
                            'name':      r.get('display_name', username),
                            'pin':       r.get('pin_hash', ''),
                            'authority': r.get('authority', ''),
                            'tags':      tags,
                        }
        except Exception as exc:
            print(f'[user_repository] get_gov_account Postgres failed: {exc}')
    return None


def get_ngo_account(username: str) -> Optional[dict]:
    """
    Look up an NGO manager account by username.

    Returns a dict with keys matching NGO_ACCOUNTS structure:
      name, pin, org_name, focus, tags, operating_areas
    Returns None if not found.
    """
    if _state.get('mode') == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT username, display_name, pin_hash, org_name,
                                  focus, tags, operating_areas
                           FROM users
                           WHERE username = %s AND role = 'ngo_manager' AND is_active = TRUE""",
                        (username.lower(),),
                    )
                    row = cur.fetchone()
                    if row:
                        if hasattr(cur, 'description') and cur.description:
                            cols = [d.name for d in cur.description]
                            r = {cols[i]: row[i] for i in range(len(cols))}
                        else:
                            r = {
                                'username': row[0], 'display_name': row[1],
                                'pin_hash': row[2], 'org_name': row[3],
                                'focus': row[4], 'tags': row[5], 'operating_areas': row[6],
                            }
                        tags  = r.get('tags') or []
                        areas = r.get('operating_areas') or []
                        for field, val in [('tags', tags), ('operating_areas', areas)]:
                            if isinstance(val, str):
                                try:
                                    val = json.loads(val)
                                except Exception:
                                    val = []
                            if field == 'tags':
                                tags = val
                            else:
                                areas = val
                        return {
                            'name':            r.get('display_name', username),
                            'pin':             r.get('pin_hash', ''),
                            'org_name':        r.get('org_name', ''),
                            'focus':           r.get('focus', ''),
                            'tags':            tags,
                            'operating_areas': areas,
                        }
        except Exception as exc:
            print(f'[user_repository] get_ngo_account Postgres failed: {exc}')
    return None


def seed_demo_accounts(gov_accounts: dict, ngo_accounts: dict) -> None:
    """
    Insert hardcoded demo accounts into the users table if they don't exist.
    PINs are bcrypt-hashed on first insert.
    Safe to call repeatedly — uses ON CONFLICT DO NOTHING.

    Call this after _seed_postgres_if_empty() during init.
    """
    if _state.get('mode') != 'postgres':
        return

    from services.auth_service import hash_pin

    rows = []
    for username, acct in gov_accounts.items():
        pin_hash = (
            acct['pin'] if acct['pin'].startswith('$2')
            else hash_pin(acct['pin'])
        )
        rows.append((
            username, acct['name'], 'gov_officer', pin_hash,
            json.dumps(acct.get('tags', [])),
            acct.get('authority', ''),
            None, None, None,
        ))
    for username, acct in ngo_accounts.items():
        pin_hash = (
            acct['pin'] if acct['pin'].startswith('$2')
            else hash_pin(acct['pin'])
        )
        rows.append((
            username, acct['name'], 'ngo_manager', pin_hash,
            json.dumps(acct.get('tags', [])),
            None,
            acct.get('org_name', acct['name']),
            acct.get('focus', ''),
            json.dumps(acct.get('operating_areas', [])),
        ))

    if not rows:
        return

    try:
        with _state['pg_pool'].connection() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    (uname, dname, role, pin_hash,
                     tags, authority, org_name, focus, areas) = row
                    cur.execute(
                        """INSERT INTO users
                            (username, display_name, role, pin_hash,
                             tags, authority, org_name, focus, operating_areas)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (username) DO NOTHING""",
                        (uname, dname, role, pin_hash,
                         tags, authority, org_name, focus, areas),
                    )
            conn.commit()
        print(f'[user_repository] Seeded {len(rows)} demo user accounts')
    except Exception as exc:
        print(f'[user_repository] seed_demo_accounts failed: {exc}')
