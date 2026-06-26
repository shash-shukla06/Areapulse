"""
AreaPulse GovNGO Portal — app.py
Premium SaaS portal for Government Officers and NGO Partners.
Shares Neon Postgres + Firebase backend with the main AreaPulse citizen app.
"""
import os, sys, time, json
from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, Response, stream_with_context, send_file
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── DATABASE: import from existing AreaPulse project ─────────
# Try to import from the parent AreaPulse directory first,
# then fall back to bundled stub if not found.
_parent_dir = os.path.join(os.path.dirname(__file__), '..', 'Areapulse')
if os.path.isdir(_parent_dir):
    sys.path.insert(0, _parent_dir)

try:
    from database import (
        init_db, get_issues, get_issue_by_id, update_issue_status,
        get_all_ngos, escalate_issue, get_issues_for_gov,
        SLA_HOURS, CROWD_ESCALATION_THRESHOLD,
    )
    _DB_AVAILABLE = True
    print('[portal] ✓ Connected to AreaPulse database module')
except ImportError:
    _DB_AVAILABLE = False
    print('[portal] ⚠ AreaPulse database not found — using demo stub data')

    # ── STUB DATA for standalone demo ────────────────────────
    _STUB_ISSUES = [
        {'id': 1001, 'area': 'Rohini', 'tag': 'pothole', 'severity': 'high',
         'description': 'Large pothole on Sector 7 main road', 'status': 'open',
         'upvotes': 28, 'timestamp': time.time() - 3600*5, 'lat': 28.7493, 'lng': 77.1000,
         'user_name': 'priya', 'assigned_to': None, 'image': None},
        {'id': 1002, 'area': 'Karol Bagh', 'tag': 'water', 'severity': 'high',
         'description': 'Water supply contaminated near metro exit', 'status': 'acknowledged',
         'upvotes': 41, 'timestamp': time.time() - 3600*30, 'lat': 28.6520, 'lng': 77.1904,
         'user_name': 'arjun', 'assigned_to': 'gov_water', 'image': None},
        {'id': 1003, 'area': 'Lajpat Nagar', 'tag': 'electricity', 'severity': 'medium',
         'description': 'Streetlights out for 3 days near Central Market', 'status': 'in_progress',
         'upvotes': 15, 'timestamp': time.time() - 3600*50, 'lat': 28.5700, 'lng': 77.2373,
         'user_name': 'meera', 'assigned_to': 'gov_electricity', 'image': None},
        {'id': 1004, 'area': 'Chandni Chowk', 'tag': 'garbage', 'severity': 'medium',
         'description': 'Overflowing bins near Fatehpuri mosque', 'status': 'open',
         'upvotes': 8, 'timestamp': time.time() - 3600*80, 'lat': 28.6507, 'lng': 77.2334,
         'user_name': 'rohit', 'assigned_to': None, 'image': None},
        {'id': 1005, 'area': 'Dwarka', 'tag': 'sewage', 'severity': 'high',
         'description': 'Sewage overflow on Sector 10 road', 'status': 'open',
         'upvotes': 33, 'timestamp': time.time() - 3600*20, 'lat': 28.5921, 'lng': 77.0460,
         'user_name': 'kavita', 'assigned_to': None, 'image': None},
        {'id': 1006, 'area': 'Vasant Kunj', 'tag': 'tree', 'severity': 'high',
         'description': 'Fallen tree blocking main road after storm', 'status': 'escalated',
         'upvotes': 52, 'timestamp': time.time() - 3600*10, 'lat': 28.5200, 'lng': 77.1569,
         'user_name': 'sanjay', 'assigned_to': None, 'image': None},
        {'id': 1007, 'area': 'Saket', 'tag': 'traffic', 'severity': 'low',
         'description': 'Signal at Select City Walk broken since yesterday', 'status': 'resolved',
         'upvotes': 6, 'timestamp': time.time() - 3600*100, 'lat': 28.5245, 'lng': 77.2066,
         'user_name': 'neha', 'assigned_to': 'gov_traffic', 'image': None},
        {'id': 1008, 'area': 'Pitampura', 'tag': 'pothole', 'severity': 'medium',
         'description': 'Potholes near community centre', 'status': 'open',
         'upvotes': 12, 'timestamp': time.time() - 3600*60, 'lat': 28.7100, 'lng': 77.1279,
         'user_name': 'deepak', 'assigned_to': None, 'image': None},
    ]
    SLA_HOURS = {
        'sewage': 24, 'electricity': 24, 'traffic': 24, 'noise': 24,
        'water': 48, 'streetlight': 48, 'garbage': 72, 'other': 120,
        'pothole': 168, 'tree': 168,
    }
    CROWD_ESCALATION_THRESHOLD = 25

    def init_db(): pass
    def get_issues(tag=None, status=None, limit=300):
        r = list(_STUB_ISSUES)
        if tag:    r = [i for i in r if i.get('tag') == tag]
        if status: r = [i for i in r if i.get('status') == status]
        return r[:limit]
    def get_issue_by_id(iid):
        for i in _STUB_ISSUES:
            if int(i['id']) == int(iid): return i
        return None
    def update_issue_status(iid, status, updated_by='gov', note=''):
        for i in _STUB_ISSUES:
            if int(i['id']) == int(iid):
                i['status'] = status
                return i
        return None
    def get_all_ngos():
        return [
            {'id':1,'name':'Delhi Green Mission','focus':'Sanitation','tag':'garbage','rating':4.6,'area':'Rohini','phone':'011-27551234','email':'contact@delhigreen.org','lat':28.75,'lng':77.10,'issues_resolved':34},
            {'id':2,'name':'Jal Seva Trust','focus':'Water & Sewage','tag':'water','rating':4.7,'area':'Hauz Khas','phone':'011-26960001','email':'help@jalseva.org','lat':28.54,'lng':77.22,'issues_resolved':28},
            {'id':3,'name':'Road Safety India','focus':'Roads','tag':'pothole','rating':4.4,'area':'Dwarka','phone':'011-28567890','email':'info@roadsafetyindia.in','lat':28.59,'lng':77.05,'issues_resolved':19},
        ]
    def escalate_issue(iid, reason='sla_breach'):
        for i in _STUB_ISSUES:
            if int(i['id']) == int(iid):
                i['status'] = 'escalated'
                i['escalated'] = True
                return True
        return False
    def get_issues_for_gov(user, tags=None):
        r = get_issues()
        if tags: r = [i for i in r if i.get('tag') in tags]
        return r


# ── MODULES ──────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'modules'))
from auth          import login_user, logout_user, current_user, require_auth, require_gov, require_ngo, ALL_ACCOUNTS
from sla_engine    import annotate_issues, get_sla_summary, calc_sla, format_remaining
from ai_engine     import chat, chat_stream, gov_briefing, ngo_recommend
from export_engine import export_pdf_summary, export_excel

# ── APP ───────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'areapulse-portal-dev-2026')
MAPTILER_KEY   = os.environ.get('MAPTILER_KEY', '')

init_db()


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def _get_issues_annotated(tag=None, status=None, limit=300):
    issues = get_issues(tag=tag, status=status, limit=limit)
    return annotate_issues(issues)


def _portal_ctx():
    """Common template context."""
    u = current_user()
    return {
        'cu':          u,
        'maptiler_key': MAPTILER_KEY,
    }


def _whatsapp_send(phone, message):
    """Send WhatsApp via Twilio. Mirrors _wa_notify from existing app.py."""
    sid   = os.environ.get('TWILIO_ACCOUNT_SID')
    token = os.environ.get('TWILIO_AUTH_TOKEN')
    from_  = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
    if not sid or not token:
        return {'ok': False, 'mode': 'simulated', 'detail': 'No Twilio credentials'}
    try:
        from twilio.rest import Client
        c = Client(sid, token)
        m = c.messages.create(body=message, from_=from_, to=f'whatsapp:{phone}')
        return {'ok': True, 'mode': 'sent', 'detail': m.sid}
    except Exception as e:
        return {'ok': False, 'mode': 'error', 'detail': str(e)}


# ─────────────────────────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────────────────────────
@app.route('/')
def root():
    if session.get('portal_user'):
        role = session.get('portal_role', 'gov')
        return redirect(url_for('gov_dashboard' if role == 'gov' else 'ngo_dashboard'))
    return redirect(url_for('auth_login'))


@app.route('/login', methods=['GET', 'POST'])
def auth_login():
    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip().lower()
        pin      = (request.form.get('pin') or '').strip()
        ok, role, _ = login_user(username, pin)
        if ok:
            return redirect(url_for('gov_dashboard' if role == 'gov' else 'ngo_dashboard'))
        error = 'Invalid username or PIN. Try gov_rmc / ngo_sanitation with PIN 0000.'
    return render_template('auth/login.html', error=error, **_portal_ctx())


@app.route('/logout')
def auth_logout():
    logout_user()
    return redirect(url_for('auth_login'))


# ─────────────────────────────────────────────────────────────
#  GOV — PAGE ROUTES
# ─────────────────────────────────────────────────────────────
@app.route('/gov/dashboard')
@require_gov
def gov_dashboard():
    u      = current_user()
    issues = _get_issues_annotated()
    my_issues = [i for i in issues if not u['tags'] or i.get('tag') in u['tags']]
    open_issues = [i for i in my_issues if i.get('status') not in ('resolved',)]

    sla_summary  = get_sla_summary(open_issues)
    urgent       = sorted([i for i in open_issues if i.get('sla_state') in ('breached','critical')],
                          key=lambda x: x.get('sla_overdue_hours', 0), reverse=True)[:8]
    briefing     = gov_briefing(open_issues)

    # Category breakdown
    by_tag = {}
    for i in open_issues:
        t = i.get('tag', 'other')
        by_tag[t] = by_tag.get(t, 0) + 1

    # Recent activity (last 10 status changes)
    recent = sorted(my_issues, key=lambda x: x.get('timestamp', 0), reverse=True)[:10]

    return render_template('gov/dashboard.html',
        issues=open_issues, urgent=urgent, sla_summary=sla_summary,
        briefing=briefing, by_tag=by_tag, recent=recent,
        total=len(my_issues), open_count=len(open_issues),
        resolved_count=len([i for i in my_issues if i.get('status')=='resolved']),
        breached_count=sla_summary.get('breached', 0),
        **_portal_ctx())


@app.route('/gov/queue')
@require_gov
def gov_queue():
    u      = current_user()
    tag    = request.args.get('tag') or None
    status = request.args.get('status') or None
    area   = request.args.get('area') or None
    q      = request.args.get('q', '').lower()

    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]
    if tag:    issues = [i for i in issues if i.get('tag') == tag]
    if status: issues = [i for i in issues if i.get('status') == status]
    if area:   issues = [i for i in issues if i.get('area', '').lower() == area.lower()]
    if q:      issues = [i for i in issues if q in (i.get('description','') + i.get('area','')).lower()]

    ngos  = get_all_ngos()
    areas = sorted(set(i.get('area','') for i in get_issues(limit=300) if i.get('area')))

    return render_template('gov/queue.html',
        issues=issues, ngos=ngos, areas=areas,
        sla_hours=SLA_HOURS,
        filters={'tag': tag, 'status': status, 'area': area, 'q': q},
        **_portal_ctx())


@app.route('/gov/sla')
@require_gov
def gov_sla():
    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]
    open_issues = [i for i in issues if i.get('status') not in ('resolved',)]

    lanes = {
        'healthy':  [i for i in open_issues if i.get('sla_state') == 'healthy'],
        'at_risk':  [i for i in open_issues if i.get('sla_state') == 'at_risk'],
        'critical': [i for i in open_issues if i.get('sla_state') == 'critical'],
        'breached': sorted([i for i in open_issues if i.get('sla_state') == 'breached'],
                           key=lambda x: x.get('sla_overdue_hours', 0), reverse=True),
    }
    return render_template('gov/sla.html',
        lanes=lanes, sla_hours=SLA_HOURS,
        sla_summary=get_sla_summary(open_issues),
        **_portal_ctx())


@app.route('/gov/map')
@require_gov
def gov_map():
    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]
    return render_template('gov/map.html',
        issues_json=json.dumps([{
            'id': i['id'], 'lat': i.get('lat'), 'lng': i.get('lng'),
            'tag': i.get('tag'), 'area': i.get('area'),
            'description': i.get('description', '')[:80],
            'status': i.get('status'), 'severity': i.get('severity'),
            'upvotes': i.get('upvotes', 0), 'timestamp': i.get('timestamp', 0),
            'sla_state': i.get('sla_state', 'healthy'),
        } for i in issues if i.get('lat') and i.get('lng')]),
        **_portal_ctx())


@app.route('/gov/analytics')
@require_gov
def gov_analytics():
    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]

    # Build analytics data
    by_tag  = {}
    by_area = {}
    by_status = {}
    for i in issues:
        t = i.get('tag','other'); by_tag[t]  = by_tag.get(t, 0) + 1
        a = i.get('area','?');   by_area[a] = by_area.get(a, 0) + 1
        s = i.get('status','open'); by_status[s] = by_status.get(s, 0) + 1

    top_areas = sorted(by_area.items(), key=lambda x: -x[1])[:10]
    sla_summary = get_sla_summary(issues)
    compliance  = round(
        (sla_summary.get('healthy', 0) + sla_summary.get('at_risk', 0)) /
        max(sum(sla_summary.values()), 1) * 100, 1
    )

    return render_template('gov/analytics.html',
        issues=issues, by_tag=by_tag, by_area=by_area, by_status=by_status,
        top_areas=top_areas, sla_summary=sla_summary, compliance=compliance,
        **_portal_ctx())


@app.route('/gov/ai-assistant')
@require_gov
def gov_ai_assistant():
    return render_template('gov/ai_assistant.html', **_portal_ctx())


@app.route('/gov/reports')
@require_gov
def gov_reports():
    return render_template('gov/reports.html', **_portal_ctx())


@app.route('/gov/departments')
@require_gov
def gov_departments():
    issues = _get_issues_annotated()
    # Build dept performance
    depts = {}
    for username, acct in __import__('modules.auth', fromlist=['GOV_ACCOUNTS']).GOV_ACCOUNTS.items():
        dept_issues = [i for i in issues if i.get('tag') in acct['tags']]
        open_c   = len([i for i in dept_issues if i.get('status') != 'resolved'])
        resolved = len([i for i in dept_issues if i.get('status') == 'resolved'])
        breached = len([i for i in dept_issues if i.get('sla_state') == 'breached'])
        depts[username] = {
            'name': acct['name'], 'dept': acct['dept'],
            'tags': acct['tags'], 'total': len(dept_issues),
            'open': open_c, 'resolved': resolved, 'breached': breached,
            'compliance': round((resolved / max(len(dept_issues), 1)) * 100, 1),
        }
    return render_template('gov/departments.html', depts=depts, **_portal_ctx())


@app.route('/gov/ngo-coordination')
@require_gov
def gov_ngo_coordination():
    ngos   = get_all_ngos()
    issues = _get_issues_annotated()
    return render_template('gov/ngo_coordination.html',
        ngos=ngos, issues=issues, **_portal_ctx())


@app.route('/gov/notifications')
@require_gov
def gov_notifications():
    return render_template('gov/notifications.html', **_portal_ctx())


@app.route('/gov/settings')
@require_gov
def gov_settings():
    return render_template('gov/settings.html', **_portal_ctx())


@app.route('/gov/issue/<int:issue_id>')
@require_gov
def gov_issue_detail(issue_id):
    issue = get_issue_by_id(issue_id)
    if not issue:
        return redirect(url_for('gov_queue'))
    annotate_issues([issue])
    ngos = get_all_ngos()
    nearby_ngos = [n for n in ngos if n.get('tag') == issue.get('tag')][:3]
    return render_template('gov/issue_detail.html',
        issue=issue, nearby_ngos=nearby_ngos, sla_hours=SLA_HOURS,
        **_portal_ctx())


# ─────────────────────────────────────────────────────────────
#  GOV — API ROUTES
# ─────────────────────────────────────────────────────────────
@app.route('/gov/api/issues')
@require_gov
def gov_api_issues():
    u      = current_user()
    tag    = request.args.get('tag') or None
    status = request.args.get('status') or None
    limit  = min(int(request.args.get('limit', 300)), 500)
    issues = _get_issues_annotated(tag=tag, status=status, limit=limit)
    if u['tags'] and not tag:
        issues = [i for i in issues if i.get('tag') in u['tags']]
    return jsonify({'issues': issues, 'total': len(issues)})


@app.route('/gov/api/issues/<int:issue_id>')
@require_gov
def gov_api_issue_get(issue_id):
    issue = get_issue_by_id(issue_id)
    if not issue:
        return jsonify({'error': 'Not found'}), 404
    annotate_issues([issue])
    return jsonify(issue)


@app.route('/gov/update-status', methods=['POST'])
@require_gov
def gov_update_status():
    data      = request.get_json(silent=True) or {}
    issue_id  = data.get('id')
    new_status= data.get('status', '').lower().strip()
    note      = data.get('note', '')
    u         = current_user()

    if not issue_id or not new_status:
        return jsonify({'error': 'id and status required'}), 400

    result = update_issue_status(int(issue_id), new_status, updated_by=u['username'], note=note)
    if result is None:
        return jsonify({'error': 'Update failed or invalid status'}), 400

    # WhatsApp notification if contact available
    issue = get_issue_by_id(int(issue_id))
    if issue and issue.get('contact') and new_status in ('resolved', 'in_progress'):
        msg = (
            f"Update on your AreaPulse issue #AP-{issue_id}: "
            f"Status → {new_status.replace('_',' ').title()}. "
            f"Thank you for reporting. — {u['dept']}"
        )
        _whatsapp_send(issue['contact'], msg)

    return jsonify({'ok': True, 'id': issue_id, 'status': new_status})


@app.route('/gov/bulk-update', methods=['POST'])
@require_gov
def gov_bulk_update():
    data   = request.get_json(silent=True) or {}
    ids    = data.get('ids', [])
    status = data.get('status', '').lower().strip()
    u      = current_user()
    updated = 0
    for iid in ids:
        r = update_issue_status(int(iid), status, updated_by=u['username'])
        if r is not None:
            updated += 1
    return jsonify({'ok': True, 'updated': updated})


@app.route('/gov/api/escalate', methods=['POST'])
@require_gov
def gov_api_escalate():
    data   = request.get_json(silent=True) or {}
    iid    = data.get('id')
    reason = data.get('reason', 'manual_escalation')
    if not iid:
        return jsonify({'error': 'id required'}), 400
    ok = escalate_issue(int(iid), reason)
    return jsonify({'ok': ok})


@app.route('/gov/api/deescalate', methods=['POST'])
@require_gov
def gov_api_deescalate():
    """Bring an escalated issue back to in_progress and clear escalated flag."""
    data = request.get_json(silent=True) or {}
    iid  = data.get('id')
    note = data.get('note', 'Manually de-escalated')
    u    = current_user()
    if not iid:
        return jsonify({'error': 'id required'}), 400
    result = update_issue_status(int(iid), 'in_progress',
                                 updated_by=u['username'], note=note)
    if result is None:
        return jsonify({'error': 'Issue not found'}), 404
    try:
        issue = get_issue_by_id(int(iid))
        if issue and 'escalated' in issue:
            issue['escalated'] = False
            issue['escalation_reason'] = None
    except Exception:
        pass
    return jsonify({'ok': True, 'id': iid, 'status': 'in_progress'})


@app.route('/ngo/api/deescalate', methods=['POST'])
@require_ngo
def ngo_api_deescalate():
    """NGO requests de-escalation of an issue they are committed to."""
    data = request.get_json(silent=True) or {}
    iid  = data.get('id')
    note = data.get('note', 'NGO requested de-escalation')
    u    = current_user()
    if not iid:
        return jsonify({'error': 'id required'}), 400
    result = update_issue_status(int(iid), 'in_progress',
                                 updated_by=u['username'], note=note)
    if result is None:
        return jsonify({'error': 'Issue not found'}), 404
    try:
        issue = get_issue_by_id(int(iid))
        if issue and 'escalated' in issue:
            issue['escalated'] = False
            issue['escalation_reason'] = None
    except Exception:
        pass
    return jsonify({'ok': True, 'id': iid, 'status': 'in_progress'})


@app.route('/gov/api/sla-data')
@require_gov
def gov_api_sla_data():
    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]
    open_issues = [i for i in issues if i.get('status') != 'resolved']
    summary     = get_sla_summary(open_issues)
    breached    = [i for i in open_issues if i.get('sla_state') == 'breached']
    return jsonify({
        'summary': summary,
        'breached': breached[:20],
        'total_open': len(open_issues),
    })


@app.route('/gov/ai-chat', methods=['POST'])
@require_gov
def gov_ai_chat():
    data    = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    history = data.get('history', [])
    if not message:
        return jsonify({'error': 'message required'}), 400

    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]

    # Return SSE stream
    def generate():
        yield from chat_stream(message, 'gov', u, history=history, issues_snapshot=issues)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/gov/api/ai-briefing', methods=['POST'])
@require_gov
def gov_api_ai_briefing():
    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]
    briefing = gov_briefing([i for i in issues if i.get('status') != 'resolved'])
    return jsonify({'briefing': briefing})


@app.route('/gov/api/notifications')
@require_gov
def gov_api_notifications():
    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]

    notifs = []
    for i in issues:
        if i.get('sla_state') == 'breached':
            notifs.append({'type':'sla','title':f"SLA Breached: {i.get('tag','?').title()} in {i.get('area','?')}",
                           'body':f"Issue #AP-{i['id']} is {round(i.get('sla_overdue_hours',0),1)}h overdue.",
                           'ts': i.get('timestamp',0), 'issue_id': i['id']})
        if (i.get('upvotes',0) or 0) >= 25:
            notifs.append({'type':'crowd','title':f"Crowd Alert: {i.get('area','?')} {i.get('tag','?')}",
                           'body':f"{i.get('upvotes')} citizens upvoted issue #AP-{i['id']}.",
                           'ts': i.get('timestamp',0), 'issue_id': i['id']})

    notifs.sort(key=lambda x: x.get('ts',0), reverse=True)
    return jsonify({'notifications': notifs[:30], 'unread': len(notifs)})


@app.route('/gov/api/send-whatsapp', methods=['POST'])
@require_gov
def gov_api_send_whatsapp():
    data    = request.get_json(silent=True) or {}
    phone   = (data.get('phone') or '').strip()
    message = (data.get('message') or '').strip()
    if not phone or not message:
        return jsonify({'error': 'phone and message required'}), 400
    result = _whatsapp_send(phone, message)
    return jsonify(result)


@app.route('/gov/api/export-pdf')
@require_gov
def gov_api_export_pdf():
    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]
    pdf = export_pdf_summary(issues, officer_name=u['name'], dept=u['dept'])
    if pdf is None:
        return jsonify({'error': 'reportlab not installed'}), 501
    import io
    return send_file(io.BytesIO(pdf), mimetype='application/pdf',
                     download_name='areapulse-report.pdf', as_attachment=True)


@app.route('/gov/api/export-csv')
@require_gov
def gov_api_export_csv():
    u      = current_user()
    issues = _get_issues_annotated()
    if u['tags']:
        issues = [i for i in issues if i.get('tag') in u['tags']]
    import csv, io
    buf = io.StringIO()
    writer = csv.DictWriter(buf,
        fieldnames=['id','area','tag','severity','description','status','sla_state','upvotes','timestamp'])
    writer.writeheader()
    for i in issues:
        writer.writerow({k: i.get(k,'') for k in writer.fieldnames})
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=areapulse-issues.csv'})


# ─────────────────────────────────────────────────────────────
#  NGO — PAGE ROUTES
# ─────────────────────────────────────────────────────────────
@app.route('/ngo/dashboard')
@require_ngo
def ngo_dashboard():
    u      = current_user()
    issues = _get_issues_annotated()
    matching = [i for i in issues if i.get('tag') in u['tags'] and i.get('status') != 'resolved']

    recommendation = ngo_recommend(matching, u['tags'], u.get('dept', ''))
    ngos = get_all_ngos()
    by_tag = {}
    for i in matching:
        t = i.get('tag','other'); by_tag[t] = by_tag.get(t, 0) + 1

    high_priority = sorted(
        [i for i in matching if i.get('severity') == 'high' or i.get('sla_state') in ('breached','critical')],
        key=lambda x: (x.get('upvotes',0) + (100 if x.get('sla_state')=='breached' else 0)),
        reverse=True
    )[:6]

    return render_template('ngo/dashboard.html',
        issues=matching, high_priority=high_priority,
        recommendation=recommendation, by_tag=by_tag,
        total_matched=len(matching),
        total_issues=len(issues),
        resolved_count=len([i for i in issues if i.get('status')=='resolved']),
        **_portal_ctx())


@app.route('/ngo/opportunities')
@require_ngo
def ngo_opportunities():
    u      = current_user()
    tag    = request.args.get('tag') or None
    area   = request.args.get('area') or None
    sev    = request.args.get('severity') or None

    issues = _get_issues_annotated()
    opps   = [i for i in issues if i.get('tag') in u['tags'] and i.get('status') != 'resolved']

    if tag:  opps = [i for i in opps if i.get('tag') == tag]
    if area: opps = [i for i in opps if i.get('area','').lower() == area.lower()]
    if sev:  opps = [i for i in opps if i.get('severity') == sev]

    # Score by impact (upvotes + SLA urgency)
    for opp in opps:
        score = (opp.get('upvotes',0) * 2)
        if opp.get('sla_state') == 'breached':  score += 100
        elif opp.get('sla_state') == 'critical': score += 50
        elif opp.get('sla_state') == 'at_risk':  score += 25
        if opp.get('severity') == 'high': score += 30
        opp['match_score'] = min(round(score / 3), 99)

    opps.sort(key=lambda x: x.get('match_score', 0), reverse=True)
    areas = sorted(set(i.get('area','') for i in issues if i.get('area')))

    return render_template('ngo/opportunities.html',
        opportunities=opps, areas=areas,
        filters={'tag': tag, 'area': area, 'severity': sev},
        **_portal_ctx())


@app.route('/ngo/projects')
@require_ngo
def ngo_projects():
    u = current_user()
    issues = _get_issues_annotated()
    # "Projects" = issues this NGO has acknowledged/in_progress
    committed = [i for i in issues
                 if i.get('tag') in u['tags']
                 and i.get('status') in ('in_progress', 'acknowledged')]
    return render_template('ngo/projects.html',
        projects=committed, **_portal_ctx())


@app.route('/ngo/impact')
@require_ngo
def ngo_impact():
    u = current_user()
    issues = _get_issues_annotated()
    resolved = [i for i in issues
                if i.get('tag') in u['tags'] and i.get('status') == 'resolved']
    total_upvotes   = sum(i.get('upvotes',0) for i in resolved)
    citizens_helped = total_upvotes * 3  # estimate: each upvote ≈ 3 affected citizens
    return render_template('ngo/impact.html',
        resolved=resolved, citizens_helped=citizens_helped,
        issues_resolved=len(resolved),
        total_upvotes=total_upvotes,
        **_portal_ctx())


@app.route('/ngo/map')
@require_ngo
def ngo_map():
    u      = current_user()
    issues = _get_issues_annotated()
    opps   = [i for i in issues if i.get('tag') in u['tags'] and i.get('status') != 'resolved']
    return render_template('ngo/map.html',
        issues_json=json.dumps([{
            'id': i['id'], 'lat': i.get('lat'), 'lng': i.get('lng'),
            'tag': i.get('tag'), 'area': i.get('area'),
            'description': i.get('description', '')[:80],
            'status': i.get('status'), 'severity': i.get('severity'),
            'upvotes': i.get('upvotes', 0), 'timestamp': i.get('timestamp', 0),
            'sla_state': i.get('sla_state', 'healthy'),
        } for i in opps if i.get('lat') and i.get('lng')]),
        **_portal_ctx())


@app.route('/ngo/analytics')
@require_ngo
def ngo_analytics():
    u = current_user()
    issues = _get_issues_annotated()
    matching = [i for i in issues if i.get('tag') in u['tags']]
    by_area = {}
    for i in matching:
        a = i.get('area','?'); by_area[a] = by_area.get(a, 0) + 1
    by_status = {}
    for i in matching:
        s = i.get('status','open'); by_status[s] = by_status.get(s, 0) + 1
    return render_template('ngo/analytics.html',
        issues=matching, by_area=by_area, by_status=by_status, **_portal_ctx())


@app.route('/ngo/ai-assistant')
@require_ngo
def ngo_ai_assistant():
    return render_template('ngo/ai_assistant.html', **_portal_ctx())


@app.route('/ngo/gov-coordination')
@require_ngo
def ngo_gov_coordination():
    issues = _get_issues_annotated()
    u = current_user()
    matching = [i for i in issues if i.get('tag') in u['tags']]
    return render_template('ngo/gov_coordination.html',
        issues=matching, **_portal_ctx())


@app.route('/ngo/reports')
@require_ngo
def ngo_reports():
    return render_template('ngo/reports.html', **_portal_ctx())


@app.route('/ngo/notifications')
@require_ngo
def ngo_notifications():
    return render_template('ngo/notifications.html', **_portal_ctx())


@app.route('/ngo/settings')
@require_ngo
def ngo_settings():
    return render_template('ngo/settings.html', **_portal_ctx())


@app.route('/ngo/issue/<int:issue_id>')
@require_ngo
def ngo_issue_detail(issue_id):
    issue = get_issue_by_id(issue_id)
    if not issue:
        return redirect(url_for('ngo_opportunities'))
    annotate_issues([issue])
    return render_template('ngo/issue_detail.html', issue=issue, **_portal_ctx())


# ─────────────────────────────────────────────────────────────
#  NGO — API ROUTES
# ─────────────────────────────────────────────────────────────
@app.route('/ngo/api/opportunities')
@require_ngo
def ngo_api_opportunities():
    u = current_user()
    issues = _get_issues_annotated()
    opps = [i for i in issues if i.get('tag') in u['tags'] and i.get('status') != 'resolved']
    return jsonify({'issues': opps, 'total': len(opps)})


@app.route('/ngo/commit', methods=['POST'])
@require_ngo
def ngo_commit():
    data       = request.get_json(silent=True) or {}
    issue_id   = data.get('issue_id')
    volunteers = data.get('volunteers', 1)
    eta        = data.get('eta', '')
    note       = data.get('note', '')
    u          = current_user()
    if not issue_id:
        return jsonify({'error': 'issue_id required'}), 400
    result = update_issue_status(int(issue_id), 'in_progress',
                                 updated_by=u['username'],
                                 note=f"NGO {u['name']} committed. Volunteers: {volunteers}. ETA: {eta}. {note}")
    return jsonify({'ok': result is not None})


@app.route('/ngo/api/impact-data')
@require_ngo
def ngo_api_impact():
    u      = current_user()
    issues = _get_issues_annotated()
    resolved = [i for i in issues if i.get('tag') in u['tags'] and i.get('status') == 'resolved']
    return jsonify({
        'issues_resolved': len(resolved),
        'citizens_helped': sum(i.get('upvotes',0) for i in resolved) * 3,
        'total_upvotes': sum(i.get('upvotes',0) for i in resolved),
        'resolved': resolved[:20],
    })


@app.route('/ngo/api/projects')
@require_ngo
def ngo_api_projects():
    u      = current_user()
    issues = _get_issues_annotated()
    committed = [i for i in issues
                 if i.get('tag') in u['tags']
                 and i.get('status') in ('in_progress','acknowledged')]
    return jsonify({'projects': committed})


@app.route('/ngo/ai-chat', methods=['POST'])
@require_ngo
def ngo_ai_chat():
    data    = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    history = data.get('history', [])
    if not message:
        return jsonify({'error': 'message required'}), 400
    u      = current_user()
    issues = _get_issues_annotated()
    matching = [i for i in issues if i.get('tag') in u['tags']]

    def generate():
        yield from chat_stream(message, 'ngo', u, history=history, issues_snapshot=matching)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route('/ngo/api/ai-recommend')
@require_ngo
def ngo_api_recommend():
    u      = current_user()
    issues = _get_issues_annotated()
    matching = [i for i in issues if i.get('tag') in u['tags'] and i.get('status') != 'resolved']
    rec = ngo_recommend(matching, u['tags'], u.get('dept',''))
    return jsonify({'recommendation': rec})


@app.route('/ngo/api/notifications')
@require_ngo
def ngo_api_notifications():
    u      = current_user()
    issues = _get_issues_annotated()
    matching = [i for i in issues if i.get('tag') in u['tags']]
    notifs = []
    for i in matching:
        if i.get('sla_state') in ('breached','critical') and i.get('status') != 'resolved':
            notifs.append({'type':'opportunity','title':f"Urgent: {i.get('tag','?').title()} in {i.get('area','?')}",
                           'body':f"SLA {i.get('sla_state')} — this is a high-impact opportunity for your team.",
                           'ts': i.get('timestamp',0), 'issue_id': i['id']})
    notifs.sort(key=lambda x: x.get('ts',0), reverse=True)
    return jsonify({'notifications': notifs[:20], 'unread': len(notifs)})


@app.route('/ngo/api/export-impact-pdf')
@require_ngo
def ngo_api_export_impact_pdf():
    u      = current_user()
    issues = _get_issues_annotated()
    resolved = [i for i in issues if i.get('tag') in u['tags'] and i.get('status') == 'resolved']
    pdf = export_pdf_summary(resolved, officer_name=u['name'], dept=u['dept'])
    if pdf is None:
        return jsonify({'error': 'reportlab not installed'}), 501
    import io
    return send_file(io.BytesIO(pdf), mimetype='application/pdf',
                     download_name='impact-report.pdf', as_attachment=True)


# ─────────────────────────────────────────────────────────────
#  SHARED API
# ─────────────────────────────────────────────────────────────
@app.route('/api/health')
def api_health():
    return jsonify({'ok': True, 'db': _DB_AVAILABLE, 'ts': time.time()})


@app.route('/api/settings', methods=['POST'])
@require_auth
def api_settings():
    # Settings are stored in session for demo; extend to DB in production
    data  = request.get_json(silent=True) or {}
    key   = data.get('key')
    value = data.get('value')
    if key:
        session[f'setting_{key}'] = value
    return jsonify({'ok': True})


@app.route('/api/realtime-token')
@require_auth
def api_realtime_token():
    fb_key = os.environ.get('FIREBASE_KEY_JSON')
    if not fb_key:
        return jsonify({'firebase_config': None}), 204
    try:
        cfg = json.loads(fb_key)
        return jsonify({'firebase_config': {'projectId': cfg.get('project_id')}})
    except Exception:
        return jsonify({'firebase_config': None}), 204


@app.route('/issue/<int:issue_id>/upvote', methods=['POST'])
@require_auth
def api_upvote(issue_id):
    """Proxies to existing AreaPulse upvote if available."""
    if _DB_AVAILABLE:
        try:
            from database import upvote_issue
            upvote_issue(issue_id)
        except Exception:
            pass
    return jsonify({'ok': True})



if __name__ == '__main__':
    port  = int(os.environ.get('PORT', 5001))
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    print(f'[portal] Starting AreaPulse GovNGO Portal on port {port}')
    app.run(host='0.0.0.0', port=port, debug=debug)