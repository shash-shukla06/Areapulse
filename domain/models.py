"""
domain/models.py — AreaPulse canonical data shapes
====================================================
RULE: This file has ZERO imports from database, ai_engine, app, or any
      AreaPulse infrastructure module. Only Python standard library.

These dataclasses are the single source of truth for what each concept
looks like as it moves through the system. Every layer (service, repository,
controller) works with these types — not raw dicts.

Phase 0 — Domain Modeling
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ─────────────────────────────────────────────────────────────────────────────
#  ENUMERATIONS
#  Using (str, Enum) so values serialize to plain strings automatically.
#  This means existing code that does  if tag == 'pothole'  keeps working.
# ─────────────────────────────────────────────────────────────────────────────

class IssueTag(str, Enum):
    """The 10 canonical civic issue categories."""
    POTHOLE     = 'pothole'
    WATER       = 'water'
    GARBAGE     = 'garbage'
    STREETLIGHT = 'streetlight'
    TRAFFIC     = 'traffic'
    NOISE       = 'noise'
    SEWAGE      = 'sewage'
    ELECTRICITY = 'electricity'
    TREE        = 'tree'
    OTHER       = 'other'


class SeverityLevel(str, Enum):
    LOW    = 'low'
    MEDIUM = 'medium'
    HIGH   = 'high'


class IssueStatus(str, Enum):
    OPEN         = 'open'
    ACKNOWLEDGED = 'acknowledged'
    IN_PROGRESS  = 'in_progress'
    RESOLVED     = 'resolved'
    ESCALATED    = 'escalated'


class SpamVerdict(str, Enum):
    """Possible outcomes from the spam / validation pipeline."""
    REAL       = 'real'
    SPAM       = 'spam'
    ABUSE      = 'abuse'
    TEST       = 'test'
    RATE_LIMIT = 'rate_limit'
    BAN        = 'ban'
    FLAG       = 'flag'
    DUPLICATE  = 'duplicate'


class UserRole(str, Enum):
    """Platform roles used for RBAC."""
    CITIZEN     = 'citizen'
    GOV_OFFICER = 'gov_officer'
    NGO_MANAGER = 'ngo_manager'
    ADMIN       = 'admin'


# ─────────────────────────────────────────────────────────────────────────────
#  STATUS CHANGE  (one entry in the status_history audit trail)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StatusChange:
    """
    Represents a single status transition on an Issue.
    Stored as a JSON list in the status_history column.
    """
    status:     str                 # IssueStatus value
    changed_by: str                 # username of the officer / system
    changed_at: float               # Unix timestamp
    note:       Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'status':     self.status,
            'changed_by': self.changed_by,
            'changed_at': self.changed_at,
            'note':       self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> StatusChange:
        return cls(
            status     = d.get('status', ''),
            changed_by = d.get('changed_by', ''),
            changed_at = float(d.get('changed_at') or 0.0),
            note       = d.get('note'),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  ISSUE  (core domain entity)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    """
    Canonical representation of a civic issue report.

    id            — unique integer, assigned by the database
    user          — display name of the reporting citizen (or 'anonymous')
    area          — Delhi neighbourhood name
    description   — free-text description from the citizen
    severity      — low / medium / high
    tag           — the 10-category issue type
    status        — current workflow status
    lat / lng     — GPS coordinates (may be None for area-centroid fallback)
    landmark      — optional nearby landmark text
    contact       — citizen phone number (NEVER expose to unauthenticated callers)
    image         — data-URL of photo (future: object-storage URL)
    image_hash    — perceptual hash for duplicate detection
    timestamp     — Unix time of submission
    upvotes       — corroboration count
    upvoters      — list of user names who upvoted (for dedup)
    verified      — manually verified by a government officer
    escalated     — SLA-breached or crowd-escalated
    resolved      — convenience boolean mirroring status == resolved
    escalation_reason — why it was escalated
    escalated_at  — Unix time of escalation
    resolved_at   — Unix time of resolution
    status_history — ordered audit trail of status changes
    assigned_to   — officer username if manually assigned
    ai_confidence — 0-100 confidence from AI classification
    verified_by   — officer username who verified
    last_updated_at — Unix time of last status change
    last_updated_by — who made the last status change
    """
    id:                int
    user:              str
    area:              str
    description:       str
    severity:          str              # SeverityLevel value
    tag:               str              # IssueTag value
    status:            str              # IssueStatus value
    timestamp:         float

    # Optional fields — may not exist on older records or memory seeds
    lat:               Optional[float] = None
    lng:               Optional[float] = None
    landmark:          Optional[str]   = None
    contact:           Optional[str]   = None   # ⚠ SENSITIVE — never send to public callers
    image:             Optional[str]   = None
    image_hash:        Optional[str]   = None
    upvotes:           int             = 0
    upvoters:          List[str]       = field(default_factory=list)
    verified:          bool            = False
    escalated:         bool            = False
    resolved:          bool            = False
    escalation_reason: Optional[str]   = None
    escalated_at:      Optional[float] = None
    resolved_at:       Optional[float] = None
    status_history:    List[StatusChange] = field(default_factory=list)
    assigned_to:       Optional[str]   = None
    ai_confidence:     Optional[int]   = None
    verified_by:       Optional[str]   = None
    last_updated_at:   Optional[float] = None
    last_updated_by:   Optional[str]   = None

    def to_public_dict(self) -> dict:
        """
        Safe serialization for unauthenticated API callers.
        Strips the contact field — phone numbers are not public.
        """
        d = self.__dict__.copy()
        d.pop('contact', None)
        d['status_history'] = [s.to_dict() for s in self.status_history]
        return d

    def to_gov_dict(self) -> dict:
        """
        Full serialization for authenticated government officers.
        Includes contact field.
        """
        d = self.__dict__.copy()
        d['status_history'] = [s.to_dict() for s in self.status_history]
        return d


# ─────────────────────────────────────────────────────────────────────────────
#  NGO
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NGO:
    """Represents a registered NGO in the platform."""
    id:              int
    name:            str
    focus:           Optional[str]  = None
    tag:             Optional[str]  = None   # primary issue tag they handle
    rating:          Optional[float]= None
    area:            Optional[str]  = None
    phone:           Optional[str]  = None
    email:           Optional[str]  = None
    lat:             Optional[float]= None
    lng:             Optional[float]= None
    issues_resolved: int            = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ─────────────────────────────────────────────────────────────────────────────
#  REPORT SUBMISSION  (what a citizen sends to /report)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReportSubmission:
    """
    Typed representation of an inbound citizen report request.
    The /report route handler should parse form fields into this object
    so the rest of the pipeline works with typed data, not raw request.form.
    """
    user:        str
    description: str
    area:        str                = 'Delhi'
    severity:    str                = SeverityLevel.MEDIUM
    landmark:    str                = ''
    contact:     str                = ''
    lat:         Optional[float]    = None
    lng:         Optional[float]    = None
    image_b64:   Optional[str]      = None   # raw base64, no data: prefix
    image_data:  Optional[str]      = None   # data:mime;base64,... for storage
    image_mime:  str                = 'image/jpeg'


# ─────────────────────────────────────────────────────────────────────────────
#  VALIDATION RESULT  (what ai_engine.validate_submission returns)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """
    Typed result from the AI validation pipeline.
    Replaces the raw dict currently returned by validate_submission().
    """
    approved:         bool
    action:           Optional[str]  = None   # 'reject' | 'ban' | 'flag' | None
    rejection_reason: Optional[str]  = None
    image_hash:       Optional[str]  = None
    checks:           dict           = field(default_factory=dict)

    @classmethod
    def approved_result(cls, image_hash: Optional[str] = None, checks: dict = None) -> ValidationResult:
        return cls(approved=True, image_hash=image_hash, checks=checks or {})

    @classmethod
    def rejected(cls, action: str, reason: str, checks: dict = None) -> ValidationResult:
        return cls(approved=False, action=action, rejection_reason=reason, checks=checks or {})


# ─────────────────────────────────────────────────────────────────────────────
#  SPAM REPORT  (what gets written to spam_issues)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SpamReport:
    """
    Represents a rejected submission stored for audit and model retraining.
    """
    user:             str
    description:      str
    tag:              str
    severity:         str
    area:             str
    spam_verdict:     str           # SpamVerdict value
    spam_reason:      str
    spam_confidence:  int
    timestamp:        float         = field(default_factory=time.time)
    lat:              Optional[float] = None
    lng:              Optional[float] = None
    image:            Optional[str]   = None
    id:               Optional[int]   = None   # assigned by DB on insert


# ─────────────────────────────────────────────────────────────────────────────
#  AUTH USER  (the authenticated principal attached to every request)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AuthUser:
    """
    Represents the currently authenticated user on a request.
    Populated by the JWT middleware and stored in flask.g.current_user.

    Fields:
      user_id          — unique identifier (username for gov/ngo, display name for citizens)
      name             — display name
      role             — UserRole enum value
      tags             — issue tags this user's department handles (gov only)
      authority        — government authority name (gov only)
      org_name         — NGO organisation name (ngo_manager only)
      operating_areas  — NGO operating areas (ngo_manager only)
    """
    user_id:         str
    name:            str
    role:            str                    # UserRole value

    # Gov-officer fields
    tags:            List[str]              = field(default_factory=list)
    authority:       Optional[str]          = None

    # NGO-manager fields
    org_name:        Optional[str]          = None
    operating_areas: List[str]              = field(default_factory=list)

    def is_gov(self) -> bool:
        return self.role == UserRole.GOV_OFFICER

    def is_ngo(self) -> bool:
        return self.role == UserRole.NGO_MANAGER

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def to_session_dict(self) -> dict:
        """Minimal dict to store in Flask session for legacy compatibility."""
        return {
            'username':        self.user_id,
            'name':            self.name,
            'role':            self.role,
            'tags':            self.tags,
            'authority':       self.authority,
            'org_name':        self.org_name,
            'operating_areas': self.operating_areas,
        }
