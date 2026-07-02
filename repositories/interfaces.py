"""
repositories/interfaces.py — abstract repository contracts
===========================================================
These ABCs define WHAT repositories must do, not HOW.
Services depend only on these interfaces — never on concrete classes.

This is the Dependency Inversion Principle:
  High-level modules (services) depend on abstractions.
  Low-level modules (Postgres/Firebase/Memory) implement abstractions.

Phase 2 — Dependency Inversion + Repository Pattern.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional


# ─────────────────────────────────────────────────────────────────────────────
#  ISSUE REPOSITORY
# ─────────────────────────────────────────────────────────────────────────────

class AbstractIssueRepository(ABC):
    """Contract for all issue data access."""

    @abstractmethod
    def get_all(self, tag: Optional[str] = None,
                status: Optional[str] = None,
                limit: int = 300) -> List[dict]:
        """Return issues filtered by tag and/or status, newest first."""

    @abstractmethod
    def get_by_id(self, issue_id: int) -> Optional[dict]:
        """Return a single issue dict or None."""

    @abstractmethod
    def get_for_gov(self, tags: List[str]) -> List[dict]:
        """Return issues filtered to a government department's tag list."""

    @abstractmethod
    def save(self, user: str, area: str, description: str, severity: str,
             tag: str, landmark: str, contact: str,
             lat: Optional[float], lng: Optional[float],
             image: Optional[str], image_hash: Optional[str]) -> Optional[int]:
        """
        Persist a new issue and return its assigned id.
        Returns None on failure.
        """

    @abstractmethod
    def update_status(self, issue_id: int, new_status: str,
                      updated_by: str, note: str = '') -> Optional[dict]:
        """
        Transition an issue to new_status.
        Returns the updated issue dict, or None if status is invalid.
        """

    @abstractmethod
    def upvote(self, issue_id: int, user: str) -> str:
        """Toggle upvote for user. Returns 'added' | 'removed' | 'not_found'."""

    @abstractmethod
    def escalate(self, issue_id: int, reason: str) -> bool:
        """Mark issue as escalated. Returns True if updated."""

    @abstractmethod
    def get_image_hashes(self) -> List[str]:
        """Return all stored perceptual image hashes for duplicate detection."""

    @abstractmethod
    def get_recent(self, hours: int = 24) -> List[dict]:
        """Return lightweight dicts of issues filed in the last N hours."""

    @abstractmethod
    def find_nearby_duplicate(self, lat: float, lng: float, tag: str,
                              within_meters: int = 50,
                              within_days: int = 7) -> Optional[dict]:
        """
        Find an existing issue of the same tag within within_meters.
        Returns the matching issue dict with '_distance_meters' key, or None.
        """

    @abstractmethod
    def log_duplicate_merge(self, original_issue_id: int,
                            duplicate_user: str,
                            duplicate_description: str,
                            duplicate_tag: str,
                            duplicate_severity: str,
                            lat: float, lng: float,
                            distance_meters: float,
                            match_reason: str) -> Optional[int]:
        """Record that a duplicate was merged into an existing issue."""

    @abstractmethod
    def is_rate_limited(self, user: str) -> bool:
        """Return True if this user has exceeded the submission rate limit."""


# ─────────────────────────────────────────────────────────────────────────────
#  NGO REPOSITORY
# ─────────────────────────────────────────────────────────────────────────────

class AbstractNGORepository(ABC):
    """Contract for NGO data access."""

    @abstractmethod
    def get_all(self) -> List[dict]:
        """Return all NGOs."""

    @abstractmethod
    def get_nearby(self, lat: float, lng: float,
                   tag: Optional[str] = None,
                   limit: int = 5,
                   radius_km: float = 8.0) -> List[dict]:
        """Return NGOs within radius_km of lat/lng, scored by relevance."""


# ─────────────────────────────────────────────────────────────────────────────
#  SPAM REPOSITORY
# ─────────────────────────────────────────────────────────────────────────────

class AbstractSpamRepository(ABC):
    """Contract for spam/rejected submission storage."""

    @abstractmethod
    def save_spam(self, user: str, description: str, tag: str,
                  severity: str, area: str,
                  lat: Optional[float], lng: Optional[float],
                  image: Optional[str],
                  spam_verdict: str, spam_reason: str,
                  spam_confidence: int) -> None:
        """Persist a rejected submission for audit and model retraining."""

    @abstractmethod
    def get_all(self, limit: int = 2000) -> List[dict]:
        """Return all spam records (used by admin CSV export)."""
