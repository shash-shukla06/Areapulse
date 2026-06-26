"""
AreaPulse Portal — auth.py
Session-based auth for Gov Officers and NGO Partners.
Uses same GOV_ACCOUNTS pattern from existing app.py.
"""
import functools
from flask import session, redirect, url_for, request, jsonify

# ── ACCOUNTS ─────────────────────────────────────────────────
# Mirrors GOV_ACCOUNTS from existing app.py, extended for Delhi + NGOs
GOV_ACCOUNTS = {
    'gov_rmc': {
        'pin': '0000',
        'name': 'RMC Officer',
        'dept': 'Delhi Municipal Corporation',
        'tags': ['pothole', 'garbage', 'sewage', 'streetlight', 'tree', 'other'],
        'avatar': 'RO',
    },
    'gov_water': {
        'pin': '0000',
        'name': 'Water Board Officer',
        'dept': 'Delhi Jal Board',
        'tags': ['water', 'sewage'],
        'avatar': 'WB',
    },
    'gov_electricity': {
        'pin': '0000',
        'name': 'Electricity Officer',
        'dept': 'BSES / TPDDL',
        'tags': ['electricity', 'streetlight'],
        'avatar': 'EO',
    },
    'gov_traffic': {
        'pin': '0000',
        'name': 'Traffic Police',
        'dept': 'Delhi Traffic Police',
        'tags': ['traffic', 'noise'],
        'avatar': 'TP',
    },
}

NGO_ACCOUNTS = {
    'ngo_sanitation': {
        'pin': '0000',
        'name': 'Delhi Green Mission',
        'focus': 'Sanitation & Waste Management',
        'tags': ['garbage', 'sewage'],
        'area': 'Rohini',
        'avatar': 'DG',
        'rating': 4.6,
    },
    'ngo_water': {
        'pin': '0000',
        'name': 'Jal Seva Trust',
        'focus': 'Water & Sewage',
        'tags': ['water', 'sewage'],
        'area': 'Hauz Khas',
        'avatar': 'JS',
        'rating': 4.7,
    },
    'ngo_civic': {
        'pin': '0000',
        'name': 'Sahayata Foundation',
        'focus': 'General Civic Issues',
        'tags': ['other', 'pothole', 'tree'],
        'area': 'Connaught Place',
        'avatar': 'SF',
        'rating': 4.2,
    },
    'ngo_power': {
        'pin': '0000',
        'name': 'Light Up Delhi',
        'focus': 'Street Lighting & Energy',
        'tags': ['streetlight', 'electricity'],
        'area': 'Saket',
        'avatar': 'LD',
        'rating': 4.3,
    },
}

ALL_ACCOUNTS = {**GOV_ACCOUNTS, **NGO_ACCOUNTS}


def login_user(username, pin):
    """
    Validate credentials. Returns (success, role, account_info).
    role: 'gov' | 'ngo' | None
    """
    acct = ALL_ACCOUNTS.get(username)
    if not acct:
        return False, None, None
    if acct['pin'] != pin:
        return False, None, None
    role = 'gov' if username in GOV_ACCOUNTS else 'ngo'
    session['portal_user']   = username
    session['portal_role']   = role
    session['portal_name']   = acct['name']
    session['portal_dept']   = acct.get('dept') or acct.get('focus', '')
    session['portal_tags']   = acct.get('tags', [])
    session['portal_avatar'] = acct.get('avatar', username[:2].upper())
    return True, role, acct


def logout_user():
    session.pop('portal_user', None)
    session.pop('portal_role', None)
    session.pop('portal_name', None)
    session.pop('portal_dept', None)
    session.pop('portal_tags', None)
    session.pop('portal_avatar', None)


def current_user():
    return {
        'username': session.get('portal_user'),
        'role':     session.get('portal_role'),
        'name':     session.get('portal_name', 'Guest'),
        'dept':     session.get('portal_dept', ''),
        'tags':     session.get('portal_tags', []),
        'avatar':   session.get('portal_avatar', '?'),
        'is_gov':   session.get('portal_role') == 'gov',
        'is_ngo':   session.get('portal_role') == 'ngo',
    }


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('portal_user'):
            if request.is_json or request.path.startswith('/gov/api') or request.path.startswith('/ngo/api'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect(url_for('auth_login'))
        return f(*args, **kwargs)
    return decorated


def require_gov(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('portal_user'):
            return redirect(url_for('auth_login'))
        if session.get('portal_role') != 'gov':
            # NGO user visiting gov page → go to their dashboard, not a redirect loop
            return redirect(url_for('ngo_dashboard') if session.get('portal_role') == 'ngo' else url_for('auth_login'))
        return f(*args, **kwargs)
    return decorated


def require_ngo(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('portal_user'):
            return redirect(url_for('auth_login'))
        if session.get('portal_role') != 'ngo':
            # Gov user visiting ngo page → go to their dashboard
            return redirect(url_for('gov_dashboard') if session.get('portal_role') == 'gov' else url_for('auth_login'))
        return f(*args, **kwargs)
    return decorated