"""
services/notification_service.py — outbound notification logic
==============================================================
Handles all citizen-facing notifications: WhatsApp status pings.

Why it exists here and not in app.py:
  Notification logic is business logic, not HTTP logic. It does not
  depend on Flask request/response objects and should be callable
  from any context (route handler, background job, admin script).

Phase 1 — Service Layer extraction.
"""

import base64
import json
import os
import urllib.parse
import urllib.request


# ─────────────────────────────────────────────────────────────────────────────
#  WHATSAPP — outbound status pings via Twilio
# ─────────────────────────────────────────────────────────────────────────────

# Human-readable verb phrases for each status value.
# Kept here so they live in one place and are easy to edit.
_STATUS_VERBS: dict = {
    'acknowledged': 'has been *acknowledged*',
    'in_progress':  'is now *being worked on*',
    'resolved':     'has been marked *RESOLVED* ✓',
    'escalated':    'has been *escalated to a higher authority*',
    'open':         'is open',
}


def compose_status_message(issue: dict, new_status: str) -> str:
    """
    Build the WhatsApp message text for an issue status change.

    Args:
        issue:      issue dict (needs 'id', 'tag', 'area' keys)
        new_status: the new IssueStatus value string

    Returns:
        Formatted WhatsApp message string.
    """
    verb = _STATUS_VERBS.get(new_status, f'is now {new_status}')
    return (
        f"📢 *AreaPulse update*\n\n"
        f"Your report #AP-{issue.get('id')} "
        f"({(issue.get('tag') or 'issue').title()} in {issue.get('area') or 'Delhi'}) "
        f"{verb}.\n\n"
        f"Track this and nearby issues on the live map.\n"
        f"Thank you for helping improve our city. 🇮🇳"
    )


def send_whatsapp(to_phone: str, message: str) -> dict:
    """
    Send a WhatsApp message via Twilio.

    Reads credentials from environment variables — never hardcoded.
    Degrades gracefully when credentials are absent (returns mode='simulated').

    Args:
        to_phone: recipient phone number (E.164 or bare digits)
        message:  message body

    Returns:
        dict with keys: ok (bool), mode (sent|simulated|skipped|error), detail (str)
    """
    sid      = os.environ.get('TWILIO_ACCOUNT_SID', '')
    token    = os.environ.get('TWILIO_AUTH_TOKEN', '')
    from_num = os.environ.get('TWILIO_WHATSAPP_NUMBER', '')
    dry_run  = os.environ.get('WA_NOTIFY_DRY_RUN', '0') == '1'

    if not to_phone:
        return {'ok': False, 'mode': 'skipped', 'detail': 'no_phone'}

    # Normalise to whatsapp: URI scheme
    dest = to_phone.strip()
    if not dest.startswith('whatsapp:'):
        if not dest.startswith('+'):
            dest = '+91' + dest.lstrip('+')   # India country code default
        dest = 'whatsapp:' + dest

    # Simulate when Twilio not configured or dry-run mode
    if dry_run or not (sid and token and from_num):
        print(f'[notification] (simulated) -> {dest}: {message[:80]}...')
        return {'ok': True, 'mode': 'simulated', 'detail': 'twilio not configured'}

    try:
        url       = f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'
        from_full = from_num if from_num.startswith('whatsapp:') else f'whatsapp:{from_num}'
        body      = urllib.parse.urlencode({
            'From': from_full,
            'To':   dest,
            'Body': message[:1550],
        }).encode()
        req  = urllib.request.Request(url, data=body)
        creds = base64.b64encode(f'{sid}:{token}'.encode()).decode()
        req.add_header('Authorization', f'Basic {creds}')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
        return {'ok': True, 'mode': 'sent', 'detail': payload.get('sid', '')}
    except Exception as exc:
        print(f'[notification] send_whatsapp failed: {exc}')
        return {'ok': False, 'mode': 'error', 'detail': str(exc)[:120]}


def send_status_update(issue: dict, new_status: str) -> dict:
    """
    High-level helper: compose + send a status-change WhatsApp ping.

    Only sends if the issue has a contact field that looks like a phone number
    (contains digits and no @ sign — skips email addresses).

    Args:
        issue:      full issue dict (needs 'contact', 'id', 'tag', 'area')
        new_status: the new IssueStatus value string

    Returns:
        Result dict from send_whatsapp, or {'mode': 'skipped'} if no phone.
    """
    contact = (issue.get('contact') or '').strip()
    if not contact:
        return {'ok': False, 'mode': 'skipped', 'detail': 'no_contact'}

    # Only attempt SMS-style contacts — skip email addresses
    if not any(ch.isdigit() for ch in contact) or '@' in contact:
        return {'ok': False, 'mode': 'skipped', 'detail': 'not_a_phone_number'}

    # Only ping on meaningful status transitions
    if new_status not in ('acknowledged', 'in_progress', 'resolved', 'escalated'):
        return {'ok': False, 'mode': 'skipped', 'detail': 'status_not_notifiable'}

    message = compose_status_message(issue, new_status)
    return send_whatsapp(contact, message)
