"""
Database layer — PostgreSQL primary if DATABASE_URL set, else Firebase Firestore,
else in-memory.
Single interface so app.py doesn't care which is active.

v3 merge changes:
  - PostgreSQL as primary backend (DATABASE_URL detection)
  - Three modes: postgres → firebase → memory
  - Added get_all_image_hashes + get_recent_reports for Postgres
  - Added insert_spam_issue for Postgres
  - _state['pg_pool'] exposed for app.py direct postgres access
  - All existing Firebase + memory functionality preserved unchanged

v2 fixes (preserved):
  - 5-minute in-memory cache for Firebase reads
  - Graceful 429 quota handling: falls back to memory mode

v4 fixes (this pass — see AreaPulse_Final_Database_Architecture.md):
  - upvote_issue: SELECT ... FOR UPDATE row lock + issue_votes junction table,
    replacing the JSONB read-modify-write that lost concurrent upvotes
  - update_issue_status: same row lock, plus old_status now captured from the
    actual locked row instead of being absent/implicit in the history entry
  - Composite/partial indexes matching get_issues()'s real query shapes
  - pg_trgm GIN index for description search (replaces need for Elasticsearch
    at this row count)
  - CHECK constraint on status so bad values can't land regardless of app bugs
  - Materialized view for stats/analytics so those queries stop competing with
    request traffic for the 2-connection pool
  - PII-masking view (public_issues_view) for anything served to the public feed
  - RLS intentionally NOT enabled here — Phase 6 in the architecture doc requires
    set_system_context() wired into bulk_escalate, escalate_issue, and
    insert_spam_issue first, or FORCE ROW LEVEL SECURITY silently turns those
    into no-ops. Do that migration separately.

v5 fixes (this pass):
  - Image storage moved to ImageKit — issues.image_url/image_key/thumbnail_url
    replace base64-in-Postgres for new uploads; image stays for legacy rows
    until backfill_images_to_imagekit() migrates them
  - Seed-data generation removed — Postgres already holds real production
    data, so _seed_postgres_if_empty / _seed_memory / _seed_firebase_if_empty
    and the hardcoded _SEED_ISSUES/_SEED_NGOS/_USERS lists are gone. Memory
    and Firebase modes now start empty rather than pre-populated.
"""
import os, time, math, json, tempfile, threading, uuid, base64, mimetypes
_PG_OK = False
try:
    import psycopg
    from psycopg_pool import ConnectionPool
    _PG_OK = True
    print('[database] psycopg + pool available')

    import logging as _logging
    _pool_logger = _logging.getLogger("psycopg.pool")
    _pool_logger.setLevel(_logging.WARNING)
    _pool_handler = _logging.StreamHandler()
    _pool_handler.setFormatter(_logging.Formatter('[psycopg.pool] %(message)s'))
    _pool_logger.addHandler(_pool_handler)
except ImportError:
    print('[database] psycopg not installed — Postgres unavailable')

_IK_OK = False
try:
    from imagekitio import ImageKit
    _IK_OK = True
    print('[database] imagekitio available')
except ImportError:
    print('[database] imagekitio not installed — ImageKit upload unavailable')

_IK_PRIVATE_KEY = os.environ.get('IMAGEKIT_PRIVATE_KEY', '').strip()
_ik_client = None


def _get_imagekit_client():
    global _ik_client
    if _ik_client is not None:
        return _ik_client
    if not (_IK_OK and _IK_PRIVATE_KEY):
        return None
    _ik_client = ImageKit(private_key=_IK_PRIVATE_KEY)
    return _ik_client


def _build_image_folder(prefix):
    now = time.gmtime()
    return f"/{prefix}/{now.tm_year:04d}/{now.tm_mon:02d}"


def _build_image_filename(ext):
    ext = ext.lstrip('.') or 'jpg'
    return f"{uuid.uuid4().hex}.{ext}"


def upload_image_to_imagekit(image_bytes, content_type='image/jpeg', prefix='issues'):
    client = _get_imagekit_client()
    if client is None or not image_bytes:
        return None, None, None
    ext = (mimetypes.guess_extension(content_type) or '.jpg')
    if ext == '.jpe':
        ext = '.jpg'
    file_name = _build_image_filename(ext)
    folder = _build_image_folder(prefix)
    try:
        response = client.files.upload(
            file=image_bytes,
            file_name=file_name,
            folder=folder,
            use_unique_file_name=False,
        )
        return response.file_id, response.url, response.thumbnail_url
    except Exception as e:
        print(f'[database] ImageKit upload failed: {e}')
        return None, None, None


def delete_image_from_imagekit(file_id):
    client = _get_imagekit_client()
    if client is None or not file_id:
        return False
    try:
        client.files.delete(file_id)
        return True
    except Exception as e:
        print(f'[database] ImageKit delete failed: {e}')
        return False


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
_CACHE_TTL = 300 

_fallback = {
    'issues': [],
    'ts':     0.0,
}

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
    # Neon's free-tier compute autosuspends when idle, so how long a cold
    # start takes to wake varies — a single roll of the dice can lose that
    # race even with a generous timeout. Retry a few times with a short
    # pause instead of permanently pinning the whole worker to Firebase
    # over one slow wake-up. Budget stays well under gunicorn's --timeout 60.
    if dsn and _PG_OK:
        # connect_timeout must be shorter than pg_timeout: if they're equal,
        # the pool's own checkout wait and the underlying TCP connect attempt
        # expire at the same instant, so our own PoolTimeout wrapper fires
        # before the inner connect error ever gets raised (and logged via the
        # psycopg.pool WARNING handler above) — we saw exactly this, every
        # attempt logging only the generic "couldn't get a connection"
        # message with no real cause. A shorter connect_timeout lets the
        # real error surface first, with time left in the outer budget for
        # the pool to log it.
        pg_attempts, pg_timeout, pg_connect_timeout, pg_retry_delay = 3, 10, 5, 3
        for attempt in range(1, pg_attempts + 1):
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
                    min_size=0, max_size=2,
                    open=True,
                    timeout=pg_timeout,
                    max_idle=120,
                    max_lifetime=600,
                    kwargs={
                        "connect_timeout": pg_connect_timeout,
                        "keepalives":       1,
                        "keepalives_idle":  30,
                        "keepalives_interval": 5,
                        "keepalives_count": 3,
                        "prepare_threshold": None,
                    },
                    configure=_ensure_pg_schema,
                    check=ConnectionPool.check_connection,
                )
                with _state['pg_pool'].connection(timeout=pg_timeout) as _c:
                    with _c.cursor() as _cur:
                        _cur.execute("SELECT 1")
                        _cur.fetchone()
                _state['mode'] = 'postgres'
                print(f'[database] Postgres connected (primary, attempt {attempt}/{pg_attempts})')
                return
            except Exception as e:
                if _state.get('pg_pool'):
                    try:
                        _state['pg_pool'].close(timeout=2)
                    except Exception:
                        pass
                    _state['pg_pool'] = None
                more = attempt < pg_attempts
                print(f'[database] Postgres connection attempt {attempt}/{pg_attempts} failed '
                      f'({type(e).__name__}: {e})' + (f' — retrying in {pg_retry_delay}s...' if more else ''))
                if more:
                    time.sleep(pg_retry_delay)
        print(f'[database] Postgres unavailable after {pg_attempts} attempts, trying Firebase...')

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
    except Exception as e:
        print(f'[database] Firebase unavailable ({type(e).__name__}), using in-memory mode')
        _state['mode'] = 'memory'


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
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS image_url       TEXT;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS image_key       TEXT;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS thumbnail_url   TEXT;
    ALTER TABLE spam_issues ADD COLUMN IF NOT EXISTS image_url  TEXT;
    ALTER TABLE spam_issues ADD COLUMN IF NOT EXISTS image_key  TEXT;
    ALTER TABLE spam_issues ADD COLUMN IF NOT EXISTS thumbnail_url TEXT;
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
    ALTER TABLE issues ALTER COLUMN id SET DEFAULT nextval('issues_id_seq');

    -- ═══════════════════════════════════════════════════════
    -- v4: junction table for upvotes (replaces JSONB read-modify-write).
    -- PK on (issue_id, user_name) makes double-voting impossible at the
    -- database layer — no app-level "already in the list?" check needed,
    -- and no lost updates when two upvotes for the same issue land at once.
    -- ═══════════════════════════════════════════════════════
    CREATE TABLE IF NOT EXISTS issue_votes (
        issue_id    BIGINT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        user_name   TEXT NOT NULL,
        voted_at    DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
        PRIMARY KEY (issue_id, user_name)
    );
    CREATE INDEX IF NOT EXISTS idx_issue_votes_issue ON issue_votes(issue_id);

    -- v4: CHECK constraint on status — bad data can't land regardless of
    -- app-layer bugs. Wrapped in a DO block since Postgres has no
    -- ADD CONSTRAINT IF NOT EXISTS.
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'chk_issues_status'
        ) THEN
            ALTER TABLE issues ADD CONSTRAINT chk_issues_status
                CHECK (status IN ('open','acknowledged','in_progress','resolved','escalated'));
        END IF;
    END $$;

    -- v4: composite/partial indexes matching get_issues()'s real query shapes
    -- (tag+status together, and the common "not resolved" filter used by
    -- get_issues_for_gov / SLA scans).
    CREATE INDEX IF NOT EXISTS idx_issues_tag_status_time
        ON issues(tag, status, timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_issues_active_time
        ON issues(timestamp DESC) WHERE status != 'resolved';

    -- v4: trigram search — replaces need for Elasticsearch at this row count.
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    CREATE INDEX IF NOT EXISTS idx_issues_description_trgm
        ON issues USING GIN (description gin_trgm_ops);

    -- v4: PII-masking view — anything serving the public feed should select
    -- from this, not from issues directly, so `contact` never leaks.
    CREATE OR REPLACE VIEW public_issues_view AS
    SELECT id, user_name, area, description, severity, tag, status,
           lat, lng, landmark, image_hash, timestamp, upvotes,
           verified, escalated, resolved, is_verified, is_escalated,
           escalation_reason, escalated_at, resolved_at, assigned_to,
           ai_confidence, verified_by, last_updated_at, last_updated_by
    FROM issues;

    -- v4: materialized view for stats/analytics so those queries stop
    -- competing with request traffic for the 2-connection pool. Refresh
    -- lazily via refresh_stats_if_stale() below, not on every read.
    CREATE MATERIALIZED VIEW IF NOT EXISTS issue_stats_mv AS
    SELECT tag,
           status,
           COUNT(*)                                   AS issue_count,
           AVG(upvotes)::NUMERIC(10,2)                 AS avg_upvotes,
           MAX(timestamp)                              AS latest_timestamp
    FROM issues
    GROUP BY tag, status;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_issue_stats_mv_key
        ON issue_stats_mv(tag, status);
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
        # Positional fallback — matches _ISSUE_COLS order (no image column)
        keys = [
            'id','user_name','area','description','severity','tag','status',
            'lat','lng','landmark','contact','image_hash','image_url','image_key',
            'thumbnail_url','timestamp',
            'upvotes','verified','escalated','resolved',
            'is_verified','is_escalated','status_history',
            'escalation_reason','escalated_at','resolved_at','assigned_to',
            'ai_confidence','verified_by','upvoters','last_updated_at','last_updated_by',
            'has_image',
        ]
        r = {k: row[i] if i < len(row) else None for i, k in enumerate(keys)}

    # Map DB column user_name → user for app.py compatibility
    result = {k: v for k, v in r.items()}
    if 'user_name' in result and result.get('user_name') is not None:
        result['user'] = result.pop('user_name')
    elif 'user_name' in result:
        result['user'] = result.pop('user_name')

    # has_image: from the computed column in _ISSUE_COLS; fallback to image_hash
    if 'has_image' not in result or result.get('has_image') is None:
        result['has_image'] = bool(result.get('image_hash'))
    else:
        result['has_image'] = bool(result['has_image'])

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


# ═══════════════════════════════════════════════════════
#  GETTERS
# ═══════════════════════════════════════════════════════
def get_areas():
    return sorted(AREA_COORDS.keys())


# Explicit column list — intentionally excludes the `image` column (can be MB of base64).
# The /issue/<id>/image endpoint serves images on demand. has_image lets the frontend
# know which cards have a photo without fetching the blob.
_ISSUE_COLS = (
    "id, user_name, area, description, severity, tag, status, "
    "lat, lng, landmark, contact, image_hash, image_url, image_key, thumbnail_url, "
    "timestamp, upvotes, "
    "verified, escalated, resolved, is_verified, is_escalated, status_history, "
    "escalation_reason, escalated_at, resolved_at, assigned_to, ai_confidence, "
    "verified_by, upvoters, last_updated_at, last_updated_by, "
    "(image_url IS NOT NULL OR (image IS NOT NULL AND image != '')) AS has_image"
)


def get_issues(tag=None, status=None, limit=300):
    """List issues — postgres → firebase (cached) → memory."""
    # ── Postgres ───────────────────────────────────────
    if _state['mode'] == 'postgres':
        last_error = None
        pg_read_attempts = 2
        for attempt in range(pg_read_attempts):
            try:
                with _state['pg_pool'].connection() as conn:
                    with conn.cursor() as cur:
                        params = []
                        if tag and status:
                            q = (f"SELECT {_ISSUE_COLS} FROM issues WHERE tag=%s AND status=%s "
                                 "ORDER BY timestamp DESC LIMIT %s")
                            params = [tag, status, limit]
                        elif tag:
                            q = (f"SELECT {_ISSUE_COLS} FROM issues WHERE tag=%s "
                                 "ORDER BY timestamp DESC LIMIT %s")
                            params = [tag, limit]
                        elif status:
                            q = (f"SELECT {_ISSUE_COLS} FROM issues WHERE status=%s "
                                 "ORDER BY timestamp DESC LIMIT %s")
                            params = [status, limit]
                        else:
                            q = f"SELECT {_ISSUE_COLS} FROM issues ORDER BY timestamp DESC LIMIT %s"
                            params = [limit]
                        cur.execute(q, params)
                        rows = cur.fetchall()
                        if rows and hasattr(rows[0], '_asdict'):
                            out = [_pg_row_to_issue(r) for r in rows]
                        elif cur.description:
                            cols = [d.name for d in cur.description]
                            out = [_pg_row_to_issue({cols[i]: row[i] for i in range(len(cols))})
                                    for row in rows]
                        else:
                            out = []
                        # Refresh the fallback snapshot on unfiltered reads so a later
                        # DB hiccup still serves recent data (including new issues).
                        if not tag and not status:
                            _fallback['issues'] = out
                            _fallback['ts'] = time.time()
                        return out
            except psycopg.Error as e:
                last_error = e
                print(f'[database] get_issues attempt {attempt+1}/{pg_read_attempts} failed: {type(e).__name__}: {e}')
                if attempt < pg_read_attempts - 1:
                    time.sleep(0.5)
                    continue
        print(f'[database] get_issues failed after {pg_read_attempts} attempts: {last_error}')
        fb = _fallback['issues'] or list(_state.get('issues') or [])
        if fb:
            age = int(time.time() - _fallback['ts']) if _fallback['issues'] else -1
            src = 'snapshot' if _fallback['issues'] else 'memory seeds'
            print(f'[database] serving fallback {src} ({len(fb)} issues' + (f', {age}s old)' if age >= 0 else ')'))
            results = list(fb)
            if tag:    results = [i for i in results if i.get('tag') == tag]
            if status: results = [i for i in results if (i.get('status') or 'open') == status]
            results.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            return results[:limit]
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


def refresh_stats_if_stale(max_age_seconds: int = 300) -> bool:
    """
    Refresh issue_stats_mv if it's older than max_age_seconds.
    Call this from the stats/analytics endpoint, not on every request —
    that's the whole point of the materialized view. Returns True if a
    refresh actually ran.
    CONCURRENTLY requires the unique index created in _ensure_pg_schema
    and avoids locking the view against concurrent reads.
    """
    if _state['mode'] != 'postgres':
        return False
    try:
        with _state['pg_pool'].connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXTRACT(EPOCH FROM NOW()) - COALESCE(
                        (SELECT MAX(latest_timestamp) FROM issue_stats_mv), 0
                    )
                """)
                age = cur.fetchone()[0]
                if age is None or age < max_age_seconds:
                    return False
                cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY issue_stats_mv")
            conn.commit()
        return True
    except Exception as e:
        print(f'[database] refresh_stats_if_stale failed: {e}')
        return False


def backfill_images_to_imagekit(batch_size=25):
    if _state['mode'] != 'postgres':
        return {'migrated': 0, 'failed': 0}

    migrated = 0
    failed = 0
    try:
        with _state['pg_pool'].connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, image FROM issues "
                    "WHERE image IS NOT NULL AND image != '' AND image_url IS NULL "
                    "LIMIT %s",
                    (batch_size,),
                )
                rows = cur.fetchall()
    except Exception as e:
        print(f'[database] backfill_images_to_imagekit query failed: {e}')
        return {'migrated': 0, 'failed': 0}

    for issue_id, b64_image in rows:
        try:
            raw = base64.b64decode(b64_image.split(',')[-1])
            image_key, image_url, thumbnail_url = upload_image_to_imagekit(
                raw, content_type='image/jpeg', prefix='issues'
            )
            if not image_url:
                failed += 1
                continue
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE issues SET image_url = %s, image_key = %s, "
                        "thumbnail_url = %s, image = NULL WHERE id = %s",
                        (image_url, image_key, thumbnail_url, issue_id),
                    )
                conn.commit()
            migrated += 1
        except Exception as e:
            print(f'[database] backfill_images_to_imagekit failed for issue {issue_id}: {e}')
            failed += 1

    if migrated or failed:
        _invalidate_cache()
    return {'migrated': migrated, 'failed': failed}


def delete_issue_image(issue_id):
    if _state['mode'] != 'postgres':
        return False
    try:
        with _state['pg_pool'].connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT image_key FROM issues WHERE id = %s", (issue_id,))
                row = cur.fetchone()
                image_key = row[0] if row else None
                if not image_key:
                    return False
                cur.execute(
                    "UPDATE issues SET image_url = NULL, image_key = NULL, "
                    "thumbnail_url = NULL WHERE id = %s",
                    (issue_id,),
                )
            conn.commit()
        return delete_image_from_imagekit(image_key)
    except Exception as e:
        print(f'[database] delete_issue_image failed: {e}')
        return False


# ═══════════════════════════════════════════════════════
#  WRITERS
# ═══════════════════════════════════════════════════════
def insert_issue(user, area, description, severity, tag,
                 landmark='', contact='', lat=None, lng=None, image=None,
                 image_hash=None, image_bytes=None, image_content_type='image/jpeg'):
    ts = time.time()

    _invalidate_cache()

    image_url = None
    image_key = None
    thumbnail_url = None
    if image_bytes:
        image_key, image_url, thumbnail_url = upload_image_to_imagekit(
            image_bytes, content_type=image_content_type, prefix='issues'
        )

    if _state['mode'] == 'postgres':
        insert_sql = """INSERT INTO issues
                            (user_name, area, description, severity, tag, status,
                             lat, lng, landmark, contact, image, image_hash,
                             image_url, image_key, thumbnail_url, timestamp,
                             upvotes, verified, escalated, resolved)
                           VALUES (%s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s,
                                   %s, %s, %s, %s)
                           RETURNING id"""
        params = (user, area, description, severity, tag, 'open',
                  lat, lng, landmark, contact,
                  (image if not image_url else None), image_hash,
                  image_url, image_key, thumbnail_url, ts,
                  0, False, False, False)

        def _resync_and_retry(trigger: str):
            print(f'[database] insert_issue: {trigger} — resyncing sequence and retrying')
            try:
                with _state['pg_pool'].connection() as conn2:
                    with conn2.cursor() as cur2:
                        cur2.execute(
                            "SELECT setval('issues_id_seq', "
                            "GREATEST((SELECT COALESCE(MAX(id),0) FROM issues), 1), true)"
                        )
                        cur2.execute(insert_sql, params)
                        row2 = cur2.fetchone()
                        retried_id = row2[0] if row2 else None
                    conn2.commit()
                if retried_id:
                    print(f'[database] insert_issue retry succeeded: id={retried_id}')
                else:
                    print('[database] insert_issue retry also returned None')
                return retried_id
            except Exception as se:
                print(f'[database] insert_issue retry failed: {se}')
                return None

        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(insert_sql, params)
                    row = cur.fetchone()
                    issue_id = row[0] if row else None
                conn.commit()
            if issue_id is None:
                return _resync_and_retry('no id returned')
            return issue_id
        except psycopg.errors.UniqueViolation as e:
            # The id sequence has fallen behind the table's actual max id
            # (e.g. after seeding rows with explicit ids) — nextval() collided
            # with an existing row. This used to be swallowed by the generic
            # except below and surface to the client as "#AP-null".
            return _resync_and_retry(f'id collision ({e})')
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
        'lat': lat, 'lng': lng,
        'image': (image if not image_url else None),
        'image_hash': image_hash,
        'image_url': image_url, 'image_key': image_key,
        'thumbnail_url': thumbnail_url,
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
    """
    v4 fix: was a JSONB read-modify-write — two concurrent upvotes for the
    same issue could both read the same `upvoters` array, both append/remove,
    and whichever UPDATE committed last silently overwrote the other's vote
    (classic lost update, invisible under low load, guaranteed under any
    real concurrency).

    Fixed with a junction table (`issue_votes`) whose primary key on
    (issue_id, user_name) makes double-voting impossible at the database
    layer, plus `SELECT ... FOR UPDATE` on the issues row so the toggle
    check-then-act (already voted? remove : add) and the upvotes counter
    update happen inside one serialized transaction. No app-level "is user
    already in the list" check is needed or trusted anymore — the database
    enforces it.
    """
    _invalidate_cache()

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    # Lock the issues row first so the whole toggle+count is
                    # serialized against any other upvote/status update on
                    # this same issue.
                    cur.execute("SELECT id FROM issues WHERE id = %s FOR UPDATE", (issue_id,))
                    if cur.fetchone() is None:
                        return 'not_found'

                    cur.execute(
                        "SELECT 1 FROM issue_votes WHERE issue_id = %s AND user_name = %s",
                        (issue_id, user),
                    )
                    already_voted = cur.fetchone() is not None

                    if already_voted:
                        cur.execute(
                            "DELETE FROM issue_votes WHERE issue_id = %s AND user_name = %s",
                            (issue_id, user),
                        )
                        action = 'removed'
                    else:
                        cur.execute(
                            "INSERT INTO issue_votes (issue_id, user_name) VALUES (%s, %s) "
                            "ON CONFLICT (issue_id, user_name) DO NOTHING",
                            (issue_id, user),
                        )
                        action = 'added'

                    # Recompute the cached counter from the source of truth
                    # (the junction table) rather than incrementing blindly.
                    cur.execute(
                        """UPDATE issues SET upvotes = (
                               SELECT COUNT(*) FROM issue_votes WHERE issue_id = %s
                           ) WHERE id = %s""",
                        (issue_id, issue_id),
                    )
                conn.commit()
                return action
        except Exception as e:
            print(f'[database] Postgres upvote failed: {e}')
            return 'error'

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

    upvoters = _state['upvoters'].setdefault(issue_id, set())
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
#  SPAM / DUPLICATE / SLA / ESCALATION
# ═══════════════════════════════════════════════════════

def insert_spam_issue(user, description, tag, severity, area,
                      lat=None, lng=None, image=None,
                      spam_verdict='spam', spam_reason='unspecified',
                      spam_confidence=0, image_bytes=None,
                      image_content_type='image/jpeg'):
    image_url = None
    image_key = None
    thumbnail_url = None
    if image_bytes:
        image_key, image_url, thumbnail_url = upload_image_to_imagekit(
            image_bytes, content_type=image_content_type, prefix='spam'
        )

    record = {
        'user': user, 'description': description, 'tag': tag,
        'severity': severity, 'area': area, 'lat': lat, 'lng': lng,
        'image': (image if not image_url else None),
        'image_url': image_url, 'image_key': image_key,
        'thumbnail_url': thumbnail_url,
        'timestamp': time.time(),
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
                             image, image_url, image_key, thumbnail_url,
                             spam_verdict, spam_reason, spam_confidence)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (user, description, tag, severity, area, lat, lng,
                         record['image'], image_url, image_key, thumbnail_url,
                         spam_verdict, spam_reason, spam_confidence),
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


def toggle_verify_issue(issue_id, user=None):
    issue_id = int(issue_id)
    _invalidate_cache()

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT is_verified FROM issues WHERE id = %s FOR UPDATE",
                        (issue_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return 'not_found'
                    new_val = not bool(row[0])
                    cur.execute(
                        "UPDATE issues SET is_verified=%s, verified=%s, verified_by=%s WHERE id=%s",
                        (new_val, new_val, user if new_val else None, issue_id),
                    )
                conn.commit()
            return 'added' if new_val else 'removed'
        except Exception as e:
            print(f'[database] toggle_verify_issue failed: {e}')
            return 'error'

    issue = get_issue_by_id(issue_id)
    if not issue:
        return 'not_found'
    new_val = not bool(issue.get('is_verified', False))

    if _state['mode'] == 'firebase':
        try:
            _state['fs_db'].collection('issues').document(str(issue_id)).update({
                'is_verified': new_val, 'verified': new_val,
                'verified_by': user if new_val else None,
            })
        except Exception as e:
            print(f'[database] Firestore verify toggle failed: {e}')
            return 'error'
    else:
        issue['is_verified'] = new_val
        issue['verified']    = new_val
        issue['verified_by'] = user if new_val else None

    return 'added' if new_val else 'removed'


def toggle_escalate_issue(issue_id, require_verified=False):
    issue_id = int(issue_id)
    _invalidate_cache()

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT is_verified, is_escalated FROM issues WHERE id = %s FOR UPDATE",
                        (issue_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return 'not_found'
                    is_verified, is_escalated = row
                    if require_verified and not is_verified and not is_escalated:
                        return 'not_verified'
                    new_val = not bool(is_escalated)
                    cur.execute(
                        "UPDATE issues SET is_escalated=%s, escalated=%s, escalated_at=%s WHERE id=%s",
                        (new_val, new_val, time.time() if new_val else None, issue_id),
                    )
                conn.commit()
            return 'added' if new_val else 'removed'
        except Exception as e:
            print(f'[database] toggle_escalate_issue failed: {e}')
            return 'error'

    issue = get_issue_by_id(issue_id)
    if not issue:
        return 'not_found'
    is_verified  = bool(issue.get('is_verified', False))
    is_escalated = bool(issue.get('is_escalated', False))
    if require_verified and not is_verified and not is_escalated:
        return 'not_verified'
    new_val = not is_escalated

    if _state['mode'] == 'firebase':
        try:
            _state['fs_db'].collection('issues').document(str(issue_id)).update({
                'is_escalated': new_val, 'escalated': new_val,
            })
        except Exception as e:
            print(f'[database] Firestore escalate toggle failed: {e}')
            return 'error'
    else:
        issue['is_escalated'] = new_val
        issue['escalated']    = new_val

    return 'added' if new_val else 'removed'


_ALLOWED_STATUSES = {'open', 'acknowledged', 'in_progress', 'resolved', 'escalated'}

def update_issue_status(issue_id, new_status, updated_by='gov', note=''):
    """
    v4 fix: was a read-then-write of status_history with no row lock — two
    concurrent status changes on the same issue could both read the same
    history array, both append, and the later commit would silently drop
    the earlier one's entry from the audit trail (same lost-update shape
    as the old upvote bug). Also, old_status was never actually recorded
    on the history entry itself, only implied by the previous list item.

    Fixed with SELECT ... FOR UPDATE to serialize concurrent status
    changes on this issue, and old_status is now read explicitly from the
    locked row and stored on the entry, not inferred or hardcoded.
    """
    issue_id = int(issue_id)
    new_status = (new_status or '').lower().strip()
    if new_status not in _ALLOWED_STATUSES: return None
    now = time.time()
    _invalidate_cache()

    if _state['mode'] == 'postgres':
        try:
            with _state['pg_pool'].connection() as conn:
                with conn.cursor() as cur:
                    # Lock the row before reading status_history/status so a
                    # concurrent update on the same issue can't interleave
                    # with this read-modify-write.
                    cur.execute(
                        "SELECT status, status_history FROM issues WHERE id = %s FOR UPDATE",
                        (issue_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    old_status, history = row[0], row[1] or []
                    if isinstance(history, str):
                        try:
                            history = json.loads(history)
                        except Exception:
                            history = []
                    if not isinstance(history, list):
                        history = []

                    history_entry = {
                        'old_status': old_status,
                        'status': new_status,
                        'changed_at': now,
                        'changed_by': updated_by,
                        'note': (note or '')[:200],
                    }
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

                    # Return the updated record — still inside the same
                    # locked transaction, so this reflects exactly what we
                    # just wrote.
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
            old_status = data.get('status')
            history = data.get('status_history', [])
            history.append({
                'old_status': old_status, 'status': new_status,
                'changed_at': now, 'changed_by': updated_by,
                'note': (note or '')[:200],
            })
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
            old_status = issue.get('status')
            issue.setdefault('status_history', []).append({
                'old_status': old_status, 'status': new_status,
                'changed_at': now, 'changed_by': updated_by,
                'note': (note or '')[:200],
            })
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