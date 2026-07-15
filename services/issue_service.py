"""
services/issue_service.py — core civic issue business logic
============================================================
Owns the full lifecycle of a civic issue:
  - Submitting a new report (validation → dedup → insert)
  - Updating issue status (+ audit trail + notification trigger)

Dependencies (injected at startup via configure()):
  - AbstractIssueRepository : issue data access
  - AbstractNGORepository   : NGO data access
  - AbstractSpamRepository  : spam log data access

No direct imports from database.py — the service depends on
abstractions (interfaces), not concrete implementations.
This is the Dependency Inversion Principle.

Phase 2 — Dependency Inversion + Repository Pattern.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from domain.models import ReportSubmission, ValidationResult
from domain.constants import AREA_COORDS
from classifier import auto_classify
from services import ai_service, notification_service
from services import storage_service

if TYPE_CHECKING:
    from repositories.interfaces import (
        AbstractIssueRepository,
        AbstractNGORepository,
        AbstractSpamRepository,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DEPENDENCY INJECTION
#  app.py calls configure() once at startup with the concrete repositories.
#  All functions below use these module-level references.
# ─────────────────────────────────────────────────────────────────────────────

_issue_repo: Optional['AbstractIssueRepository'] = None
_ngo_repo:   Optional['AbstractNGORepository']   = None
_spam_repo:  Optional['AbstractSpamRepository']  = None


def configure(
    issue_repo: 'AbstractIssueRepository',
    ngo_repo:   'AbstractNGORepository',
    spam_repo:  'AbstractSpamRepository',
) -> None:
    """
    Inject concrete repository implementations.
    Must be called once at application startup before any request is served.

    Example (app.py):
        from repositories.database_repository import (
            DatabaseIssueRepository, DatabaseNGORepository, DatabaseSpamRepository
        )
        issue_service.configure(
            issue_repo = DatabaseIssueRepository(),
            ngo_repo   = DatabaseNGORepository(),
            spam_repo  = DatabaseSpamRepository(),
        )
    """
    global _issue_repo, _ngo_repo, _spam_repo
    _issue_repo = issue_repo
    _ngo_repo   = ngo_repo
    _spam_repo  = spam_repo


def _repos_ready() -> bool:
    return _issue_repo is not None and _ngo_repo is not None and _spam_repo is not None


# ─────────────────────────────────────────────────────────────────────────────
#  RESULT TYPES
#  Simple wrapper classes — will become full DTOs in Phase 3.
# ─────────────────────────────────────────────────────────────────────────────

class ReportResult:
    """
    Typed result from submit_report().
    Route handler uses:  return jsonify(result.data), result.status_code
    """
    __slots__ = ('data', 'status_code')

    def __init__(self, data: dict, status_code: int = 200):
        self.data        = data
        self.status_code = status_code


class StatusUpdateResult:
    """Typed result from update_status()."""
    __slots__ = ('data', 'status_code')

    def __init__(self, data: dict, status_code: int = 200):
        self.data        = data
        self.status_code = status_code


# ─────────────────────────────────────────────────────────────────────────────
#  RESPONSE BUILDERS  (private helpers)
# ─────────────────────────────────────────────────────────────────────────────

def _ok(issue_id: int, tag: str, nearby_ngos: list) -> dict:
    return {
        'status':        'ok',
        'id':            issue_id,
        'tag':           tag,
        'points_earned': 5,
        'collection':    'issues',
        'nearby_ngos':   nearby_ngos,
    }


def _merged(dup_id: int, tag: str, distance_m: float, merge_doc_id) -> dict:
    return {
        'status':          'merged',
        'id':              dup_id,
        'tag':             tag,
        'points_earned':   2,
        'collection':      'duplicate_reports',
        'merge_doc_id':    merge_doc_id,
        'distance_meters': round(distance_m, 1),
        'merged_with':     dup_id,
        'message':         (
            f'Already reported as #AP-{dup_id} '
            f'({distance_m:.0f}m away). Added your voice (+1 corroboration).'
        ),
    }


def _spam_response(verdict: str, reason: str) -> dict:
    return {
        'status':       'spam_filtered',
        'spam_verdict': verdict,
        'spam_reason':  reason,
        'message':      'Too many reports in a short time. Please wait a moment.',
    }


def _rejected(action: str, reason: str, checks: dict) -> dict:
    return {'error': reason, 'action': action, 'checks': checks}


def _db_error(detail: str) -> dict:
    return {
        'error':   'Failed to save report to database',
        'detail':  detail,
        '_status': 'db_error',
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SUBMIT REPORT
# ─────────────────────────────────────────────────────────────────────────────

def submit_report(submission: ReportSubmission) -> ReportResult:
    """
    Full citizen report submission pipeline.

    Steps:
      0. Basic validation   (description length)
      1. Coordinate resolve (GPS → area centroid fallback)
      2. Text classify      (keyword auto_classify)
      3. Rate limit         (fast, no AI)
      4. AI validation      (spam, image, duplicate hash, coordinate, cross-modal)
      5. Geographic dedup   (50m radius merge)
      6. Insert new issue

    All data access goes through injected repositories —
    no direct database.py calls except AREA_COORDS lookup.
    """
    if not _repos_ready():
        return ReportResult(_db_error('Service not configured — call issue_service.configure()'), 500)

    user       = submission.user
    description = submission.description
    area        = submission.area
    severity    = submission.severity
    landmark    = submission.landmark
    contact     = submission.contact
    image_b64   = submission.image_b64
    image_data  = submission.image_data
    image_mime  = submission.image_mime

    print(f'[issue_service] submit: user={user!r} area={area!r} desc={description[:60]!r}')

    # ── 0. Basic validation ──────────────────────────────────────────────────
    if len(description) < 10:
        return ReportResult(
            {'error': 'Description must be at least 10 characters'},
            status_code=400,
        )

    # ── 1. Coordinate resolution ─────────────────────────────────────────────
    lat = submission.lat
    lng = submission.lng
    if (lat is None or lng is None) and area in AREA_COORDS:
        lat, lng = AREA_COORDS[area]

    # ── 2. Text classification ───────────────────────────────────────────────
    tag = auto_classify(description)

    # ── 3. Rate limit ────────────────────────────────────────────────────────
    if _issue_repo.is_rate_limited(user):
        _log_spam(user, description, tag, severity, area, lat, lng,
                  verdict='rate_limit', reason='rate_limit_flood', confidence=100)
        return ReportResult(_spam_response('rate_limit', 'rate_limit_flood'), status_code=429)

    # ── 4. AI validation ─────────────────────────────────────────────────────
    validation: ValidationResult = ai_service.validate_report(
        description    = description,
        image_b64      = image_b64,
        user_id        = user,
        tag            = tag,
        lat            = lat,
        lng            = lng,
        stored_hashes  = _issue_repo.get_image_hashes(),
        recent_reports = _issue_repo.get_recent(hours=24),
        mime           = image_mime,
    )

    if not validation.approved:
        action = validation.action or 'reject'
        reason = validation.rejection_reason or 'Submission rejected by AI pipeline'
        print(f'[issue_service] rejected: action={action} reason={reason[:80]}')
        _log_spam(user, description, tag, severity, area, lat, lng,
                  verdict=action, reason=reason[:200], confidence=90)
        return ReportResult(
            _rejected(action, reason, validation.checks),
            status_code=403 if action == 'ban' else 400,
        )

    # ── 5. Geographic duplicate ───────────────────────────────────────────────
    if lat and lng:
        dup = _issue_repo.find_nearby_duplicate(lat, lng, tag,
                                                within_meters=50, within_days=7)
        if dup:
            dup_id     = int(dup.get('id'))
            distance_m = dup.get('_distance_meters', 0)
            merge_doc_id = None
            try:
                merge_doc_id = _issue_repo.log_duplicate_merge(
                    original_issue_id     = dup_id,
                    duplicate_user        = user,
                    duplicate_description = description,
                    duplicate_tag         = tag,
                    duplicate_severity    = severity,
                    lat                   = lat,
                    lng                   = lng,
                    distance_meters       = distance_m,
                    match_reason          = 'geographic_radius',
                )
                print(f'[issue_service] merged with #AP-{dup_id} ({distance_m:.1f}m)')
            except Exception as exc:
                print(f'[issue_service] ⚠ duplicate log write failed: {exc}')
            _issue_repo.upvote(dup_id, user)
            return ReportResult(_merged(dup_id, tag, distance_m, merge_doc_id))

    # ── 6. Insert new issue ───────────────────────────────────────────────────
    # Phase 5: upload image to object storage before DB insert.
    # If storage is not configured → image_data stays as data-URL (passthrough).
    # Either way the returned string goes into the `image` column.
    stored_image = storage_service.upload_image(
        image_b64 = image_b64 or '',
        mime      = image_mime,
        issue_id  = None,   # not known yet — used in filename after insert
    ) if image_b64 else image_data

    try:
        issue_id = _issue_repo.save(
            user        = user,
            area        = area,
            description = description,
            severity    = severity,
            tag         = tag,
            landmark    = landmark,
            contact     = contact,
            lat         = lat,
            lng         = lng,
            image       = stored_image,
            image_hash  = validation.image_hash,
        )
        print(f'[issue_service] → issues/#{issue_id} (NEW) tag={tag} '
              f'img={"url" if storage_service.is_object_url(stored_image or "") else "b64" if stored_image else "none"}')
    except Exception as exc:
        detail = f'{type(exc).__name__}: {exc}'
        print(f'[issue_service] ✗ insert failed: {detail}')
        return ReportResult(_db_error(detail), status_code=500)

    if issue_id is None:
        print('[issue_service] ✗ insert returned no id — refusing to report success')
        return ReportResult(_db_error('Insert did not return an id'), status_code=500)

    nearby = _ngo_repo.get_nearby(lat, lng, tag, limit=3) if (lat and lng) else []
    return ReportResult(_ok(issue_id, tag, nearby))


# ─────────────────────────────────────────────────────────────────────────────
#  UPDATE STATUS
# ─────────────────────────────────────────────────────────────────────────────

def update_status(
    issue_id:   int,
    new_status: str,
    officer:    dict,
    note:       str = '',
) -> StatusUpdateResult:
    """
    Update an issue's status with department-level authorization
    and automatic WhatsApp notification.

    Business rules:
      - Issue must exist
      - Issue tag must be in officer's department tags
      - Status must be a valid transition value
    """
    if not _repos_ready():
        return StatusUpdateResult(_db_error('Service not configured'), 500)

    issue = _issue_repo.get_by_id(issue_id)
    if not issue:
        return StatusUpdateResult({'error': f'Issue #{issue_id} not found'}, 404)

    issue_tag    = (issue.get('tag') or 'other').lower()
    allowed_tags = [t.lower() for t in (officer.get('tags') or [])]
    if allowed_tags and issue_tag not in allowed_tags:
        return StatusUpdateResult(
            {'error': f'Your department does not handle "{issue_tag}" issues'}, 403
        )

    updated = _issue_repo.update_status(
        issue_id,
        new_status,
        updated_by = officer['username'],
        note       = note,
    )
    if not updated:
        return StatusUpdateResult({'error': f'Invalid status "{new_status}"'}, 400)

    notify = notification_service.send_status_update(updated, new_status)

    return StatusUpdateResult({
        'status':     'ok',
        'issue_id':   issue_id,
        'new_status': new_status,
        'updated_by': officer['username'],
        'notify':     notify,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _log_spam(user, description, tag, severity, area, lat, lng,
              verdict, reason, confidence):
    """Persist rejected submission via spam repository."""
    if _spam_repo:
        _spam_repo.save_spam(
            user            = user,
            description     = description,
            tag             = tag,
            severity        = severity,
            area            = area,
            lat             = lat,
            lng             = lng,
            image           = None,
            spam_verdict    = verdict,
            spam_reason     = reason,
            spam_confidence = confidence,
        )
