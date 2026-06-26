"""
AreaPulse Portal — sla_engine.py
SLA calculations. Uses exact same SLA_HOURS as existing database.py.
"""
import time

# Exact mirror of SLA_HOURS from existing database.py / app.py
SLA_HOURS = {
    'sewage':      24,
    'electricity': 24,
    'traffic':     24,
    'noise':       24,
    'water':       48,
    'streetlight': 48,
    'garbage':     72,
    'other':       120,
    'pothole':     168,
    'tree':        168,
}

CROWD_ESCALATION_THRESHOLD = 25  # from existing app.py


def calc_sla(issue):
    """
    Returns SLA dict for an issue.
    Mirrors calculate_sla() from existing database.py.
    """
    tag       = issue.get('tag', 'other')
    timestamp = issue.get('timestamp', time.time())
    sla_h     = SLA_HOURS.get(tag, 120)
    sla_due   = timestamp + (sla_h * 3600)
    now       = time.time()
    remaining = sla_due - now
    elapsed   = now - timestamp

    if remaining <= 0:
        overdue_h = abs(remaining) / 3600
        state     = 'breached'
        pct_used  = 100
    else:
        overdue_h = 0
        pct_used  = min((elapsed / (sla_h * 3600)) * 100, 100)
        if pct_used >= 75:
            state = 'critical'
        elif pct_used >= 50:
            state = 'at_risk'
        else:
            state = 'healthy'

    return {
        'sla_hours':        sla_h,
        'sla_due_at':       sla_due,
        'sla_overdue_hours': round(overdue_h, 1),
        'sla_state':        state,
        'sla_pct_used':     round(pct_used, 1),
        'remaining_seconds': max(remaining, 0),
        'remaining_hours':   max(remaining / 3600, 0),
    }


def annotate_issues(issues):
    """Add SLA fields to a list of issues in-place."""
    for issue in issues:
        if issue.get('status') == 'resolved':
            issue['sla_state'] = 'healthy'
            issue['sla_overdue_hours'] = 0
            issue['sla_pct_used'] = 0
            continue
        sla = calc_sla(issue)
        issue.update(sla)
    return issues


def get_sla_summary(issues):
    """Count issues by SLA state."""
    summary = {'healthy': 0, 'at_risk': 0, 'critical': 0, 'breached': 0}
    for issue in issues:
        if issue.get('status') == 'resolved':
            continue
        state = issue.get('sla_state', 'healthy')
        if state in summary:
            summary[state] += 1
    return summary


def format_remaining(remaining_seconds):
    """Human-readable remaining time string."""
    if remaining_seconds <= 0:
        h = abs(remaining_seconds) / 3600
        return f'OVERDUE +{int(h)}h'
    if remaining_seconds < 3600:
        m = int(remaining_seconds / 60)
        return f'{m}m'
    if remaining_seconds < 86400:
        h = int(remaining_seconds / 3600)
        m = int((remaining_seconds % 3600) / 60)
        return f'{h}h {m}m'
    d = int(remaining_seconds / 86400)
    h = int((remaining_seconds % 86400) / 3600)
    return f'{d}d {h}h'
