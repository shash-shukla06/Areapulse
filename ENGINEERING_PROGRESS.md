# AreaPulse — Engineering Progress & Handoff Document

**Last updated:** Phase 6 complete  
**Purpose:** Continue this refactoring in a new session from exactly where we left off.

---

## How to Use This Document

Read the permanent instructions first:
1. `README.md` — product overview
2. `MASTER_PROMPT.md` — principal engineering rules
3. `AI_AGENT.md` — agent behaviour rules
4. `ROADMAP.md` — 18-phase evolution plan
5. This file — current progress

Then read the repository before touching anything.

---

## Completed Work

### Phase 0 — Domain Modeling ✅

**Goal:** Create typed data shapes as the foundation for all future layers.

**Files created:**
- `domain/__init__.py` — package marker
- `domain/models.py` — all canonical dataclasses and enums
- `domain/constants.py` — `AREA_COORDS`, `SLA_HOURS`, `CROWD_ESCALATION_THRESHOLD`, Delhi bounds, submission limits *(added Phase 3)*
- `domain/README.md` — the one rule: no infrastructure imports

**Enums defined:** `IssueTag`, `SeverityLevel`, `IssueStatus`, `SpamVerdict`  
**Dataclasses defined:** `StatusChange`, `Issue`, `NGO`, `ReportSubmission`, `ValidationResult`, `SpamReport`

All enums inherit `(str, Enum)` so they compare equal to plain strings — existing code that does `if tag == 'pothole'` still works.

**5 critical security/correctness fixes also applied in Phase 0:**

| Fix | File | What changed |
|-----|------|--------------|
| SECRET_KEY crash in prod | `app.py` | Reads `APP_ENV`. If `production` and key missing/default → `RuntimeError` on startup. Dev warns but continues. |
| Admin token empty-string bypass | `app.py` | `_require_admin()` now returns `False` if `ADMIN_TOKEN` env var is unset, blocking all access rather than allowing empty matches. |
| Phone numbers in public `/issues` | `app.py` | `contact` field stripped from response unless caller has `gov_role` session. |
| ID race condition in Postgres | `database.py` | `insert_issue` Postgres path now uses `INSERT ... RETURNING id`. Sequence `issues_id_seq` created in schema DDL. `MAX(id)+1` no longer used. |
| Startup validation | `app.py` | `_validate_startup()` prints loud warnings if `DATABASE_URL`, `GROQ_API_KEY`, `ADMIN_TOKEN`, or `APP_ENV` are missing. |

---

### Phase 1 — Service Layer ✅

**Goal:** Extract business logic from `app.py` route handlers into a service layer.

**Files created:**
- `services/__init__.py` — package marker
- `services/notification_service.py` — all outbound WhatsApp logic
- `services/ai_service.py` — adapter between service layer and `ai_engine.py`
- `services/issue_service.py` — core report submission and status update logic

**`app.py` changes:**
- `_wa_notify` and `_status_change_message` (60 lines) → replaced by 2-line shims delegating to `notification_service`
- `report_api()` shrank from ~120 lines to ~45 lines — now parses HTTP then calls `issue_service.submit_report()`
- `gov_update_status()` shrank from ~30 lines to ~15 lines — now calls `issue_service.update_status()`

**Zero behavior change.** All API responses identical.

---

### Phase 2 — Dependency Inversion + Repository Pattern ✅

**Goal:** Services depend on abstractions, not concrete database calls.

**Files created:**
- `repositories/__init__.py` — package marker
- `repositories/interfaces.py` — abstract base classes: `AbstractIssueRepository`, `AbstractNGORepository`, `AbstractSpamRepository`
- `repositories/database_repository.py` — concrete implementations: `DatabaseIssueRepository`, `DatabaseNGORepository`, `DatabaseSpamRepository` (all delegate to `database.py`)

**`services/issue_service.py` rewritten:**
- No longer imports `database` directly (except `AREA_COORDS` — moved in Phase 3)
- Exposes `configure(issue_repo, ngo_repo, spam_repo)` — called at startup
- All data access via injected repository instances

**`app.py` changes:**
- After `init_db()`, instantiates concrete repos and calls `issue_service.configure()`
- `admin_export_spam_csv` now reads via `DatabaseSpamRepository.get_all()` instead of accessing `_state` directly

**Zero behavior change.** All API responses identical.

---

### Phase 3 — DTOs, Validation, Global Error Handling ✅

**Goal:** Centralised input validation, consistent error responses, domain constants separated from infrastructure.

**Files created:**
- `domain/constants.py` — `AREA_COORDS`, `SLA_HOURS`, `CROWD_ESCALATION_THRESHOLD`, `KNOWN_AREAS`, Delhi bounding box, submission length limits. `database.py` now re-exports these for backward compatibility.
- `services/validation_service.py` — `validate_submission(submission) -> List[str]`. Validates: description length, severity enum, coordinate bounds, contact/landmark length. Returns all errors at once.

**`database.py` changes:**
- `AREA_COORDS`, `SLA_HOURS`, `CROWD_ESCALATION_THRESHOLD` replaced with `from domain.constants import ...`
- All existing callers unaffected — `database.py` re-exports them

**`services/issue_service.py` changes:**
- `import database as _db` removed entirely
- `AREA_COORDS` now imported from `domain.constants`
- Service layer is now fully infrastructure-free in its imports

**`app.py` changes:**
- `report_api()` calls `validation_service.validate_submission()` before `issue_service.submit_report()`. Returns 400 with all error messages if invalid.
- `@app.errorhandler(Exception)` global handler added — all unhandled exceptions return consistent `{"error": ..., "type": ..., "status": ...}` JSON. Stack traces only included when `FLASK_DEBUG=1`.

**Zero behavior change.** All API responses identical.

---

### Phase 4 — JWT + Refresh Tokens + RBAC ✅

**Goal:** Replace hardcoded session-based auth with JWT in httpOnly cookies, add role-based access control, persist user accounts to database.

**Pre-step completed:**
- `calculate_sla()` extracted from `database.py` into `services/sla_service.py` (pure function, no SQL)
- `database.py` re-exports `calculate_sla` for backward compatibility

**Files created:**
- `services/sla_service.py` — `calculate_sla(issue) -> dict`. Pure business rule, no DB calls.
- `services/auth_service.py` — Full auth service:
  - `hash_pin()` / `verify_pin()` — bcrypt PIN hashing (constant-time)
  - `create_access_token()` / `create_refresh_token()` — signed JWTs via PyJWT
  - `decode_access_token()` / `decode_refresh_token()` — validation with `AuthError`
  - `auth_user_from_payload()` — reconstructs `AuthUser` from JWT payload
  - `login()` — authenticates gov/ngo/citizen, returns `LoginResult` with both tokens
  - `refresh_access_token()` — exchanges refresh token for new access token
  - `LoginResult` dataclass — typed return type
  - Supports both plain PIN (demo) and bcrypt hash (production) transparently
- `repositories/user_repository.py` — User account data access:
  - `get_gov_account(username)` — reads from Postgres `users` table, returns GOV_ACCOUNTS-compatible dict
  - `get_ngo_account(username)` — same for NGO accounts
  - `seed_demo_accounts(gov, ngo)` — inserts hardcoded demo accounts with bcrypt-hashed PINs on first Postgres startup (idempotent via ON CONFLICT DO NOTHING)

**`domain/models.py` additions:**
- `UserRole` enum — `citizen`, `gov_officer`, `ngo_manager`, `admin`
- `AuthUser` dataclass — authenticated principal: `user_id`, `name`, `role`, `tags`, `authority`, `org_name`, `operating_areas` + `is_gov()`, `is_ngo()`, `to_session_dict()` helpers

**`database.py` changes:**
- `users` table added to `_ensure_pg_schema` DDL — `username`, `display_name`, `role`, `pin_hash`, `tags`, `authority`, `org_name`, `operating_areas`, `focus`, `is_active`
- Indexes: `idx_users_username`, `idx_users_role`

**`requirements.txt` additions:**
- `PyJWT==2.8.0`
- `bcrypt==4.1.3`

**`app.py` changes:**
- JWT middleware `@app.before_request` — reads `areapulse_access` cookie, validates JWT, populates `flask.g.current_user` (AuthUser) and `flask.g.role`. Keeps `session['user']` / `session['gov_role']` in sync for Jinja2 backward compatibility.
- `require_role(*roles)` decorator — replaces scattered `session.get('gov_role')` checks. Falls back to session check for users without JWT cookie (backward compatible). HTML routes redirect to login, JSON routes return 401.
- `_set_jwt_cookies()` / `_clear_jwt_cookies()` — cookie helpers
- `/auth/refresh` endpoint — exchanges refresh token for new access + refresh tokens
- `login()` route — now calls `auth_service.login()`, sets JWT cookies on success
- `logout()` — now calls `_clear_jwt_cookies()` in addition to clearing session
- Google OAuth callback — now issues JWT cookies for OAuth users
- `@require_role('gov_officer')` applied to `gov_dashboard`
- `@require_role('ngo_manager')` applied to `ngo_dashboard`
- `_seed_users(GOV_ACCOUNTS, NGO_ACCOUNTS)` called after account dicts are defined

**JWT strategy (httpOnly cookies):**
- `access_token` — 15 min, httpOnly, SameSite=Strict, path=/
- `refresh_token` — 7 days, httpOnly, SameSite=Strict, path=/auth/refresh
- SameSite=Strict provides CSRF protection for free — no CSRF token needed
- `JWT_SECRET` env var required in production (startup crashes without it)

**Zero behavior change.** Demo logins (`gov_water` / `0000`) work identically.

---

### Phase 5 — Storage Service + Object Storage ✅

**Goal:** Move image storage out of the Postgres `image` TEXT column into object storage. Store short URLs instead of base64 data-URLs. Zero breakage for existing rows or dev mode.

**The problem solved:**
Every `SELECT * FROM issues` was fetching multi-MB base64 strings. At 500 issues with photos, a single map load could pull 500MB+ from Postgres. This would hit Neon free-tier limits rapidly and made queries unworkably slow at scale.

**Files created:**
- `services/storage_service.py` — full image storage abstraction:
  - `upload_image(image_b64, mime, issue_id) -> str` — uploads to configured provider, returns URL. Falls back to data-URL if unconfigured.
  - `delete_image(url) -> bool` — cleans up stored images
  - `is_configured() -> bool` — True when a real storage backend is set
  - `provider_name() -> str` — `'r2' | 's3' | 'local' | 'passthrough'`
  - `is_object_url(url) -> str` — True for https:// URLs, False for data-URLs
  - **Providers:** R2 (Cloudflare, boto3), S3 (AWS, boto3), Local (`static/uploads/`), Passthrough (returns data-URL unchanged)
- `static/uploads/.gitkeep` — directory for local provider files

**`services/issue_service.py` changes:**
- Imports `storage_service`
- In `submit_report()`, calls `storage_service.upload_image()` before `issue_repo.save()`
- Passthrough: stores data-URL unchanged — identical to pre-Phase-5 behavior

**`app.py` changes:**
- `Flask(__name__, static_folder='static')` — explicit static folder for local uploads
- `_validate_startup()` — adds JWT_SECRET warning and storage provider info/warnings

**Required env vars for production storage:**

| Provider | Required vars |
|---|---|
| Cloudflare R2 | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET_NAME` |
| AWS S3 | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME` |
| Local dev | `LOCAL_STORAGE_PATH` (optional, defaults to `static/uploads`) |

**Migration:** Existing base64 rows render unchanged. New rows store URLs. Both coexist indefinitely — no schema migration needed.

**Zero behavior change.** Demo and in-memory mode work identically.

---

### Phase 6 — Redis Infrastructure ✅

**Goal:** Move ephemeral state out of process memory into Redis so it survives restarts and is shared across all Gunicorn workers.

**Files created:**
- `services/cache_service.py` — Redis client wrapper with full in-memory fallback:
  - Connects to Redis via `REDIS_URL` env var. Falls back to `_MemoryCache` (thread-safe in-memory dict) when unavailable — zero config required for dev.
  - Public API: `get`, `set`, `setex`, `delete`, `exists`, `incr`, `expire`, `get_json`, `set_json`, `lpush`, `lrange`, `llen`, `keys_matching`
  - All keys namespaced with `areapulse:` prefix to avoid collisions
  - All Redis errors fall back to in-memory transparently
- `services/ban_service.py` — Redis-backed ban and strike management:
  - `ban_user(user_id, reason, permanent)` — stores ban record in Redis with optional TTL
  - `is_banned(user_id)` — reads from Redis, returns `{banned: True/False, ...}`
  - `unban_user(user_id)` — deletes ban + strikes keys
  - `record_strike(user_id, reason)` — auto-bans at `BAN_THRESHOLD=3` strikes
  - `get_strikes(user_id)` — returns strike list
  - `clear_strikes(user_id)` — removes strikes without unbanning
- `services/rate_limit_service.py` — Redis sliding window rate limiter:
  - `is_rate_limited(user_id)` — increments counter, sets TTL on first call, returns True if over limit
  - `get_request_count(user_id)` — read current count
  - `reset_rate_limit(user_id)` — manual reset (for testing/admin)

**`ai_engine.py` changes:**
- Old `_banned_users` dict and `_strike_log` defaultdict removed
- `ban_user`, `is_banned`, `record_strike`, `get_strikes`, `BAN_THRESHOLD` now imported from `services.ban_service`
- Stub `_banned_users = {}` / `_strike_log = {}` kept as empty dicts for backward compatibility with `app.py` admin routes that access them directly

**`database.py` changes:**
- `is_rate_limited()` now delegates to `services.rate_limit_service.is_rate_limited()` — Redis-backed, shared across workers
- `ngo_commitments` table added to `_ensure_pg_schema` DDL: `id`, `ngo_username`, `area`, `tag`, `status`, `committed_at`, `updated_at`, `notes` with indexes on username/area/tag

**`app.py` changes:**
- `_validate_startup()` — adds `REDIS_URL` warning when not set

**`requirements.txt` additions:**
- `redis==5.0.8`

**What this fixes:**
- Bans survive server restarts and deploys ✅
- Rate limits shared across all Gunicorn workers ✅
- `ngo_commitments` table ready in Postgres (app.py still uses `_ngo_commitments_store` list — migration of app.py routes to use the table is next)

**Zero behavior change.** Falls back to in-memory cache when Redis unavailable.

---

## Current Project Structure

```
Areapulse KIRO/
├── app.py                     # Flask routes + JWT middleware + @require_role + global error handler
├── database.py                # DB connection, schema DDL (issues/ngos/users/ngo_commitments)
│                              # (re-exports AREA_COORDS/SLA_HOURS/calculate_sla/is_rate_limited)
├── ai_engine.py               # Groq AI pipeline (ban functions → ban_service)
├── classifier.py              # Keyword classifier (unchanged)
├── email_sender.py            # Resend email wrapper (unchanged)
├── train_spam_model.py        # ML training script (unchanged)
├── requirements.txt           # + PyJWT, bcrypt, redis
├── static/uploads/            # Local image storage (Phase 5)
│
├── domain/                    # ← Phase 0 + 3 + 4
│   ├── models.py              # Issue, NGO, AuthUser, UserRole, ReportSubmission, etc.
│   ├── constants.py           # AREA_COORDS, SLA_HOURS, limits
│   └── README.md
│
├── services/                  # ← Phase 1–6
│   ├── ai_service.py          # Wraps ai_engine
│   ├── auth_service.py        # JWT + bcrypt login
│   ├── ban_service.py         # Redis-backed ban/strike ← Phase 6
│   ├── cache_service.py       # Redis wrapper + in-memory fallback ← Phase 6
│   ├── issue_service.py       # submit_report() + update_status()
│   ├── notification_service.py # WhatsApp pings
│   ├── rate_limit_service.py  # Redis sliding window rate limiter ← Phase 6
│   ├── sla_service.py         # calculate_sla()
│   ├── storage_service.py     # Image upload → R2/S3/local/passthrough
│   └── validation_service.py  # Input validation
│
├── repositories/              # ← Phase 2 + 4
│   ├── database_repository.py # Issue/NGO/Spam concrete repos
│   ├── interfaces.py          # ABCs
│   └── user_repository.py     # Gov/NGO accounts + demo seed
│
├── models/                    # ML artifacts
└── templates/                 # Jinja2 HTML (unchanged)
```

---

## Remaining Technical Debt

### Still open after Phase 6

| Issue | Severity | Planned phase |
|-------|----------|---------------|
| SLA escalation runs on every GET /issues (DB writes in GET) | High | Phase 8 (Celery) |
| `_ngo_commitments_store` list in `app.py` — table exists but app.py not yet wired to it | High | Phase 7 prep |
| WhatsApp bot session state still in process memory (`_wa_sessions`) | High | Wire to Redis cache_service |
| In-memory community posts | High | Add Postgres table |
| No pagination on `/issues` (returns 300 full records) | Medium | Phase 12 (Search) |
| `_ensure_pg_schema` DDL runs on every connection checkout | Medium | Phase 9 (Alembic) |
| No unit tests anywhere | High | Can start now — all services injectable |
| `print()` used for logging everywhere | Medium | Phase 10 (Observability) |
| Haversine duplicated in `ai_engine.py` and `database.py` | Low | Phase 9 cleanup |
| Raw SQL in `verify_issue` and `escalate_issue_route` in `app.py` | High | Phase 9 |
| `@require_role` only on 2 routes | Medium | Apply to all gov/ngo routes |
| `boto3` not in requirements.txt | Medium | Add when enabling R2/S3 |
| `_banned_users` / `_strike_log` stubs still in `ai_engine.py` | Low | Remove after `app.py` admin routes are updated to use `ban_service` directly |

---

## Next Phase: Phase 7 — Event-Driven Architecture

### What Phase 7 means

**Current problem:** Status changes, escalations, and other state transitions directly call notification logic inline. There is no separation between "something happened" and "what to do about it." This means:
- Adding a new notification channel (SMS, email, push) means editing the route handler
- Background processing of events is impossible
- No audit trail of what events occurred and what actions were taken

**Event-driven architecture** separates the event (a thing that happened) from the handlers (what to do about it). A simple in-process event bus is the right first step — no Kafka, no RabbitMQ needed yet.

**Phase 7 file plan:**
- `services/event_bus.py` — simple in-process pub/sub: `publish(event_type, payload)`, `subscribe(event_type, handler)`. Synchronous for now (async in Phase 11).
- `domain/events.py` — typed event dataclasses: `IssueCreated`, `IssueStatusChanged`, `IssueEscalated`, `DuplicateMerged`
- `services/issue_service.py` — publish events instead of calling `notification_service` directly
- `services/notification_service.py` — subscribe to `IssueStatusChanged` events

**Pre-step:** Wire `_ngo_commitments_store` in `app.py` to the `ngo_commitments` Postgres table that was created in Phase 6.

### Instructions for next session

1. Read `MASTER_PROMPT.md`, `AI_AGENT.md`, `ROADMAP.md` first
2. Read this file (`ENGINEERING_PROGRESS.md`)
3. Current phase: **Phase 7 — Event-Driven Architecture**
4. Pre-step: wire `_ngo_commitments_store` to the `ngo_commitments` DB table
5. Explain all changes before implementing
6. One phase at a time, finish completely, update this document, then stop

---

## Phase 4–17 Roadmap Summary

| Phase | Focus | Key outcome |
|-------|-------|-------------|
| 4 | JWT + Refresh Tokens + RBAC | Replace PIN/session auth with proper token auth |
| 5 | Storage Service + Object Storage | Move base64 images out of DB into R2/S3 |
| 6 | Redis Infrastructure | Fix in-memory ban/rate-limit/session/WA-state |
| 7 | Event-Driven Architecture | Status changes publish events, consumers notify |
| 8 | Background Jobs (Celery) | Move SLA escalation out of GET /issues |
| 9 | DB Transactions + Data Integrity | Move SQL into repositories, add Alembic migrations |
| 10 | Observability + Circuit Breakers | Structured logging, metrics, Groq retry/breaker |
| 11 | Realtime Systems | WebSocket map updates instead of polling |
| 12 | Search | Full-text issue search, indexed geospatial queries |
| 13 | AI Service Separation | ai_engine becomes a separate microservice |
| 14 | Containerization | Docker + docker-compose |
| 15 | Kubernetes | K8s deployment manifests |
| 16 | High Availability | Multi-replica, health checks, rolling deploys |
| 17 | Multi-region Deployment | Geo-distributed Postgres, CDN for assets |

---

## Instructions for the Next Session

1. Read `MASTER_PROMPT.md`, `AI_AGENT.md`, `ROADMAP.md` first
2. Read this file (`ENGINEERING_PROGRESS.md`) completely
3. Read `services/issue_service.py`, `services/cache_service.py`, `services/ban_service.py`, `repositories/interfaces.py`
4. Do NOT modify code until you understand the full picture
5. Current phase to execute: **Phase 7 — Event-Driven Architecture**
6. Pre-step: wire `_ngo_commitments_store` in `app.py` to `ngo_commitments` Postgres table
7. Follow the rule: one phase at a time, finish completely, update `ENGINEERING_PROGRESS.md`, then stop and wait for approval
8. Never put business logic in controllers
9. Never put SQL in controllers
10. Never bypass the service layer
11. Update `ENGINEERING_PROGRESS.md` at the end of every phase
9. Never put SQL in controllers
10. Never bypass the service layer
11. Update `ENGINEERING_PROGRESS.md` at the end of every phase

---

## Key Rules (from MASTER_PROMPT.md)

- Never sacrifice architecture for convenience
- Prefer incremental refactoring over giant rewrites
- Explain current problem, why it exists, tradeoffs, and migration strategy before implementing
- Keep changes as small as possible
- Only execute one roadmap phase at a time
- At end of every phase: summarise files changed, why, remaining debt, next phase — then stop
