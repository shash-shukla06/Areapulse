"""
services/auth_service.py — authentication and token management
==============================================================
Owns all authentication logic:
  - PIN hashing (bcrypt)
  - JWT creation and validation
  - Login for citizen / gov / ngo roles
  - Token cookie helpers

JWT Strategy (Phase 4):
  Tokens live in httpOnly cookies — correct for browser-based Jinja2 apps.
  This is identical to how Next.js, Django REST + Jinja, and Rails ship JWT
  for server-rendered flows. SameSite=Strict gives CSRF protection for free.

  access_token  — 15 min,  httpOnly, SameSite=Strict
  refresh_token — 7 days,  httpOnly, SameSite=Strict, path=/auth/refresh

  The JWT middleware (app.py @before_request) reads the cookie, validates it,
  and populates flask.g.current_user with an AuthUser instance.
  Route handlers use @require_role() decorator — no more session.get('gov_role').

Phase 4 — JWT + Refresh Tokens + RBAC.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import jwt            # PyJWT
import bcrypt

from domain.models import AuthUser, UserRole

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
_JWT_ALGORITHM    = 'HS256'
_ACCESS_EXPIRY    = 15 * 60          # 15 minutes in seconds
_REFRESH_EXPIRY   = 7 * 24 * 3600    # 7 days in seconds
_DEV_JWT_SECRET   = 'areapulse-jwt-dev-secret-2026'


def _jwt_secret() -> str:
    """Read JWT secret from env. Falls back to dev secret in non-production."""
    secret = os.environ.get('JWT_SECRET', '').strip()
    if not secret:
        env = os.environ.get('APP_ENV', 'development').lower()
        if env == 'production':
            raise RuntimeError(
                'JWT_SECRET environment variable must be set in production. '
                'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        return _DEV_JWT_SECRET
    return secret


# ─────────────────────────────────────────────────────────────────────────────
#  PIN HASHING
# ─────────────────────────────────────────────────────────────────────────────

def hash_pin(pin: str) -> str:
    """Hash a PIN with bcrypt. Store the result, never the raw PIN."""
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()


def verify_pin(plain: str, hashed: str) -> bool:
    """
    Constant-time PIN comparison using bcrypt.
    Always call this — never compare PINs with == directly.
    """
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  TOKEN CREATION
# ─────────────────────────────────────────────────────────────────────────────

def create_access_token(user: AuthUser) -> str:
    """
    Create a short-lived access JWT (15 min).
    Payload contains enough to rebuild AuthUser without a DB lookup.
    """
    now = int(time.time())
    payload = {
        'sub':             user.user_id,
        'name':            user.name,
        'role':            user.role,
        'tags':            user.tags,
        'authority':       user.authority,
        'org_name':        user.org_name,
        'operating_areas': user.operating_areas,
        'iat':             now,
        'exp':             now + _ACCESS_EXPIRY,
        'type':            'access',
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """
    Create a long-lived refresh JWT (7 days).
    Contains only the user_id — client must call /auth/refresh to get a new access token.
    """
    now = int(time.time())
    payload = {
        'sub':  user_id,
        'iat':  now,
        'exp':  now + _REFRESH_EXPIRY,
        'type': 'refresh',
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


# ─────────────────────────────────────────────────────────────────────────────
#  TOKEN VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

class AuthError(Exception):
    """Raised when a token is missing, expired, or tampered with."""
    pass


def decode_access_token(token: str) -> dict:
    """
    Validate and decode an access token.
    Raises AuthError on failure — never returns None silently.
    """
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
        if payload.get('type') != 'access':
            raise AuthError('Token is not an access token')
        return payload
    except jwt.ExpiredSignatureError:
        raise AuthError('Access token has expired')
    except jwt.InvalidTokenError as exc:
        raise AuthError(f'Invalid token: {exc}')


def decode_refresh_token(token: str) -> dict:
    """
    Validate and decode a refresh token.
    Raises AuthError on failure.
    """
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
        if payload.get('type') != 'refresh':
            raise AuthError('Token is not a refresh token')
        return payload
    except jwt.ExpiredSignatureError:
        raise AuthError('Refresh token has expired — please log in again')
    except jwt.InvalidTokenError as exc:
        raise AuthError(f'Invalid token: {exc}')


def auth_user_from_payload(payload: dict) -> AuthUser:
    """Reconstruct an AuthUser from a decoded JWT payload."""
    return AuthUser(
        user_id         = payload['sub'],
        name            = payload.get('name', payload['sub']),
        role            = payload.get('role', UserRole.CITIZEN),
        tags            = payload.get('tags', []),
        authority       = payload.get('authority'),
        org_name        = payload.get('org_name'),
        operating_areas = payload.get('operating_areas', []),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  LOGIN RESULT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LoginResult:
    """Returned by login(). Contains both tokens and the AuthUser."""
    user:          AuthUser
    access_token:  str
    refresh_token: str
    success:       bool    = True
    error:         Optional[str] = None

    @classmethod
    def failure(cls, error: str) -> LoginResult:
        return cls(
            user          = AuthUser(user_id='', name='', role=UserRole.CITIZEN),
            access_token  = '',
            refresh_token = '',
            success       = False,
            error         = error,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  LOGIN
# ─────────────────────────────────────────────────────────────────────────────

def login(username: str, pin: str,
          gov_accounts: dict, ngo_accounts: dict) -> LoginResult:
    """
    Authenticate a user and return JWT tokens.

    Checks gov accounts first, then NGO accounts, then treats as citizen
    (citizens need no PIN — just a valid name).

    Args:
        username:     submitted username / name
        pin:          submitted PIN (may be empty for citizens)
        gov_accounts: GOV_ACCOUNTS dict (from app.py or DB)
        ngo_accounts: NGO_ACCOUNTS dict (from app.py or DB)

    Returns:
        LoginResult with .success, .user, .access_token, .refresh_token
    """
    name_lower = username.lower().strip()

    # ── Government officer ───────────────────────────────────────────────────
    gov = gov_accounts.get(name_lower)
    if gov:
        stored_pin = gov.get('pin', '')
        # Support both plain PIN (demo) and bcrypt hash (production)
        if stored_pin.startswith('$2b$') or stored_pin.startswith('$2a$'):
            valid = verify_pin(pin, stored_pin)
        else:
            valid = (pin == stored_pin)   # plain PIN comparison for demo mode

        if not valid:
            return LoginResult.failure('Incorrect PIN for government account')

        user = AuthUser(
            user_id   = name_lower,
            name      = gov['name'],
            role      = UserRole.GOV_OFFICER,
            tags      = gov.get('tags', []),
            authority = gov.get('authority', ''),
        )
        return LoginResult(
            user          = user,
            access_token  = create_access_token(user),
            refresh_token = create_refresh_token(user.user_id),
        )

    # ── NGO manager ──────────────────────────────────────────────────────────
    ngo = ngo_accounts.get(name_lower)
    if ngo:
        stored_pin = ngo.get('pin', '')
        if stored_pin.startswith('$2b$') or stored_pin.startswith('$2a$'):
            valid = verify_pin(pin, stored_pin)
        else:
            valid = (pin == stored_pin)

        if not valid:
            return LoginResult.failure('Incorrect PIN for NGO account')

        user = AuthUser(
            user_id         = name_lower,
            name            = ngo['name'],
            role            = UserRole.NGO_MANAGER,
            tags            = ngo.get('tags', []),
            org_name        = ngo.get('org_name', ngo['name']),
            operating_areas = ngo.get('operating_areas', []),
        )
        return LoginResult(
            user          = user,
            access_token  = create_access_token(user),
            refresh_token = create_refresh_token(user.user_id),
        )

    # ── Citizen ───────────────────────────────────────────────────────────────
    # Citizens have no stored account — any valid name is accepted
    if len(username.strip()) < 2:
        return LoginResult.failure('Enter a name (min 2 chars)')
    if len(username.strip()) > 50:
        return LoginResult.failure('Name too long (max 50 chars)')

    user = AuthUser(
        user_id = username.strip(),
        name    = username.strip(),
        role    = UserRole.CITIZEN,
    )
    return LoginResult(
        user          = user,
        access_token  = create_access_token(user),
        refresh_token = create_refresh_token(user.user_id),
    )


def refresh_access_token(refresh_token_str: str,
                         gov_accounts: dict,
                         ngo_accounts: dict) -> LoginResult:
    """
    Exchange a valid refresh token for a new access token.
    Looks up current account data to ensure tags/authority are fresh.
    """
    try:
        payload = decode_refresh_token(refresh_token_str)
    except AuthError as exc:
        return LoginResult.failure(str(exc))

    user_id = payload['sub']

    # Rebuild AuthUser from current account data
    gov = gov_accounts.get(user_id)
    if gov:
        user = AuthUser(
            user_id   = user_id,
            name      = gov['name'],
            role      = UserRole.GOV_OFFICER,
            tags      = gov.get('tags', []),
            authority = gov.get('authority', ''),
        )
        return LoginResult(
            user          = user,
            access_token  = create_access_token(user),
            refresh_token = create_refresh_token(user_id),
        )

    ngo = ngo_accounts.get(user_id)
    if ngo:
        user = AuthUser(
            user_id         = user_id,
            name            = ngo['name'],
            role            = UserRole.NGO_MANAGER,
            tags            = ngo.get('tags', []),
            org_name        = ngo.get('org_name', ngo['name']),
            operating_areas = ngo.get('operating_areas', []),
        )
        return LoginResult(
            user          = user,
            access_token  = create_access_token(user),
            refresh_token = create_refresh_token(user_id),
        )

    # Citizen — reconstruct from token payload (no DB entry)
    user = AuthUser(
        user_id = user_id,
        name    = user_id,
        role    = UserRole.CITIZEN,
    )
    return LoginResult(
        user          = user,
        access_token  = create_access_token(user),
        refresh_token = create_refresh_token(user_id),
    )
