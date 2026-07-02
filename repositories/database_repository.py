"""
repositories/database_repository.py — concrete repository implementations
=========================================================================
These classes implement the abstract interfaces by delegating to the
existing database.py functions.

Design decision: We wrap database.py rather than rewriting it.
This is intentional incremental refactoring — the SQL stays in database.py
for now and will migrate into these classes in Phase 9 (Database Transactions
/ Data Integrity) when we also introduce proper connection management per
repository. Rewriting working database code prematurely creates risk.

All three repos (Issue, NGO, Spam) read from the same database.py module
which owns the connection pool and the backend-switching logic
(Postgres → Firebase → memory).

Phase 2 — Dependency Inversion + Repository Pattern.
"""

from __future__ import annotations

from typing import List, Optional

from repositories.interfaces import (
    AbstractIssueRepository,
    AbstractNGORepository,
    AbstractSpamRepository,
)
import database as _db


# ─────────────────────────────────────────────────────────────────────────────
#  ISSUE REPOSITORY
# ─────────────────────────────────────────────────────────────────────────────

class DatabaseIssueRepository(AbstractIssueRepository):
    """
    Postgres / Firebase / Memory issue repository.
    Delegates to database.py — backend selected at startup by init_db().
    """

    def get_all(self, tag: Optional[str] = None,
                status: Optional[str] = None,
                limit: int = 300) -> List[dict]:
        return _db.get_issues(tag=tag, status=status, limit=limit)

    def get_by_id(self, issue_id: int) -> Optional[dict]:
        return _db.get_issue_by_id(issue_id)

    def get_for_gov(self, tags: List[str]) -> List[dict]:
        return _db.get_issues_for_gov(tags=tags)

    def save(self, user: str, area: str, description: str, severity: str,
             tag: str, landmark: str, contact: str,
             lat: Optional[float], lng: Optional[float],
             image: Optional[str], image_hash: Optional[str]) -> Optional[int]:
        return _db.insert_issue(
            user        = user,
            area        = area,
            description = description,
            severity    = severity,
            tag         = tag,
            landmark    = landmark,
            contact     = contact,
            lat         = lat,
            lng         = lng,
            image       = image,
            image_hash  = image_hash,
        )

    def update_status(self, issue_id: int, new_status: str,
                      updated_by: str, note: str = '') -> Optional[dict]:
        return _db.update_issue_status(issue_id, new_status,
                                       updated_by=updated_by, note=note)

    def upvote(self, issue_id: int, user: str) -> str:
        return _db.upvote_issue(issue_id, user)

    def escalate(self, issue_id: int, reason: str) -> bool:
        return _db.escalate_issue(issue_id, reason=reason)

    def get_image_hashes(self) -> List[str]:
        return _db.get_all_image_hashes()

    def get_recent(self, hours: int = 24) -> List[dict]:
        return _db.get_recent_reports(hours=hours)

    def find_nearby_duplicate(self, lat: float, lng: float, tag: str,
                              within_meters: int = 50,
                              within_days: int = 7) -> Optional[dict]:
        return _db.find_nearby_duplicate(lat, lng, tag,
                                         within_meters=within_meters,
                                         within_days=within_days)

    def log_duplicate_merge(self, original_issue_id: int,
                            duplicate_user: str,
                            duplicate_description: str,
                            duplicate_tag: str,
                            duplicate_severity: str,
                            lat: float, lng: float,
                            distance_meters: float,
                            match_reason: str) -> Optional[int]:
        return _db.log_duplicate_merge(
            original_issue_id     = original_issue_id,
            duplicate_user        = duplicate_user,
            duplicate_description = duplicate_description,
            duplicate_tag         = duplicate_tag,
            duplicate_severity    = duplicate_severity,
            lat                   = lat,
            lng                   = lng,
            distance_meters       = distance_meters,
            match_reason          = match_reason,
        )

    def is_rate_limited(self, user: str) -> bool:
        return _db.is_rate_limited(user)


# ─────────────────────────────────────────────────────────────────────────────
#  NGO REPOSITORY
# ─────────────────────────────────────────────────────────────────────────────

class DatabaseNGORepository(AbstractNGORepository):
    """Postgres / Firebase / Memory NGO repository."""

    def get_all(self) -> List[dict]:
        return _db.get_all_ngos()

    def get_nearby(self, lat: float, lng: float,
                   tag: Optional[str] = None,
                   limit: int = 5,
                   radius_km: float = 8.0) -> List[dict]:
        return _db.get_nearby_ngos(lat, lng, tag=tag, limit=limit,
                                   radius_km=radius_km)


# ─────────────────────────────────────────────────────────────────────────────
#  SPAM REPOSITORY
# ─────────────────────────────────────────────────────────────────────────────

class DatabaseSpamRepository(AbstractSpamRepository):
    """Postgres / Firebase / Memory spam/rejected submission repository."""

    def save_spam(self, user: str, description: str, tag: str,
                  severity: str, area: str,
                  lat: Optional[float], lng: Optional[float],
                  image: Optional[str],
                  spam_verdict: str, spam_reason: str,
                  spam_confidence: int) -> None:
        try:
            _db.insert_spam_issue(
                user            = user,
                description     = description,
                tag             = tag,
                severity        = severity,
                area            = area,
                lat             = lat,
                lng             = lng,
                image           = image,
                spam_verdict    = spam_verdict,
                spam_reason     = spam_reason,
                spam_confidence = spam_confidence,
            )
        except Exception as exc:
            print(f'[spam_repository] save_spam failed: {exc}')

    def get_all(self, limit: int = 2000) -> List[dict]:
        from database import _state
        if _state.get('mode') == 'postgres':
            try:
                with _state['pg_pool'].connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT user_name, description, tag, severity, area, "
                            "spam_verdict, spam_reason, spam_confidence "
                            "FROM spam_issues LIMIT %s",
                            (limit,),
                        )
                        rows = cur.fetchall()
                        if hasattr(cur, 'description') and cur.description:
                            cols = [d.name for d in cur.description]
                            return [{cols[i]: row[i] for i in range(len(cols))}
                                    for row in rows]
            except Exception as exc:
                print(f'[spam_repository] get_all Postgres failed: {exc}')
        if _state.get('mode') == 'firebase':
            try:
                docs = _state['fs_db'].collection('spam_issues').limit(limit).stream()
                return [d.to_dict() for d in docs]
            except Exception as exc:
                print(f'[spam_repository] get_all Firebase failed: {exc}')
        return list(_state.get('spam_issues', []))
