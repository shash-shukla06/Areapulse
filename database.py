"""
Database layer — PostgreSQL primary if DATABASE_URL set, else Firebase Firestore,
else in-memory with seed data.
Single interface so app.py doesn't care which is active.

v3 merge changes:
  - PostgreSQL as primary backend (DATABASE_URL detection)
  - Three modes: postgres → firebase → memory
  - Added get_all_image_hashes + get_recent_reports for Postgres
  - Added insert_spam_issue for Postgres
  - _state['pg_pool'] exposed for app.py direct postgres access
  - All existing Firebase + memory functionality preserved unchanged

v2 fixes (preserved):
  - 200+ seed issues (was 32) spread across all Delhi areas
  - 5-minute in-memory cache for Firebase reads
  - Graceful 429 quota handling: falls back to rich memory seed
  - _seed_firebase_if_empty() now writes even when the read check fails
"""
import os, time, math, json, tempfile, threading, random

# ═══════════════════════════════════════════════════════
#  POSTGRESQL (primary)
# ═══════════════════════════════════════════════════════
_PG_OK = False
try:
    import psycopg
    from psycopg_pool import ConnectionPool
    _PG_OK = True
    print('[database] psycopg + pool available')
except ImportError:
    print('[database] psycopg not installed — Postgres unavailable')


# ═══════════════════════════════════════════════════════
#  AREA COORDINATES + SLA CONSTANTS
#  Moved to domain/constants.py (Phase 3).
#  Re-exported here so existing callers (app.py, templates) keep working.
# ═══════════════════════════════════════════════════════
from domain.constants import (
    AREA_COORDS,
    SLA_HOURS,
    CROWD_ESCALATION_THRESHOLD,
    DELHI_LAT_MIN, DELHI_LAT_MAX,
    DELHI_LNG_MIN, DELHI_LNG_MAX,
)


# ═══════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════
_state = {
    'mode': 'memory',
    'fs_db': None,
    'pg_pool': None,
    'issues': [],
    'spam_issues': [],
    'ngos': [],
    'next_id': 1,
    'lock': threading.Lock(),
    'upvoters': {},
    'recent_reports': {},
}

# ── READ CACHE (prevents quota exhaustion) ─────────────
_cache = {
    'issues':    None,
    'issues_ts': 0.0,
}
_CACHE_TTL = 300  # 5 minutes

def _get_cached_issues():
    now = time.time()
    if _cache['issues'] is not None and (now - _cache['issues_ts']) < _CACHE_TTL:
        return _cache['issues']
    return None

def _set_cached_issues(issues):
    _cache['issues'] = issues
    _cache['issues_ts'] = time.time()

def _invalidate_cache():
    _cache['issues']    = None
    _cache['issues_ts'] = 0.0

# ──────────────────────────────────────────────────────

# SLA_HOURS, CROWD_ESCALATION_THRESHOLD now imported from domain.constants above.


# ═══════════════════════════════════════════════════════
#  INIT  —  postgres → firebase → memory
# ═══════════════════════════════════════════════════════
def init_db():
    """Try DATABASE_URL (Postgres) first, then Firebase, then in-memory seeds."""
    dsn = os.environ.get('DATABASE_URL', '').strip()

    # ── Attempt 1: PostgreSQL ──────────────────────────
    if dsn and _PG_OK:
        try:
            # Pool tuning — keepalives prevent SSL/TCP dead connections from
            # going undetected (the ssl/tls alert bad record mac error).
            # TCP keepalives probe the Neon connection every 30s and detect
            # failures within 15s (5s interval × 3 probes), so the pool
            # discards dead connections before a request tries to use them.
            # reconnect_timeout is left at default (300s) so the pool's
            # background worker has 5 full minutes to restore connections
            # after a Neon hiccup — never set this below connect_timeout.
            _state['pg_pool'] = ConnectionPool(
                dsn,
                min_size=1, max_size=3,
                open=True,
                timeout=15,
                max_idle=60,
                kwargs={
                    "connect_timeout": 10,
                    "keepalives":       1,
                    "keepalives_idle":  30,
                    "keepalives_interval": 5,
                    "keepalives_count": 3,
                },
                configure=_ensure_pg_schema,
            )
            _state['mode'] = 'postgres'
            print('[database] Postgres connected (primary)')
            # Seed if empty (safe — uses ON CONFLICT DO NOTHING)
            _seed_postgres_if_empty()
            return
        except Exception as e:
            print(f'[database] Postgres connection failed ({type(e).__name__}: {e}), trying Firebase...')
            _state['pg_pool'] = None

    # ── Attempt 2: Firebase Firestore ──────────────────
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
        print('[database] Firebase connected')
        _seed_firebase_if_empty()
    except Exception as e:
        print(f'[database] Firebase unavailable ({type(e).__name__}), using in-memory mode')
        _state['mode'] = 'memory'
        _seed_memory()


# ═══════════════════════════════════════════════════════
#  POSTGRES HELPERS
# ═══════════════════════════════════════════════════════

def _ensure_pg_schema(conn):
    """Ensure tables + indexes exist on every new connection."""
    ddl = """
    CREATE TABLE IF NOT EXISTS issues (
        id              BIGINT PRIMARY KEY,
        user_name       TEXT,
        area            TEXT,
        description     TEXT,
        severity        TEXT,
        tag             TEXT,
        status          TEXT DEFAULT 'open',
        lat             DOUBLE PRECISION,
        lng             DOUBLE PRECISION,
        landmark        TEXT,
        contact         TEXT,
        image           TEXT,
        image_hash      TEXT,
        timestamp       DOUBLE PRECISION,
        upvotes         INTEGER DEFAULT 0,
        verified        BOOLEAN DEFAULT FALSE,
        escalated       BOOLEAN DEFAULT FALSE,
        resolved        BOOLEAN DEFAULT FALSE,
        is_verified     BOOLEAN DEFAULT FALSE,
        is_escalated    BOOLEAN DEFAULT FALSE,
        status_history  JSONB DEFAULT '[]'::jsonb,
        escalation_reason TEXT,
        escalated_at    DOUBLE PRECISION,
        resolved_at     DOUBLE PRECISION,
        assigned_to     TEXT,
        ai_confidence   INTEGER,
        verified_by     TEXT
    );

    CREATE TABLE IF NOT EXISTS ngos (
        id              BIGINT PRIMARY KEY,
        name            TEXT NOT NULL,
        focus           TEXT,
        tag             TEXT,
        rating          REAL,
        area            TEXT,
        phone           TEXT,
        email           TEXT,
        lat             DOUBLE PRECISION,
        lng             DOUBLE PRECISION,
        issues_resolved INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS spam_issues (
        id              BIGSERIAL PRIMARY KEY,
        user_name       TEXT,
        description     TEXT,
        tag             TEXT,
        severity        TEXT,
        area            TEXT,
        lat             DOUBLE PRECISION,
        lng             DOUBLE PRECISION,
        image           TEXT,
        timestamp       DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
        spam_verdict    TEXT,
        spam_reason     TEXT,
        spam_confidence INTEGER
    );

    -- Add columns if missing (migration safety)
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS issues_resolved INTEGER DEFAULT 0;
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS lat             DOUBLE PRECISION;
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS lng             DOUBLE PRECISION;
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS phone           TEXT;
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS email           TEXT;
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS focus           TEXT;
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS tag             TEXT;
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS rating          REAL;
    ALTER TABLE ngos   ADD COLUMN IF NOT EXISTS area            TEXT;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS image_hash      TEXT;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS status_history  JSONB DEFAULT '[]'::jsonb;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS escalation_reason TEXT;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS escalated_at    DOUBLE PRECISION;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS resolved_at     DOUBLE PRECISION;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS assigned_to     TEXT;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS ai_confidence   INTEGER;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS verified        BOOLEAN DEFAULT FALSE;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS escalated       BOOLEAN DEFAULT FALSE;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS resolved        BOOLEAN DEFAULT FALSE;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS is_verified     BOOLEAN DEFAULT FALSE;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS is_escalated    BOOLEAN DEFAULT FALSE;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS verified_by     TEXT;

    CREATE TABLE IF NOT EXISTS duplicate_log (
        id              BIGSERIAL PRIMARY KEY,
        original_id     BIGINT,
        duplicate_desc  TEXT,
        user_name       TEXT,
        tag             TEXT,
        severity        TEXT,
        lat             DOUBLE PRECISION,
        lng             DOUBLE PRECISION,
        distance_m      DOUBLE PRECISION,
        reason          TEXT,
        timestamp       DOUBLE PRECISION
    );

    -- Phase 4: persistent user accounts (replaces hardcoded GOV/NGO dicts in prod)
    CREATE TABLE IF NOT EXISTS users (
        id              BIGSERIAL PRIMARY KEY,
        username        TEXT UNIQUE NOT NULL,
        display_name    TEXT NOT NULL,
        role            TEXT NOT NULL,            -- 'gov_officer' | 'ngo_manager' | 'citizen'
        pin_hash        TEXT NOT NULL,             -- bcrypt hash of PIN
        tags            JSONB DEFAULT '[]'::jsonb, -- issue tags (gov only)
        authority       TEXT,                      -- govt authority name (gov only)
        org_name        TEXT,                      -- NGO org name (ngo only)
        operating_areas JSONB DEFAULT '[]'::jsonb, -- NGO areas (ngo only)
        focus           TEXT,                      -- NGO focus description
        created_at      DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
        is_active       BOOLEAN DEFAULT TRUE
    );
    CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
    CREATE INDEX IF NOT EXISTS idx_users_role     ON users(role);

    -- Phase 6: persistent NGO commitments (replaces _ngo_commitments_store list in app.py)
    CREATE TABLE IF NOT EXISTS ngo_commitments (
        id              BIGSERIAL PRIMARY KEY,
        ngo_username    TEXT NOT NULL,
        area            TEXT NOT NULL,
        tag             TEXT NOT NULL,
        status          TEXT DEFAULT 'active',   -- active | completed | withdrawn
        committed_at    DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
        updated_at      DOUBLE PRECISION,
        notes           TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ngo_commit_user ON ngo_commitments(ngo_username);
    CREATE INDEX IF NOT EXISTS idx_ngo_commit_area ON ngo_commitments(area);
    CREATE INDEX IF NOT EXISTS idx_ngo_commit_tag  ON ngo_commitments(tag);

    ALTER TABLE issues ADD COLUMN IF NOT EXISTS upvoters        JSONB DEFAULT '[]'::jsonb;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS last_updated_at DOUBLE PRECISION;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS last_updated_by TEXT;

    CREATE INDEX IF NOT EXISTS idx_issues_tag      ON issues(tag);
    CREATE INDEX IF NOT EXISTS idx_issues_status   ON issues(status);
    CREATE INDEX IF NOT EXISTS idx_issues_time     ON issues(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_ngos_tag        ON ngos(tag);
    CREATE INDEX IF NOT EXISTS idx_spam_verdict    ON spam_issues(spam_verdict);
    CREATE INDEX IF NOT EXISTS idx_dup_log_orig    ON duplicate_log(original_id);

    -- Phase 0 Fix 4: atomic ID generation via Postgres sequence.
    -- The issues table was created with BIGINT PRIMARY KEY (app-assigned).
    -- We introduce a sequence and set it as the column default so that
    -- INSERT ... RETURNING id is atomic — no two concurrent inserts can
    -- ever claim the same ID.  The sequence starts after the current MAX
    -- so existing seed data is not disturbed.
    CREATE SEQUENCE IF NOT EXISTS issues_id_seq;
    SELECT setval('issues_id_seq', GREATEST((SELECT COALESCE(MAX(id), 0) FROM issues), 1), true);
    ALTER TABLE issues ALTER COLUMN id SET DEFAULT nextval('issues_id_seq');
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def _pg_row_to_issue(row):
    """Convert a Postgres issues row (dict or sequence) to the dict format app.py expects."""
    if isinstance(row, dict):
        r = row
    elif hasattr(row, '_asdict'):
        r = row._asdict()
    else:
        # Fallback: assume positional — expand to match SELECT * order
        keys = [
            'id','user_name','area','description','severity','tag','status',
            'lat','lng','landmark','contact','image','image_hash','timestamp',
            'upvotes','verified','escalated','resolved',
            'is_verified','is_escalated','status_history',
            'escalation_reason','escalated_at','resolved_at','assigned_to',
            'ai_confidence','verified_by','upvoters','last_updated_at','last_updated_by',
        ]
        r = {k: row[i] if i < len(row) else None for i, k in enumerate(keys)}

    # Map DB column user_name → user for app.py compatibility
    result = {k: v for k, v in r.items()}
    if 'user_name' in result and result.get('user_name') is not None:
        result['user'] = result.pop('user_name')
    elif 'user_name' in result:
        result['user'] = result.pop('user_name')
    # Ensure status_history is a list, not a JSONB string
    sh = result.get('status_history')
    if isinstance(sh, str):
        try:
            result['status_history'] = json.loads(sh)
        except Exception:
            result['status_history'] = []
    elif sh is None:
        result['status_history'] = []
    upvoters = result.get('upvoters') or []
    if isinstance(upvoters, str):
        try:
            upvoters = json.loads(upvoters)
        except Exception:
            upvoters = []
    result['upvoters'] = upvoters
    return result


def _pg_next_id(conn, table):
    """Get next integer ID for a table."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}")
        return cur.fetchone()[0]


def _seed_postgres_if_empty():
    """Seed Postgres with sample data if issues table is empty."""
    try:
        with _state['pg_pool'].connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM issues")
                count = cur.fetchone()[0]
                if count > 0:
                    print(f'[database] Postgres already has {count} issues, skipping seed')
                    return
    except Exception as e:
        print(f'[database] Could not check Postgres emptiness: {e}')
        return

    # Import seed data and insert
    try:
        now = time.time()
        issue_rows = []
        for idx, (area, tag, sev, desc) in enumerate(_SEED_ISSUES):
            lat, lng = AREA_COORDS.get(area, (28.6139, 77.2090))
            lat += (idx % 9 - 4) * 0.0018
            lng += ((idx // 9) % 9 - 4) * 0.0018
            age_hours = (idx * 2.3) % (24 * 25)
            issue_id = idx + 1
            issue_rows.append((
                issue_id, _USERS[idx % len(_USERS)], area, desc, sev, tag,
                'resolved' if idx % 9 == 0 else ('escalated' if idx % 11 == 0 else 'open'),
                round(lat, 6), round(lng, 6), '', '', None, None,
                now - (age_hours * 3600), (idx * 7) % 20,
                False, idx % 11 == 0, idx % 9 == 0,
                False, idx % 11 == 0, '[]',
                None, None, None, None, None, None,
            ))

        ngo_rows = []
        for idx, (name, focus, tag, rating, area, phone, email) in enumerate(_SEED_NGOS):
            lat, lng = AREA_COORDS.get(area, (28.6139, 77.2090))
            ngo_rows.append((
                idx + 1, name, focus, tag, float(rating), area, phone, email,
                lat + 0.005, lng + 0.005, (idx * 3) % 25 + 5,
            ))

        with _state['pg_pool'].connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO issues
                        (id, user_name, area, description, severity, tag, status,
                         lat, lng, landmark, contact, image, image_hash, timestamp,
                         upvotes, verified, escalated, resolved,
                         is_verified, is_escalated, status_history,
                         escalation_reason, escalated_at, resolved_at,
                         assigned_to, ai_confidence, verified_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s,
                               %s, %s, %s,
                               %s, %s, %s,
                               %s, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    issue_rows,
                )
                cur.executemany(
                    """INSERT INTO ngos
                        (id, name, focus, tag, rating, area, phone, email, lat, lng, issues_resolved)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    ngo_rows,
                )
            conn.commit()
        print(f'[database] Postgres seeded with {len(issue_rows)} issues, {len(ngo_rows)} NGOs')
    except Exception as e:
        print(f'[database] Postgres seed failed: {e}')


# ═══════════════════════════════════════════════════════
#  GETTERS
# ═══════════════════════════════════════════════════════
def get_areas():
    return sorted(AREA_COORDS.keys())


def get_issues(tag=None, status=None, limit=300):
    """List issues — postgres → firebase (cached) → memory."""
    # ── Postgres ───────────────────────────────────────
    if _state['mode'] == 'postgres':
        last_error = None
        for attempt in range(2):
            try:
                with _state['pg_pool'].connection() as conn:
                    with conn.cursor() as cur:
                        params = []
                        if tag and status:
                            q = ("SELECT * FROM issues WHERE tag=%s AND status=%s "
                                 "ORDER BY timestamp DESC LIMIT %s")
                            params = [tag, status, limit]
                        elif tag:
                            q = ("SELECT * FROM issues WHERE tag=%s "
                                 "ORDER BY timestamp DESC LIMIT %s")
                            params = [tag, limit]
                        elif status:
                            q = ("SELECT * FROM issues WHERE status=%s "
                                 "ORDER BY timestamp DESC LIMIT %s")
                            params = [status, limit]
                        else:
                            q = "SELECT * FROM issues ORDER BY timestamp DESC LIMIT %s"
                            params = [limit]
                        cur.execute(q, params)
                        rows = cur.fetchall()
                        if rows and hasattr(rows[0], '_asdict'):
                            return [_pg_row_to_issue(r) for r in rows]
                        elif cur.description:
                            cols = [d.name for d in cur.description]
                            return [_pg_row_to_issue({cols[i]: row[i] for i in range(len(cols))})
                                    for row in rows]
                        return []
            except psycopg.Error as e:
                last_error = e
                print(f'[database] get_issues attempt {attempt+1}/2 failed: {type(e).__name__}: {e}')
                if attempt == 0:
                    time.sleep(0.5)
                    continue
        print(f'[database] get_issues failed after retry: {last_error}')
        return []

    # ── Firebase (cached) ──────────────────────────────
    if _state['mode'] == 'firebase':
        cached = _get_cached_issues()
        if cached is not None:
            results = cached
        else:
            try:
                q    = _state['fs_db'].collection('issues')
                docs = q.limit(limit).stream()
                results = []
                for d in docs:
                    data = d.to_dict()
                    data.setdefault('id', d.id)
                    results.append(data)
                results.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                _set_cached_issues(results)
                print(f'[database] Cache refreshed: {len(results)} issues from Firebase')
            except Exception as e:
                print(f'[database] Firestore read failed -> memory fallback: {e}')
                results = list(_state['issues'])

        if tag:    results = [i for i in results if i.get('tag') == tag]
        if status: results = [i for i in results if (i.get('status') or 'open') == status]
        return results[:limit]

    # ── Pure memory mode ───────────────────────────────
    results = list(_state['issues'])
    if tag:    results = [i for i in results if i.get('tag') == tag]
    if status: results = [i for i in results if (i.get('status') or 'open') == status]
    results.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    return results[:limit]


def get_all_ngos():
    """List all NGOs — postgres → firebase → memory."""
    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM ngos")
                    rows = cur.fetchall()
                    if not rows:
                        return []
                    if hasattr(rows[0], '_asdict'):
                        return [_ngo_row_to_dict(r) for r in rows]
                    elif hasattr(cur, 'description') and cur.description:
                        cols = [d.name for d in cur.description]
                        return [_ngo_row_to_dict({cols[i]: row[i] for i in range(len(cols))}) for row in rows]
                    return []
        except Exception as e:
            print(f'[database] Postgres get_all_ngos failed: {e}')
            return []

    if _state['mode'] == 'firebase':
        try:
            docs = _state['fs_db'].collection('ngos').stream()
            return [{**d.to_dict(), 'id': d.id} for d in docs]
        except Exception:
            pass
    return list(_state['ngos'])


def _ngo_row_to_dict(row):
    """Convert a Postgres ngos row to dict."""
    if isinstance(row, dict):
        r = row
    elif hasattr(row, '_asdict'):
        r = row._asdict()
    else:
        return {}
    result = {k: v for k, v in r.items()}
    return result


def get_nearby_ngos(lat, lng, tag=None, limit=5, radius_km=8):
    if lat is None or lng is None:
        return []
    ngos = get_all_ngos()
    results = []
    for n in ngos:
        if not n.get('lat') or not n.get('lng'):
            continue
        dist = _haversine(lat, lng, float(n['lat']), float(n['lng']))
        if dist > radius_km:
            continue
        score = 1.0
        if tag and n.get('tag') == tag:
            score += 5.0
        results.append({**n, 'distance_km': round(dist, 2), '_score': score - dist * 0.1})
    results.sort(key=lambda x: x.get('_score', 0), reverse=True)
    return results[:limit]


def get_all_image_hashes() -> list:
    """Return list of every stored image_hash string (non-None only)."""
    # Prefer the in-memory cache (already hydrated by get_issues)
    cached = _get_cached_issues()
    if cached is not None:
        return [i['image_hash'] for i in cached if i.get('image_hash')]

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT image_hash FROM issues WHERE image_hash IS NOT NULL")
                    return [row[0] for row in cur.fetchall()]
        except Exception as e:
            print(f'[database] get_all_image_hashes Postgres failed: {e}')

    if _state['mode'] == 'firebase':
        try:
            docs = _state['fs_db'].collection('issues').stream()
            return [d.to_dict().get('image_hash') for d in docs
                    if d.to_dict().get('image_hash')]
        except Exception as e:
            print(f'[database] get_all_image_hashes Firebase read failed: {e}')

    return [i['image_hash'] for i in _state['issues'] if i.get('image_hash')]


def get_recent_reports(hours: int = 24) -> list:
    """Return issues filed in the last N hours as lightweight dicts."""
    cutoff = time.time() - (hours * 3600)

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT lat, lng, user_name, tag, timestamp
                           FROM issues
                           WHERE timestamp >= %s AND lat IS NOT NULL AND lng IS NOT NULL""",
                        (cutoff,),
                    )
                    return [
                        {'lat': row[0], 'lng': row[1], 'user_id': row[2], 'tag': row[3]}
                        for row in cur.fetchall()
                    ]
        except Exception as e:
            print(f'[database] get_recent_reports Postgres failed: {e}')

    # Fallback for firebase + memory (uses get_issues which is cached)
    issues = get_issues(limit=500)
    return [
        {
            'lat':     i.get('lat'),
            'lng':     i.get('lng'),
            'user_id': i.get('user'),
            'tag':     i.get('tag'),
        }
        for i in issues
        if (i.get('timestamp') or 0) >= cutoff
        and i.get('lat') is not None
        and i.get('lng') is not None
    ]


# ═══════════════════════════════════════════════════════
#  WRITERS
# ═══════════════════════════════════════════════════════
def insert_issue(user, area, description, severity, tag,
                 landmark='', contact='', lat=None, lng=None, image=None,
                 image_hash=None):
    # For Firebase and memory modes we still need a pre-assigned ID so we can
    # build the in-memory record dict before writing.  For Postgres we let the
    # database assign the ID atomically via the sequence (Phase 0 Fix 4).
    ts = time.time()

    _invalidate_cache()

    # ── Postgres: let the sequence assign the ID atomically (RETURNING id) ───
    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO issues
                            (user_name, area, description, severity, tag, status,
                             lat, lng, landmark, contact, image, image_hash, timestamp,
                             upvotes, verified, escalated, resolved)
                           VALUES (%s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s)
                           RETURNING id""",
                        (user, area, description, severity, tag, 'open',
                         lat, lng, landmark, contact, image, image_hash, ts,
                         0, False, False, False),
                    )
                    issue_id = cur.fetchone()[0]
                conn.commit()
            return issue_id
        except Exception as e:
            print(f'[database] Postgres insert_issue failed: {e}')
            return None  # don't fall through to memory store in postgres mode

    # ── Firebase / memory: pre-assign ID via counter ─────────────────────────
    with _state['lock']:
        issue_id = _next_int_id('issues')

    record = {
        'id': issue_id, 'user': user, 'area': area,
        'description': description, 'severity': severity, 'tag': tag,
        'status': 'open', 'landmark': landmark, 'contact': contact,
        'lat': lat, 'lng': lng, 'image': image,
        'image_hash': image_hash,
        'timestamp': ts, 'upvotes': 0,
        'verified': False, 'escalated': False, 'resolved': False,
    }

    if _state['mode'] == 'firebase':
        try:
            _state['fs_db'].collection('issues').document(str(issue_id)).set(record)
        except Exception as e:
            print(f'[database] Firestore write failed, saving to memory: {e}')
            _state['issues'].insert(0, record)
    else:
        _state['issues'].insert(0, record)

    return issue_id


def upvote_issue(issue_id, user):
    upvoters = _state['upvoters'].setdefault(issue_id, set())
    _invalidate_cache()

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    # Read current upvoters
                    cur.execute(
                        "SELECT upvoters FROM issues WHERE id = %s",
                        (issue_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return 'not_found'
                    current_upvoters = row[0] or []
                    if isinstance(current_upvoters, str):
                        try:
                            current_upvoters = json.loads(current_upvoters)
                        except Exception:
                            current_upvoters = []
                    if not isinstance(current_upvoters, list):
                        current_upvoters = []

                    if user in current_upvoters:
                        current_upvoters.remove(user)
                        action = 'removed'
                    else:
                        current_upvoters.append(user)
                        action = 'added'

                    cur.execute(
                    "UPDATE issues SET upvoters = %s, upvotes = GREATEST(0, upvotes + %s) WHERE id = %s",
                    (json.dumps(current_upvoters), 1 if action == 'added' else -1, issue_id),
                    )
                conn.commit()
                return action
        except Exception as e:
            print(f'[database] Postgres upvote failed: {e}')

    if _state['mode'] == 'firebase':
        try:
            doc_ref = _state['fs_db'].collection('issues').document(str(issue_id))
            snap = doc_ref.get()
            if not snap.exists:
                return 'not_found'
            data = snap.to_dict()
            ups = set(data.get('upvoters', []))
            if user in ups:
                ups.remove(user); action = 'removed'
            else:
                ups.add(user); action = 'added'
            doc_ref.update({'upvoters': list(ups), 'upvotes': len(ups)})
            return action
        except Exception as e:
            print(f'[database] Firestore upvote failed: {e}')

    for i in _state['issues']:
        if int(i.get('id', -1)) == int(issue_id):
            if user in upvoters:
                upvoters.remove(user); i['upvotes'] = max(0, i.get('upvotes', 0) - 1)
                return 'removed'
            else:
                upvoters.add(user); i['upvotes'] = i.get('upvotes', 0) + 1
                return 'added'
    return 'not_found'


# ═══════════════════════════════════════════════════════
#  INTERNALS
# ═══════════════════════════════════════════════════════
def _next_int_id(collection):
    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                return _pg_next_id(conn, collection)
        except Exception:
            pass

    if _state['mode'] == 'firebase':
        try:
            cref = _state['fs_db'].collection('_counters').document(collection)
            snap = cref.get()
            n = (snap.to_dict() or {}).get('n', 0) + 1 if snap.exists else 1
            cref.set({'n': n})
            return n
        except Exception:
            pass

    n = _state['next_id']
    _state['next_id'] += 1
    return n


def _haversine(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))


# ═══════════════════════════════════════════════════════
#  SEED DATA  — 200 issues across all Delhi areas
# ═══════════════════════════════════════════════════════
# Format: (area, tag, severity, description)
_SEED_ISSUES = [
    # ── POTHOLES ──────────────────────────────────────────────────
    ('Rohini',          'pothole','high',   'Large pothole on Sector 7 main road, multiple bike accidents reported this week'),
    ('Karol Bagh',      'pothole','high',   'Deep crater near metro station exit, vehicles swerving dangerously'),
    ('Dwarka',          'pothole','medium', 'Multiple potholes on Sector 10 internal road after monsoon'),
    ('Pitampura',       'pothole','medium', 'Potholes near community centre causing daily traffic jams'),
    ('Model Town',      'pothole','high',   'Deep pothole on E Block road, car suspension damaged last night'),
    ('Mayur Vihar',     'pothole','medium', 'Phase 1 Extension road full of potholes, auto-rickshaws refusing route'),
    ('Lajpat Nagar',    'pothole','low',    'Small potholes appearing near Central Market, needs preventive repair'),
    ('Greater Kailash', 'pothole','low',    'M-Block market road needs resurfacing, potholes worsening'),
    ('Nehru Place',     'pothole','medium', 'Potholes near IT park entrance, heavy vehicle damage'),
    ('Janakpuri',       'pothole','high',   'Pothole-ridden road in C Block, school bus nearly overturned'),
    ('Saket',           'pothole','medium', 'Select City Walk access road pothole causing traffic backlog'),
    ('Vasant Kunj',     'pothole','low',    'Aruna Asaf Ali Marg developing potholes near mall'),
    ('Rajouri Garden',  'pothole','high',   'Main metro feeder road completely broken, emergency needed'),
    ('Punjabi Bagh',    'pothole','medium', 'West Avenue Road potholes accumulating near park'),
    ('Okhla',           'pothole','high',   'Industrial area road dangerous for trucks, pothole 2 feet deep'),
    ('Kalkaji',         'pothole','medium', 'Near Kalkaji Mandir, road surface broken after recent digging'),
    ('Chandni Chowk',   'pothole','medium', 'Naya Bazar road pothole causing rickshaw accidents daily'),
    ('Paharganj',       'pothole','high',   'Main Bazaar road pothole, tourist complaints rising'),
    ('Civil Lines',     'pothole','low',    'Flagstaff Road developing potholes near ITO'),
    ('Shahdara',        'pothole','medium', 'GT Road pothole cluster near Shahdara metro, peak-hour danger'),
    # ── WATER ─────────────────────────────────────────────────────
    ('Dwarka',          'water', 'high',   'Major water pipe burst flooding Sector 5 road, supply cut for 3 days'),
    ('Janakpuri',       'water', 'medium', 'No water supply in C Block for 3 days, tanker request ignored'),
    ('Civil Lines',     'water', 'medium', 'Water seepage from main road tap near ISBT, wastage for 2 weeks'),
    ('Defence Colony',  'water', 'medium', 'Brown water from taps in C Block, possible contamination'),
    ('Rohini',          'water', 'high',   'Underground pipe burst in Sector 15, road collapsing into sinkhole'),
    ('Mehrauli',        'water', 'medium', 'Water supply only 30 minutes daily, residents using tankers'),
    ('Karol Bagh',      'water', 'low',    'Slow water pressure in DDA flats, top floors getting no supply'),
    ('Hauz Khas',       'water', 'high',   'Water contamination complaint, yellowish supply since yesterday'),
    ('Pitampura',       'water', 'medium', 'Water meter reading incorrect, bill tripled this month'),
    ('Preet Vihar',     'water', 'high',   'Pipeline burst near market, 500 families without water 24 hours'),
    ('Vasant Vihar',    'water', 'low',    'Overhead tank overflow wasting hundreds of litres daily'),
    ('Model Town',      'water', 'medium', 'Water timing changed without notice, residents miss supply window'),
    ('Sarojini Nagar',  'water', 'high',   'Old colonial pipe burst, massive waterlogging near market'),
    ('Laxmi Nagar',     'water', 'medium', 'Water supply contaminated after nearby construction'),
    ('Patel Nagar',     'water', 'medium', 'No supply alternate days, official schedule not followed'),
    # ── GARBAGE ───────────────────────────────────────────────────
    ('Karol Bagh',      'garbage','medium', 'Overflowing dustbin near Ajmal Khan Road metro entrance, 3 days'),
    ('Mehrauli',        'garbage','high',   'Illegal garbage dump near heritage zone growing daily'),
    ('Shahdara',        'garbage','high',   'MCD garbage truck not visiting sector for over a week'),
    ('Connaught Place', 'garbage','medium', 'Litter accumulation around inner circle benches and gardens'),
    ('Nizamuddin',      'garbage','medium', 'Construction debris dumped illegally on Mathura Road service lane'),
    ('Lajpat Nagar',    'garbage','high',   'Garbage pile near Central Market, causing stench and flies'),
    ('Okhla',           'garbage','high',   'Industrial waste dumped in residential area, health hazard'),
    ('Dwarka',          'garbage','medium', 'Sector 12 park dustbin overflowing, not cleared in 5 days'),
    ('Rohini',          'garbage','medium', 'Sector 7 market garbage not collected, vendor complaints'),
    ('Mukherjee Nagar', 'garbage','medium', 'Student hostel area overflowing bins, disease risk rising'),
    ('Saket',           'garbage','low',    'Mall area garbage not cleared on Sundays, stench complaint'),
    ('RK Puram',        'garbage','high',   'Community park used as garbage dump at night by nearby shops'),
    ('Vasant Kunj',     'garbage','medium', 'DLF area garbage timing issue, bins full before truck comes'),
    ('Kashmere Gate',   'garbage','high',   'Old Delhi wholesale market area garbage crisis, rodent sighting'),
    ('Kalkaji',         'garbage','medium', 'Temple area garbage accumulation on festival days'),
    # ── STREETLIGHT ───────────────────────────────────────────────
    ('Lajpat Nagar',    'streetlight','low',    'Broken streetlight outside Central Market gate 3, existing since 2 weeks'),
    ('Hauz Khas',       'streetlight','medium', 'Village road unlit at night, incidents increasing'),
    ('Vasant Kunj',     'streetlight','medium', 'Five streetlights out on Nelson Mandela Road stretch'),
    ('Sarojini Nagar',  'streetlight','medium', 'Market area dark after sunset, safety concern for women'),
    ('Pitampura',       'streetlight','low',    'Solar light near park with dead battery, no maintenance'),
    ('Rajouri Garden',  'streetlight','low',    'Street light flickering near metro pillar 405'),
    ('INA',             'streetlight','medium', 'Underpass lights out for 2 months, accident reported'),
    ('Mayur Vihar',     'streetlight','high',   'Entire Phase 3 road unlit, women attacked last week'),
    ('Mukherjee Nagar', 'streetlight','medium', 'Coaching area unsafe at night, 4 lights non-functional'),
    ('Mehrauli',        'streetlight','medium', 'Qutub area approach road dark at night'),
    ('Civil Lines',     'streetlight','low',    'Parks Magistrate lane poorly lit, jogger safety concern'),
    ('Lodhi Colony',    'streetlight','medium', 'Garden approach road pitch dark after 9pm'),
    ('Nizamuddin',      'streetlight','low',    'Dargah approach lane completely unlit'),
    ('Shahdara',        'streetlight','medium', 'Bus stand area dark, antisocial elements gathering'),
    ('Preet Vihar',     'streetlight','high',   'Metro feeder road dark, two snatching incidents this week'),
    # ── TRAFFIC ───────────────────────────────────────────────────
    ('Chandni Chowk',   'traffic','medium', 'Traffic signal malfunctioning at Lal Quila intersection since Monday'),
    ('Preet Vihar',     'traffic','medium', 'Signal timer too short on Ring Road junction, 2km jams nightly'),
    ('Connaught Place', 'traffic','high',   'Illegal parking on inner circle blocking emergency vehicle lane'),
    ('Dwarka',          'traffic','medium', 'Sector 9 market encroachment reducing road to single lane'),
    ('Hauz Khas',       'traffic','high',   'Village road completely blocked by pub-goers parking'),
    ('Rohini',          'traffic','medium', 'Sector 3 school zone no speed breakers, children at risk'),
    ('Kashmere Gate',   'traffic','high',   'Bus terminal overflowing, blocking main GT Karnal Road'),
    ('Okhla',           'traffic','medium', 'Industrial area truck movement blocking residential access'),
    ('Laxmi Nagar',     'traffic','medium', 'Vikas Marg encroachment by vegetable market every morning'),
    ('Janakpuri',       'traffic','high',   'Signal at B1-B2 junction broken for 4 days, accidents'),
    ('Model Town',      'traffic','medium', 'Sabzi Mandi market vehicles blocking entire stretch 7-11am'),
    ('Pitampura',       'traffic','low',    'Speed breaker removed during road work, not replaced'),
    ('RK Puram',        'traffic','medium', 'Sector 4 crossroads no traffic police during peak hour'),
    # ── SEWAGE ────────────────────────────────────────────────────
    ('Saket',           'sewage', 'high',   'Sewage overflow near NSP housing complex, foul smell unbearable'),
    ('Kashmere Gate',   'sewage', 'high',   'Open manhole on busy road near bus stand, no warning signs'),
    ('Malviya Nagar',   'sewage', 'medium', 'Drain blocked behind District Court, flooding during rain'),
    ('Mehrauli',        'sewage', 'high',   'Sewage seeping from Mehrauli drain into residential lanes'),
    ('Okhla',           'sewage', 'high',   'Industrial effluent mixing with residential sewage drain'),
    ('Shahdara',        'sewage', 'high',   'Main sewer collapsed under road near market, 50m exposure'),
    ('Rohini',          'sewage', 'medium', 'Drainage blocked in Sector 11 colony after heavy rain'),
    ('Laxmi Nagar',     'sewage', 'high',   'Sewage overflow entering ground floor homes in B Block'),
    ('Chandni Chowk',   'sewage', 'high',   'Old sewer collapsed near Kinari Bazaar, health emergency'),
    ('Preet Vihar',     'sewage','medium', 'Drain cover broken near school, open sewage gap'),
    ('Patel Nagar',     'sewage','medium', 'Drainage not cleaned for months, mosquito breeding'),
    ('Vasant Kunj',     'sewage','high',   'DLF Promenade back drain overflowing after last night rain'),
    ('Janakpuri',       'sewage','medium', 'Colony drain blocked by tree roots, backing up in basement'),
    ('Kalkaji',         'sewage','high',   'Sewage mixing with drinking water supply, urgent fix needed'),
    # ── ELECTRICITY ───────────────────────────────────────────────
    ('Hauz Khas',       'electricity','medium', 'Frequent power outages in SDA, transformer humming loudly'),
    ('Laxmi Nagar',     'electricity','medium', 'Exposed live wires at chest height near market entrance'),
    ('Patel Nagar',     'electricity','high',   'Daily 4-hour power cuts disrupting work-from-home'),
    ('INA',             'electricity','low',    'Generator running 24/7 near residential building, noise + fumes'),
    ('Defence Colony',  'electricity','medium', 'Electricity bill tripled, meter not checked in 6 months'),
    ('Dwarka',          'electricity','high',   'Transformer tripped, Sector 6 without power 20+ hours'),
    ('Mukherjee Nagar', 'electricity','medium', 'Power fluctuation damaging electronics, 3 inverters blown'),
    ('Rohini',          'electricity','medium', 'New connection pending 4 months despite payment'),
    ('Pitampura',       'electricity','high',   'Live wire hanging from pole after storm, sparking on tree'),
    ('Sarojini Nagar',  'electricity','medium', 'Market area power cuts exactly 6-10pm daily for 2 weeks'),
    ('Vasant Vihar',    'electricity','low',    'Electric meter reading appears incorrect, abnormal bill'),
    ('Lodhi Colony',    'electricity','medium', 'Substation issue causing repeated outages in south block'),
    ('Chandni Chowk',   'electricity','high',   'Open electrical box near school gate, children at risk'),
    ('Nizamuddin',      'electricity','medium', 'Cable fault since Tuesday, no restoration schedule given'),
    # ── NOISE ─────────────────────────────────────────────────────
    ('Mukherjee Nagar', 'noise',  'low',    'Loud construction at night past 11 PM violating noise norms'),
    ('INA',             'noise',  'medium', 'Banquet hall DJ past midnight every weekend'),
    ('Paharganj',       'noise',  'high',   'Generator noise from 3 hotels all night, residents sleepless'),
    ('Kashmere Gate',   'noise',  'medium', 'Loudspeaker from shop from 6am to 10pm daily'),
    ('Hauz Khas',       'noise',  'high',   'Bar music till 3am in village, police complaint filed'),
    ('Connaught Place', 'noise',  'medium', 'Road drilling at midnight for metro work, unbearable'),
    ('Model Town',      'noise',  'low',    'Transformer humming very loud since new installation'),
    ('Rohini',          'noise',  'medium', 'Construction blasting noise near hospital zone'),
    ('Lajpat Nagar',    'noise',  'low',    'Market loudspeaker announcements disrupting nearby school'),
    ('Vasant Kunj',     'noise',  'low',    'Mall loading dock night deliveries waking residents'),
    # ── TREE ──────────────────────────────────────────────────────
    ('Mayur Vihar',     'tree',   'medium', 'Fallen tree blocking lane near Phase 1 metro after storm'),
    ('Punjabi Bagh',    'tree',   'low',    'Tree branch hanging dangerously over road near Club Road'),
    ('Lodhi Colony',    'tree',   'low',    'Trees need pruning, branches touching 11kV power lines'),
    ('Mehrauli',        'tree',   'high',   'Large dead tree leaning over residential building, urgent'),
    ('Hauz Khas',       'tree',   'medium', 'Tree roots breaking footpath, tripping hazard near market'),
    ('Civil Lines',     'tree',   'medium', 'Diseased tree spreading to others in Coronation Park'),
    ('Vasant Vihar',    'tree',   'low',    'Tree planted too close to compound wall, cracking it'),
    ('RK Puram',        'tree',   'high',   'Old peepal tree leaning at 45 degrees after rain'),
    ('Saket',           'tree',   'medium', 'Tree roots blocking storm drain causing regular flooding'),
    ('Greater Kailash', 'tree',   'low',    'M-Block green belt trees need seasonal trimming'),
    # ── OTHER ─────────────────────────────────────────────────────
    ('Connaught Place', 'other',  'medium', 'Stray dog pack near Rajiv Chowk metro exit, biting incidents'),
    ('Saket',           'other',  'medium', 'Broken playground equipment in Select City park, sharp edges'),
    ('Rohini',          'other',  'high',   'Open manhole on Sector 3 road, no cover, no barrier at night'),
    ('Hauz Khas',       'other',  'medium', 'Footpath in village completely encroached by shops'),
    ('RK Puram',        'other',  'low',    'Stray cattle on Sector 2 road, traffic hazard at night'),
    ('Karol Bagh',      'other',  'medium', 'Illegal encroachment on public park behind metro'),
    ('Dwarka',          'other',  'low',    'Abandoned vehicles in Sector 7 reducing road to one lane'),
    ('Pitampura',       'other',  'medium', 'Public toilet non-functional for 2 months, open defecation'),
    ('Vasant Kunj',     'other',  'low',    'Community water cooler installed but never connected'),
    ('Janakpuri',       'other',  'high',   'Illegal construction blocking emergency access to society'),
    ('Nizamuddin',      'other',  'medium', 'Waterlogging on main road after blocked storm drain'),
    ('Paharganj',       'other',  'high',   'Open transformer pit near tourist area, safety emergency'),
    ('Mukherjee Nagar', 'other',  'low',    'Parking lot encroachment on public park land'),
    ('Laxmi Nagar',     'other',  'medium', 'Hospital gate always blocked by commercial vehicles'),
    ('Kashmere Gate',   'other',  'medium', 'ISBT approach road encroached by hawkers, 2 lanes blocked'),
]

_SEED_NGOS = [
    ('Delhi Green Mission',  'Sanitation & Waste Management', 'garbage',     4.6, 'Rohini',          '011-27551234', 'contact@delhigreen.org'),
    ('Road Safety India',    'Road Infrastructure & Safety',  'pothole',     4.4, 'Dwarka',          '011-28567890', 'info@roadsafetyindia.in'),
    ('Jal Seva Trust',       'Water & Sewage',                'water',       4.7, 'Hauz Khas',       '011-26960001', 'help@jalseva.org'),
    ('Sahayata Foundation',  'General Civic Issues',          'other',       4.2, 'Connaught Place', '011-23347788', 'sahayata@gmail.com'),
    ('Light Up Delhi',       'Street Lighting & Energy',      'streetlight', 4.3, 'Saket',           '011-29563322', 'lightup@delhi.org'),
    ('SafeTraffic NGO',      'Traffic & Road Discipline',     'traffic',     4.1, 'Mayur Vihar',     '011-22720011', 'safetraffic@gmail.com'),
    ('Tree Protect Delhi',   'Urban Trees & Green Cover',     'tree',        4.5, 'Pitampura',       '011-27340099', 'treeprotect@gmail.com'),
    ('Aman Bijli Sewak',     'Electricity & Power',           'electricity', 4.0, 'Lajpat Nagar',    '011-29832200', 'amanbijli@gmail.com'),
    ('Nirmal Delhi',         'Sanitation & Cleanliness',      'garbage',     4.4, 'Karol Bagh',      '011-25721100', 'nirmal@delhi.in'),
    ('Drain Watch',          'Sewage & Drainage',             'sewage',      4.2, 'Mehrauli',        '011-26642244', 'drainwatch@ngo.in'),
    ('Sound Free Society',   'Noise Pollution',               'noise',       4.0, 'Greater Kailash', '011-29242266', 'soundfree@gmail.com'),
    ('Citizen Watch Delhi',  'General Reporting',             'other',       4.3, 'Civil Lines',     '011-23949900', 'citizen@watchdelhi.org'),
    ('Sahayog Trust',        'Multi-issue NGO',               'other',       4.1, 'Janakpuri',       '011-25551122', 'sahayog@ngo.org'),
    ('Yamuna Bachao',        'Water Bodies',                  'water',       4.6, 'Kashmere Gate',   '011-23862244', 'yamuna@bachao.in'),
    ('Pothole Patrol',       'Roads & Potholes',              'pothole',     4.5, 'Model Town',      '011-27123344', 'patrol@potholes.in'),
    ('Bijli Bachao',         'Power & Streetlights',          'electricity', 4.2, 'Vasant Kunj',     '011-26891133', 'bijli@bachao.org'),
]

_USERS = ['priya','arjun','meera','rohit','kavita','sanjay','neha','deepak',
          'garv_chopra','shashwat_s','civic_reporter','rwa_secretary','anonymous']


def _seed_memory():
    """Seed in-memory store with 200 issues + NGOs. Spread over time for realism."""
    now = time.time()
    for idx, (area, tag, sev, desc) in enumerate(_SEED_ISSUES):
        lat, lng = AREA_COORDS.get(area, (28.6139, 77.2090))
        # Scatter markers so they don't stack exactly
        lat += (idx % 9 - 4) * 0.0018
        lng += ((idx // 9) % 9 - 4) * 0.0018
        issue_id = _next_int_id('issues')
        age_hours = (idx * 2.3) % (24 * 25)    # spread over 25 days
        _state['issues'].append({
            'id':          issue_id,
            'user':        _USERS[idx % len(_USERS)],
            'area':        area,
            'description': desc,
            'severity':    sev,
            'tag':         tag,
            'status':      'resolved' if idx % 9 == 0 else ('escalated' if idx % 11 == 0 else 'open'),
            'lat':         round(lat, 6),
            'lng':         round(lng, 6),
            'landmark':    '',
            'contact':     '',
            'image':       None,
            'timestamp':   now - (age_hours * 3600),
            'upvotes':     (idx * 7) % 20,
            'verified':    False,
            'escalated':   idx % 11 == 0,
            'resolved':    idx % 9 == 0,
        })
    for idx, (name, focus, tag, rating, area, phone, email) in enumerate(_SEED_NGOS):
        lat, lng = AREA_COORDS.get(area, (28.6139, 77.2090))
        ngo_id = _next_int_id('ngos')
        _state['ngos'].append({
            'id': ngo_id, 'name': name, 'focus': focus, 'tag': tag, 'rating': rating,
            'area': area, 'phone': phone, 'email': email,
            'lat': lat + 0.005, 'lng': lng + 0.005,
        })
    print(f'[database] Seeded {len(_state["issues"])} issues and {len(_state["ngos"])} NGOs into memory')


def _seed_firebase_if_empty():
    """
    Seed Firebase with sample data if the collection is empty.
    FIXED: handles 429 quota errors gracefully.
    """
    try:
        existing = list(_state['fs_db'].collection('issues').limit(1).stream())
        if existing:
            print(f'[database] Firebase already has data, skipping seed')
            _seed_memory()
            return
    except Exception as e:
        print(f'[database] Could not check Firebase emptiness: {e}')
        _seed_memory()
        _try_write_seeds_to_firebase()
        return

    # Firebase is empty AND readable — seed it
    print('[database] Seeding Firebase with sample data...')
    now = time.time()
    seeded = 0
    for idx, (area, tag, sev, desc) in enumerate(_SEED_ISSUES):
        lat, lng = AREA_COORDS.get(area, (28.6139, 77.2090))
        lat += (idx % 9 - 4) * 0.0018
        lng += ((idx // 9) % 9 - 4) * 0.0018
        try:
            iid = _next_int_id('issues')
            age_hours = (idx * 2.3) % (24 * 25)
            _state['fs_db'].collection('issues').document(str(iid)).set({
                'id': iid, 'user': _USERS[idx % len(_USERS)],
                'area': area, 'description': desc, 'severity': sev, 'tag': tag,
                'status': 'resolved' if idx % 9 == 0 else ('escalated' if idx % 11 == 0 else 'open'),
                'lat': round(lat, 6), 'lng': round(lng, 6),
                'landmark': '', 'contact': '', 'image': None,
                'timestamp': now - (age_hours * 3600),
                'upvotes': (idx * 7) % 20,
                'verified': False, 'escalated': idx % 11 == 0, 'resolved': idx % 9 == 0,
            })
            seeded += 1
        except Exception as e:
            print(f'[database] Issue seed error #{idx}: {e}')

    for idx, (name, focus, tag, rating, area, phone, email) in enumerate(_SEED_NGOS):
        lat, lng = AREA_COORDS.get(area, (28.6139, 77.2090))
        try:
            nid = _next_int_id('ngos')
            _state['fs_db'].collection('ngos').document(str(nid)).set({
                'id': nid, 'name': name, 'focus': focus, 'tag': tag, 'rating': rating,
                'area': area, 'phone': phone, 'email': email,
                'lat': lat + 0.005, 'lng': lng + 0.005,
            })
        except Exception as e:
            print(f'[database] NGO seed error: {e}')

    print(f'[database] Firebase seeded with {seeded} issues, {len(_SEED_NGOS)} NGOs')
    _seed_memory()


def _try_write_seeds_to_firebase():
    """Write seeds to Firebase in the background (fire-and-forget)."""
    def _write():
        now = time.time()
        written = 0
        for idx, (area, tag, sev, desc) in enumerate(_SEED_ISSUES):
            lat, lng = AREA_COORDS.get(area, (28.6139, 77.2090))
            lat += (idx % 9 - 4) * 0.0018
            lng += ((idx // 9) % 9 - 4) * 0.0018
            try:
                iid = idx + 1000  # offset to avoid conflicts
                age_hours = (idx * 2.3) % (24 * 25)
                _state['fs_db'].collection('issues').document(str(iid)).set({
                    'id': iid, 'user': _USERS[idx % len(_USERS)],
                    'area': area, 'description': desc, 'severity': sev, 'tag': tag,
                    'status': 'resolved' if idx % 9 == 0 else 'open',
                    'lat': round(lat, 6), 'lng': round(lng, 6),
                    'landmark': '', 'contact': '', 'image': None,
                    'timestamp': now - (age_hours * 3600),
                    'upvotes': (idx * 7) % 20,
                    'verified': False, 'escalated': False, 'resolved': idx % 9 == 0,
                })
                written += 1
                time.sleep(0.05)  # rate limit writes
            except Exception as e:
                print(f'[database] Background seed write {idx} failed: {e}')
                break
        print(f'[database] Background seed wrote {written} issues to Firebase')
    t = threading.Thread(target=_write, daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════
#  SPAM / DUPLICATE / SLA / ESCALATION
# ═══════════════════════════════════════════════════════

def insert_spam_issue(user, description, tag, severity, area,
                      lat=None, lng=None, image=None,
                      spam_verdict='spam', spam_reason='unspecified',
                      spam_confidence=0):
    record = {
        'user': user, 'description': description, 'tag': tag,
        'severity': severity, 'area': area, 'lat': lat, 'lng': lng,
        'image': image, 'timestamp': time.time(),
        'spam_verdict': spam_verdict, 'spam_reason': spam_reason,
        'spam_confidence': spam_confidence,
    }

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO spam_issues
                            (user_name, description, tag, severity, area, lat, lng,
                             image, spam_verdict, spam_reason, spam_confidence)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (user, description, tag, severity, area, lat, lng,
                         image, spam_verdict, spam_reason, spam_confidence),
                    )
                conn.commit()
            return
        except Exception as e:
            print(f'[database] Postgres insert_spam_issue failed: {e}')

    if _state['mode'] == 'firebase':
        try:
            _state['fs_db'].collection('spam_issues').document().set(record)
            return
        except Exception as e:
            print(f'[database] Spam write failed: {e}')

    _state['spam_issues'].insert(0, record)


def find_nearby_duplicate(lat, lng, tag, within_meters=50, within_days=7):
    if lat is None or lng is None or not tag:
        return None
    cutoff_ts = time.time() - (within_days * 86400)
    candidates = []

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT * FROM issues
                           WHERE tag = %s AND timestamp >= %s AND status != 'resolved'""",
                        (tag, cutoff_ts),
                    )
                    rows = cur.fetchall()
                    if rows and hasattr(rows[0], '_asdict'):
                        candidates = [_pg_row_to_issue(r) for r in rows]
                    elif rows and hasattr(cur, 'description') and cur.description:
                        cols = [d.name for d in cur.description]
                        candidates = []
                        for row in rows:
                            rdict = {cols[i]: row[i] for i in range(len(cols))}
                            candidates.append(_pg_row_to_issue(rdict))
        except Exception:
            candidates = list(_state['issues'])

    elif _state['mode'] == 'firebase':
        try:
            docs = _state['fs_db'].collection('issues') \
                .where('tag', '==', tag) \
                .where('timestamp', '>=', cutoff_ts) \
                .stream()
            for d in docs:
                candidates.append(d.to_dict())
        except Exception:
            candidates = list(_state['issues'])
    else:
        candidates = list(_state['issues'])

    closest = None; closest_m = within_meters + 1
    for issue in candidates:
        if issue.get('tag') != tag: continue
        if issue.get('timestamp', 0) < cutoff_ts: continue
        if issue.get('status') == 'resolved': continue
        i_lat, i_lng = issue.get('lat'), issue.get('lng')
        if i_lat is None or i_lng is None: continue
        meters = _haversine(lat, lng, i_lat, i_lng) * 1000
        if meters <= within_meters and meters < closest_m:
            closest = issue; closest_m = meters
    return closest


def is_rate_limited(user, max_reports=5, window_seconds=60):
    # Phase 6: delegated to services/rate_limit_service (Redis-backed).
    # Falls back to in-memory cache_service when Redis is unavailable.
    from services.rate_limit_service import is_rate_limited as _rl
    return _rl(str(user), max_reports=max_reports, window_seconds=window_seconds)


def calculate_sla(issue):
    # Moved to services/sla_service.py (Phase 4 pre-step).
    # Re-exported here so existing callers (app.py) keep working.
    from services.sla_service import calculate_sla as _calc
    return _calc(issue)


def escalate_issue(issue_id, reason='sla_breach'):
    issue_id = int(issue_id)

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE issues
                           SET escalated = TRUE, is_escalated = TRUE, status = 'escalated',
                               escalation_reason = %s, escalated_at = %s
                           WHERE id = %s AND escalated = FALSE""",
                        (reason, time.time(), issue_id),
                    )
                    if cur.rowcount == 0:
                        return False
                conn.commit()
            _invalidate_cache()
            return True
        except Exception as e:
            print(f'[database] Postgres escalate failed: {e}')

    if _state['mode'] == 'firebase':
        try:
            doc_ref = _state['fs_db'].collection('issues').document(str(issue_id))
            snap = doc_ref.get()
            if not snap.exists: return False
            if snap.to_dict().get('escalated'): return False
            doc_ref.update({'escalated': True, 'status': 'escalated',
                            'escalation_reason': reason, 'escalated_at': time.time()})
            _invalidate_cache()
            return True
        except Exception as e:
            print(f'[database] Escalate failed: {e}')

    for issue in _state['issues']:
        if int(issue.get('id', -1)) == issue_id:
            if issue.get('escalated'): return False
            issue.update({'escalated': True, 'status': 'escalated',
                          'escalation_reason': reason, 'escalated_at': time.time()})
            return True
    return False


def bulk_escalate(issue_ids: list, reason: str = 'sla_breach') -> int:
    """
    Escalate multiple issues in a SINGLE database call.
    Returns count of rows updated.

    Used by issues_api to replace N individual escalate_issue() calls
    (which each opened their own connection) with one connection checkout.
    This is the fix for Neon connection pool exhaustion under load.
    """
    if not issue_ids:
        return 0

    if _state['mode'] == 'postgres':
        try:
            now = time.time()
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    placeholders = ','.join(['%s'] * len(issue_ids))
                    cur.execute(
                        f"""UPDATE issues
                           SET escalated=TRUE, is_escalated=TRUE, status='escalated',
                               escalation_reason=%s, escalated_at=%s
                           WHERE id IN ({placeholders})
                             AND escalated=FALSE
                             AND status != 'resolved'""",
                        [reason, now] + list(issue_ids),
                    )
                    updated = cur.rowcount
                conn.commit()
            _invalidate_cache()
            return updated
        except Exception as e:
            print(f'[database] bulk_escalate failed: {e}')
            return 0

    # Firebase / memory fallback: call individual escalate_issue
    count = 0
    for iid in issue_ids:
        if escalate_issue(iid, reason=reason):
            count += 1
    return count


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
                        elif hasattr(cur, 'description') and cur.description:
                            cols = [d.name for d in cur.description]
                            rdict = {cols[i]: row[i] for i in range(len(cols))}
                            return _pg_row_to_issue(rdict)
        except Exception as e:
            print(f'[database] Postgres lookup failed: {e}')

    if _state['mode'] == 'firebase':
        try:
            snap = _state['fs_db'].collection('issues').document(str(issue_id)).get()
            if snap.exists: return snap.to_dict()
        except Exception as e:
            print(f'[database] Lookup failed: {e}')

    for i in _state['issues']:
        if int(i.get('id', -1)) == issue_id: return i
    return None


_ALLOWED_STATUSES = {'open', 'acknowledged', 'in_progress', 'resolved', 'escalated'}

def update_issue_status(issue_id, new_status, updated_by='gov', note=''):
    issue_id = int(issue_id)
    new_status = (new_status or '').lower().strip()
    if new_status not in _ALLOWED_STATUSES: return None
    now = time.time()
    history_entry = {'status': new_status, 'changed_at': now,
                     'changed_by': updated_by, 'note': (note or '')[:200]}
    _invalidate_cache()

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    # Read current status_history
                    cur.execute("SELECT status_history FROM issues WHERE id = %s", (issue_id,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    history = row[0] or []
                    if isinstance(history, str):
                        try:
                            history = json.loads(history)
                        except Exception:
                            history = []
                    if not isinstance(history, list):
                        history = []
                    history.append(history_entry)

                    updates = {
                        'status': new_status,
                        'status_history': json.dumps(history),
                        'last_updated_at': now,
                        'last_updated_by': updated_by,
                    }
                    if new_status == 'resolved':
                        updates['resolved'] = True
                        updates['resolved_at'] = now

                    set_clause = ', '.join(f"{k} = %s" for k in updates)
                    values = list(updates.values()) + [issue_id]
                    cur.execute(f"UPDATE issues SET {set_clause} WHERE id = %s", values)

                    # Return the updated record — capture description BEFORE commit
                    cur.execute("SELECT * FROM issues WHERE id = %s", (issue_id,))
                    row = cur.fetchone()
                    rdict = None
                    if row and hasattr(cur, 'description') and cur.description:
                        cols = [d.name for d in cur.description]
                        rdict = {cols[i]: row[i] for i in range(len(cols))}
                conn.commit()
                return _pg_row_to_issue(rdict) if rdict else None
        except Exception as e:
            print(f'[database] Postgres status update failed: {e}')

    if _state['mode'] == 'firebase':
        try:
            doc_ref = _state['fs_db'].collection('issues').document(str(issue_id))
            snap = doc_ref.get()
            if not snap.exists: return None
            data = snap.to_dict()
            history = data.get('status_history', [])
            history.append(history_entry)
            updates = {'status': new_status, 'status_history': history,
                       'last_updated_at': now, 'last_updated_by': updated_by}
            if new_status == 'resolved':
                updates['resolved'] = True; updates['resolved_at'] = now
            doc_ref.update(updates); data.update(updates)
            return data
        except Exception as e:
            print(f'[database] Status update failed: {e}')

    for issue in _state['issues']:
        if int(issue.get('id', -1)) == issue_id:
            issue.setdefault('status_history', []).append(history_entry)
            issue['status'] = new_status; issue['last_updated_at'] = now
            issue['last_updated_by'] = updated_by
            if new_status == 'resolved':
                issue['resolved'] = True; issue['resolved_at'] = now
            return issue
    return None


def get_issues_for_gov(tags=None, limit=300):
    issues = get_issues(limit=limit)
    if tags:
        tag_set = set(t.lower() for t in tags)
        issues = [i for i in issues if (i.get('tag') or 'other').lower() in tag_set]
    for i in issues:
        i.update(calculate_sla(i))
    priority = {'overdue': 0, 'soon': 1, 'safe': 2, 'resolved': 3}
    issues.sort(key=lambda i: (priority.get(i.get('sla_state'), 4), -(i.get('upvotes', 0))))
    return issues


def log_duplicate_merge(original_issue_id, duplicate_user, duplicate_description,
                        duplicate_tag=None, duplicate_severity=None,
                        lat=None, lng=None, distance_meters=None, match_reason=None):
    """
    Write a duplicate-merge audit record.
    Returns the document id (Firebase) or None (memory / Postgres mode).
    """
    record = {
        'original_id':     original_issue_id,
        'duplicate_desc':  duplicate_description,
        'user':            duplicate_user,
        'tag':             duplicate_tag,
        'severity':        duplicate_severity,
        'lat':             lat,
        'lng':             lng,
        'distance_m':      distance_meters,
        'reason':          match_reason,
        'timestamp':       time.time(),
    }
    if _state['mode'] == 'firebase':
        try:
            ref = _state['fs_db'].collection('duplicate_log').document()
            ref.set(record)
            return ref.id
        except Exception as e:
            print(f'[database] duplicate_log write failed: {e}')

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO duplicate_log
                            (original_id, duplicate_desc, user_name, tag, severity,
                             lat, lng, distance_m, reason, timestamp)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           RETURNING id""",
                        (original_issue_id, duplicate_description, duplicate_user,
                         duplicate_tag, duplicate_severity, lat, lng,
                         distance_meters, match_reason, record['timestamp']),
                    )
                    row = cur.fetchone()
                    conn.commit()
                    return str(row[0]) if row else None
        except Exception as e:
            print(f'[database] Postgres duplicate_log write failed: {e}')

    # In memory mode: silently discard (not critical)
    return None
