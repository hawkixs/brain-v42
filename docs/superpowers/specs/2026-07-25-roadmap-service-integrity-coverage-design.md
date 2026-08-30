# RoadmapService — status validation and error coverage

**Date**: 2026-07-25

**Status**: approved by Brain ticket `5619c851`

## Context

The ticket ranks `roadmap_service.py` among the four modules below 70% and mandates
the order `project_group_ticket_service → roadmap_service → pg_ticket → thresholds`.
The first module is handled in `9f41b01`. The concurrent lot `2f797d6` touches
only `ProjectGroupTicketService`'s tests and stays out of scope.

A fresh measurement of the existing unit test gives 55.97% of statements and
40.74% of branches for `roadmap_service.py`. The pivots and happy paths of
`update_feature_statuses` are already covered. The preconditions and errors of
`update_project_focus` are not, in the unit suite.

`update_feature_statuses` currently accepts any string as a status. The
database already protects the column with the canonical
`features_status_check` constraint. The service's validation adds defense in
depth: it rejects the whole batch before any session or SQL and provides a
deterministic `ProjectFocusValidationError`, rather than a PostgreSQL error.

## Decision

The service will reject any batch containing a status absent from
`VALID_FEATURE_STATUSES` before opening a session. The error will use
`ProjectFocusValidationError`, already exposed by this module for invalid
roadmap mutations. A mixed valid/invalid batch will fail entirely before SQL.

The lot will add targeted unit tests for:

- the early rejection of invalid statuses in `update_feature_statuses`;
- the focus and revision preconditions;
- the conflict between a status update and unpinning;
- the resolutions of a missing, ambiguous, or merged feature;
- the construction of `ProjectFocusConflictError`;
- the empty path of `_lock_requested_features`.

The existing pivot tests remain the proof for this criterion. The existing
PostgreSQL tests continue to prove atomicity and concurrency; this lot
does not duplicate them and does not touch any live database.

## Limits

- No change to the API, schema, transaction, or roadmap query.
- No change in `test_project_group_ticket_service.py`.
- No merge, push, deployment, or live PostgreSQL mutation.
- The exit threshold is at least 70% of the module's statements, without a drop
  in overall coverage.

## Alternatives discarded

1. **Add only tests**: coverage would progress, but the writer
   would remain permissive and delegate rejection to PostgreSQL instead of providing
   a deterministic application error.
2. **Duplicate the PostgreSQL tests as unit tests**: this would blur the boundary
   between fast validation and integration proof.
3. **Rework both roadmap writers**: this consolidation exceeds the
   coverage ticket and would widen the change radius.

## Verification

- Functional RED: a batch containing `in_progress` currently reaches SQL
  instead of raising `ProjectFocusValidationError`.
- Targeted GREEN: module tests and a coverage threshold ≥70%.
- Repository validation: unit suite, Ruff, format, mypy, `git diff --check`, and
  `gitnexus_detect_changes()`.
