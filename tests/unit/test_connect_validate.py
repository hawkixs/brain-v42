from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream.connect_validate import (
    ValidationFailure,
    _mark_latest_connect_partial,
    parse_report,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import dream_runs
from tests.conftest import require_test_db_url

VALID_REPORT = (
    "STEP_A: entities_processed=10 created=2 matched=8 skipped=0 errors=0 freshness=0.20\n"
    "STEP_B: orphans_listed=4 created=3 matched=1 invalid=0 errors=0\n"
)


def test_parse_report_accepts_exact_zero_error_contract() -> None:
    report = parse_report(VALID_REPORT)
    assert report.step_a["entities_processed"] == 10
    assert report.step_a["freshness"] == 0.20
    assert report.step_b["orphans_listed"] == 4


@pytest.mark.parametrize(
    "raw",
    [
        VALID_REPORT.replace("errors=0 freshness", "errors=1 freshness", 1),
        VALID_REPORT.rsplit("errors=0", 1)[0] + "errors=2\n",
    ],
)
def test_parse_report_rejects_any_non_zero_error_bucket(raw: str) -> None:
    with pytest.raises(ValidationFailure, match="reported errors"):
        parse_report(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (VALID_REPORT.splitlines()[0], "exactly two"),
        (VALID_REPORT + VALID_REPORT.splitlines()[1] + "\n", "exactly two"),
        (VALID_REPORT.replace("STEP_A", "STEP_X"), "malformed STEP_A"),
        (VALID_REPORT.replace("created=2", "created=-2"), "non-negative"),
        (VALID_REPORT.replace("freshness=0.20", "freshness=1.20"), "freshness"),
    ],
)
def test_parse_report_fails_closed(raw: str, message: str) -> None:
    with pytest.raises(ValidationFailure, match=message):
        parse_report(raw)


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncEngine:  # type: ignore[misc]
    database = create_async_engine(require_test_db_url(), poolclass=NullPool)
    try:
        async with database.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL test database is not reachable: {exc}")
    yield database  # type: ignore[misc]
    await database.dispose()


@pytest_asyncio.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_marks_only_latest_connect_row_partial_with_bounded_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_date = dt.date(2099, 12, 30)
    async with session_factory.begin() as session:
        first_id = (
            await session.execute(
                dream_runs.insert()
                .values(
                    run_date=run_date,
                    phase="connect",
                    status="done",
                    project_key="test-project",
                )
                .returning(dream_runs.c.id)
            )
        ).scalar_one()
        second_id = (
            await session.execute(
                dream_runs.insert()
                .values(
                    run_date=run_date,
                    phase="connect",
                    status="done",
                    project_key="test-project",
                )
                .returning(dream_runs.c.id)
            )
        ).scalar_one()
        # The decoy, and it is what makes the proof: same date, same phase, HIGHEST
        # id, different project. Without connect_run_id_statement's project filter the
        # validator would mark THAT row — one project's failure written onto another,
        # which spec §12 forbids. A test without this decoy stays green with the
        # filter removed.
        decoy_id = (
            await session.execute(
                dream_runs.insert()
                .values(
                    run_date=run_date,
                    phase="connect",
                    status="done",
                    project_key="other-project",
                )
                .returning(dream_runs.c.id)
            )
        ).scalar_one()

    assert await _mark_latest_connect_partial(
        session_factory, run_date, "x" * 1_200, "test-project"
    )

    async with session_factory.begin() as session:
        rows = (
            (
                await session.execute(
                    sa.select(
                        dream_runs.c.id,
                        dream_runs.c.status,
                        dream_runs.c.error_message,
                    )
                    .where(dream_runs.c.id.in_([first_id, second_id, decoy_id]))
                    .order_by(dream_runs.c.id)
                )
            )
            .mappings()
            .all()
        )
        await session.execute(
            dream_runs.delete().where(dream_runs.c.id.in_([first_id, second_id, decoy_id]))
        )

    assert rows[0]["status"] == "done"
    assert rows[0]["error_message"] is None
    assert rows[1]["status"] == "partial"
    assert rows[1]["error_message"] == "x" * 1_000
    # The decoy is intact: the validator did not spill over onto the other project.
    assert rows[2]["status"] == "done"
    assert rows[2]["error_message"] is None


def test_main_reports_missing_dream_run_on_invalid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.dream import connect_validate

    report_log = tmp_path / "connect.log"
    report_log.write_text(
        VALID_REPORT.replace("errors=0 freshness", "errors=6 freshness"),
        encoding="utf-8",
    )
    marker = AsyncMock(return_value=False)
    settings_loader = MagicMock(return_value=MagicMock(postgres_url="unused"))
    monkeypatch.setattr(connect_validate, "_mark_latest_connect_partial", marker)
    monkeypatch.setattr(connect_validate, "get_settings", settings_loader)
    monkeypatch.setattr(connect_validate, "_build_factory", lambda _url: MagicMock())

    assert (
        connect_validate.main(
            [
                "--report-log",
                str(report_log),
                "--run-date",
                "2026-07-25",
                "--project-key",
                "test-project",
            ]
        )
        == 1
    )
    settings_loader.assert_called_once_with()
    assert "no CONNECT dream_runs row" in capsys.readouterr().err


def test_main_valid_report_does_not_load_settings_or_build_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.dream import connect_validate

    report_log = tmp_path / "connect.log"
    report_log.write_text(VALID_REPORT, encoding="utf-8")
    settings_loader = MagicMock(side_effect=AssertionError("settings must stay lazy"))
    factory_builder = MagicMock(side_effect=AssertionError("session must stay lazy"))
    marker = AsyncMock()
    monkeypatch.setattr(connect_validate, "get_settings", settings_loader)
    monkeypatch.setattr(connect_validate, "_build_factory", factory_builder)
    monkeypatch.setattr(connect_validate, "_mark_latest_connect_partial", marker)

    assert (
        connect_validate.main(
            [
                "--report-log",
                str(report_log),
                "--run-date",
                "2026-07-25",
                "--project-key",
                "test-project",
            ]
        )
        == 0
    )
    settings_loader.assert_not_called()
    factory_builder.assert_not_called()
    marker.assert_not_awaited()
    assert "CONNECT VALIDATE: OK" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raw", "expected_detail"),
    [
        pytest.param("\n" + VALID_REPORT, "exactly two", id="leading-blank"),
        pytest.param(VALID_REPORT + "\n", "exactly two", id="extra-trailing-blank"),
        pytest.param(" " + VALID_REPORT, "malformed STEP_A", id="leading-space"),
    ],
)
def test_main_rejects_whitespace_outside_exact_report_and_marks_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str,
    expected_detail: str,
) -> None:
    from scripts.dream import connect_validate

    report_log = tmp_path / "connect.log"
    report_log.write_text(raw, encoding="utf-8")
    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(connect_validate, "get_settings", lambda: MagicMock(postgres_url="unused"))
    monkeypatch.setattr(connect_validate, "_build_factory", lambda _url: MagicMock())
    monkeypatch.setattr(connect_validate, "_mark_latest_connect_partial", marker)

    assert (
        connect_validate.main(
            [
                "--report-log",
                str(report_log),
                "--run-date",
                "2026-07-25",
                "--project-key",
                "test-project",
            ]
        )
        == 1
    )
    marker.assert_awaited_once()
    _, marked_date, marked_error, _project = marker.await_args.args
    assert marked_date == dt.date(2026, 7, 25)
    assert expected_detail in marked_error
    assert expected_detail in capsys.readouterr().err


@pytest.mark.parametrize(
    ("read_error", "expected_detail"),
    [
        pytest.param(
            FileNotFoundError("missing report " + "x" * 1_200),
            "missing report",
            id="missing",
        ),
        pytest.param(PermissionError("report is unreadable"), "unreadable", id="unreadable"),
        pytest.param(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            "utf-8",
            id="invalid-utf8",
        ),
    ],
)
def test_main_handles_report_read_failures_and_marks_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_error: Exception,
    expected_detail: str,
) -> None:
    from scripts.dream import connect_validate

    report_log = tmp_path / "connect.log"
    marker = AsyncMock(return_value=True)
    session_factory = MagicMock()
    monkeypatch.setattr(
        connect_validate.Path,
        "read_text",
        MagicMock(side_effect=read_error),
    )
    monkeypatch.setattr(connect_validate, "_mark_latest_connect_partial", marker)
    monkeypatch.setattr(connect_validate, "get_settings", lambda: MagicMock(postgres_url="unused"))
    monkeypatch.setattr(connect_validate, "_build_factory", lambda _url: session_factory)

    assert (
        connect_validate.main(
            [
                "--report-log",
                str(report_log),
                "--run-date",
                "2026-07-25",
                "--project-key",
                "test-project",
            ]
        )
        == 1
    )
    marker.assert_awaited_once()
    marked_factory, marked_date, marked_error, marked_project = marker.await_args.args
    assert marked_factory is session_factory
    assert marked_date == dt.date(2026, 7, 25)
    assert expected_detail in marked_error
    assert len(marked_error) <= 1_000
    stderr = capsys.readouterr().err
    prefix = "CONNECT VALIDATION FAILED: "
    assert stderr.startswith(prefix)
    assert expected_detail in stderr
    assert len(stderr.removeprefix(prefix).rstrip("\n")) <= 1_000


def test_main_handles_oversized_numeric_counter_and_marks_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.dream import connect_validate

    report_log = tmp_path / "connect.log"
    report_log.write_text(
        VALID_REPORT.replace("entities_processed=10", f"entities_processed={'9' * 5_000}"),
        encoding="utf-8",
    )
    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(connect_validate, "_mark_latest_connect_partial", marker)
    monkeypatch.setattr(connect_validate, "get_settings", lambda: MagicMock(postgres_url="unused"))
    monkeypatch.setattr(connect_validate, "_build_factory", lambda _url: MagicMock())

    assert (
        connect_validate.main(
            [
                "--report-log",
                str(report_log),
                "--run-date",
                "2026-07-25",
                "--project-key",
                "test-project",
            ]
        )
        == 1
    )
    marker.assert_awaited_once()
    _, marked_date, marked_error, _project = marker.await_args.args
    assert marked_date == dt.date(2026, 7, 25)
    assert "numeric report field" in marked_error
    assert len(marked_error) <= 1_000
    assert "numeric report field" in capsys.readouterr().err


def test_main_preserves_validation_error_when_partial_marking_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.dream import connect_validate

    report_log = tmp_path / "connect.log"
    report_log.write_text(
        VALID_REPORT.replace("errors=0 freshness", "errors=6 freshness"),
        encoding="utf-8",
    )
    marker = AsyncMock(side_effect=RuntimeError("db down " + "x" * 1_200))
    monkeypatch.setattr(connect_validate, "_mark_latest_connect_partial", marker)
    monkeypatch.setattr(connect_validate, "get_settings", lambda: MagicMock(postgres_url="unused"))
    monkeypatch.setattr(connect_validate, "_build_factory", lambda _url: MagicMock())

    assert (
        connect_validate.main(
            [
                "--report-log",
                str(report_log),
                "--run-date",
                "2026-07-25",
                "--project-key",
                "test-project",
            ]
        )
        == 1
    )
    marker.assert_awaited_once()
    _, marked_date, marked_error, _project = marker.await_args.args
    assert marked_date == dt.date(2026, 7, 25)
    assert "STEP_A.errors=6" in marked_error
    stderr = capsys.readouterr().err
    prefix = "CONNECT VALIDATION FAILED: "
    assert stderr.startswith(prefix)
    detail = stderr.removeprefix(prefix).rstrip("\n")
    assert "STEP_A.errors=6" in detail
    assert "failed to mark CONNECT dream_runs row partial" in detail
    assert "db down" in detail
    assert len(detail) <= 1_000
