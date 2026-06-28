"""
database_standalone.py — AreaPulse shared database layer
=========================================================
DROP THIS FILE into the GovNGO portal repo as `database.py`.

It connects to the SAME Neon Postgres database as the citizen app.
Set the same DATABASE_URL env var on both Render deployments and
both apps share real data instantly — no HTTP calls, no sync needed.

Setup:
  1. Copy this file into the gov portal repo as `database.py`
  2. In Render → gov portal → Environment → add:
       DATABASE_URL = <same value as citizen app's DATABASE_URL>
  3. Redeploy. Done.

Fallback chain (same as citizen app):
  Postgres (DATABASE_URL set) → Firebase (FIREBASE_KEY_JSON set) → in-memory stub

Requirements (add to gov portal's requirements.txt if not already present):
  psycopg[binary]==3.2.10
  psycopg-pool==3.2.8
  firebase-admin==6.5.0   (only needed for Firebase fallback)
"""

import os, time, math, json, tempfile, threading

# ─────────────────────────────────────────────────────────────────────────────
#  POSTGRESQL
# ─────────────────────────────────────────────────────────────────────────────
_PG_OK = False
try:
    import psycopg
    from psycopg_pool import ConnectionPool
    _PG_OK = True
    print('[database] psycopg + pool available')
except ImportError:
    print('[database] psycopg not installed — Postgres unavailable')

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS  (inlined — no dependency on domain/constants.py)
# ─────────────────────────────────────────────────────────────────────────────
AREA_COORDS = {
    'Connaught Place': (28.6315, 77.2167), 'Karol Bagh': (28.6514, 77.1907),
    'Rohini': (28.7041, 77.1025),          'Saket': (28.5244, 77.2090),
    'Lajpat Nagar': (28.5677, 77.2378),   'Hauz Khas': (28.5494, 77.2001),
    'Dwarka': (28.5921, 77.0460),          'Janakpuri': (28.6219, 77.0878),
    'Chandni Chowk': (28.6506, 77.2303),  'Paharganj': (28.6448, 77.2167),
    'Mehrauli': (28.5244, 77.1855),        'Malviya Nagar': (28.5355, 77.2068),
    'Greater Kailash': (28.5494, 77.2378),'Vasant Kunj': (28.5200, 77.1590),
    'Pitampura': (28.7007, 77.1311),       'Model Town': (28.7167, 77.1900),
    'Civil Lines': (28.6800, 77.2250),     'Mukherjee Nagar': (28.7050, 77.2100),
    'Rajouri Garden': (28.6447, 77.1220),  'Punjabi Bagh': (28.6590, 77.1311),
    'Mayur Vihar': (28.6090, 77.2944),     'Preet Vihar': (28.6355, 77.2944),
    'Shahdara': (28.6706, 77.2944),        'Laxmi Nagar': (28.6310, 77.2780),
    'Okhla': (28.5355, 77.2780),           'Kalkaji': (28.5494, 77.2590),
    'Nehru Place': (28.5491, 77.2509),     'Lodhi Colony': (28.5887, 77.2208),
    'Kashmere Gate': (28.6675, 77.2280),   'Nizamuddin': (28.5910, 77.2429),
    'Sarojini Nagar': (28.5760, 77.1980),  'INA': (28.5733, 77.2080),
    'Patel Nagar': (28.6500, 77.1700),     'RK Puram': (28.5650, 77.1800),
    'Vasant Vihar': (28.5670, 77.1600),    'Defence Colony': (28.5731, 77.2294),
}

SLA_HOURS = {
    'pothole': 168, 'water': 48, 'garbage': 72,
    'streetlight': 48, 'traffic': 24, 'noise': 24,
    'sewage': 24, 'electricity': 24, 'tree': 168, 'other': 120,
}

CROWD_ESCALATION_THRESHOLD = 25

# ─────────────────────────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────────────────────────
_state = {
    'mode':     'memory',
    'fs_db':    None,
    'pg_pool':  None,
    'issues':   [],
    'ngos':     [],
    'next_id':  1,
    'lock':     threading.Lock(),
}

_cache = {'issues': None, 'issues_ts': 0.0}
_CACHE_TTL = 300

def _get_cached_issues():
    now = time.time()
    if _cache['issues'] is not None and (now - _cache['issues_ts']) < _CACHE_TTL:
        return _cache['issues']
    return None

def _set_cached_issues(issues):
    _cache['issues'] = issues
    _cache['issues_ts'] = time.time()

def _invalidate_cache():
    _cache['issues'] = None
    _cache['issues_ts'] = 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  INIT
# ─────────────────────────────────────────────────────────────────────────────
def init_db():
    """Connect: Postgres → Firebase → in-memory stub."""
    dsn = os.environ.get('DATABASE_URL', '').strip()

    if dsn and _PG_OK:
        try:
            _state['pg_pool'] = ConnectionPool(
    dsn, min_size=1, max_size=4, open=True,
    reconnect_timeout=5,
    kwargs={
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 5,
        "keepalives_count": 3,
    },
    configure=_ensure_pg_schema,
)
            _state['mode'] = 'postgres'
            print('[database] ✓ Postgres connected — sharing AreaPulse database')
            return
        except Exception as e:
            print(f'[database] Postgres failed ({e}), trying Firebase...')
            _state['pg_pool'] = None

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        cred = None
        if os.path.exists('firebase_key.json'):
            cred = credentials.Certificate('firebase_key.json')
        elif os.environ.get('FIREBASE_KEY_JSON'):
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
            tmp.write(os.environ['FIREBASE_KEY_JSON'])
            tmp.close()
            cred = credentials.Certificate(tmp.name)
        else:
            raise FileNotFoundError('No Firebase credentials')
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        _state['fs_db'] = firestore.client()
        _state['mode'] = 'firebase'
        print('[database] ✓ Firebase connected')
        return
    except Exception as e:
        print(f'[database] Firebase unavailable ({e}), using stub data')
        _state['mode'] = 'memory'
        _seed_stub()


# ─────────────────────────────────────────────────────────────────────────────
#  SCHEMA  (read-only for the portal — doesn't create, just ensures columns exist)
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_pg_schema(conn):
    """Ensure any missing columns are added. Safe to run on an existing DB."""
    ddl = """
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS status_history  JSONB DEFAULT '[]'::jsonb;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS escalation_reason TEXT;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS escalated_at    DOUBLE PRECISION;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS resolved_at     DOUBLE PRECISION;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS assigned_to     TEXT;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS upvoters        JSONB DEFAULT '[]'::jsonb;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS last_updated_at DOUBLE PRECISION;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS last_updated_by TEXT;
    """
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    except Exception as e:
        print(f'[database] schema check warning: {e}')


# ─────────────────────────────────────────────────────────────────────────────
#  ROW CONVERTER
# ─────────────────────────────────────────────────────────────────────────────
def _pg_row_to_issue(row):
    if isinstance(row, dict):
        r = row
    elif hasattr(row, '_asdict'):
        r = row._asdict()
    else:
        keys = [
            'id','user_name','area','description','severity','tag','status',
            'lat','lng','landmark','contact','image','image_hash','timestamp',
            'upvotes','verified','escalated','resolved',
            'is_verified','is_escalated','status_history',
            'escalation_reason','escalated_at','resolved_at','assigned_to',
            'ai_confidence','verified_by','upvoters','last_updated_at','last_updated_by',
        ]
        r = {k: row[i] if i < len(row) else None for i, k in enumerate(keys)}

    result = dict(r)
    if 'user_name' in result:
        result['user'] = result.pop('user_name')

    sh = result.get('status_history')
    if isinstance(sh, str):
        try:    result['status_history'] = json.loads(sh)
        except: result['status_history'] = []
    elif sh is None:
        result['status_history'] = []

    upvoters = result.get('upvoters') or []
    if isinstance(upvoters, str):
        try:    upvoters = json.loads(upvoters)
        except: upvoters = []
    result['upvoters'] = upvoters
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  GETTERS
# ─────────────────────────────────────────────────────────────────────────────
def get_issues(tag=None, status=None, limit=300):
    if _state['mode'] == 'postgres':
        # Use cache for unfiltered queries (the common case for dashboards)
        if tag is None and status is None:
            cached = _get_cached_issues()
            if cached is not None:
                return cached[:limit]

        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    if tag and status:
                        q = "SELECT * FROM issues WHERE tag=%s AND status=%s ORDER BY timestamp DESC LIMIT %s"
                        params = [tag, status, limit]
                    elif tag:
                        q = "SELECT * FROM issues WHERE tag=%s ORDER BY timestamp DESC LIMIT %s"
                        params = [tag, limit]
                    elif status:
                        q = "SELECT * FROM issues WHERE status=%s ORDER BY timestamp DESC LIMIT %s"
                        params = [status, limit]
                    else:
                        q = "SELECT * FROM issues ORDER BY timestamp DESC LIMIT %s"
                        params = [limit]
                    cur.execute(q, params)
                    rows = cur.fetchall()
                    if not rows:
                        return []
                    if hasattr(rows[0], '_asdict'):
                        results = [_pg_row_to_issue(r) for r in rows]
                    else:
                        cols = [d.name for d in cur.description]
                        results = [_pg_row_to_issue({cols[i]: row[i] for i in range(len(cols))})
                                   for row in rows]
                    # Cache unfiltered full results
                    if tag is None and status is None:
                        _set_cached_issues(results)
                    return results
        except Exception as e:
            print(f'[database] get_issues failed: {e}')
            _invalidate_cache()
            # try once more with a fresh connection
            try:
                _state['pg_pool'].check()
                with _state['pg_pool'].connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT * FROM issues ORDER BY timestamp DESC LIMIT %s", [limit])
                        rows = cur.fetchall()
                        cols = [d.name for d in cur.description]
                        return [_pg_row_to_issue({cols[i]: row[i] for i in range(len(cols))}) for row in rows]
            except Exception as e2:
                print(f'[database] get_issues retry also failed: {e2}')
                return []


    if _state['mode'] == 'firebase':
        cached = _get_cached_issues()
        if cached is None:
            try:
                docs = _state['fs_db'].collection('issues').limit(limit).stream()
                results = []
                for d in docs:
                    data = d.to_dict()
                    data.setdefault('id', d.id)
                    results.append(data)
                results.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                _set_cached_issues(results)
                cached = results
            except Exception as e:
                print(f'[database] Firebase read failed: {e}')
                cached = list(_state['issues'])
        results = cached
        if tag:    results = [i for i in results if i.get('tag') == tag]
        if status: results = [i for i in results if i.get('status') == status]
        return results[:limit]

    # Memory stub
    results = list(_state['issues'])
    if tag:    results = [i for i in results if i.get('tag') == tag]
    if status: results = [i for i in results if i.get('status') == status]
    results.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    return results[:limit]


def get_issue_by_id(issue_id):
    issue_id = int(issue_id)
    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM issues WHERE id = %s", (issue_id,))
                    row = cur.fetchone()
                    if row:
                        if hasattr(row, '_asdict'):
                            return _pg_row_to_issue(row)
                        cols = [d.name for d in cur.description]
                        return _pg_row_to_issue({cols[i]: row[i] for i in range(len(cols))})
        except Exception as e:
            print(f'[database] get_issue_by_id failed: {e}')
    if _state['mode'] == 'firebase':
        try:
            snap = _state['fs_db'].collection('issues').document(str(issue_id)).get()
            if snap.exists:
                return snap.to_dict()
        except Exception:
            pass
    for i in _state['issues']:
        if int(i.get('id', -1)) == issue_id:
            return i
    return None


def get_issues_for_gov(tags=None, limit=300):
    issues = get_issues(limit=limit)
    if tags:
        issues = [i for i in issues if i.get('tag') in tags]
    return issues


def get_all_ngos():
    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM ngos")
                    rows = cur.fetchall()
                    if not rows:
                        return []
                    if hasattr(rows[0], '_asdict'):
                        return [dict(r) for r in rows]
                    cols = [d.name for d in cur.description]
                    return [{cols[i]: row[i] for i in range(len(cols))} for row in rows]
        except Exception as e:
            print(f'[database] get_all_ngos failed: {e}')
            return []
    if _state['mode'] == 'firebase':
        try:
            docs = _state['fs_db'].collection('ngos').stream()
            return [{**d.to_dict(), 'id': d.id} for d in docs]
        except Exception:
            pass
    return list(_state['ngos'])


# ─────────────────────────────────────────────────────────────────────────────
#  WRITERS
# ─────────────────────────────────────────────────────────────────────────────
_ALLOWED_STATUSES = {'open', 'acknowledged', 'in_progress', 'resolved', 'escalated'}

def update_issue_status(issue_id, new_status, updated_by='gov', note=''):
    issue_id   = int(issue_id)
    new_status = (new_status or '').lower().strip()
    if new_status not in _ALLOWED_STATUSES:
        return None
    now = time.time()
    history_entry = {'status': new_status, 'changed_at': now,
                     'changed_by': updated_by, 'note': (note or '')[:200]}
    _invalidate_cache()

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT status_history, contact, tag, area FROM issues WHERE id = %s",
                                (issue_id,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    history = row[0] or []
                    if isinstance(history, str):
                        try:    history = json.loads(history)
                        except: history = []
                    history.append(history_entry)
                    extras = {}
                    if new_status == 'resolved':
                        extras['resolved_at'] = now
                    cur.execute(
                        """UPDATE issues
                           SET status=%s, status_history=%s, last_updated_at=%s,
                               last_updated_by=%s, resolved_at=COALESCE(%s, resolved_at)
                           WHERE id=%s""",
                        (new_status, json.dumps(history), now,
                         updated_by, extras.get('resolved_at'), issue_id),
                    )
                conn.commit()
            return get_issue_by_id(issue_id)
        except Exception as e:
            print(f'[database] update_issue_status failed: {e}')
            return None

    if _state['mode'] == 'firebase':
        try:
            doc_ref = _state['fs_db'].collection('issues').document(str(issue_id))
            snap = doc_ref.get()
            if not snap.exists:
                return None
            data    = snap.to_dict()
            history = data.get('status_history', [])
            if isinstance(history, str):
                try:    history = json.loads(history)
                except: history = []
            history.append(history_entry)
            update  = {'status': new_status, 'status_history': history,
                       'last_updated_at': now, 'last_updated_by': updated_by}
            if new_status == 'resolved':
                update['resolved_at'] = now
            doc_ref.update(update)
            return {**data, **update, 'id': issue_id}
        except Exception as e:
            print(f'[database] Firebase update failed: {e}')
            return None

    for i in _state['issues']:
        if int(i.get('id', -1)) == issue_id:
            i['status'] = new_status
            i['last_updated_at'] = now
            i['last_updated_by'] = updated_by
            if new_status == 'resolved':
                i['resolved_at'] = now
            return i
    return None


def escalate_issue(issue_id, reason='sla_breach'):
    issue_id = int(issue_id)
    _invalidate_cache()
    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE issues
                           SET escalated=TRUE, is_escalated=TRUE, status='escalated',
                               escalation_reason=%s, escalated_at=%s
                           WHERE id=%s AND escalated=FALSE""",
                        (reason, time.time(), issue_id),
                    )
                    updated = cur.rowcount > 0
                conn.commit()
            return updated
        except Exception as e:
            print(f'[database] escalate failed: {e}')
    if _state['mode'] == 'firebase':
        try:
            doc_ref = _state['fs_db'].collection('issues').document(str(issue_id))
            snap = doc_ref.get()
            if not snap.exists or snap.to_dict().get('escalated'):
                return False
            doc_ref.update({'escalated': True, 'status': 'escalated',
                            'escalation_reason': reason, 'escalated_at': time.time()})
            return True
        except Exception:
            pass
    for i in _state['issues']:
        if int(i.get('id', -1)) == issue_id:
            if i.get('escalated'):
                return False
            i.update({'escalated': True, 'status': 'escalated',
                      'escalation_reason': reason, 'escalated_at': time.time()})
            return True
    return False


def upvote_issue(issue_id, user='anon'):
    issue_id = int(issue_id)
    _invalidate_cache()
    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT upvoters FROM issues WHERE id=%s", (issue_id,))
                    row = cur.fetchone()
                    if not row:
                        return 'not_found'
                    current = row[0] or []
                    if isinstance(current, str):
                        try:    current = json.loads(current)
                        except: current = []
                    if user in current:
                        current.remove(user); action = 'removed'
                    else:
                        current.append(user); action = 'added'
                    cur.execute(
                        "UPDATE issues SET upvoters=%s, upvotes=GREATEST(0, upvotes+%s) WHERE id=%s",
                        (json.dumps(current), 1 if action == 'added' else -1, issue_id),
                    )
                conn.commit()
                return action
        except Exception as e:
            print(f'[database] upvote failed: {e}')
    return 'ok'


# ─────────────────────────────────────────────────────────────────────────────
#  SLA  (pure calculation — no DB calls)
# ─────────────────────────────────────────────────────────────────────────────
def calculate_sla(issue):
    tag        = issue.get('tag') or 'other'
    sla_hours  = SLA_HOURS.get(tag, SLA_HOURS['other'])
    created    = issue.get('timestamp') or time.time()
    sla_due_at = created + (sla_hours * 3600)
    status     = issue.get('status', 'open')
    if status == 'resolved':
        return {'sla_hours': sla_hours, 'sla_due_at': sla_due_at,
                'sla_overdue_hours': 0, 'sla_state': 'resolved'}
    overdue_s  = time.time() - sla_due_at
    overdue_h  = max(0, overdue_s / 3600)
    remain_h   = -overdue_s / 3600
    if overdue_h > 0:
        state = 'overdue'
    elif remain_h < (sla_hours * 0.25):
        state = 'soon'
    else:
        state = 'safe'
    return {'sla_hours': sla_hours, 'sla_due_at': sla_due_at,
            'sla_overdue_hours': round(overdue_h, 1), 'sla_state': state}


def get_areas():
    return sorted(AREA_COORDS.keys())


# ─────────────────────────────────────────────────────────────────────────────
#  HAVERSINE (internal)
# ─────────────────────────────────────────────────────────────────────────────
def _haversine(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2)
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ─────────────────────────────────────────────────────────────────────────────
#  STUB DATA  (used when DATABASE_URL is not set — demo/local mode)
# ─────────────────────────────────────────────────────────────────────────────
_STUB_ISSUES = [
    {'id':1001,'area':'Rohini','tag':'pothole','severity':'high',
     'description':'Large pothole on Sector 7 main road','status':'open',
     'upvotes':28,'timestamp':time.time()-3600*5,'lat':28.7493,'lng':77.1000,
     'user':'priya','contact':None,'image':None,'status_history':[]},
    {'id':1002,'area':'Karol Bagh','tag':'water','severity':'high',
     'description':'Water supply contaminated near metro exit','status':'acknowledged',
     'upvotes':41,'timestamp':time.time()-3600*30,'lat':28.6520,'lng':77.1904,
     'user':'arjun','contact':None,'image':None,'status_history':[]},
    {'id':1003,'area':'Lajpat Nagar','tag':'electricity','severity':'medium',
     'description':'Streetlights out near Central Market','status':'in_progress',
     'upvotes':15,'timestamp':time.time()-3600*50,'lat':28.5700,'lng':77.2373,
     'user':'meera','contact':None,'image':None,'status_history':[]},
    {'id':1004,'area':'Chandni Chowk','tag':'garbage','severity':'medium',
     'description':'Overflowing bins near Fatehpuri mosque','status':'open',
     'upvotes':8,'timestamp':time.time()-3600*80,'lat':28.6507,'lng':77.2334,
     'user':'rohit','contact':None,'image':None,'status_history':[]},
    {'id':1005,'area':'Dwarka','tag':'sewage','severity':'high',
     'description':'Sewage overflow on Sector 10 road','status':'open',
     'upvotes':33,'timestamp':time.time()-3600*20,'lat':28.5921,'lng':77.0460,
     'user':'kavita','contact':None,'image':None,'status_history':[]},
]

_STUB_NGOS = [
    {'id':1,'name':'Delhi Green Mission','focus':'Sanitation','tag':'garbage',
     'rating':4.6,'area':'Rohini','phone':'011-27551234','email':'contact@delhigreen.org',
     'lat':28.75,'lng':77.10,'issues_resolved':34},
    {'id':2,'name':'Jal Seva Trust','focus':'Water & Sewage','tag':'water',
     'rating':4.7,'area':'Hauz Khas','phone':'011-26960001','email':'help@jalseva.org',
     'lat':28.54,'lng':77.22,'issues_resolved':28},
]


def _seed_stub():
    _state['issues'] = list(_STUB_ISSUES)
    _state['ngos']   = list(_STUB_NGOS)
    print('[database] Running on stub data — set DATABASE_URL to connect to real data')


def _next_int_id(collection):
    n = _state['next_id']
    _state['next_id'] += 1
    return n
