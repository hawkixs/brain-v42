# Dream Operational Failure Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. ReD keeps implementation in the primary mission; the controller will dispatch the independent reviewer mission.

**Goal:** Keep Dream failure visibility while preventing post-run operational alerts from becoming durable Brain learnings and CONNECT orphans.

**Architecture:** Treat `dream_runs` and the dated Dream log as the operational sources. `post_run_alert` queries the 21 most recent failed rows, prints at most 20 details plus an omission marker through the existing shell redirection, and writes no knowledge row. `brain_session_start` continues to surface the latest failure directly from `dream_runs`.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, pytest/pytest-asyncio, Bash.

## Global Constraints

- Stay inside Dream scope; do not change CONNECT, other projects, or historical data.
- Follow RED → GREEN → REFACTOR for the changed behavior.
- Preserve the `python -m scripts.dream.post_run_alert` CLI and its non-fatal shell invocation.
- Keep error details bounded to the first line and 240 characters.
- Do not merge, push, deploy, delete, clean the worktree, or transition tickets.
- Commit the complete lot from base `d2e3792575e0bf7710bd63eba571b59c57e487f2`.

## Observed Run Evidence

- `2026-07-25.log` reports 8/8 phases, but its CONNECT report contains 10 orphans, 4 creations, and 6 errors. The global result was a false green before CONNECT report validation became effective.
- `2026-07-26.log` and `2026-07-27.log` report 7/8 phases with CONNECT as the sole failed phase. Both CONNECT reports contain 6 assignment errors; the other seven phases completed.
- The 2026-07-27 post-run helper created learning `e13b36dd-349e-4f60-9541-f740ad638ba5`, which the current orphan-classification query then returned as a CONNECT orphan.
- Historical alert learnings remain untouched. This lot stops the prospective loop; it does not delete or reclassify prior data.

---

### Task 1: Prove that failure reporting must not write knowledge

**Files:**

- Modify: `tests/unit/test_dream_post_run_alert.py`

**Interfaces:**

- Consumes: `write_alert_if_failed(session, run_date, project_key)`.
- Proves: a failed Dream row produces a report and leaves the learnings table empty.

- [x] **Step 1: Add an isolated persistence-boundary fixture**

Return one failed phase from an `AsyncSession` test double and record every SQL and commit call. This avoids the sandbox's known `aiosqlite` worker-thread stall, independently reproduced by an existing Dream service test.

- [x] **Step 2: Add the behavior regression**

Call `write_alert_if_failed`, assert that the returned report contains the date and failed phase, and assert that only the `dream_runs` query executes: no scalar knowledge lookup, second SQL statement, or commit. Add the no-failure case and retain the formatter boundary cases.

- [x] **Step 3: Run RED**

Run:

```bash
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python -m pytest \
  tests/unit/test_dream_post_run_alert.py::test_failed_run_is_reported_without_writing_learning -q
```

Expected: fail because the current implementation inserts one learning and returns its identifier.

Observed: exit 1; the function returned the inserted learning identifier `"42"` instead of the dated report.

---

### Task 2: Render failures without persisting them

**Files:**

- Modify: `scripts/dream/post_run_alert.py`
- Modify: `scripts/dream.sh:691-699`

**Interfaces:**

- Preserves: CLI arguments and exit codes.
- Changes: `write_alert_if_failed(...) -> str | None` returns the formatted operational report and performs no insert or commit.

- [x] **Step 1: Implement the minimum GREEN change**

Remove the learning lookup, insert, tags, and learning import. Keep the legacy function name/signature for compatibility. Return `build_alert_insight(...)` when failures exist and `None` otherwise.

- [x] **Step 2: Log the report**

Print the returned report from `_run`. Keep the no-failure message. Update module and shell comments to state that `dream_runs` powers the next session briefing and the helper appends a bounded operational report to the dated log.

- [x] **Step 3: Run GREEN**

Run the single regression, then the complete `test_dream_post_run_alert.py` module.

- [x] **Step 4: Refactor documentation and tests**

Remove assertions tied to knowledge topics or tags. Keep formatter, empty-input, missing-error, no-failure, and executable-module coverage. Bound reports to the 20 most recent rows and cover ordering, global volume, per-error truncation, and stdout emission.

---

### Task 3: Verify Dream visibility and repository quality

**Files:**

- Verify only: `tests/unit/services/test_dream_run_service.py`
- Verify only: `tests/unit/mcp/test_session_tools.py`
- Verify only: `tests/integration/test_session_start_briefing.py`

- [x] **Step 1: Run focused regressions**

Run the post-run tests plus the service and briefing tests that prove `brain_session_start` still obtains the latest failure from `dream_runs`.

- [x] **Step 2: Run static checks**

Run Ruff lint/format checks on the modified Python files, `bash -n scripts/dream.sh`, and `git diff --check`.

- [x] **Step 3: Run the unit suite**

Run `tests/unit` with the canonical repository virtual environment. Explain any unrelated failure instead of masking it.

Observed limit: the full 6,291-test suite stops at the unchanged `test_health_route_returns_success` because the sandbox forbids opening its localhost socket (`PermissionError: Operation not permitted`; 50 tests passed first). Existing `aiosqlite` tests independently stall because their worker thread cannot wake the sandboxed event loop. Focused no-socket regressions remain the acceptance gate.

- [x] **Step 4: Audit the change graph**

Run `gitnexus_detect_changes()` and confirm the affected scope is limited to the expected Dream alert symbols and tests.

---

### Task 4: Commit and prove the exact delivery

**Files:**

- Modify: Dream tracking replies only; no ticket transitions.

- [x] **Step 1: Review the exact diff**

Confirm that no historical learning, non-Dream tracker, or unrelated file changed.

- [ ] **Step 2: Commit the lot**

Stage only the plan, implementation, shell comment, and focused tests. Create one conventional Dream commit.

- [ ] **Step 3: Apply the ReD Git gate**

Require `HEAD == head_sha`, match `files_changed` to `git diff --name-only base_sha..head_sha`, and prove that the final staged/unstaged/untracked fingerprint equals the initial empty snapshot.

- [ ] **Step 4: Publish proof**

Reply with `WorkerProofPacketV1` on the primary mission and a concise evidence update on the Dream classification ticket. Leave the Brain session open for the controller-dispatched reviewer.
