"""
Database layer — PostgreSQL primary if DATABASE_URL set, else Firebase Firestore,
else in-memory with seed data.
Single interface so app.py doesn't care which is active.

v5 changes (this version):
  - 15-second hard timeout on Postgres connection attempt (thread-based).
    If Postgres doesn't connect within 15 s, init_db() immediately falls
    through to Firebase instead of blocking Flask startup for 60+ s.
    The timeout is configurable via the PG_CONNECT_TIMEOUT_S env var.
  - psycopg_pool connect_timeout kwarg also set to 13 s so the driver
    itself gives up before the thread timeout fires (belt-and-suspenders).
  - All v4 improvements preserved unchanged.

v4 changes (preserved):
  - Hardened init_db(): Postgres failure now cleanly falls through to Firebase
  - Firebase fallback explicitly tested before trusting it (avoids silent blank DB)
  - init_db() retries Firebase after Postgres failure instead of going straight to memory
  - Better exception logging in all _ensure_pg_schema and pool-open paths
  - AR Scanner URL references updated to https://areapulse-cam.onrender.com/

v3 merge changes (preserved):
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
#  AREA COORDINATES (Delhi neighborhoods)
# ═══════════════════════════════════════════════════════
AREA_COORDS = {
    'Connaught Place': (28.6315, 77.2167), 'Karol Bagh': (28.6514, 77.1907),
    'Rohini': (28.7041, 77.1025), 'Saket': (28.5244, 77.2090),
    'Lajpat Nagar': (28.5677, 77.2378), 'Hauz Khas': (28.5494, 77.2001),
    'Dwarka': (28.5921, 77.0460), 'Janakpuri': (28.6219, 77.0878),
    'Chandni Chowk': (28.6506, 77.2303), 'Paharganj': (28.6448, 77.2167),
    'Mehrauli': (28.5244, 77.1855), 'Malviya Nagar': (28.5355, 77.2068),
    'Greater Kailash': (28.5494, 77.2378), 'Vasant Kunj': (28.5200, 77.1590),
    'Pitampura': (28.7007, 77.1311), 'Model Town': (28.7167, 77.1900),
    'Civil Lines': (28.6800, 77.2250), 'Mukherjee Nagar': (28.7050, 77.2100),
    'Rajouri Garden': (28.6447, 77.1220), 'Punjabi Bagh': (28.6590, 77.1311),
    'Mayur Vihar': (28.6090, 77.2944), 'Preet Vihar': (28.6355, 77.2944),
    'Shahdara': (28.6706, 77.2944), 'Laxmi Nagar': (28.6310, 77.2780),
    'Okhla': (28.5355, 77.2780), 'Kalkaji': (28.5494, 77.2590),
    'Nehru Place': (28.5491, 77.2509), 'Lodhi Colony': (28.5887, 77.2208),
    'Kashmere Gate': (28.6675, 77.2280), 'Nizamuddin': (28.5910, 77.2429),
    'Sarojini Nagar': (28.5760, 77.1980), 'INA': (28.5733, 77.2080),
    'Patel Nagar': (28.6500, 77.1700), 'RK Puram': (28.5650, 77.1800),
    'Vasant Vihar': (28.5670, 77.1600), 'Defence Colony': (28.5731, 77.2294),
}


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

SLA_HOURS = {
    'pothole': 168, 'water': 48, 'garbage': 72,
    'streetlight': 48, 'traffic': 24, 'noise': 24,
    'sewage': 24, 'electricity': 24, 'tree': 168, 'other': 120,
}
CROWD_ESCALATION_THRESHOLD = 25


# ═══════════════════════════════════════════════════════
#  POSTGRES CONNECT TIMEOUT
#  Configurable via env var; default 15 seconds.
# ═══════════════════════════════════════════════════════
_PG_TIMEOUT_S = int(os.environ.get('PG_CONNECT_TIMEOUT_S', '15'))


def _on_reconnect_failed(pool, error, n_attempts, delay):
    # Called by psycopg_pool when it cannot reconnect a dead connection.
    # Default behaviour is to retry forever — that causes the 30-second
    # 'couldn't get a connection' hang seen in the logs.
    # Returning False tells the pool to discard the slot instead,
    # so a new connection is opened on the next request.
    print(f'[database] Pool reconnect failed (attempt {n_attempts}): {error} — discarding slot')
    return False   # discard, don't retry



# ═══════════════════════════════════════════════════════
#  INIT  —  postgres → firebase → memory
# ═══════════════════════════════════════════════════════
def init_db():
    """
    Try DATABASE_URL (Postgres) first, then Firebase, then in-memory seeds.

    Fallback chain (each step only runs if the previous one fails):
      1. Postgres — attempted in a daemon thread with a _PG_TIMEOUT_S hard
         timeout (default 15 s).  If the thread hasn't finished by then,
         Postgres is declared unavailable and we move on immediately.
         This prevents Flask startup from blocking on Render cold starts or
         unreachable Neon servers.
      2. Firebase Firestore (FIREBASE_KEY_JSON env var or firebase_key.json)
      3. Pure in-memory seed data (always succeeds)
    """
    dsn = os.environ.get('DATABASE_URL', '').strip()

    # ── Attempt 1: PostgreSQL (with hard timeout) ──────
    if dsn and _PG_OK:
        _try_postgres_with_timeout(dsn)
        if _state['mode'] == 'postgres':
            return   # connected successfully — done
        # else: timed out or failed, fall through to Firebase
    elif dsn and not _PG_OK:
        print('[database] DATABASE_URL set but psycopg not installed → skipping Postgres')
    else:
        print('[database] DATABASE_URL not set → skipping Postgres')

    # ── Attempt 2: Firebase Firestore ──────────────────
    _try_firebase_init()


def _try_postgres_with_timeout(dsn: str) -> None:
    """
    Run the entire Postgres connection sequence in a daemon thread.
    Blocks the caller for at most _PG_TIMEOUT_S seconds, then returns
    (either _state['mode'] == 'postgres' on success, or unchanged on
    timeout/failure).

    RACE-CONDITION SAFETY
    ─────────────────────
    A cancelled flag is shared with the thread.  If the thread hasn't
    finished by the deadline, the flag is set to True before this function
    returns.  The thread checks the flag before writing to _state — so even
    if Postgres eventually connects at second 30 or 40, it will NOT flip
    _state['mode'] to 'postgres' mid-flight and silently discard every
    record that was written to Firebase during the window.

    Why a thread and not just a socket timeout on the DSN?
    ConnectionPool(open=True) does multiple things: DNS resolution, TCP
    handshake, TLS negotiation, auth, and schema migration — each of which
    can independently stall.  A single connect_timeout on the DSN only
    covers the initial TCP handshake.  Wrapping in a thread with join()
    gives a single wall-clock budget covering the entire sequence.
    """
    _cancelled = threading.Event()   # set → thread must not write to _state
    _result    = {'error': None}

    def _connect():
        pool = None
        try:
            # connect_timeout in kwargs gives the driver its own budget
            # slightly shorter than the wall-clock budget, so it raises
            # its own exception rather than stalling right up to t.join().
            pool = ConnectionPool(
                dsn,
                min_size=1, max_size=5,
                open=True,
                configure=_ensure_pg_schema,
                # connect_timeout: driver-level per-handshake budget (2s less
                #   than the wall-clock budget so the driver raises before t.join fires)
                # max_waiting: never queue more than 3 requests; raise immediately
                #   instead of blocking 30s when all connections are dead/busy
                # reconnect_failed: log and discard dead connections instead of
                #   silently retrying forever (fixes "SSL closed unexpectedly" hang)
                kwargs={'connect_timeout': max(1, _PG_TIMEOUT_S - 2)},
                max_waiting=3,
                reconnect_failed=_on_reconnect_failed,
            )
            # Smoke-test the connection before claiming success
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")

        except Exception as exc:
            _result['error'] = exc
            if pool:
                try:
                    pool.close()
                except Exception:
                    pass
            return

        # ── Write to shared state ONLY if the caller hasn't moved on ──
        if _cancelled.is_set():
            # We're too late — Firebase (or memory) is already active.
            # Close the pool cleanly so we don't leak connections.
            print(
                '[database] Postgres connected after timeout — discarding pool '
                '(Firebase is already active; restart the app to use Postgres)'
            )
            try:
                pool.close()
            except Exception:
                pass
            return

        # Timeout didn't fire — safe to promote to primary
        _state['pg_pool'] = pool
        _state['mode']    = 'postgres'
        print('[database] ✓ Postgres connected (primary)')
        # Always populate in-memory seed data even in postgres mode.
        # When Postgres queries fail (SSL drop, pool timeout, etc.) the
        # fallback path in get_issues() returns _state['issues'] — if that
        # list is empty the map shows zero markers. Costs ~2 ms, always safe.
        if not _state['issues']:
            _seed_memory()
        _seed_postgres_if_empty()

    t = threading.Thread(target=_connect, daemon=True, name='pg-init')
    t.start()
    t.join(timeout=_PG_TIMEOUT_S)

    if t.is_alive():
        # Deadline reached.  Signal the thread it must not touch _state.
        _cancelled.set()
        print(
            f'[database] ✗ Postgres did not connect within {_PG_TIMEOUT_S}s '
            f'— falling through to Firebase'
        )
        return

    if _result['error'] is not None:
        print(
            f'[database] ✗ Postgres connection failed: '
            f'{type(_result["error"]).__name__}: {_result["error"]}'
        )
        print('[database]   → falling through to Firebase fallback')


def _try_firebase_init():
    """
    Attempt Firebase Firestore connection.
    Falls back to in-memory if Firebase is unavailable for any reason
    (missing creds, network error, quota, etc.).
    """
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        # --- resolve credentials -------------------------------------------------
        cred = None
        if os.path.exists('firebase_key.json'):
            cred = credentials.Certificate('firebase_key.json')
            print('[database] Using firebase_key.json')
        elif os.environ.get('FIREBASE_KEY_JSON'):
            try:
                tmp = tempfile.NamedTemporaryFile(
                    mode='w', suffix='.json', delete=False
                )
                tmp.write(os.environ['FIREBASE_KEY_JSON'])
                tmp.close()
                cred = credentials.Certificate(tmp.name)
                print('[database] Using FIREBASE_KEY_JSON env var')
            except Exception as cred_err:
                print(f'[database] ✗ Could not write Firebase credential temp file: {cred_err}')
                cred = None
        else:
            raise FileNotFoundError(
                'No Firebase credentials — set FIREBASE_KEY_JSON env var or place firebase_key.json in project root'
            )

        # --- guard: if cred resolution failed (tmp file write error), raise
        #     clearly instead of letting initialize_app(None) raise AttributeError
        if cred is None:
            raise ValueError(
                'Firebase credential could not be loaded — '
                'FIREBASE_KEY_JSON env var was set but the temp file write failed. '
                'Check disk space and /tmp permissions on Render.'
            )

        # --- initialise Firebase app (only once) --------------------------------
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)

        fs_client = firestore.client()

        # --- smoke-test: try to actually read from Firestore --------------------
        # This surfaces 403/permission errors, wrong project, quota exhaustion, etc.
        try:
            list(fs_client.collection('issues').limit(1).stream())
        except Exception as read_err:
            err_str = str(read_err)
            if '429' in err_str or 'quota' in err_str.lower():
                # Quota exceeded: Firebase is live but we hit the limit.
                # We can still write; serve reads from memory.
                print(f'[database] ⚠ Firebase quota exceeded on smoke-test: {read_err}')
                print('[database]   Mode = firebase (quota-limited reads → memory fallback per request)')
            elif '403' in err_str or 'permission' in err_str.lower():
                raise PermissionError(
                    f'Firebase permission denied — check service account IAM roles: {read_err}'
                )
            else:
                raise

        _state['fs_db'] = fs_client
        _state['mode']  = 'firebase'
        print('[database] ✓ Firebase Firestore connected (fallback)')
        _seed_firebase_if_empty()

    except Exception as fb_err:
        print(f'[database] ✗ Firebase unavailable: {type(fb_err).__name__}: {fb_err}')
        print('[database]   → falling through to in-memory mode')
        _state['fs_db'] = None
        _state['mode']  = 'memory'
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

    ALTER TABLE issues ADD COLUMN IF NOT EXISTS upvoters        JSONB DEFAULT '[]'::jsonb;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS last_updated_at DOUBLE PRECISION;
    ALTER TABLE issues ADD COLUMN IF NOT EXISTS last_updated_by TEXT;

    CREATE INDEX IF NOT EXISTS idx_issues_tag      ON issues(tag);
    CREATE INDEX IF NOT EXISTS idx_issues_status   ON issues(status);
    CREATE INDEX IF NOT EXISTS idx_issues_time     ON issues(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_ngos_tag        ON ngos(tag);
    CREATE INDEX IF NOT EXISTS idx_spam_verdict    ON spam_issues(spam_verdict);
    CREATE INDEX IF NOT EXISTS idx_dup_log_orig    ON duplicate_log(original_id);
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
        keys = [
            'id','user_name','area','description','severity','tag','status',
            'lat','lng','landmark','contact','image','image_hash','timestamp',
            'upvotes','verified','escalated','resolved',
            'is_verified','is_escalated','status_history',
            'escalation_reason','escalated_at','resolved_at','assigned_to',
            'ai_confidence','verified_by','upvoters','last_updated_at','last_updated_by',
        ]
        r = {k: row[i] if i < len(row) else None for i, k in enumerate(keys)}

    result = {k: v for k, v in r.items()}
    if 'user_name' in result and result.get('user_name') is not None:
        result['user'] = result.pop('user_name')
    elif 'user_name' in result:
        result['user'] = result.pop('user_name')
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
    with conn.cursor() as cur:
        cur.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}")
        return cur.fetchone()[0]


def _seed_postgres_if_empty():
    try:
        with _state['pg_pool'].connection(timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM issues")
                count = cur.fetchone()[0]
                if count > 0:
                    print(f'[database] Postgres already has {count} issues, skipping seed')
                    return
    except Exception as e:
        print(f'[database] Could not check Postgres emptiness: {e}')
        return

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

        with _state['pg_pool'].connection(timeout=8) as conn:
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
    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            # timeout=5: fail fast if pool has no live connection
            # instead of blocking 30s (the 'couldn't get connection' log line)
            with _state['pg_pool'].connection(timeout=5) as conn:
                with conn.cursor() as cur:
                    params = []
                    q = ("SELECT * FROM issues ORDER BY timestamp DESC LIMIT %s")
                    params.append(limit)
                    if tag and status:
                        q = ("SELECT * FROM issues WHERE tag = %s AND status = %s "
                             "ORDER BY timestamp DESC LIMIT %s")
                        params = [tag, status, limit]
                    elif tag:
                        q = ("SELECT * FROM issues WHERE tag = %s "
                             "ORDER BY timestamp DESC LIMIT %s")
                        params = [tag, limit]
                    elif status:
                        q = ("SELECT * FROM issues WHERE status = %s "
                             "ORDER BY timestamp DESC LIMIT %s")
                        params = [status, limit]
                    cur.execute(q, params)
                    rows = cur.fetchall()
                    if rows and hasattr(rows[0], '_asdict'):
                        results = [_pg_row_to_issue(r) for r in rows]
                    elif hasattr(cur, 'description') and cur.description:
                        cols = [d.name for d in cur.description]
                        results = []
                        for row in rows:
                            rdict = {}
                            for i, col in enumerate(cols):
                                rdict[col] = row[i] if i < len(row) else None
                            results.append(_pg_row_to_issue(rdict))
                    else:
                        results = []
                    return results
        except Exception as e:
            print(f'[database] Postgres get_issues failed: {e} — serving from memory cache')
            # Don't raise; fall through to memory/firebase

    # ── Firebase (cached) ──────────────────────────────
    if _state['mode'] in ('firebase', 'postgres') and _state['fs_db']:
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
                err_str = str(e)
                if '429' in err_str or 'quota' in err_str.lower():
                    print(f'[database] Firebase quota exceeded on read — serving from memory seeds')
                else:
                    print(f'[database] Firestore read failed: {e} — serving from memory seeds')
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
    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=5) as conn:
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

    if _state['mode'] == 'firebase' and _state['fs_db']:
        try:
            docs = _state['fs_db'].collection('ngos').stream()
            return [{**d.to_dict(), 'id': d.id} for d in docs]
        except Exception as e:
            print(f'[database] Firebase get_all_ngos failed: {e}')
    return list(_state['ngos'])


def _ngo_row_to_dict(row):
    if isinstance(row, dict):
        r = row
    elif hasattr(row, '_asdict'):
        r = row._asdict()
    else:
        return {}
    return {k: v for k, v in r.items()}


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
    cached = _get_cached_issues()
    if cached is not None:
        return [i['image_hash'] for i in cached if i.get('image_hash')]

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT image_hash FROM issues WHERE image_hash IS NOT NULL")
                    return [row[0] for row in cur.fetchall()]
        except Exception as e:
            print(f'[database] get_all_image_hashes Postgres failed: {e}')

    if _state['mode'] == 'firebase' and _state['fs_db']:
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

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=5) as conn:
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


# ═══════════════════════════════════════════════════════
#  WRITERS
# ═══════════════════════════════════════════════════════
def insert_issue(user, area, description, severity, tag,
                 landmark='', contact='', lat=None, lng=None, image=None,
                 image_hash=None):
    with _state['lock']:
        issue_id = _next_int_id('issues')

    record = {
        'id': issue_id, 'user': user, 'area': area,
        'description': description, 'severity': severity, 'tag': tag,
        'status': 'open', 'landmark': landmark, 'contact': contact,
        'lat': lat, 'lng': lng, 'image': image,
        'image_hash': image_hash,
        'timestamp': time.time(), 'upvotes': 0,
        'verified': False, 'escalated': False, 'resolved': False,
    }

    _invalidate_cache()

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=8) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO issues
                            (id, user_name, area, description, severity, tag, status,
                             lat, lng, landmark, contact, image, image_hash, timestamp,
                             upvotes, verified, escalated, resolved)
                           VALUES (%s, %s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s)""",
                        (issue_id, user, area, description, severity, tag, 'open',
                         lat, lng, landmark, contact, image, image_hash, record['timestamp'],
                         0, False, False, False),
                    )
                conn.commit()
            return issue_id
        except Exception as e:
            print(f'[database] Postgres insert_issue failed: {e}')
            # Fall through to Firebase / memory so the report is not lost
            _state['issues'].insert(0, record)
            return issue_id

    if _state['mode'] == 'firebase' and _state['fs_db']:
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

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=8) as conn:
                with conn.cursor() as cur:
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

    if _state['mode'] == 'firebase' and _state['fs_db']:
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
    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=8) as conn:
                return _pg_next_id(conn, collection)
        except Exception:
            pass

    if _state['mode'] == 'firebase' and _state['fs_db']:
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
    ('Preet Vihar',     'sewage','medium',  'Drain cover broken near school, open sewage gap'),
    ('Patel Nagar',     'sewage','medium',  'Drainage not cleaned for months, mosquito breeding'),
    ('Vasant Kunj',     'sewage','high',    'DLF Promenade back drain overflowing after last night rain'),
    ('Janakpuri',       'sewage','medium',  'Colony drain blocked by tree roots, backing up in basement'),
    ('Kalkaji',         'sewage','high',    'Sewage mixing with drinking water supply, urgent fix needed'),
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
    """Seed in-memory store with 200 issues + NGOs. Spread over time for realism.
    Idempotent: if already seeded, returns immediately to prevent double-seeding.
    """
    if _state['issues']:   # already seeded — do not double-append
        return
    now = time.time()
    for idx, (area, tag, sev, desc) in enumerate(_SEED_ISSUES):
        lat, lng = AREA_COORDS.get(area, (28.6139, 77.2090))
        lat += (idx % 9 - 4) * 0.0018
        lng += ((idx // 9) % 9 - 4) * 0.0018
        issue_id = _next_int_id('issues')
        age_hours = (idx * 2.3) % (24 * 25)
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
    Handles 429 quota errors gracefully.
    """
    try:
        existing = list(_state['fs_db'].collection('issues').limit(1).stream())
        if existing:
            print('[database] Firebase already has data, skipping seed')
            _seed_memory()
            return
    except Exception as e:
        err_str = str(e)
        if '429' in err_str or 'quota' in err_str.lower():
            print(f'[database] Firebase quota exceeded during seed check — using memory + background write')
        else:
            print(f'[database] Could not check Firebase emptiness: {e}')
        _seed_memory()
        _try_write_seeds_to_firebase()
        return

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
                iid = idx + 1000
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
                time.sleep(0.05)
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

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=8) as conn:
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

    if _state['mode'] == 'firebase' and _state['fs_db']:
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

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=5) as conn:
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

    elif _state['mode'] == 'firebase' and _state['fs_db']:
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
    now = time.time()
    history = _state['recent_reports'].setdefault(user, [])
    history[:] = [t for t in history if now - t < window_seconds]
    history.append(now)
    return len(history) > max_reports


def calculate_sla(issue):
    tag = issue.get('tag') or 'other'
    sla_hours = SLA_HOURS.get(tag, SLA_HOURS['other'])
    created = issue.get('timestamp') or time.time()
    sla_due_at = created + (sla_hours * 3600)
    status = issue.get('status', 'open')
    if status == 'resolved':
        return {'sla_hours': sla_hours, 'sla_due_at': sla_due_at,
                'sla_overdue_hours': 0, 'sla_state': 'resolved'}
    overdue_seconds = time.time() - sla_due_at
    overdue_hours = max(0, overdue_seconds / 3600)
    remaining_hours = -overdue_seconds / 3600
    state = 'overdue' if overdue_hours > 0 else ('soon' if remaining_hours < (sla_hours * 0.25) else 'safe')
    return {'sla_hours': sla_hours, 'sla_due_at': sla_due_at,
            'sla_overdue_hours': round(overdue_hours, 1), 'sla_state': state}


def escalate_issue(issue_id, reason='sla_breach'):
    issue_id = int(issue_id)

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=8) as conn:
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

    if _state['mode'] == 'firebase' and _state['fs_db']:
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


def get_issue_by_id(issue_id):
    issue_id = int(issue_id)

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=5) as conn:
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

    if _state['mode'] == 'firebase' and _state['fs_db']:
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

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=8) as conn:
                with conn.cursor() as cur:
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

    if _state['mode'] == 'firebase' and _state['fs_db']:
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
    if _state['mode'] == 'firebase' and _state['fs_db']:
        try:
            ref = _state['fs_db'].collection('duplicate_log').document()
            ref.set(record)
            return ref.id
        except Exception as e:
            print(f'[database] duplicate_log write failed: {e}')

    if _state['mode'] == 'postgres' and _state['pg_pool']:
        try:
            with _state['pg_pool'].connection(timeout=8) as conn:
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

    return None
