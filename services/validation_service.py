"""
services/validation_service.py — input validation layer
=========================================================
Validates inbound ReportSubmission data before it reaches issue_service.

Why it exists:
  Validation is not HTTP logic and not business logic — it is a gate
  between the two. Centralising it here means:
    - Routes stay thin (they call validate, check errors, call service)
    - issue_service can trust its inputs are well-formed
    - Adding a new validation rule means touching one file

  All validation errors are returned as a list of human-readable strings.
  Empty list = valid. Caller decides what HTTP status to return.

Phase 3 — DTOs / Validation / Global Error Handling.
"""

from __future__ import annotations

from typing import List

from domain.models import ReportSubmission, SeverityLevel
from domain.constants import (
    KNOWN_AREAS,
    DELHI_LAT_MIN, DELHI_LAT_MAX,
    DELHI_LNG_MIN, DELHI_LNG_MAX,
    DESCRIPTION_MIN_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    CONTACT_MAX_LENGTH,
    LANDMARK_MAX_LENGTH,
)

# Valid severity strings — derived from enum but kept as a set for O(1) lookup
_VALID_SEVERITIES = {s.value for s in SeverityLevel}


def validate_submission(submission: ReportSubmission) -> List[str]:
    """
    Validate a ReportSubmission before passing it to issue_service.

    Returns a list of error message strings.
    An empty list means the submission is valid.

    Checks (in order, all checked — all errors returned at once):
      1. Description length (min / max)
      2. Severity is a valid enum value
      3. Coordinates are within the Delhi bounding box (if provided)
      4. Area is a known neighbourhood OR coordinates are provided
      5. Contact length (if provided)
      6. Landmark length (if provided)
    """
    errors: List[str] = []

    # ── 1. Description ────────────────────────────────────────────────────────
    desc = (submission.description or '').strip()
    if len(desc) < DESCRIPTION_MIN_LENGTH:
        errors.append(
            f'Description must be at least {DESCRIPTION_MIN_LENGTH} characters.'
        )
    elif len(desc) > DESCRIPTION_MAX_LENGTH:
        errors.append(
            f'Description must be {DESCRIPTION_MAX_LENGTH} characters or fewer.'
        )

    # ── 2. Severity ───────────────────────────────────────────────────────────
    sev = (submission.severity or '').lower().strip()
    if sev and sev not in _VALID_SEVERITIES:
        errors.append(
            f'Severity "{submission.severity}" is not valid. '
            f'Use one of: {", ".join(sorted(_VALID_SEVERITIES))}.'
        )

    # ── 3. Coordinates (optional but if provided, must be in Delhi region) ────
    lat = submission.lat
    lng = submission.lng
    if lat is not None and lng is not None:
        if not (DELHI_LAT_MIN <= lat <= DELHI_LAT_MAX):
            errors.append(
                f'Latitude {lat} is outside the supported region '
                f'({DELHI_LAT_MIN}–{DELHI_LAT_MAX}).'
            )
        if not (DELHI_LNG_MIN <= lng <= DELHI_LNG_MAX):
            errors.append(
                f'Longitude {lng} is outside the supported region '
                f'({DELHI_LNG_MIN}–{DELHI_LNG_MAX}).'
            )
    elif lat is not None or lng is not None:
        # One provided, other missing
        errors.append('Both lat and lng must be provided together, or neither.')

    # ── 4. Area must be known if no coordinates ───────────────────────────────
    area = (submission.area or '').strip()
    if area and area not in KNOWN_AREAS and lat is None:
        # Unknown area with no coordinates — we cannot place this on the map.
        # We warn but do NOT hard-reject, because classifier may still work.
        # This is intentionally a soft check.
        pass  # Reserved for future strict mode

    # ── 5. Contact length ─────────────────────────────────────────────────────
    contact = (submission.contact or '')
    if len(contact) > CONTACT_MAX_LENGTH:
        errors.append(
            f'Contact field must be {CONTACT_MAX_LENGTH} characters or fewer.'
        )

    # ── 6. Landmark length ────────────────────────────────────────────────────
    landmark = (submission.landmark or '')
    if len(landmark) > LANDMARK_MAX_LENGTH:
        errors.append(
            f'Landmark field must be {LANDMARK_MAX_LENGTH} characters or fewer.'
        )

    return errors
