"""
services/ai_service.py — AI inference service wrapper
======================================================
Thin adapter between the service layer and ai_engine.py.

Why it exists:
  issue_service should not import ai_engine directly. ai_engine returns
  raw dicts. This wrapper translates those raw dicts into typed
  ValidationResult domain objects and provides a clean interface
  that can be swapped, mocked in tests, or pointed at a different
  AI provider without touching issue_service.py.

Phase 1 — Service Layer extraction.
"""

from __future__ import annotations

from typing import Optional

from domain.models import ValidationResult


def validate_report(
    description:    str,
    image_b64:      Optional[str],
    user_id:        str,
    tag:            str,
    lat:            Optional[float],
    lng:            Optional[float],
    stored_hashes:  list,
    recent_reports: list,
    mime:           str = 'image/jpeg',
) -> ValidationResult:
    """
    Run the full 7-stage AI validation pipeline on a submission.

    Delegates to ai_engine.validate_submission() and wraps the result
    in a typed ValidationResult so callers don't work with raw dicts.

    Args:
        description:    citizen's text description
        image_b64:      raw base64 image (no data: prefix), or None
        user_id:        citizen identifier for ban/rate-limit checks
        tag:            pre-classified issue tag from classifier.auto_classify()
        lat/lng:        GPS coordinates, or None
        stored_hashes:  all existing image hashes for duplicate detection
        recent_reports: recent submissions for coordinate spam detection
        mime:           image MIME type

    Returns:
        ValidationResult domain object — always returns, never raises.
    """
    import ai_engine  # late import keeps domain layer free of infrastructure

    raw = ai_engine.validate_submission(
        description    = description,
        image_b64      = image_b64,
        user_id        = user_id,
        tag            = tag,
        lat            = lat,
        lng            = lng,
        stored_hashes  = stored_hashes,
        recent_reports = recent_reports,
        mime           = mime,
    )

    if raw.get('approved'):
        return ValidationResult.approved_result(
            image_hash = raw.get('image_hash'),
            checks     = raw.get('checks', {}),
        )
    return ValidationResult.rejected(
        action = raw.get('action', 'reject'),
        reason = raw.get('rejection_reason', 'Submission rejected by AI pipeline'),
        checks = raw.get('checks', {}),
    )


def analyze_image(image_b64: str, mime: str = 'image/jpeg') -> dict:
    """
    Vision classification of a civic issue photo.
    Delegates directly to ai_engine.analyze_image().
    Returns raw dict — callers that need the image analysis result
    use this for the /ai/analyze-image endpoint.
    """
    import ai_engine
    return ai_engine.analyze_image(image_b64, mime)
