# RoadmapService Integrity Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject invalid roadmap status batches before SQL and raise the unit-test coverage of `roadmap_service.py` above 70 %.

**Architecture:** Keep the existing transactions and interfaces. Add early validation in `update_feature_statuses`, then cover the preconditions and resolution errors of `update_project_focus` in a dedicated file; the existing pivot tests remain unchanged.

**Tech Stack:** Python 3.12+, pytest 9.1.1, pytest-asyncio 1.4.0, SQLAlchemy 2 async, `AsyncMock`.

## Global Constraints

- Follow RED → GREEN → REFACTOR and keep the expected functional failure.
- Modify a single production symbol: `RoadmapService.update_feature_statuses`.
- Open no session if a batch contains an invalid status.
- Modify neither `test_project_group_ticket_service.py` nor the concurrent commit `2f797d6`.
- Run no live PostgreSQL mutation, no deployment, no merge, no push.

---

### Task 1: Fail-closed status batches and roadmap error coverage

**Files:**
- Create: `tests/unit/services/test_roadmap_service_focus.py`
- Modify: `tests/unit/services/test_roadmap_service.py:678`
- Modify: `src/brain_v42/services/roadmap_service.py:352-379`
- Modify: `docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md:427`
- Create: `docs/superpowers/specs/2026-07-25-roadmap-service-integrity-coverage-design.md`
- Create: `docs/superpowers/plans/2026-07-25-roadmap-service-integrity-coverage.md`

**Interfaces:**
- Consumes: `VALID_FEATURE_STATUSES`, `ProjectFocusValidationError`, `ProjectFocusConflictError`, and `RoadmapService` private validation helpers.
- Produces: unchanged `RoadmapService.update_feature_statuses(project_key: str, feature_status: dict[str, str]) -> int`, with fail-closed validation before session creation.

- [x] **Step 1: Write the failing invalid-batch test**

Create the focused test module with a scripted session factory and this regression:

```python
def _update_factory(*rowcounts: int) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[SimpleNamespace(rowcount=value) for value in rowcounts]
    )
    session.commit = AsyncMock()
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=context)
    return factory, session


async def test_update_feature_statuses_rejects_mixed_invalid_batch_before_sql() -> None:
    factory, session = _update_factory(1, 1)
    service = RoadmapService(factory)

    with pytest.raises(
        ProjectFocusValidationError,
        match="invalid feature status: Invalid=in_progress",
    ):
        await service.update_feature_statuses(
            "brain-v42",
            {"Valid": "done", "Invalid": "in_progress"},
        )

    factory.assert_not_called()
    session.execute.assert_not_awaited()
```

- [x] **Step 2: Run RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/services/test_roadmap_service_focus.py::test_update_feature_statuses_rejects_mixed_invalid_batch_before_sql -q
```

Expected: failure `DID NOT RAISE ProjectFocusValidationError`; the existing method executes and commits both updates.

- [x] **Step 3: Check GitNexus impact and implement the minimum**

Run upstream impact on `RoadmapService.update_feature_statuses`, then add before `updated = 0`:

```python
invalid_statuses = {
    name: status
    for name, status in feature_status.items()
    if status not in VALID_FEATURE_STATUSES
}
if invalid_statuses:
    rendered = ", ".join(
        f"{name}={status}" for name, status in sorted(invalid_statuses.items())
    )
    raise ProjectFocusValidationError(f"invalid feature status: {rendered}")
```

Do not change SQL, commit behavior, return values, or pinning semantics for valid statuses.

- [x] **Step 4: Run GREEN**

Run the single regression again. Expected: pass, with no session or SQL call.

- [x] **Step 5: Add focused characterization tests**

Realign the legacy happy-path fixture in
`TestUpdateFeatureStatuses.test_updates_multiple_features` from the obsolete
`in_progress` value to the canonical valid value `building`. Keep the new
regression on `in_progress` unchanged so it continues to prove rejection.

Add one test per behavior:

```python
@pytest.mark.parametrize("current_focus", ["", "   "])
async def test_update_project_focus_rejects_blank_focus_before_db(current_focus: str) -> None:
    factory = MagicMock()
    with pytest.raises(ProjectFocusValidationError, match="must not be blank"):
        await RoadmapService(factory).update_project_focus(
            "brain-v42", current_focus, expected_focus_revision=0
        )
    factory.assert_not_called()


@pytest.mark.parametrize("revision", [True, -1, "0"])
async def test_update_project_focus_rejects_invalid_revision_before_db(revision: object) -> None:
    factory = MagicMock()
    with pytest.raises(ProjectFocusValidationError, match="non-negative integer"):
        await RoadmapService(factory).update_project_focus(
            "brain-v42", "focus", expected_focus_revision=revision  # type: ignore[arg-type]
        )
    factory.assert_not_called()


async def test_update_project_focus_rejects_invalid_status_before_db() -> None:
    factory = MagicMock()
    with pytest.raises(ProjectFocusValidationError, match="Feature=invalid"):
        await RoadmapService(factory).update_project_focus(
            "brain-v42",
            "focus",
            expected_focus_revision=0,
            feature_status={"Feature": "invalid"},
        )
    factory.assert_not_called()


async def test_update_project_focus_rejects_status_unpin_overlap_before_db() -> None:
    factory = MagicMock()
    with pytest.raises(ProjectFocusValidationError, match="status-updated and unpinned"):
        await RoadmapService(factory).update_project_focus(
            "brain-v42",
            "focus",
            expected_focus_revision=0,
            feature_status={"Feature": "done"},
            unpin=["Feature", "Feature"],
        )
    factory.assert_not_called()
```

Add the remaining error and helper cases:

```python
def test_project_focus_conflict_exposes_current_state() -> None:
    error = ProjectFocusConflictError(current_focus="current", current_revision=4)
    assert error.current_focus == "current"
    assert error.current_revision == 4
    assert "current revision is 4" in str(error)


async def test_lock_requested_features_skips_sql_for_empty_names() -> None:
    session = AsyncMock()
    result = await RoadmapService(MagicMock())._lock_requested_features(
        session,
        project_key="brain-v42",
        requested_names=(),
    )
    assert result == []
    session.execute.assert_not_awaited()


def test_validate_requested_features_reports_missing_name() -> None:
    with pytest.raises(ProjectFocusValidationError, match="missing: Missing"):
        RoadmapService._validate_requested_features(
            requested_names=("Missing",),
            feature_rows=[],
            status_updates={"Missing": "done"},
        )


def test_validate_requested_features_reports_ambiguous_name() -> None:
    rows = [
        {"id": 1, "name": "Duplicate", "merged_into": None},
        {"id": 2, "name": "Duplicate", "merged_into": None},
    ]
    with pytest.raises(ProjectFocusValidationError, match="ambiguous: Duplicate"):
        RoadmapService._validate_requested_features(
            requested_names=("Duplicate",),
            feature_rows=rows,
            status_updates={"Duplicate": "done"},
        )


def test_validate_requested_features_rejects_merged_reactivation() -> None:
    rows = [{"id": 1, "name": "Merged", "merged_into": "canonical"}]
    with pytest.raises(ProjectFocusValidationError, match="cannot be reactivated: Merged"):
        RoadmapService._validate_requested_features(
            requested_names=("Merged",),
            feature_rows=rows,
            status_updates={"Merged": "building"},
        )


def test_validate_requested_features_allows_archiving_merged_feature() -> None:
    row = {"id": 1, "name": "Merged", "merged_into": "canonical"}
    result = RoadmapService._validate_requested_features(
        requested_names=("Merged",),
        feature_rows=[row],
        status_updates={"Merged": "archived"},
    )
    assert result == {"Merged": row}
```

- [x] **Step 6: Run the focused suite and coverage gate**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/services/test_roadmap_service.py \
  tests/unit/services/test_roadmap_service_focus.py \
  --cov=brain_v42 \
  --cov-report=json:/tmp/brain-v42-c552-roadmap-coverage.json -q
jq -e \
  '.files["src/brain_v42/services/roadmap_service.py"].summary.percent_statements_covered >= 70' \
  /tmp/brain-v42-c552-roadmap-coverage.json
```

Expected: all focused tests pass and the per-file threshold returns `true`.

- [x] **Step 7: Update tracking and verify**

Record this second sub-lot in the Sol Ultra roadmap and ticket thread while keeping the ticket `in_progress` for `pg_ticket` and `thresholds`. Run the full unit suite, `ruff check src/ tests/`, `ruff format --check src/ tests/`, `mypy src/`, `git diff --check`, and `gitnexus_detect_changes(scope="staged")`.

- [x] **Step 8: Review and commit atomically**

Obtain an independent requirements/code review, stage only the service, the
legacy status-fixture correction, focused tests, roadmap, spec, and plan, then
commit:

Coordinator override at delivery: the controller owns the independent final
review after the worker commit; the worker does not merge or push.

```bash
git commit -m "🐛 fix(roadmap): reject invalid legacy status batches"
```

Keep the detached worktree intact; do not merge or push.

## Self-review

- Spec coverage: validation fail-closed, error paths, coverage threshold, tracking, and repository gates map to explicit steps.
- Placeholder scan: each code and verification step has concrete inputs and expected results.
- Type consistency: the plan preserves the public method signature and uses the module's existing error types and status tuple.
