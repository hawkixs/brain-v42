"""Unit tests for the Dream post-run operational failure report."""

from __future__ import annotations

import datetime as dt
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _no_host_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut these tests' dependency on the MACHINE's systemd drop-in.

    `fetch_failed_runs` calls `expected_dream_phase_pairs()` with no argument, so
    it reads `~/.config/systemd/user/brain-v42-dream.service.d/`. As long as the
    host had no pool, the function returned an empty set and these tests stayed
    green without knowing it. The day the pool was opened, two of them went red —
    on one machine, not in CI.

    That is the worst kind of dependency: green where nobody looks, red where the
    system really runs. The default is therefore "no pool", and a test that wants
    the cartesian product sets it explicitly.
    """
    monkeypatch.setattr(post_run_alert, "expected_dream_phase_pairs", set)


def _result(rows: list[dict[str, object]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [SimpleNamespace(_mapping=row) for row in rows]
    return result


def test_missing_expected_phases_are_lexical_and_precede_persisted_failures() -> None:
    failed = post_run_alert.include_missing_expected_phases(
        [
            {"phase": "scan", "status": "done", "error_message": None},
            {"phase": "connect", "status": "fail", "error_message": "broken"},
        ],
        {"scan", "zeta", "alpha"},
    )

    assert [row["phase"] for row in failed] == ["alpha", "zeta", "connect"]
    assert [row["status"] for row in failed[:2]] == ["partial", "partial"]


def test_build_alert_insight_globally_caps_synthetic_and_persisted_details() -> None:
    run_date = dt.date(2026, 7, 27)
    details = [
        {
            "phase": "absent",
            "status": "partial",
            "error_message": "expected enabled phase missing from dream_runs",
        },
        *[
            {"phase": f"phase-{index:02d}", "status": "fail", "error_message": "failure"}
            for index in range(20)
        ],
    ]

    report = post_run_alert.build_alert_insight(run_date, details)
    report_details = [line for line in report.splitlines() if line.startswith("- ")]

    assert len(report_details) == 20
    assert report_details[0].startswith("- absent [partial]")
    assert "phase-18" in report
    assert "phase-19" not in report
    assert "1 additional failure record omitted" in report


def test_build_alert_insight_truncates_error_to_first_240_characters() -> None:
    report = post_run_alert.build_alert_insight(
        dt.date(2026, 7, 27),
        [{"phase": "connect", "status": "fail", "error_message": "x" * 300 + "\nsecond"}],
    )

    detail = next(line for line in report.splitlines() if line.startswith("- connect"))
    assert "x" * 240 in detail
    assert "x" * 241 not in detail
    assert "second" not in detail


@pytest.mark.asyncio
async def test_failed_run_report_never_accesses_learnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock(spec=AsyncSession)
    persisted = [
        {
            "id": index,
            "phase": f"phase-{index:02d}",
            "status": "fail",
            "error_message": "broken",
            "created_at": dt.datetime(2026, 7, 27, 1, 0) - dt.timedelta(minutes=index),
        }
        for index in range(20)
    ]
    count_result = MagicMock()
    count_result.scalar_one.return_value = len(persisted)
    session.execute = AsyncMock(
        side_effect=[
            _result([{"phase": "scan", "status": "done"}]),
            _result(persisted),
            count_result,
        ]
    )
    session.commit = AsyncMock()
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", lambda: {"scan", "extract"})

    report = await post_run_alert.write_alert_if_failed(session, dt.date(2026, 7, 27))

    assert report is not None
    detail_lines = [line for line in report.splitlines() if line.startswith("- ")]
    assert len(detail_lines) == 20
    assert detail_lines[0].startswith("- extract [partial]")
    assert "phase-18 [fail]" in report
    assert "phase-19 [fail]" not in report
    assert "1 additional failure record omitted" in report
    assert session.execute.await_count == 3
    session.commit.assert_not_awaited()
    for call in session.execute.await_args_list:
        assert "learnings" not in str(call.args[0]).lower()
    # ── Guard WIDENED, and it must be said ───────────────────────────────
    # This line forbade the module from NAMING `learnings`. Step 0 of `55a21fb8`
    # forces it to: it COUNTS the freshness transitions with no provenance over
    # the six decay tables — 3 out of 44 measured on 2026-08-22, invisible until
    # then for lack of a reader.
    #
    # The naming ban was a COARSE guardrail for a narrower intent: the alert must
    # not touch the corpus. It is replaced by that intent, which forbids strictly
    # MORE than the literal version — an `INSERT` into `dream_runs` passed the old
    # one, it fails here. The REPORT path stays corpus-free: the assertion above,
    # which inspects the queries actually executed, has not moved.
    source = inspect.getsource(post_run_alert)
    for mutation in ("sa.insert", "sa.update", "sa.delete", "session.commit", "session.add"):
        assert mutation not in source, f"l'alerte post-run n'écrit RIEN : {mutation} interdit"
    touching_corpus = sorted(
        name
        for name, obj in vars(post_run_alert).items()
        if inspect.isfunction(obj) and "_FRESHNESS_TABLES" in inspect.getsource(obj)
    )
    assert touching_corpus == ["fetch_mute_transitions"], (
        f"une seule fonction accède au corpus, et par un COMPTE : {touching_corpus}"
    )
    counter = inspect.getsource(post_run_alert.fetch_mute_transitions)
    assert "sa.func.count()" in counter
    assert "learnings" not in counter.lower(), (
        "le compteur passe par la liste énumérée, jamais par une table en dur"
    )
    failures_statement = session.execute.await_args_list[1].args[0]
    rendered_failures_statement = str(
        failures_statement.compile(compile_kwargs={"literal_binds": True})
    )
    assert "ORDER BY dream_runs.created_at DESC, dream_runs.id DESC" in rendered_failures_statement
    # The FETCH cap sits UPSTREAM of the per-project grouping: at 21, a noisy
    # project at the end of the night filled the rows returned and the quiet
    # projects never reached the grouper — the order being `created_at DESC`, the
    # last projects served were the ones that won. What this test protects stays
    # the same: a BOUNDED query, never an open SELECT.
    assert f"LIMIT {post_run_alert.MAX_FETCHED_FAILURES}" in rendered_failures_statement
    assert post_run_alert.MAX_FETCHED_FAILURES <= 500, (
        "la requête doit rester bornée : c'est la propriété, pas le nombre"
    )


def _session_returning(observed: list[dict[str, object]]) -> AsyncMock:
    """A mock session faithful to `fetch_failed_runs`'s SQL.

    The persisted failures are NOT frozen empty: they are derived from the
    observed rows through the same `status in FAILED_STATUSES` filter as the real
    query. A harness that always returned `[]` would short-circuit the status —
    `include_missing_expected_phases` then no longer applies its filter and only
    the set of `phase` counts, so an observed row turned failing would redden no
    assertion.
    """
    session = AsyncMock(spec=AsyncSession)
    persisted = [row for row in observed if row.get("status") in post_run_alert.FAILED_STATUSES]
    count_result = MagicMock()
    count_result.scalar_one.return_value = len(persisted)
    session.execute = AsyncMock(side_effect=[_result(observed), _result(persisted), count_result])
    return session


@pytest.mark.asyncio
async def test_recorded_empty_pool_promote_row_raises_no_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row written on an empty pool makes the phase OBSERVED: no synthetic partial.

    The status comes from the real writer (`_promote_helpers`), not from a copy,
    and the harness applies the real `FAILED_STATUSES` filter: making that row
    failing over there reddens here. The message, for its part, is not observable
    at this level as long as the status is not — it is pinned at the writer
    (`test_empty_pool_row_says_the_pool_was_empty`), not claimed here.
    """
    from scripts.dream._promote_helpers import EMPTY_POOL_STATUS

    session = _session_returning([{"phase": "promote", "status": EMPTY_POOL_STATUS}])
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", lambda: {"promote"})

    report = await post_run_alert.write_alert_if_failed(session, dt.date(2026, 8, 8))

    assert report is None


@pytest.mark.asyncio
async def test_observed_promote_row_with_a_failing_status_still_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harness guard: an OBSERVED but failing row must still ring.

    Without it, `_session_returning` could fall back to "no persisted failure"
    without reddening anything, and the empty-pool test would become status-blind
    again — the very flaw this revision corrects.
    """
    session = _session_returning([{"phase": "promote", "status": "fail"}])
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", lambda: {"promote"})

    report = await post_run_alert.write_alert_if_failed(session, dt.date(2026, 8, 8))

    assert report is not None
    assert "- promote [fail]" in report


@pytest.mark.asyncio
async def test_expected_promote_that_never_wrote_a_row_still_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observability property: a promote that CRASHES writes nothing and still rings.

    This is the assertion that forbids the "simplification" of removing promote
    from the expected phases — the crashes of 2026-05-02/05-03 went unnoticed for
    two days for exactly that reason.
    """
    session = _session_returning([{"phase": "scan", "status": "done", "error_message": None}])
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", lambda: {"promote", "scan"})

    report = await post_run_alert.write_alert_if_failed(session, dt.date(2026, 8, 8))

    assert report is not None
    assert "- promote [partial]: expected enabled phase missing from dream_runs" in report


def test_dream_script_keeps_post_run_report_in_dated_log_and_original_failure_exit() -> None:
    script = Path("scripts/dream.sh").read_text()

    assert "python -m scripts.dream.post_run_alert" in script
    assert '>> "$LOG_DIR/$TIMESTAMP.log" 2>&1' in script
    assert "exit 1" in script


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report", "expected_first_line"),
    [
        ("bounded operational report", "bounded operational report"),
        (None, "no failures for 2026-07-27"),
    ],
)
async def test_run_keeps_operational_report_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    report: str | None,
    expected_first_line: str,
) -> None:
    """The report stays on stdout — and the machine line FOLLOWS it, always.

    Contract widened by ticket `0a9c067e`: the `COVERAGE …` line is the last line
    of stdout INCLUDING on nights with no anomaly. That is the whole point of the
    ticket — putting side by side, every morning, what the night says it did and
    what it wrote. A contract saying "stdout equals exactly the report" would
    forbid precisely the line that was missing.
    """
    engine = MagicMock()
    engine.dispose = AsyncMock()
    session = AsyncMock(spec=AsyncSession)
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_context)
    coverage = post_run_alert.coverage_fallback(expected=0, observed=0, missing=0)
    reporter = AsyncMock(return_value=post_run_alert.NightReport(report=report, coverage=coverage))
    monkeypatch.setattr(
        post_run_alert,
        "Settings",
        MagicMock(return_value=SimpleNamespace(postgres_url="postgresql+asyncpg://unused")),
    )
    monkeypatch.setattr(post_run_alert, "create_async_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(post_run_alert, "async_sessionmaker", MagicMock(return_value=factory))
    monkeypatch.setattr(post_run_alert, "review_night", reporter)
    # `review_and_render` also reads the provenance (step 0 of `55a21fb8`). This
    # test pins `_run`'s PLUMBING, not the count: we neutralise it.
    monkeypatch.setattr(
        post_run_alert,
        "fetch_mute_transitions",
        AsyncMock(
            return_value=post_run_alert.ProvenanceReport(run_date=dt.date(2026, 7, 27), counts=())
        ),
    )

    return_code = await post_run_alert._run(dt.date(2026, 7, 27))

    assert return_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == expected_first_line
    assert lines[-1].startswith("COVERAGE mode=fallback")
    reporter.assert_awaited_once_with(session, dt.date(2026, 7, 27), manifest=None)
    engine.dispose.assert_awaited_once()


def test_module_is_executable_as_main() -> None:
    assert callable(getattr(post_run_alert, "main", None))
