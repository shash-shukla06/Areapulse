"""
Email sender — Resend API integration.
Free tier: 100 emails/day, 3000/month.
Get key at: https://resend.com/api-keys
"""
import os
import json
import urllib.request
import urllib.error

RESEND_API_URL = 'https://api.resend.com/emails'


def is_available():
    return bool(os.environ.get('RESEND_API_KEY'))


def send_complaint(to_email, subject, body_html, body_text=None, attachments=None, reply_to=None):
    api_key = os.environ.get('RESEND_API_KEY')
    if not api_key:
        return {'error': 'RESEND_API_KEY not configured', '_status': 'not_configured'}

    sender = os.environ.get('RESEND_FROM', 'AreaPulse <onboarding@resend.dev>')

    # ── FORCE DEMO OVERRIDE ──
    # Resend free tier without a verified domain can ONLY deliver to your verified
    # Resend account email. If DEMO_RECIPIENT_EMAIL is set, every email goes there.
    demo_to = os.environ.get('DEMO_RECIPIENT_EMAIL', '').strip()
    if demo_to:
        original_to = to_email
        to_email = demo_to
        if not str(subject).startswith('[DEMO'):
            subject = f'[DEMO → {original_to}] {subject}'

    payload = {
        'from': sender,
        'to': [to_email] if isinstance(to_email, str) else to_email,
        'subject': subject,
        'html': body_html,
    }
    if body_text: payload['text'] = body_text
    if reply_to:  payload['reply_to'] = reply_to
    if attachments:
        payload['attachments'] = [{
            'filename':    a['filename'],
            'content':     a['content_b64'],
            'content_type': a.get('content_type', 'image/jpeg'),
        } for a in attachments]

    print(f'[resend] POST → from={sender} to={to_email} subject={subject[:60]!r}')

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        RESEND_API_URL, data=data, method='POST',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'User-Agent': 'AreaPulse/1.0 (+https://areapulse.app)',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            result = json.loads(body) if body else {}
            print(f'[resend] ✓ sent id={result.get("id", "?")}')
            return {'success': True, 'id': result.get('id', ''), 'raw': result}
    except urllib.error.HTTPError as e:
        # Read full error body and log it loudly so user can see exact cause
        err_body = ''
        try:
            err_body = e.read().decode('utf-8')
        except Exception as r_err:
            err_body = f'(could not read body: {r_err})'

        print(f'[resend] ✗ HTTP {e.code}')
        print(f'[resend]   response body: {err_body}')

        # Try to pull the human message from the JSON, but fall back to raw body
        msg = err_body
        try:
            err_json = json.loads(err_body)
            msg = err_json.get('message') or err_json.get('error') or err_json.get('name') or err_body
        except Exception:
            pass

        # Common 403 cause: domain/sender not verified
        if e.code == 403:
            hint = ''
            ml = (msg or '').lower()
            if 'verify' in ml or 'domain' in ml or 'testing' in ml or 'own' in ml:
                hint = (' · Resend free tier requires you to send FROM a verified email AND TO your '
                        'own Resend account email. Verify your domain at https://resend.com/domains or '
                        'set DEMO_RECIPIENT_EMAIL to your Resend signup email.')
            else:
                hint = ' · Check RESEND_API_KEY is valid and RESEND_FROM is a verified sender.'
            msg = (msg or 'forbidden') + hint

        return {'error': f'{msg}', '_status': 'api_error', '_code': e.code, '_body': err_body[:500]}
    except Exception as e:
        print(f'[resend] ✗ exception: {type(e).__name__}: {e}')
        return {'error': f'{type(e).__name__}: {e}', '_status': 'server_error'}
