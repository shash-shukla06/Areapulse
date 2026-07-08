"""
AreaPulse — Map-First Civic Reporting Platform
Full-screen interactive city map. AI vision + auto-classification + NGO routing.
Runs out of the box with seeded data. Optionally connects to Firebase + Groq.

Merged v3 changes:
  - PostgreSQL as primary database (via DATABASE_URL)
  - NGO dashboard + AI triage/draft/recommendations
  - /issues safety improvements (MAX_ESCALATIONS cap, per-issue try/except)
  - /api/authority/<tag> endpoint
  - Admin CSV exports for both spam and real issues (with Postgres support)
  - NGO login accounts alongside gov accounts
"""
import os, time, json, base64
import urllib.request as _ureq
import json as _json
from flask import Flask, request, jsonify, render_template, session, redirect, url_for

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from database import (
    init_db, insert_issue, get_issues, upvote_issue,
    get_all_ngos, get_nearby_ngos, get_areas, AREA_COORDS,
    insert_spam_issue, find_nearby_duplicate, is_rate_limited,
    calculate_sla, escalate_issue, get_issue_by_id,
    update_issue_status, get_issues_for_gov,
    log_duplicate_merge, get_all_image_hashes, get_recent_reports,
    SLA_HOURS, CROWD_ESCALATION_THRESHOLD,
    bulk_escalate as _bulk_escalate,
)
from classifier import auto_classify, severity_from_text
import ai_engine
import email_sender
from services import issue_service, notification_service
from services.issue_service import ReportSubmission
from services.validation_service import validate_submission as _validate_submission
from services import auth_service
from services.auth_service import AuthError

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

# ── Phase 0 Fix 1: SECRET_KEY — crash in production if absent/default ─────────
_DEV_SECRET   = 'areapulse-dev-secret-2026'
_APP_ENV       = os.environ.get('APP_ENV', os.environ.get('FLASK_ENV', 'development')).lower()
_SECRET_KEY    = os.environ.get('SECRET_KEY', '').strip()

if _APP_ENV == 'production':
    if not _SECRET_KEY or _SECRET_KEY == _DEV_SECRET:
        raise RuntimeError(
            '\n\n'
            '╔══════════════════════════════════════════════════════════════╗\n'
            '║  STARTUP ABORTED — SECRET_KEY is not set for production.    ║\n'
            '║                                                              ║\n'
            '║  Set the SECRET_KEY environment variable to a long random   ║\n'
            '║  string before deploying. Example (generate one with):      ║\n'
            '║      python -c "import secrets; print(secrets.token_hex())" ║\n'
            '╚══════════════════════════════════════════════════════════════╝\n'
        )
    app.secret_key = _SECRET_KEY
else:
    if not _SECRET_KEY or _SECRET_KEY == _DEV_SECRET:
        print(
            '\n[areapulse] ⚠  WARNING: SECRET_KEY is not set or is using the dev default.\n'
            '            Sessions are not secure. Set SECRET_KEY in your .env file.\n'
            '            This will CRASH in production (APP_ENV=production).\n'
        )
    app.secret_key = _SECRET_KEY or _DEV_SECRET

# Initialize DB (Postgres if DATABASE_URL, else Firebase if configured, else seeded in-memory)
init_db()

# ── Phase 2: inject concrete repositories into issue_service ──────────────────
from repositories.database_repository import (
    DatabaseIssueRepository,
    DatabaseNGORepository,
    DatabaseSpamRepository,
)
issue_service.configure(
    issue_repo = DatabaseIssueRepository(),
    ngo_repo   = DatabaseNGORepository(),
    spam_repo  = DatabaseSpamRepository(),
)

# ── Phase 0 Fix 5: Startup validation — warn loudly about degraded state ──────
def _validate_startup():
    """Print runtime warnings for any missing critical configuration."""
    warnings = []
    if not os.environ.get('DATABASE_URL'):
        warnings.append(
            '⚠  DATABASE_URL not set → running IN-MEMORY. '
            'All reported issues will be lost on restart.'
        )
    else:
        dsn = os.environ.get('DATABASE_URL', '')
        if '-pooler.' not in dsn and 'pooler' not in dsn:
            warnings.append(
                '⚠  DATABASE_URL does not appear to use Neon pooled endpoint. '
                'Use the pooled connection string (contains -pooler.) to avoid '
                'connection exhaustion under load.'
            )
    if not os.environ.get('GROQ_API_KEY'):
        warnings.append(
            '⚠  GROQ_API_KEY not set → AI pipeline disabled. '
            'Spam filtering and image classification will use keyword fallback only.'
        )
    _admin_tok = os.environ.get('ADMIN_TOKEN', '').strip()
    if not _admin_tok:
        warnings.append(
            '⚠  ADMIN_TOKEN not set → all /admin/* routes will return 401. '
            'Set ADMIN_TOKEN in your .env to enable admin access.'
        )
    if not os.environ.get('APP_ENV'):
        warnings.append(
            '⚠  APP_ENV not set → assuming development. '
            'Set APP_ENV=production on your deployment platform.'
        )
    # Phase 4: JWT secret
    if not os.environ.get('JWT_SECRET'):
        warnings.append(
            '⚠  JWT_SECRET not set → using insecure dev default. '
            'Set JWT_SECRET in production (python -c "import secrets; print(secrets.token_hex(32))").'
        )
    # Phase 6: Redis
    if not os.environ.get('REDIS_URL'):
        warnings.append(
            '⚠  REDIS_URL not set → ban/rate-limit state is in-memory only. '
            'Bans reset on restart and are not shared across workers. '
            'Set REDIS_URL (e.g. Upstash free tier) for production.'
        )
    else:
        from services.cache_service import is_redis_available as _redis_ok
        if _redis_ok():
            print('[areapulse]   ✓ Redis connected')
    # Phase 5: storage
    from services.storage_service import provider_name as _storage_provider
    _sp = _storage_provider()
    if _sp == 'passthrough':
        warnings.append(
            'ℹ  No object storage configured → images stored as base64 in DB. '
            'Set R2_ACCOUNT_ID + R2_ACCESS_KEY + R2_SECRET_KEY + R2_BUCKET_NAME '
            'for Cloudflare R2, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY + S3_BUCKET_NAME for S3.'
        )
    else:
        print(f'[areapulse]   ✓ Image storage: {_sp}')
    if warnings:
        print('\n[areapulse] ── Startup warnings ' + '─' * 40)
        for w in warnings:
            print(f'[areapulse]   {w}')
        print('[areapulse] ' + '─' * 54 + '\n')

_validate_startup()

# Public config exposed to template
MAPTILER_KEY = os.environ.get('MAPTILER_KEY', '')
MAPTILER_STYLE = os.environ.get('MAPTILER_STYLE', 'hybrid')  # FR24-style satellite + labels overlay


# ═══════════════════════════════════════════════════════
#  JWT MIDDLEWARE + RBAC  (Phase 4)
# ═══════════════════════════════════════════════════════
import functools
from flask import g as _g

# Cookie names — centralised so they're easy to change
_ACCESS_COOKIE  = 'areapulse_access'
_REFRESH_COOKIE = 'areapulse_refresh'
_COOKIE_SECURE  = _APP_ENV == 'production'   # HTTPS-only in prod


@app.before_request
def _jwt_middleware():
    """
    Runs before every request.
    Reads the access_token cookie, validates it, and populates:
      flask.g.current_user  — AuthUser instance or None
      flask.g.role          — role string or None

    On token expiry the cookie is cleared so the browser re-authenticates.
    Keeps session['user'] in sync for Jinja2 template compatibility.
    """
    _g.current_user = None
    _g.role         = None

    token = request.cookies.get(_ACCESS_COOKIE)
    if not token:
        return   # unauthenticated — routes decide whether to reject

    try:
        payload = auth_service.decode_access_token(token)
        _g.current_user = auth_service.auth_user_from_payload(payload)
        _g.role         = _g.current_user.role
        # Keep session['user'] in sync so Jinja2 templates keep working
        session['user'] = _g.current_user.name
        if _g.current_user.is_gov():
            session['gov_role'] = _g.current_user.to_session_dict()
            session.pop('ngo_role', None)
        elif _g.current_user.is_ngo():
            session['ngo_role'] = _g.current_user.to_session_dict()
            session.pop('gov_role', None)
        else:
            session.pop('gov_role', None)
            session.pop('ngo_role', None)
    except AuthError:
        # Token expired or tampered — clear cookie and treat as anonymous
        _g.current_user = None
        _g.role         = None
        session.pop('user', None)
        session.pop('gov_role', None)
        session.pop('ngo_role', None)


def require_role(*roles: str):
    """
    Route decorator for RBAC.

    Usage:
        @app.route('/gov')
        @require_role('gov_officer')
        def gov_dashboard(): ...

        @app.route('/ngo/dashboard')
        @require_role('ngo_manager')
        def ngo_dashboard(): ...

    For HTML routes — redirects to /login on failure.
    For JSON routes (Accept: application/json) — returns 401 JSON.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user = _g.get('current_user')
            # Fallback to legacy session check for requests without JWT cookie
            # (handles users who logged in before Phase 4 was deployed)
            if user is None:
                if 'gov_officer' in roles and session.get('gov_role'):
                    return fn(*args, **kwargs)
                if 'ngo_manager' in roles and session.get('ngo_role'):
                    return fn(*args, **kwargs)
            if user is not None and user.role in roles:
                return fn(*args, **kwargs)
            # Not authorised
            wants_json = (
                request.accept_mimetypes.best == 'application/json'
                or request.path.startswith('/api/')
                or request.path.startswith('/gov/ai')
                or request.path.startswith('/ngo/ai')
                or request.path.startswith('/gov/update')
                or request.path.startswith('/gov/all')
                or request.path.startswith('/ngo/commit')
                or request.path.startswith('/ngo/ai')
            )
            if wants_json:
                return jsonify({'error': 'Not authorised', 'required_role': list(roles)}), 401
            return redirect(url_for('login'))
        return wrapper
    return decorator


def _set_jwt_cookies(response, access_token: str, refresh_token: str):
    """Attach both JWT cookies to a response object."""
    # access_token — short-lived, readable by all paths
    response.set_cookie(
        _ACCESS_COOKIE,
        access_token,
        max_age    = 15 * 60,
        httponly   = True,
        samesite   = 'Strict',
        secure     = _COOKIE_SECURE,
        path       = '/',
    )
    # refresh_token — long-lived, only sent to /auth/refresh
    response.set_cookie(
        _REFRESH_COOKIE,
        refresh_token,
        max_age    = 7 * 24 * 3600,
        httponly   = True,
        samesite   = 'Strict',
        secure     = _COOKIE_SECURE,
        path       = '/auth/refresh',
    )
    return response


def _clear_jwt_cookies(response):
    """Expire both JWT cookies."""
    response.set_cookie(_ACCESS_COOKIE,  '', expires=0, path='/')
    response.set_cookie(_REFRESH_COOKIE, '', expires=0, path='/auth/refresh')
    return response


# ═══════════════════════════════════════════════════════
#  WHATSAPP — OUTBOUND NOTIFICATIONS
# ═══════════════════════════════════════════════════════
# Extracted to services/notification_service.py (Phase 1).
# _wa_notify is kept as a thin shim here because the WhatsApp
# inbound bot handler (/whatsapp route) calls it directly and
# will be fully migrated in a later phase.

def _wa_notify(to_phone, message):
    return notification_service.send_whatsapp(to_phone, message)


def _status_change_message(issue, new_status):
    return notification_service.compose_status_message(issue, new_status)


def _wa_twiml(*messages):
    """Wrap reply strings in Twilio TwiML XML."""
    from flask import Response
    body = '<?xml version="1.0" encoding="UTF-8"?><Response>'
    for m in messages:
        m_escaped = (m.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
        body += f'<Message>{m_escaped}</Message>'
    body += '</Response>'
    return Response(body, mimetype='application/xml')


# ═══════════════════════════════════════════════════════
#  GOV DASHBOARD CONFIG (Feature 2)
# ═══════════════════════════════════════════════════════
# Demo gov-officer accounts. PIN '0000' for all in demo mode.
# Each officer sees only issues tagged for their department.
# To add real users in production: replace this dict with a Firestore lookup.
GOV_ACCOUNTS = {
    'gov_rmc': {
        'pin': '0000', 'name': 'RMC Officer',
        'authority': 'Ranchi Municipal Corporation',
        'tags': ['pothole', 'garbage', 'sewage', 'streetlight', 'tree', 'other'],
    },
    'gov_water': {
        'pin': '0000', 'name': 'Water Board Officer',
        'authority': 'Drinking Water & Sanitation Dept (Jharkhand)',
        'tags': ['water'],
    },
    'gov_electricity': {
        'pin': '0000', 'name': 'Electricity Officer',
        'authority': 'Jharkhand Bijli Vitran Nigam (JBVNL)',
        'tags': ['electricity'],
    },
    'gov_traffic': {
        'pin': '0000', 'name': 'Traffic Police',
        'authority': 'Ranchi Traffic Police',
        'tags': ['traffic', 'noise'],
    },
}

# ═══════════════════════════════════════════════════════
#  NGO ACCOUNTS  (merged from app 2.py)
# ═══════════════════════════════════════════════════════
NGO_ACCOUNTS = {
    'ngo_sanitation': {
        'pin': '0000',
        'name': 'Delhi Sanitation Trust',
        'org_name': 'Delhi Sanitation Trust',
        'focus': 'Sanitation & Waste Management',
        'tags': ['sewage', 'garbage', 'tree'],
        'operating_areas': ['Lajpat Nagar', 'Defence Colony', 'Greater Kailash', 'Saket'],
    },
    'ngo_water': {
        'pin': '0000',
        'name': 'Jal Seva Foundation',
        'org_name': 'Jal Seva Foundation',
        'focus': 'Water Access & Conservation',
        'tags': ['water'],
        'operating_areas': ['Dwarka', 'Janakpuri', 'Rohini', 'Pitampura'],
    },
    'ngo_civic': {
        'pin': '0000',
        'name': 'Delhi Civic Trust',
        'org_name': 'Delhi Civic Trust',
        'focus': 'General Civic Infrastructure',
        'tags': ['pothole', 'streetlight', 'traffic', 'other'],
        'operating_areas': ['Connaught Place', 'Karol Bagh', 'Paharganj', 'Civil Lines'],
    },
    'ngo_power': {
        'pin': '0000',
        'name': 'Bijli Pratikriya',
        'org_name': 'Bijli Pratikriya',
        'focus': 'Electricity & Energy Access',
        'tags': ['electricity', 'streetlight'],
        'operating_areas': ['Shahdara', 'Laxmi Nagar', 'Mayur Vihar', 'Preet Vihar'],
    },
}

_ngo_commitments_store = []

# ── Phase 4: seed demo user accounts into Postgres users table ───────────────
# Must run AFTER GOV_ACCOUNTS + NGO_ACCOUNTS are defined above.
from repositories.user_repository import seed_demo_accounts as _seed_users
_seed_users(GOV_ACCOUNTS, NGO_ACCOUNTS)


# ═══════════════════════════════════════════════════════
#  ROUTES — PAGES
# ═══════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════
#  DUPLICATE DETECTION DEBUG ENDPOINT
# ═══════════════════════════════════════════════════════
@app.route('/api/debug/dup-check')
def debug_dup_check():
    """
    Test what the duplicate detector would do without actually submitting.
    Usage: /api/debug/dup-check?lat=28.6514&lng=77.1907&tag=pothole

    Returns the match result + any candidates found. Useful for verifying
    duplicate detection works without polluting the issues collection.
    """
    try:
        lat = float(request.args.get('lat'))
        lng = float(request.args.get('lng'))
    except (TypeError, ValueError):
        return jsonify({
            'error': 'pass ?lat=...&lng=...&tag=...',
            'example': '/api/debug/dup-check?lat=28.6514&lng=77.1907&tag=pothole',
        }), 400

    tag = request.args.get('tag', 'pothole').strip()
    radius = int(request.args.get('radius', '50'))
    days = int(request.args.get('days', '7'))

    from database import _state
    dup = find_nearby_duplicate(lat, lng, tag, within_meters=radius, within_days=days)

    return jsonify({
        'query':            {'lat': lat, 'lng': lng, 'tag': tag, 'radius_m': radius, 'days': days},
        'mode':             _state.get('mode'),
        'matched':          dup is not None,
        'matched_issue_id': dup.get('id') if dup else None,
        'distance_meters':  round(dup.get('_distance_meters', 0), 1) if dup else None,
        'matched_description': (dup.get('description', '')[:100] if dup else None),
    })


@app.route('/')
def home():
    """Single page — the map IS the app."""
    return render_template(
        'index.html',
        current_user=session.get('user'),
        maptiler_key=MAPTILER_KEY,
        maptiler_style=MAPTILER_STYLE,
        ai_available=ai_engine.is_available(),
        email_available=email_sender.is_available(),
        wa_number=os.environ.get('TWILIO_WHATSAPP_NUMBER', '').replace('whatsapp:', '').replace('+', ''),
        wa_join_code=os.environ.get('TWILIO_SANDBOX_CODE', ''),
    )


# ═══════════════════════════════════════════════════════
#  LOGIN — merged with NGO support (Change 2)
# ═══════════════════════════════════════════════════════
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        pin  = (request.form.get('pin') or '').strip()
        if not name or len(name) < 2:
            return render_template('login.html', error='Enter a name (min 2 chars)')
        if len(name) > 50:
            return render_template('login.html', error='Name too long (max 50 chars)')

        # ── Phase 4: delegate to auth_service ────────────────────────────────
        result = auth_service.login(name, pin, GOV_ACCOUNTS, NGO_ACCOUNTS)
        if not result.success:
            return render_template('login.html', error=result.error)

        # Populate session for Jinja2 backward compatibility
        session['user'] = result.user.name
        session.pop('gov_role', None)
        session.pop('ngo_role', None)

        if result.user.is_gov():
            session['gov_role'] = result.user.to_session_dict()
            target = url_for('gov_dashboard')
        elif result.user.is_ngo():
            session['ngo_role'] = result.user.to_session_dict()
            target = url_for('ngo_dashboard')
        else:
            target = url_for('home')

        # Set JWT cookies on the redirect response
        resp = redirect(target)
        _set_jwt_cookies(resp, result.access_token, result.refresh_token)
        return resp

    if 'user' in session:
        if session.get('gov_role'):
            return redirect(url_for('gov_dashboard'))
        if session.get('ngo_role'):
            return redirect(url_for('ngo_dashboard'))
        return redirect(url_for('home'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('gov_role', None)
    session.pop('ngo_role', None)
    session.pop('google_email', None)
    session.pop('oauth_state', None)
    resp = redirect(url_for('login'))
    _clear_jwt_cookies(resp)
    return resp


@app.route('/auth/refresh', methods=['POST'])
def auth_refresh():
    """
    Exchange a valid refresh token for a new access token.
    Called automatically by the frontend when an API request returns 401.

    Phase 4 — JWT refresh endpoint.
    """
    refresh_tok = request.cookies.get(_REFRESH_COOKIE)
    if not refresh_tok:
        return jsonify({'error': 'No refresh token'}), 401

    result = auth_service.refresh_access_token(refresh_tok, GOV_ACCOUNTS, NGO_ACCOUNTS)
    if not result.success:
        resp = jsonify({'error': result.error})
        resp.status_code = 401
        _clear_jwt_cookies(resp)
        return resp

    resp = jsonify({'status': 'ok', 'role': result.user.role})
    _set_jwt_cookies(resp, result.access_token, result.refresh_token)
    return resp


# ═══════════════════════════════════════════════════════
#  GOOGLE OAUTH 2.0
# ═══════════════════════════════════════════════════════
_GOOGLE_AUTH_URL     = 'https://accounts.google.com/o/oauth2/v2/auth'
_GOOGLE_TOKEN_URL    = 'https://oauth2.googleapis.com/token'
_GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'


@app.route('/auth/google')
def auth_google():
    """Step 1 — redirect the browser to Google's consent screen."""
    import secrets as _secrets
    import urllib.parse as _uparse

    client_id = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
    if not client_id:
        return redirect(url_for('login') + '?error=' + _uparse.quote(
            'Google Sign-In is not configured. Set GOOGLE_CLIENT_ID in environment variables.'))

    state = _secrets.token_urlsafe(20)
    session['oauth_state'] = state

    redirect_uri = (
        os.environ.get('GOOGLE_REDIRECT_URI', '').strip()
        or url_for('auth_google_callback', _external=True)
    )
    if redirect_uri.startswith('http://') and 'onrender.com' in redirect_uri:
        redirect_uri = 'https://' + redirect_uri[7:]

    auth_url = _GOOGLE_AUTH_URL + '?' + _uparse.urlencode({
        'client_id':     client_id,
        'redirect_uri':  redirect_uri,
        'response_type': 'code',
        'scope':         'openid email profile',
        'state':         state,
        'access_type':   'online',
        'prompt':        'select_account',
    })
    return redirect(auth_url)


@app.route('/auth/google/callback')
def auth_google_callback():
    """Step 2 — exchange code -> tokens -> user info -> set session."""
    import urllib.parse as _uparse
    import requests as _rq

    client_id     = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
    client_secret = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()

    error = request.args.get('error', '')
    if error:
        return redirect(url_for('login') + '?error=' + _uparse.quote(
            f'Google sign-in cancelled: {error}'))

    code  = request.args.get('code', '')
    state = request.args.get('state', '')

    if not code:
        return redirect(url_for('login') + '?error=' + _uparse.quote(
            'No authorisation code received from Google.'))

    if not state or state != session.pop('oauth_state', None):
        return redirect(url_for('login') + '?error=' + _uparse.quote(
            'Invalid OAuth state. Please try again.'))

    redirect_uri = (
        os.environ.get('GOOGLE_REDIRECT_URI', '').strip()
        or url_for('auth_google_callback', _external=True)
    )
    if redirect_uri.startswith('http://') and 'onrender.com' in redirect_uri:
        redirect_uri = 'https://' + redirect_uri[7:]

    try:
        token_resp = _rq.post(_GOOGLE_TOKEN_URL, data={
            'code':          code,
            'client_id':     client_id,
            'client_secret': client_secret,
            'redirect_uri':  redirect_uri,
            'grant_type':    'authorization_code',
        }, timeout=10)
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except Exception as exc:
        print(f'[google_oauth] token exchange failed: {exc}')
        return redirect(url_for('login') + '?error=' + _uparse.quote(
            'Failed to exchange token with Google. Please try again.'))

    access_token = token_data.get('access_token', '')
    if not access_token:
        return redirect(url_for('login') + '?error=' + _uparse.quote(
            'Google did not return an access token.'))

    try:
        info_resp = _rq.get(_GOOGLE_USERINFO_URL,
                            headers={'Authorization': f'Bearer {access_token}'},
                            timeout=10)
        info_resp.raise_for_status()
        userinfo = info_resp.json()
    except Exception as exc:
        print(f'[google_oauth] userinfo fetch failed: {exc}')
        return redirect(url_for('login') + '?error=' + _uparse.quote(
            'Could not retrieve your Google profile. Please try again.'))

    name = (
        (userinfo.get('name') or '').strip()
        or (userinfo.get('email') or '').split('@')[0]
        or 'Google User'
    )

    session.pop('gov_role', None)
    session.pop('ngo_role', None)
    session['user']         = name
    session['google_email'] = userinfo.get('email', '')

    # Issue JWT for Google OAuth users (citizen role)
    from domain.models import AuthUser, UserRole
    google_user = AuthUser(user_id=name, name=name, role=UserRole.CITIZEN)
    access_token  = auth_service.create_access_token(google_user)
    refresh_token = auth_service.create_refresh_token(name)

    print(f'[google_oauth] ✓ signed in: {name} <{session["google_email"]}>')
    resp = redirect(url_for('home'))
    _set_jwt_cookies(resp, access_token, refresh_token)
    return resp


# ═══════════════════════════════════════════════════════
#  ROUTES — ISSUE API
# ═══════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════
#  /issues — connection-efficient version (Phase 6 fix)
#  - Single bulk escalation UPDATE instead of N individual connection checkouts
#  - SLA annotation in pure Python (no DB calls)
# ═══════════════════════════════════════════════════════
@app.route('/issues')
def issues_api():
    tag          = (request.args.get('tag')    or '').strip() or None
    status       = (request.args.get('status') or '').strip() or None
    current_user_name = session.get('user') or ''
    try:
        issues = get_issues(tag=tag, status=status)
    except Exception as e:
        print(f"[issues_api] get_issues failed: {type(e).__name__}: {e}")
        return jsonify([])

    # ── Bulk escalation: collect overdue IDs then do ONE DB call ─────────────
    # Old approach: up to 5 separate connection checkouts inside the loop.
    # New approach: collect all overdue IDs, issue one UPDATE, zero extra connections.
    overdue_ids = []
    MAX_ESCALATIONS_PER_REQUEST = 5
    for i in issues:
        try:
            sla = calculate_sla(i)
            i.update(sla)
            if (sla['sla_state'] == 'overdue'
                    and not i.get('escalated')
                    and i.get('status') != 'resolved'
                    and len(overdue_ids) < MAX_ESCALATIONS_PER_REQUEST):
                overdue_ids.append(int(i.get('id')))
        except Exception as sla_err:
            print(f"[issues_api] SLA calc failed for id={i.get('id')}: {sla_err}")

    if overdue_ids:
        try:
            _bulk_escalate(overdue_ids)
            # Reflect escalation in the in-memory dicts we're about to return
            id_set = set(overdue_ids)
            for i in issues:
                if int(i.get('id', -1)) in id_set:
                    i['escalated'] = True
                    i['status']    = 'escalated'
        except Exception as esc_err:
            print(f"[issues_api] bulk escalate failed: {esc_err}")

    enriched = []
    for i in issues:
        # Tag user_actions so frontend knows what the current user already did
        upvoters = i.get('upvoters') or []
        actions = []
        if current_user_name and current_user_name in upvoters:
            actions.append('upvote')
        if i.get('is_verified') or i.get('verified'):
            actions.append('verify')
        if i.get('is_escalated') or i.get('escalated'):
            actions.append('escalate')
        i['user_actions'] = actions
        enriched.append(i)

    # ── Strip sensitive / heavy fields from public response ───────────────────
    is_gov = bool(session.get('gov_role'))
    for i in enriched:
        if not is_gov:
            i.pop('contact', None)
        i.pop('image', None)      # base64 images → 2MB → stripped here
        i.pop('image_hash', None)

    return jsonify(enriched)


@app.route('/report', methods=['POST'])
def report_api():
    """
    Citizen report submission — thin HTTP adapter.
    All business logic lives in services/issue_service.py.
    Phase 1: route parses request → builds ReportSubmission → calls service.
    """
    # ── Parse form fields ────────────────────────────────────────────────────
    user        = (request.form.get('user')        or 'anonymous').strip() or 'anonymous'
    description = (request.form.get('description') or '').strip()
    area        = (request.form.get('area')        or 'Delhi').strip()
    severity    = (request.form.get('severity')    or 'medium').strip()
    landmark    = (request.form.get('landmark')    or '').strip()
    contact     = (request.form.get('contact')     or '').strip()

    try:
        lat = float(request.form.get('lat')) if request.form.get('lat') else None
        lng = float(request.form.get('lng')) if request.form.get('lng') else None
    except (TypeError, ValueError):
        lat = lng = None

    # ── Extract image ────────────────────────────────────────────────────────
    image_b64  = None
    image_data = None
    image_mime = 'image/jpeg'
    if 'image' in request.files:
        f = request.files['image']
        if f and f.filename:
            raw = f.read()
            if 0 < len(raw) < 10 * 1024 * 1024:
                image_mime  = f.mimetype or 'image/jpeg'
                image_b64   = base64.b64encode(raw).decode()
                image_data  = f'data:{image_mime};base64,{image_b64}'

    # ── Delegate to service ──────────────────────────────────────────────────
    submission = ReportSubmission(
        user        = user,
        description = description,
        area        = area,
        severity    = severity,
        landmark    = landmark,
        contact     = contact,
        lat         = lat,
        lng         = lng,
        image_b64   = image_b64,
        image_data  = image_data,
        image_mime  = image_mime,
    )

    # ── Phase 3: validate before service call ────────────────────────────────
    errors = _validate_submission(submission)
    if errors:
        return jsonify({'error': errors[0], 'errors': errors}), 400

    result = issue_service.submit_report(submission)
    return jsonify(result.data), result.status_code



# ═══════════════════════════════════════════════════════
#  POSTGRES & FIRESTORE HEALTH CHECK (renamed to db-health for generality)
# ═══════════════════════════════════════════════════════

@app.route('/api/health/db')
def api_health_db():
    """Lightweight PostgreSQL health check.
    Used by UptimeRobot to keep the Neon database warm."""
    from database import _state
    import traceback
    print("\n" + "=" * 70)
    print("[HEALTH] Starting PostgreSQL health check")
    print("=" * 70)
    print(f"[HEALTH] Mode              : {_state.get('mode')}")
    print(f"[HEALTH] Pool Exists       : {_state.get('pg_pool') is not None}")
    if _state.get('mode') != 'postgres':
        print("[HEALTH] Not running in PostgreSQL mode")
        print("=" * 70)
        return jsonify({"ok": True, "mode": _state.get("mode", "unknown"),
                        "message": "Not using PostgreSQL"}), 200
    if not _state.get('pg_pool'):
        print("[HEALTH] PostgreSQL pool is None")
        print("=" * 70)
        return jsonify({"ok": False, "error": "PostgreSQL pool not initialized"}), 500
    try:
        t0 = time.time()
        print("[HEALTH] Requesting connection from pool...")
        with _state['pg_pool'].connection() as conn:
            print("[HEALTH] ✓ Connection acquired")
            print(f"[HEALTH] Connection object : {conn}")
            with conn.cursor() as cur:
                print("[HEALTH] ✓ Cursor opened")
                print("[HEALTH] Executing SELECT 1 ...")
                cur.execute("SELECT 1")
                result = cur.fetchone()
                print(f"[HEALTH] Query Result : {result}")
        elapsed = round((time.time() - t0) * 1000, 2)
        print(f"[HEALTH] Roundtrip : {elapsed} ms")
        print("[HEALTH] SUCCESS")
        print("=" * 70)
        return jsonify({"ok": True, "mode": "postgres",
                        "roundtrip_ms": elapsed, "result": result[0]}), 200
    except Exception as e:
        print("\n" + "!" * 70)
        print("[HEALTH] DATABASE HEALTH CHECK FAILED")
        print("!" * 70)
        print(f"Exception Type : {type(e).__name__}")
        print(f"Exception      : {e}")
        print("\nFull Traceback:\n")
        traceback.print_exc()
        print("!" * 70)
        return jsonify({"ok": False, "mode": "postgres",
                        "exception_type": type(e).__name__, "error": str(e)}), 500


@app.route('/api/health/firestore')
def firestore_health():
    """Round-trip DB write+read test, plus counts of all collections."""
    from database import _state
    import time as _t
    info = {
        'mode': _state.get('mode', 'unknown'),
        'firestore_client': bool(_state.get('fs_db')),
        'postgres_pool': bool(_state.get('pg_pool')),
    }
    if _state.get('mode') == 'postgres':
        try:
            t0 = _t.time()
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            info['roundtrip_ms'] = round((_t.time() - t0) * 1000, 1)
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM issues")
                    info['issues_count'] = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM spam_issues")
                    info['spam_issues_count'] = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM ngos")
                    info['ngos_count'] = cur.fetchone()[0]
            info['status'] = 'ok'
            return jsonify(info), 200
        except Exception as e:
            info['status'] = 'error'
            info['error']  = f'{type(e).__name__}: {e}'
            return jsonify(info), 500

    if _state.get('mode') != 'firebase':
        info['status'] = 'in_memory_only'
        info['message'] = 'Firebase not configured — running in memory mode'
        return jsonify(info), 200

    try:
        t0 = _t.time()
        test_ref = _state['fs_db'].collection('_health').document('ping')
        test_ref.set({'ts': _t.time(), 'test': 'ok'})
        snap = test_ref.get()
        info['write_ok']     = True
        info['read_ok']      = snap.exists
        info['roundtrip_ms'] = round((_t.time() - t0) * 1000, 1)

        info['issues_count']            = len(list(_state['fs_db'].collection('issues').limit(500).stream()))
        info['spam_issues_count']       = len(list(_state['fs_db'].collection('spam_issues').limit(500).stream()))
        info['duplicate_reports_count'] = len(list(_state['fs_db'].collection('duplicate_reports').limit(500).stream()))
        info['ngos_count']              = len(list(_state['fs_db'].collection('ngos').limit(500).stream()))
        info['status'] = 'ok'
        return jsonify(info), 200
    except Exception as e:
        info['status'] = 'error'
        info['error']  = f'{type(e).__name__}: {e}'
        return jsonify(info), 500

@app.route('/upvote/<int:issue_id>', methods=['POST'])
def upvote_api(issue_id):
    data = request.get_json(silent=True) or {}
    user = (data.get('user') or 'anonymous').strip() or 'anonymous'
    action = upvote_issue(issue_id, user)

    # ────── Feature 6: CROWD-ESCALATION ──────
    # After upvote, check if total upvotes crossed the threshold → auto-escalate
    escalated_now = False
    if action == 'added':
        issue = get_issue_by_id(issue_id)
        if issue and not issue.get('escalated') and issue.get('upvotes', 0) >= CROWD_ESCALATION_THRESHOLD:
            escalated_now = escalate_issue(issue_id, reason='crowd_consensus')

    return jsonify({
        'status': 'ok',
        'action': action,
        'escalated_now': escalated_now,
    })


@app.route('/areas')
def areas_api():
    return jsonify(get_areas())


# ═══════════════════════════════════════════════════════
#  GOVERNMENT DASHBOARD — moved to external portal
#  HTML routes redirect to problem-map-ai-d677.onrender.com
#  Data API routes (/gov/all, /gov/update-status) remain here
#  as the external portal calls this backend as its data source.
# ═══════════════════════════════════════════════════════
_GOV_PORTAL = 'https://problem-map-ai-d677.onrender.com'


@app.route('/gov')
def gov_dashboard():
    """Redirects to the external government portal."""
    return redirect(_GOV_PORTAL + '/login', 301)


@app.route('/gov/update-status/<int:issue_id>', methods=['POST'])
def gov_update_status(issue_id):
    """
    Officer changes an issue's status — thin HTTP adapter.
    Called by the external gov portal as a data API.
    Phase 1 service layer wiring preserved.
    """
    gov = session.get('gov_role')
    if not gov:
        return jsonify({'error': 'Not authorised'}), 401

    data       = request.get_json(silent=True) or {}
    new_status = (data.get('status') or '').lower().strip()
    note       = (data.get('note') or '').strip()

    result = issue_service.update_status(issue_id, new_status, gov, note)
    return jsonify(result.data), result.status_code


# ═══════════════════════════════════════════════════════
#  PUBLIC STATS PAGE (Feature 8)
# ═══════════════════════════════════════════════════════
@app.route('/stats')
def public_stats():
    """Anonymous, read-only metrics dashboard. No login required."""
    issues = get_issues(limit=500)
    now = time.time()

    by_tag = {}
    by_area = {}
    by_severity = {'high': 0, 'medium': 0, 'low': 0}
    by_status = {'open': 0, 'acknowledged': 0, 'in_progress': 0, 'resolved': 0, 'escalated': 0}
    resolution_durations_hr = []
    overdue_count = 0
    last_7d_count = 0

    for i in issues:
        tag  = i.get('tag') or 'other'
        area = i.get('area') or 'Delhi'
        sev  = i.get('severity') or 'medium'
        stat = i.get('status') or 'open'

        by_tag[tag] = by_tag.get(tag, 0) + 1
        by_area[area] = by_area.get(area, 0) + 1
        if sev in by_severity:  by_severity[sev]  += 1
        if stat in by_status:   by_status[stat]   += 1

        sla = calculate_sla(i)
        if sla['sla_state'] == 'overdue': overdue_count += 1

        ts = i.get('timestamp') or now
        if now - ts < 7 * 86400: last_7d_count += 1

        if stat == 'resolved' and i.get('resolved_at') and ts:
            hours = (i['resolved_at'] - ts) / 3600
            if 0 < hours < 30 * 24:
                resolution_durations_hr.append(hours)

    total = len(issues)
    resolved = by_status['resolved']
    resolution_rate = round(resolved / total * 100, 1) if total else 0
    avg_resolution_hr = round(sum(resolution_durations_hr) / len(resolution_durations_hr), 1) \
                          if resolution_durations_hr else None
    sla_breach_rate = round(overdue_count / total * 100, 1) if total else 0

    top_areas = sorted(by_area.items(), key=lambda x: -x[1])[:10]
    tag_list  = sorted(by_tag.items(),  key=lambda x: -x[1])

    # By-department resolution rate
    dept_perf = []
    for tag, count in tag_list:
        dept_resolved = sum(1 for i in issues if i.get('tag') == tag and i.get('status') == 'resolved')
        dept_perf.append({
            'tag':           tag,
            'total':         count,
            'resolved':      dept_resolved,
            'resolution_rate': round(dept_resolved / count * 100, 1) if count else 0,
        })

    return render_template('stats.html',
        total=total,
        resolved=resolved,
        overdue=overdue_count,
        last_7d=last_7d_count,
        resolution_rate=resolution_rate,
        avg_resolution_hr=avg_resolution_hr,
        sla_breach_rate=sla_breach_rate,
        by_severity=by_severity,
        by_status=by_status,
        tag_list=tag_list,
        top_areas=top_areas,
        dept_perf=dept_perf,
        max_tag_count=max((c for _, c in tag_list), default=1),
        max_area_count=max((c for _, c in top_areas), default=1),
    )


# ═══════════════════════════════════════════════════════
#  ROUTES — NGO API
# ═══════════════════════════════════════════════════════

@app.route('/ngo/all')
def ngo_all_api():
    return jsonify({'ngos': get_all_ngos()})


@app.route('/ngo/nearby')
def ngo_nearby_api():
    try:
        lat = float(request.args.get('lat', 28.6139))
        lng = float(request.args.get('lng', 77.2090))
    except (TypeError, ValueError):
        lat, lng = 28.6139, 77.2090
    tag = (request.args.get('tag') or '').strip() or None
    return jsonify({'ngos': get_nearby_ngos(lat, lng, tag, limit=5)})


# ═══════════════════════════════════════════════════════
#  /api/authority/<tag> — MERGED (Change 4)
# ═══════════════════════════════════════════════════════
@app.route('/api/authority/<tag>')
def authority_for_tag(tag):
    """Return the AI-matched government authority for a given issue tag.
    Used by my_issues.html detail modal."""
    try:
        return jsonify(ai_engine.get_authority(tag or 'other') or {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════
#  ROUTES — AI
# ═══════════════════════════════════════════════════════

@app.route('/ai/analyze-image', methods=['POST'])
def ai_analyze_image():
    """Groq Llama-4-Scout vision → classifies civic issue from photo."""
    data = request.get_json(silent=True) or {}
    b64  = (data.get('image') or '').strip()
    mime = data.get('mime_type', 'image/jpeg')
    if not b64:
        return jsonify({'error': 'No image provided'}), 400
    result = ai_engine.analyze_image(b64, mime)
    if 'error' in result:
        return jsonify(result), 500 if result.get('_status') == 'server_error' else 503
    return jsonify(result)


@app.route('/ai/ask', methods=['POST'])
def ai_ask():
    """Free-form Q&A about Delhi civic issues."""
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'No question provided'}), 400
    context_issues = get_issues(limit=20)
    answer = ai_engine.ask_question(question, context_issues)
    return jsonify(answer)


@app.route('/ai/insights')
def ai_insights():
    """Summary stats + AI commentary on the current issue landscape."""
    issues = get_issues(limit=200)
    by_tag = {}
    by_severity = {'high': 0, 'medium': 0, 'low': 0}
    by_status = {}
    for i in issues:
        by_tag[i.get('tag', 'other')] = by_tag.get(i.get('tag', 'other'), 0) + 1
        sev = i.get('severity', 'medium')
        if sev in by_severity: by_severity[sev] += 1
        st = i.get('status', 'open')
        by_status[st] = by_status.get(st, 0) + 1
    return jsonify({
        'total': len(issues),
        'by_tag': by_tag,
        'by_severity': by_severity,
        'by_status': by_status,
        'ai_summary': ai_engine.summarize_landscape(by_tag, by_severity, by_status) if ai_engine.is_available() else None,
    })


@app.route('/ai/health')
def ai_health():
    return jsonify({
        'ai_available':    ai_engine.is_available(),
        'email_available': email_sender.is_available(),
        'provider':        ai_engine.provider_name(),
        'model':           ai_engine.model_name(),
    })


# ═══════════════════════════════════════════════════════
#  ROUTES — COMPLAINT LETTER + EMAIL
# ═══════════════════════════════════════════════════════

def _find_issue(issue_id):
    """Locate a single issue by id from current store."""
    for i in get_issues(limit=500):
        if int(i.get('id', -1)) == int(issue_id):
            return i
    return None


@app.route('/ai/draft-dispatch/<int:issue_id>', methods=['GET', 'POST'])
def ai_draft_dispatch(issue_id):
    """
    Flat format consumed by issues.html and my_issues.html JS:
      {llm_drafted, recipient_name, recipient_email, recipient_phone, subject, body}
    """
    issue = _find_issue(issue_id)
    if not issue:
        return jsonify({'error': f'Issue #{issue_id} not found'}), 404

    data     = request.get_json(silent=True) or {}
    citizen  = (data.get('citizen_name') or data.get('citizen') or
                session.get('user') or issue.get('user') or 'Concerned Citizen').strip()
    language = (data.get('language') or 'english').strip().lower()

    drafted   = ai_engine.draft_complaint(issue, citizen_name=citizen, language=language)
    authority = drafted.get('authority') or ai_engine.get_authority(issue.get('tag', 'other'))
    source    = (drafted.get('source') or '').lower()

    return jsonify({
        'llm_drafted':     ('groq' in source) or ('llama' in source),
        'recipient_name':  authority.get('name',  'Local Authority'),
        'recipient_email': authority.get('email', ''),
        'recipient_phone': authority.get('phone', ''),
        'subject':         drafted.get('subject', ''),
        'body':            drafted.get('body_text', '') or drafted.get('body_html', ''),
        'source':          source or 'template',
    })


@app.route('/ai/draft-complaint/<int:issue_id>', methods=['GET', 'POST'])
def ai_draft_complaint(issue_id):
    """Generate a formal complaint letter for an issue."""
    issue = _find_issue(issue_id)
    if not issue:
        return jsonify({'error': f'Issue #{issue_id} not found'}), 404

    data = request.get_json(silent=True) or {}
    citizen = (data.get('citizen_name') or session.get('user') or issue.get('user') or 'Concerned Citizen').strip()
    language = (data.get('language') or 'english').strip().lower()

    drafted = ai_engine.draft_complaint(issue, citizen_name=citizen, language=language)
    return jsonify({
        'status':   'ok',
        'issue_id': issue_id,
        'subject':  drafted['subject'],
        'body_text': drafted['body_text'],
        'body_html': drafted['body_html'],
        'authority': drafted['authority'],
        'source':   drafted.get('source', 'template'),
    })


# ────── Feature 5: PDF EXPORT (print-to-PDF route) ──────
@app.route('/complaint-print/<int:issue_id>')
def complaint_print(issue_id):
    """
    Returns a print-optimized HTML page of the complaint letter.
    User opens it in a new tab → browser print dialog → Save as PDF.
    """
    issue = _find_issue(issue_id)
    if not issue:
        return f"<h1>Issue #{issue_id} not found</h1>", 404

    citizen = session.get('user') or issue.get('user') or 'Concerned Citizen'
    drafted = ai_engine.draft_complaint(issue, citizen_name=citizen)
    sla = calculate_sla(issue)

    return render_template('complaint_print.html',
        issue=issue,
        subject=drafted['subject'],
        body_text=drafted['body_text'],
        authority=drafted['authority'],
        citizen=citizen,
        sla=sla,
        today=time.strftime('%d %B %Y'),
    )


@app.route('/email/send-complaint/<int:issue_id>', methods=['POST'])
def email_send_complaint(issue_id):
    """Dispatch the complaint letter via Resend."""
    if not email_sender.is_available():
        return jsonify({'error': 'Email not configured. Set RESEND_API_KEY in .env.'}), 503

    issue = _find_issue(issue_id)
    if not issue:
        return jsonify({'error': f'Issue #{issue_id} not found'}), 404

    data = request.get_json(silent=True) or {}
    subject   = (data.get('subject')   or '').strip()
    body_html = (data.get('body_html') or '').strip()
    body_text = (data.get('body_text') or '').strip()
    to_email  = (data.get('to_email')  or '').strip()
    reply_to  = (data.get('reply_to')  or '').strip() or None

    if not subject or (not body_html and not body_text):
        return jsonify({'error': 'Subject and body are required'}), 400

    if not to_email:
        authority = ai_engine.get_authority(issue.get('tag', 'other'))
        to_email = authority['email']

    # Demo override
    demo_override = os.environ.get('DEMO_RECIPIENT_EMAIL', '').strip()
    if demo_override:
        original_to = to_email
        to_email = demo_override
        if not subject.startswith('[DEMO'):
            subject = f'[DEMO → {original_to}] {subject}'

    if body_html and not body_text:
        body_text = body_html

    # Attach issue photo if present
    attachments = []
    image = issue.get('image') or ''
    if image.startswith('data:'):
        try:
            header, b64 = image.split(',', 1)
            mime = header.split(';')[0].replace('data:', '')
            ext = 'jpg' if 'jpeg' in mime else (mime.split('/')[-1] or 'jpg')
            attachments.append({
                'filename':     f'issue_{issue_id}.{ext}',
                'content_b64':  b64,
                'content_type': mime,
            })
        except Exception:
            pass

    result = email_sender.send_complaint(
        to_email=to_email, subject=subject,
        body_html=body_html or f'<pre>{body_text}</pre>',
        body_text=body_text or None,
        attachments=attachments or None,
        reply_to=reply_to,
    )

    if result.get('error'):
        status = 503 if result.get('_status') == 'not_configured' else 500
        return jsonify(result), status

    return jsonify({
        'status':     'ok',
        'message_id': result.get('id', ''),
        'to':         to_email,
        'subject':    subject,
    })


def _reverse_geocode(lat, lng):
    try:
        import urllib.request as _ureq
        url = f'https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lng}&zoom=14'
        req = _ureq.Request(url, headers={'User-Agent': 'AreaPulse/1.0'})
        with _ureq.urlopen(req, timeout=6) as r:
            addr = json.loads(r.read()).get('address', {})
        return (addr.get('suburb') or addr.get('neighbourhood') or
                addr.get('city_district') or addr.get('town') or 'Delhi')
    except Exception:
        return 'Delhi'



# ═══════════════════════════════════════════════════════
#  ROUTES — WHATSAPP INBOUND BOT (Twilio webhook)
# ═══════════════════════════════════════════════════════
_WA_SESSIONS: dict = {}   # phone → session data
_WA_TTL      = 600        # session timeout in seconds

_SEV_EMOJI = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}
_TAG_EMOJI = {
    'pothole': '🕳', 'garbage': '🗑', 'water': '💧',
    'streetlight': '💡', 'sewage': '🚧', 'electricity': '⚡',
    'traffic': '🚦', 'tree': '🌳', 'noise': '📢', 'other': '⚠️',
}


def _wa_prune():
    now = time.time()
    for k in [k for k, v in _WA_SESSIONS.items() if now - v.get('ts', 0) > _WA_TTL]:
        del _WA_SESSIONS[k]


def _wa_download_image(url):
    """Use requests so Authorization header survives the Twilio CDN redirect."""
    import requests as _rq
    sid   = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
    token = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
    auth  = (sid, token) if (sid and token) else None
    resp  = _rq.get(url, auth=auth, timeout=20, allow_redirects=True)
    print(f'[wa_dl] HTTP {resp.status_code} · {len(resp.content)} bytes · auth={"yes" if auth else "NO"}')
    resp.raise_for_status()
    ct = resp.headers.get('content-type', 'image/jpeg').split(';')[0].strip()
    return resp.content, ct


def _wa_extract_issue(result):
    """Normalise both old single-dict and new issues-array AI response."""
    if not isinstance(result, dict):
        return None
    if 'issues' in result:
        arr = result['issues']
        return arr[0] if arr else None
    if result.get('tag') or result.get('issue_type') or result.get('category'):
        return result
    return None


@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    _wa_prune()

    from_num  = request.form.get('From', '')
    body      = (request.form.get('Body') or '').strip()
    num_media = int(request.form.get('NumMedia', 0))
    media_url = request.form.get('MediaUrl0', '')
    lat_str   = request.form.get('Latitude', '')
    lng_str   = request.form.get('Longitude', '')
    phone     = from_num.replace('whatsapp:', '')
    sess      = _WA_SESSIONS.get(from_num, {})

    # ── 1. LOCATION PIN ───────────────────────────────
    if lat_str and lng_str:
        try:
            lat, lng = float(lat_str), float(lng_str)
            if sess.get('state') == 'AWAITING_CONFIRM':
                sess['pending'].update({'lat': lat, 'lng': lng,
                                        'area': _reverse_geocode(lat, lng)})
                sess['ts'] = time.time()
                _WA_SESSIONS[from_num] = sess
                return _wa_twiml(
                    "📍 Location saved!\n\nReply *YES* to submit or *NO* to cancel."
                )
        except Exception:
            pass

    # ── 2. PHOTO ──────────────────────────────────────
    if num_media > 0 and media_url:
        try:
            img_bytes, mime = _wa_download_image(media_url)
            img_b64         = base64.b64encode(img_bytes).decode()
            ai_result       = ai_engine.analyze_image(img_b64, mime=mime)
            issue           = _wa_extract_issue(ai_result)

            if not issue:
                return _wa_twiml(
                    "🔍 I couldn't identify a clear civic issue in this photo.\n\n"
                    "Please send a clearer image (pothole, garbage, broken light, etc.)."
                )

            tag       = (issue.get('tag') or issue.get('issue_type') or 'other').lower()
            severity  = (issue.get('severity') or 'medium').lower()
            desc      = (issue.get('improved_description') or
                         issue.get('description') or
                         issue.get('summary') or
                         f'{tag.title()} issue detected').strip()
            authority = (issue.get('suggested_authority') or
                         issue.get('recommended_authority') or 'MCD')
            confidence = issue.get('confidence') or issue.get('confidence_score') or 0

            _WA_SESSIONS[from_num] = {
                'state':   'AWAITING_CONFIRM',
                'ts':      time.time(),
                'img':     f'data:{mime};base64,{img_b64}',
                'pending': {
                    'user':        phone,
                    'area':        'Delhi',
                    'description': desc,
                    'tag':         tag,
                    'severity':    severity,
                    'lat':         None,
                    'lng':         None,
                },
            }

            conf_txt = f" ({confidence}% confidence)" if confidence else ""
            te = _TAG_EMOJI.get(tag, '⚠️')
            se = _SEV_EMOJI.get(severity, '🟡')

            return _wa_twiml(
                f"{te} *{tag.replace('_',' ').title()} Detected*{conf_txt}\n\n"
                f"{se} Severity: *{severity.upper()}*\n"
                f"🏛 Authority: {authority}\n\n"
                f"_{desc[:140]}{'…' if len(desc) > 140 else ''}_\n\n"
                f"Reply *YES* to submit ✅\n"
                f"Reply *NO* to cancel ❌\n"
                f"Or share your 📍 *location pin* for precise GPS"
            )

        except Exception as e:
            print(f"[WhatsApp] Image error: {e}")
            import traceback; traceback.print_exc()
            return _wa_twiml("❌ Trouble analyzing that image. Please try again.")

    # ── 3. TEXT COMMANDS ──────────────────────────────
    bl = body.lower()

    # YES → submit
    if bl in ('yes', 'y', 'yeah', 'ha', 'haan', 'ok', 'okay', 'submit', 'confirm', '✅'):
        if sess.get('state') == 'AWAITING_CONFIRM':
            p = sess['pending']
            try:
                lat = p.get('lat') or AREA_COORDS.get(p.get('area', ''), [28.6139, 77.2090])[0]
                lng = p.get('lng') or AREA_COORDS.get(p.get('area', ''), [28.6139, 77.2090])[1]

                issue_id = insert_issue(
                    user        = p.get('user', phone),
                    area        = p.get('area', 'Delhi'),
                    description = p['description'],
                    severity    = p.get('severity', 'medium'),
                    tag         = p['tag'],
                    landmark    = '',
                    contact     = phone,
                    lat         = lat,
                    lng         = lng,
                    image       = sess.get('img'),
                )
                del _WA_SESSIONS[from_num]

                base_url = os.environ.get('AREAPULSE_URL', 'https://areapulse.onrender.com')
                te = _TAG_EMOJI.get(p['tag'], '⚠️')
                se = _SEV_EMOJI.get(p.get('severity'), '🟡')
                return _wa_twiml(
                    f"✅ *Issue #{issue_id} Reported!*\n\n"
                    f"{te} {p['tag'].title()}  {se} {p.get('severity','medium').title()}\n"
                    f"📍 {p['area']}\n\n"
                    f"🗺 Track all issues:\n{base_url}/issues-all\n\n"
                    f"Thank you for making Delhi better! 🙏"
                )
            except Exception as e:
                print(f"[WhatsApp] Insert error: {e}")
                return _wa_twiml("❌ Error saving report. Please try again.")
        return _wa_twiml("Please send a *photo* first, then reply YES to confirm.")

    # NO → cancel
    if bl in ('no', 'n', 'cancel', 'nahi', 'nope', '❌'):
        _WA_SESSIONS.pop(from_num, None)
        return _wa_twiml("❌ Cancelled. Send a new photo anytime to report an issue.")

    # Greeting / help
    if any(w in bl for w in ('hi', 'hello', 'hey', 'start', 'help', 'helo',
                              'namaste', 'namaskar', 'menu')):
        base_url = os.environ.get('AREAPULSE_URL',
                                  'https://areapulse.onrender.com')
        return _wa_twiml(
            f"👋 *Welcome to AreaPulse!*\n\n"
            f"Report civic issues in Delhi instantly.\n\n"
            f"📸 Just *send a photo* of any problem:\n"
            f"  🕳 Pothole  🗑 Garbage  💧 Water leak\n"
            f"  💡 Broken light  🚧 Sewage  ⚡ Electrical\n\n"
            f"Our AI identifies it and routes it to the right authority automatically. "
            f"No forms. No apps. No login.\n\n"
            f"🗺 View all issues: {base_url}"
        )

    # Pending issue reminder
    if sess.get('state') == 'AWAITING_CONFIRM':
        p = sess['pending']
        return _wa_twiml(
            f"Waiting for your confirmation.\n\n"
            f"Detected: *{p['tag'].title()}* ({p.get('severity','medium')} severity)\n\n"
            f"Reply *YES* to submit or *NO* to cancel."
        )

    # Fallback
    return _wa_twiml(
        "📸 Send me a *photo* of a civic issue (pothole, garbage, broken light, etc.) "
        "and I'll report it automatically!\n\nType *hi* for help."
    )


@app.route('/whatsapp/status', methods=['POST'])
def whatsapp_status():
    """Twilio delivery status callback — just acknowledge."""
    return '', 204


# ═══════════════════════════════════════════════════════════
#  Navigation pages — rendered via base.html
# ═══════════════════════════════════════════════════════════
def _common_ctx():
    """Shared context passed into every navigation template."""
    return dict(
        current_user=session.get("user"),
        wa_number=(os.environ.get("TWILIO_WHATSAPP_NUMBER") or "").replace("whatsapp:+", "").replace("whatsapp:", "").replace("+", ""),
        wa_join_code=os.environ.get("TWILIO_SANDBOX_CODE") or "",
        maptiler_key=MAPTILER_KEY,
        maptiler_style=MAPTILER_STYLE,
    )


@app.route("/issues-all")
def issues_all_page():
    return render_template("issues.html", **_common_ctx())


@app.route("/my-reports")
def my_reports_page():
    return render_template("my_issues.html", **_common_ctx())


@app.route("/community")
def community_page():
    return render_template("community.html", **_common_ctx())


# ── Govt authority map — consumed by NGO page "Govt Agencies" tab ──────────
_GOV_LOCATIONS = {
    'pothole':     (28.6131, 77.2295, 'ITO'),
    'water':       (28.6304, 77.2177, 'Civil Lines'),
    'garbage':     (28.6517, 77.2219, 'Chandni Chowk'),
    'streetlight': (28.6517, 77.2219, 'Chandni Chowk'),
    'traffic':     (28.6275, 77.2410, 'ITO'),
    'noise':       (28.6304, 77.2050, 'Civil Lines'),
    'sewage':      (28.6210, 77.2090, 'New Delhi'),
    'electricity': (28.5274, 77.2497, 'Nehru Place'),
    'tree':        (28.5494, 77.2001, 'Hauz Khas'),
    'other':       (28.6139, 77.2090, 'Connaught Place'),
}
_GOV_ICONS = {
    'pothole': '🛣', 'water': '💧', 'garbage': '🗑',
    'streetlight': '💡', 'traffic': '🚦', 'noise': '🔊',
    'sewage': '🚧', 'electricity': '⚡', 'tree': '🌳', 'other': '🏛',
}

@app.route('/gov/all')
def gov_all_api():
    """Govt authority list for NGO page 'Govt Agencies' tab."""
    tag_filter = (request.args.get('tag') or '').strip().lower()
    results = []
    for tag, info in ai_engine._AUTHORITY_MAP.items():
        if tag_filter and tag != tag_filter:
            continue
        loc = _GOV_LOCATIONS.get(tag, _GOV_LOCATIONS['other'])
        results.append({
            'name':            info.get('name', ''),
            'email':           info.get('email', ''),
            'phone':           info.get('phone', ''),
            'tag':             tag,
            'focus':           f"{tag.replace('_',' ').title()} issues · Govt of Delhi",
            'department':      info.get('name', ''),
            'area':            loc[2],
            'lat':             loc[0],
            'lng':             loc[1],
            'icon':            _GOV_ICONS.get(tag, '🏛'),
            'rating':          4.0,
            'issues_resolved': 0,
        })
    return jsonify(results)


# ── data endpoints used by sub-pages ──
@app.route("/my-issues-data")
def my_issues_data():
    user = (request.args.get("user") or "").strip().lower()
    if not user:
        return jsonify([])
    all_issues = get_issues()
    mine = [i for i in all_issues if (i.get("user") or "").strip().lower() == user]
    enriched = []
    for i in mine:
        try:
            i.update(calculate_sla(i))
        except Exception:
            pass
        i.pop('image', None)   # strip base64 — not needed in list view, reduces 1MB to ~30KB
        enriched.append(i)
    enriched.sort(key=lambda i: i.get("timestamp") or 0, reverse=True)
    return jsonify(enriched)


@app.route("/user/stats")
def user_stats():
    name = (request.args.get("name") or "").strip().lower()
    if not name:
        return jsonify({"total_reported": 0, "total_resolved": 0, "points": 0})
    mine = [i for i in get_issues() if (i.get("user") or "").strip().lower() == name]
    resolved = sum(1 for i in mine if i.get("status") == "resolved")
    # 5 points per report + 10 bonus per resolved
    points = len(mine) * 5 + resolved * 10
    return jsonify({
        "total_reported": len(mine),
        "total_resolved": resolved,
        "points": points,
    })


@app.route("/issue/<int:issue_id>/detail")
def issue_detail(issue_id):
    """Returns {issue, timeline, matched_agency, nearby_ngos, maps_link} for my_issues.html."""
    try:
        issue = get_issue_by_id(issue_id)
        if not issue:
            issue = next((i for i in get_issues() if int(i.get("id") or 0) == issue_id), None)
        if not issue:
            return jsonify({"error": "issue not found"}), 404
        try:
            issue.update(calculate_sla(issue))
        except Exception:
            pass

        # ── Timeline (4 steps) ──────────────────────────────────────────────
        status       = issue.get("status", "open")
        is_verified  = bool(issue.get("is_verified") or issue.get("verified"))
        is_escalated = bool(issue.get("is_escalated") or issue.get("escalated"))
        is_resolved  = (status == "resolved") or bool(issue.get("resolved"))

        def _step(key):
            if key == "open":      return "done"
            if key == "verified":  return "done" if is_verified  else ("active" if status in ("open","acknowledged") else "pending")
            if key == "escalated": return "done" if is_escalated else ("active" if is_verified and not is_resolved else "pending")
            return                         "done" if is_resolved  else ("active" if is_escalated else "pending")

        timeline = [
            {"key": "open",      "label": "Reported",  "desc": "Issue submitted",        "state": _step("open")},
            {"key": "verified",  "label": "Verified",  "desc": "Confirmed by community", "state": _step("verified")},
            {"key": "escalated", "label": "Escalated", "desc": "Forwarded to authority", "state": _step("escalated")},
            {"key": "resolved",  "label": "Resolved",  "desc": "Issue fixed",            "state": _step("resolved")},
        ]

        # ── Matched authority ───────────────────────────────────────────────
        tag = issue.get("tag", "other")
        try:
            matched_agency = ai_engine.get_authority(tag)
        except Exception:
            matched_agency = {}

        # ── Nearby NGOs ─────────────────────────────────────────────────────
        lat, lng = issue.get("lat"), issue.get("lng")
        try:
            nearby_ngos = get_nearby_ngos(float(lat), float(lng), tag, limit=5) if (lat and lng) else []
        except Exception:
            nearby_ngos = []

        # Scrub any non-JSON-serialisable types (e.g. sets from upvoters)
        def _scrub(d):
            if not isinstance(d, dict): return d
            return {k: (list(v) if isinstance(v, set) else v) for k, v in d.items()}

        return jsonify({
            "issue":          _scrub(issue),
            "timeline":       timeline,
            "matched_agency": _scrub(matched_agency),
            "nearby_ngos":    [_scrub(n) for n in nearby_ngos],
            "maps_link":      f"https://maps.google.com/?q={lat},{lng}" if (lat and lng) else None,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}", "issue_id": issue_id}), 500


@app.route('/verify/<int:issue_id>', methods=['POST'])
def verify_issue(issue_id):
    try:
        data           = request.get_json(silent=True) or {}
        user           = data.get('user', 'anonymous')
        if not session.get('gov_role') and not _require_admin():
            return jsonify({'error': 'Unauthorised'}), 403
        issue = get_issue_by_id(issue_id)
        if not issue:
            return jsonify({'error': 'Issue not found'}), 404
        current = bool(issue.get('is_verified', False))
        new_val = not current
        from database import _state
        if _state.get('mode') == 'postgres':
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE issues SET is_verified=%s, verified=%s, verified_by=%s WHERE id=%s",
                        (new_val, new_val, user if new_val else None, issue_id)
                    )
                conn.commit()
        elif _state.get('mode') == 'firebase':
            _state['fs_db'].collection('issues').document(str(issue_id)).update({
                'is_verified': new_val, 'verified': new_val,
                'verified_by': user if new_val else None,
            })
        else:
            issue['is_verified'] = new_val
            issue['verified']    = new_val
            issue['verified_by'] = user if new_val else None
        return jsonify({'status': 'ok', 'action': 'removed' if current else 'added'})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)[:120]}), 500


@app.route('/ngo/escalate/<int:issue_id>', methods=['POST'])
def escalate_issue_route(issue_id):
    try:
        issue = get_issue_by_id(issue_id)
        if not issue:
            return jsonify({'error': 'Issue not found'}), 404
        if not issue.get('is_verified', False) and not issue.get('is_escalated', False):
            return jsonify({
                'error':   'verification_required',
                'message': 'Please verify the issue before escalating it.'
            }), 400
        current = bool(issue.get('is_escalated', False))
        new_val = not current
        from database import _state
        if _state.get('mode') == 'postgres':
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE issues SET is_escalated=%s, escalated=%s, escalated_at=%s WHERE id=%s",
                        (new_val, new_val, time.time() if new_val else None, issue_id)
                    )
                conn.commit()
        elif _state.get('mode') == 'firebase':
            _state['fs_db'].collection('issues').document(str(issue_id)).update({
                'is_escalated': new_val, 'escalated': new_val,
            })
        else:
            issue['is_escalated'] = new_val
            issue['escalated']    = new_val
        return jsonify({'status': 'ok', 'action': 'removed' if current else 'added'})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)[:120]}), 500


import threading as _threading
_community_lock = _threading.Lock()
_community_posts = []
_community_likes = {}
_community_seq = [0]


def _community_seed():
    """Seed a few realistic community posts so the page isn't empty on first load."""
    import time as _t
    if _community_posts:
        return
    now = int(_t.time())
    seed = [
        ("RMC_admin",  "Garbage collection schedule for Karol Bagh updated — pickup at 7am daily.", "Karol Bagh",    "update",   now - 600),
        ("citizen42",  "Anyone else losing power in Dwarka Sector 14? Third outage this week.",   "Dwarka",        "question", now - 1800),
        ("RWA_Rohini", "ALERT: water tanker delayed. Expected by 4pm. Stay tuned.",                "Rohini",        "alert",    now - 3000),
        ("citizen99",  "Big pothole near Karol Bagh metro has been filled. Thanks RMC!",          "Karol Bagh",    "resolved", now - 7200),
        ("volunteerD", "Cleanup drive at Hauz Khas park this Saturday 7am. DM to join.",          "Hauz Khas",     "update",   now - 14400),
        ("citizenA",   "Streetlight repair team active in Lajpat Nagar block C. Working tonight.","Lajpat Nagar",  "update",   now - 28000),
    ]
    for user, msg, area, t, ts in seed:
        _community_seq[0] += 1
        _community_posts.append({
            "id": _community_seq[0],
            "user": user,
            "message": msg,
            "area": area,
            "post_type": t,
            "timestamp": ts,
            "likes": 0,
        })


@app.route("/community/posts")
def community_posts_api():
    _community_seed()
    area = (request.args.get("area") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 200))
    except Exception:
        limit = 50
    with _community_lock:
        posts = list(_community_posts)
    if area:
        posts = [p for p in posts if (p.get("area") or "").lower() == area.lower()]
    posts.sort(key=lambda p: p.get("timestamp") or 0, reverse=True)
    return jsonify(posts[:limit])


@app.route("/community/post", methods=["POST"])
def community_post_create():
    import time as _t
    data = request.get_json(silent=True) or {}
    user = (data.get("user") or "").strip()
    message = (data.get("message") or "").strip()
    area = (data.get("area") or "Delhi").strip()
    post_type = (data.get("type") or "update").strip()
    if not user:
        return jsonify({"error": "name required"}), 400
    if len(message) < 5:
        return jsonify({"error": "message too short"}), 400
    if post_type not in ("update", "question", "alert", "resolved"):
        post_type = "update"
    with _community_lock:
        _community_seq[0] += 1
        post = {
            "id": _community_seq[0],
            "user": user,
            "message": message,
            "area": area,
            "post_type": post_type,
            "timestamp": int(_t.time()),
            "likes": 0,
        }
        _community_posts.append(post)
    return jsonify({"status": "ok", "id": post["id"], "points_earned": 3})


@app.route("/community/like/<int:post_id>", methods=["POST"])
def community_like(post_id):
    data = request.get_json(silent=True) or {}
    user = (data.get("user") or "").strip()
    if not user:
        return jsonify({"error": "name required"}), 400
    with _community_lock:
        likers = _community_likes.setdefault(post_id, set())
        if user in likers:
            return jsonify({"error": "already liked"}), 409
        likers.add(user)
        for p in _community_posts:
            if p.get("id") == post_id:
                p["likes"] = len(likers)
                return jsonify({"status": "ok", "likes": p["likes"]})
    return jsonify({"error": "post not found"}), 404



# ═══════════════════════════════════════════════════════
#  NGO DASHBOARD ROUTES — MERGED (Change 5)
# ═══════════════════════════════════════════════════════

def _ap_compute_opportunities(ngo_tags, ngo_areas, all_issues, commitments):
    """Build opportunities grouped by (area, tag)."""
    from collections import defaultdict
    groups = defaultdict(list)
    for issue in all_issues:
        if issue.get('status') == 'resolved':
            continue
        tag = (issue.get('tag') or 'other').lower()
        area = issue.get('area') or 'Delhi'
        if ngo_tags and tag not in [t.lower() for t in ngo_tags]:
            continue
        groups[(area, tag)].append(issue)

    tag_titles = {
        'sewage': 'Sewage Crisis', 'water': 'Water Shortage',
        'garbage': 'Garbage Overflow', 'streetlight': 'Lighting Issues',
        'pothole': 'Road Damage', 'electricity': 'Power Issues',
        'tree': 'Tree Cover', 'traffic': 'Traffic Hazards',
        'noise': 'Noise Pollution', 'other': 'Civic Issues',
    }
    committed_keys = {(c['area'], c['tag']) for c in commitments}

    opportunities = []
    for (area, tag), issues_list in groups.items():
        if len(issues_list) < 2:
            continue
        title_suffix = tag_titles.get(tag, tag.title() + ' Issues')
        opportunities.append({
            'area': area, 'tag': tag,
            'title': area + ' ' + title_suffix,
            'issue_count': len(issues_list),
            'citizens_affected': len(issues_list) * 75,
            'ngos_active': 0,
            'committed_by_me': (area, tag) in committed_keys,
        })
    opportunities.sort(key=lambda x: x['issue_count'], reverse=True)
    return opportunities[:8]


@app.route('/ngo/dashboard')
def ngo_dashboard():
    """Redirects to the external NGO portal."""
    return redirect(_GOV_PORTAL + '/login', 301)


@app.route('/ngo/commit', methods=['POST'])
def ngo_commit():
    """
    NGO commitment endpoint — kept for external portal data API calls.
    Writes to _ngo_commitments_store (in-memory fallback) until Phase 7
    wires this to the ngo_commitments Postgres table.
    """
    ngo = session.get('ngo_role')
    if not ngo:
        return jsonify({'error': 'Not authorised'}), 401
    data = request.get_json(silent=True) or {}
    area = (data.get('area') or '').strip()
    tag = (data.get('tag') or '').strip().lower()
    if not area or not tag:
        return jsonify({'error': 'Both area and tag required'}), 400
    if tag not in [t.lower() for t in ngo.get('tags', [])]:
        return jsonify({'error': 'Your NGO focus does not include this category'}), 403
    for c in _ngo_commitments_store:
        if c['ngo_username'] == ngo['username'] and c['area'] == area and c['tag'] == tag:
            return jsonify({'status': 'already_committed', 'area': area, 'tag': tag})
    _ngo_commitments_store.append({
        'ngo_username': ngo['username'],
        'ngo_name': ngo['name'],
        'area': area, 'tag': tag,
        'title': area + ' ' + tag.title() + ' Initiative',
        'committed_at': time.time(),
    })
    return jsonify({'status': 'ok', 'area': area, 'tag': tag})


# ═══════════════════════════════════════════════════════
#  AI ROUTES (Groq) — redirects to external portal
# ═══════════════════════════════════════════════════════


@app.route('/gov/ai-triage')
def gov_ai_triage():
    """Redirects to external gov portal — AI triage lives there."""
    return redirect(_GOV_PORTAL + '/login', 301)


@app.route('/gov/ai-draft-response/<int:issue_id>', methods=['POST'])
def gov_ai_draft_response(issue_id):
    """Redirects to external gov portal — AI draft response lives there."""
    return redirect(_GOV_PORTAL + '/login', 301)


@app.route('/ngo/ai-recommendations')
def ngo_ai_recommendations():
    """Redirects to external NGO portal — AI recommendations live there."""
    return redirect(_GOV_PORTAL + '/login', 301)


# ═══════════════════════════════════════════════════════
#  ADMIN — BAN / STRIKE MANAGEMENT
# ═══════════════════════════════════════════════════════
admin_token = os.environ.get('ADMIN_TOKEN', '').strip()


def _require_admin() -> bool:
    """
    Return True if the request carries a valid, non-empty admin token.

    Phase 0 Fix 2: If ADMIN_TOKEN env var is not set (empty string), every
    request is rejected — there is no way to accidentally bypass auth by
    sending an empty header.  Previously an empty ADMIN_TOKEN would match
    any request that also sent an empty/missing token.
    """
    if not admin_token:
        # ADMIN_TOKEN not configured — refuse all admin access rather than
        # allow everything.  Operator must set ADMIN_TOKEN to use these routes.
        return False
    token = (
        request.headers.get('X-Admin-Token', '')
        or request.args.get('admin_token', '')
        or (request.get_json(silent=True) or {}).get('admin_token', '')
    ).strip()
    return bool(token) and token == admin_token


@app.route('/admin/strikes/<user_id>')
def admin_get_strikes(user_id):
    """GET /admin/strikes/<user> — strikes + ban status for a single user."""
    if not _require_admin():
        return jsonify({'error': 'Unauthorised — set X-Admin-Token header'}), 401
    return jsonify({
        'user_id': user_id,
        'strikes': ai_engine.get_strikes(user_id),
        'threshold': ai_engine.BAN_THRESHOLD,
        'ban_info': ai_engine.is_banned(user_id),
    })


@app.route('/admin/ban/<user_id>', methods=['POST'])
def admin_ban_user(user_id):
    """POST /admin/ban/<user> — manually ban a user."""
    if not _require_admin():
        return jsonify({'error': 'Unauthorised'}), 401
    data      = request.get_json(silent=True) or {}
    reason    = (data.get('reason') or 'Manual ban by admin').strip()
    permanent = bool(data.get('permanent', True))
    result    = ai_engine.ban_user(user_id, reason, permanent=permanent)
    return jsonify(result)


@app.route('/admin/unban/<user_id>', methods=['POST'])
def admin_unban_user(user_id):
    """POST /admin/unban/<user> — lift a ban and clear strike history."""
    if not _require_admin():
        return jsonify({'error': 'Unauthorised'}), 401
    ai_engine._banned_users.pop(str(user_id), None)
    ai_engine._strike_log.pop(str(user_id), None)
    return jsonify({'status': 'ok', 'user_id': user_id, 'unbanned': True})


@app.route('/admin/banned')
def admin_list_banned():
    """GET /admin/banned — list all currently banned users."""
    if not _require_admin():
        return jsonify({'error': 'Unauthorised'}), 401
    return jsonify({
        'banned_users': dict(ai_engine._banned_users),
        'total': len(ai_engine._banned_users),
    })


# ═══════════════════════════════════════════════════════
#  ADMIN CSV EXPORTS — MERGED with Postgres (Changes 6 + 7)
# ═══════════════════════════════════════════════════════

@app.route('/admin/export-spam-csv')
def admin_export_spam_csv():
    """
    GET /admin/export-spam-csv — download spam_issues as CSV for model retraining.
    Phase 2: reads via DatabaseSpamRepository instead of accessing _state directly.
    """
    if not _require_admin():
        return jsonify({'error': 'Unauthorised'}), 401

    import csv, io
    from repositories.database_repository import DatabaseSpamRepository
    spam_repo = DatabaseSpamRepository()
    rows = spam_repo.get_all(limit=2000)

    verdict_to_label = {
        'spam':   'spam',
        'abuse':  'abuse',
        'test':   'test',
        'ban':    'spam',
        'reject': 'spam',
    }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['text', 'label'])
    exported = 0
    for r in rows:
        label = verdict_to_label.get(r.get('spam_verdict', ''))
        text  = (r.get('description') or '').strip()
        if label and text:
            writer.writerow([text, label])
            exported += 1

    print(f'[admin] Exported {exported} spam rows as CSV')
    buf.seek(0)
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': 'attachment; filename=spam_export.csv',
            'X-Row-Count': str(exported),
        },
    )


@app.route('/admin/export-real-csv')
def admin_export_real_csv():
    """
    GET /admin/export-real-csv — download approved (real) issues as CSV for model retraining.
    CSV columns: text, label (label = real)
    """
    if not _require_admin():
        return jsonify({'error': 'Unauthorised'}), 401

    from database import _state as _db_state
    import csv, io

    rows = []

    if _db_state.get('mode') == 'postgres':
        try:
            with _db_state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT description FROM issues ORDER BY timestamp DESC LIMIT 2000"
                    )
                    rows = [{'description': r[0]} for r in cur.fetchall()]
        except Exception as e:
            print(f'[admin] real CSV export Postgres read failed: {e}')

    elif _db_state.get('mode') == 'firebase':
        try:
            docs = _db_state['fs_db'].collection('issues').limit(2000).stream()
            rows = [d.to_dict() for d in docs]
        except Exception as e:
            print(f'[admin] real CSV export Firebase read failed: {e}')

    else:
        # Memory mode
        rows = list(_db_state.get('issues', []))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['text', 'label'])
    exported = 0
    for r in rows:
        text = (r.get('description') or '').strip()
        if text:
            writer.writerow([text, 'real'])
            exported += 1

    print(f'[admin] Exported {exported} real rows as CSV')
    buf.seek(0)
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': 'attachment; filename=real_export.csv',
            'X-Row-Count': str(exported),
        },
    )


# ═══════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLER  (Phase 3)
# ═══════════════════════════════════════════════════════
@app.errorhandler(Exception)
def handle_unhandled_exception(exc):
    """
    Catch-all for any unhandled exception that escapes a route handler.

    Why this matters:
      Without this, Flask returns a plain HTML error page which:
        a) leaks stack traces to clients in debug mode
        b) breaks frontend JavaScript that expects JSON
        c) gives inconsistent error shapes across endpoints

    This handler returns a consistent JSON shape for all errors.
    Stack traces are only included when FLASK_DEBUG=1.
    """
    import traceback
    status_code = getattr(exc, 'code', 500)
    # HTTPExceptions (404, 405, etc.) have a 'code' attribute — use it
    # All other exceptions get 500
    if not isinstance(status_code, int):
        status_code = 500

    response = {
        'error':  str(exc) if status_code < 500 else 'Internal server error',
        'type':   type(exc).__name__,
        'status': status_code,
    }
    if os.environ.get('FLASK_DEBUG') == '1':
        response['traceback'] = traceback.format_exc()

    if status_code >= 500:
        print(f'[areapulse] ✗ Unhandled {type(exc).__name__}: {exc}')

    return jsonify(response), status_code


# ═══════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'[areapulse] starting on http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG', '0') == '1')
