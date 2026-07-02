# domain/

This package contains the canonical data shapes for AreaPulse.

## The One Rule

**No imports from `database`, `ai_engine`, `app`, `classifier`, `email_sender`,
or any other AreaPulse infrastructure module.**

The only allowed imports are Python standard library modules (`dataclasses`,
`enum`, `typing`, `time`, etc.).

## Why

The domain layer sits at the centre of the architecture. Every other layer
(service, repository, controller) depends on it. If the domain layer imported
from infrastructure, the dependency would run in a circle — infrastructure
would depend on domain AND domain would depend on infrastructure. That makes
the code impossible to test in isolation and impossible to swap backends.

## What lives here

- `models.py` — dataclasses and enums for all core concepts:
  `Issue`, `NGO`, `ReportSubmission`, `ValidationResult`, `SpamReport`,
  `StatusChange`, `IssueTag`, `SeverityLevel`, `IssueStatus`, `SpamVerdict`

## What does NOT live here

- Database queries → `database.py` (today), `repositories/` (Phase 2)
- Business rules → `services/` (Phase 1)
- HTTP handling → `app.py`
- AI inference → `ai_engine.py`

## Roadmap position

Phase 0 — Domain Modeling. All subsequent phases build on these types.
