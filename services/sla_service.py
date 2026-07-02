"""
services/sla_service.py — SLA calculation logic
=================================================
calculate_sla() is a pure business rule: given an issue dict it returns
timing metadata. It has zero SQL and zero network calls. It belongs here
in the service layer, not in database.py.

Extracted from database.py in Phase 4 pre-step.
database.py re-exports calculate_sla for backward compatibility.
"""
from __future__ import annotations

import time
from domain.constants import SLA_HOURS


def calculate_sla(issue: dict) -> dict:
    """
    Compute SLA state for a given issue.

    Returns a dict with keys:
      sla_hours       — total hours allowed for this category
      sla_due_at      — Unix timestamp when SLA expires
      sla_overdue_hours — hours past due (0 if not overdue)
      sla_state       — 'resolved' | 'overdue' | 'soon' | 'safe'

    'soon' means within the last 25% of the SLA window.
    """
    tag       = issue.get('tag') or 'other'
    sla_hours = SLA_HOURS.get(tag, SLA_HOURS['other'])
    created   = issue.get('timestamp') or time.time()
    sla_due_at = created + (sla_hours * 3600)
    status    = issue.get('status', 'open')

    if status == 'resolved':
        return {
            'sla_hours':        sla_hours,
            'sla_due_at':       sla_due_at,
            'sla_overdue_hours': 0,
            'sla_state':        'resolved',
        }

    overdue_seconds = time.time() - sla_due_at
    overdue_hours   = max(0, overdue_seconds / 3600)
    remaining_hours = -overdue_seconds / 3600

    if overdue_hours > 0:
        state = 'overdue'
    elif remaining_hours < (sla_hours * 0.25):
        state = 'soon'
    else:
        state = 'safe'

    return {
        'sla_hours':         sla_hours,
        'sla_due_at':        sla_due_at,
        'sla_overdue_hours': round(overdue_hours, 1),
        'sla_state':         state,
    }
