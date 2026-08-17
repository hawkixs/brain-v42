"""Unit tests for scripts.dream.promote_validate (T9 of Dream v3 plan).

Covers:
  1. parse_report extracts the JSON block between ``=== PROMOTE REPORT ===``
     and ``=== END ===``; malformed or missing -> ValidationFailure.
  2. validate() success paths for adr / runbook / skip types.
  3. validate() fail paths: candidate_id mismatch, hallucinated target_id.
  4. On any ValidationFailure, _mark_dream_run_partial flips the dream_runs
     row to status='partial' with the error message.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream.promote_validate import (
    ValidationFailure,
    _mark_dream_run_partial,
    parse_report,
    validate,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import (
    adrs,
    dream_promotions,
    dream_runs,
    learnings,
    runbooks,
)
from tests.conftest import require_test_db_url  # noqa: E402
from tests.unit.keys import make_unit_project_key


@pytest_asyncio.fixture(scope="module")
async def _engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(require_test_db_url(), poolclass=NullPool, echo=False)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    yield eng  # type: ignore[misc]
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def isolated_pk() -> str:
    return make_unit_project_key("t9")


# ────────── parse_report ──────────────────────────────────────────────────────


def test_parse_report_extracts_json_between_markers() -> None:
    raw = (
        "some log noise\n"
        "=== PROMOTE REPORT ===\n"
        '{"target_type": "adr", "candidate_id": "abc"}\n'
        "=== END ===\n"
        "trailing stuff"
    )
    parsed = parse_report(raw)
    assert parsed == {"target_type": "adr", "candidate_id": "abc"}


def test_parse_report_missing_markers_raises() -> None:
    with pytest.raises(ValidationFailure, match="missing PROMOTE REPORT markers"):
        parse_report("no markers here, just text")


def test_parse_report_malformed_json_raises() -> None:
    # Braces are balanced so the outer regex captures — but the contents
    # aren't valid JSON, so json.loads must fail.
    raw = "=== PROMOTE REPORT ===\n{not: valid, json}\n=== END ==="
    with pytest.raises(ValidationFailure, match="malformed JSON"):
        parse_report(raw)


# ────────── validate — success paths ──────────────────────────────────────────


async def _seed_learning_and_adr(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    *,
    adr_status: str = "accepted",
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="t",
                        insight="i",
                        project_key=project_key,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()
            adr_id = (
                await session.execute(
                    adrs.insert()
                    .values(
                        number=1,
                        title=f"A-{uuid.uuid4().hex[:6]}",
                        context="c",
                        decision="d",
                        consequences="q",
                        alternatives_considered=[],
                        project_key=project_key,
                        tags=[],
                        status=adr_status,
                    )
                    .returning(adrs.c.id)
                )
            ).scalar_one()
            await session.execute(
                dream_promotions.insert().values(
                    source_learning_id=learning_id,
                    target_type="adr",
                    target_adr_id=adr_id,
                )
            )
    return learning_id, adr_id


@pytest.mark.asyncio
async def test_validate_adr_happy_path_passes(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)


@pytest.mark.asyncio
async def test_validate_none_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await validate(
        {"target_type": "none"},
        [],
        session_factory,
        dream_run_id=None,
        project_key="pk-unused",
    )


@pytest.mark.asyncio
async def test_validate_skip_path_inserts_audit_row(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="t",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "skipped_dedup",
        "candidate_id": str(learning_id),
        "cosine_observed": 0.92,
        "reason": "matches existing ADR-3",
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(
                        dream_promotions.c.source_learning_id == learning_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["target_type"] == "skipped_dedup"
    assert row["cosine_observed"] == pytest.approx(0.92)
    assert row["skipped_reason"] == "matches existing ADR-3"


@pytest.mark.asyncio
async def test_validate_skip_path_accepts_long_llm_reason(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Regression — Dream nights 2026-05-02 and 2026-05-03 both crashed
    PROMOTE on StringDataRightTruncationError because the LLM-generated
    `reason` for classification_uncertain was ~430 chars vs the
    VARCHAR(100) cap. The audit field must accept the full justification
    so the dream_run is not flagged partial just because Opus is verbose.
    """
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="t",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()

    long_reason = (
        "Source is an exploration/brainstorm survey: enumerates "
        "architecture state, 21-tool inventory, and 6 LLM-UX pain "
        "points without selecting between alternatives (no ADR-shaped "
        "decision/alternatives) and without a sequential reproducible "
        "procedure (no runbook steps). Materializing either target would "
        "require fabricating content not substantively supported by the "
        "insight."
    )
    assert len(long_reason) > 100

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "classification_uncertain",
        "candidate_id": str(learning_id),
        "reason": long_reason,
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(
                        dream_promotions.c.source_learning_id == learning_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["target_type"] == "classification_uncertain"
    assert row["skipped_reason"] == long_reason


async def _seed_learning_and_runbook(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="t",
                        insight="i",
                        project_key=project_key,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()
            runbook_id = (
                await session.execute(
                    runbooks.insert()
                    .values(
                        title=f"R-{uuid.uuid4().hex[:6]}",
                        description="d",
                        project_key=project_key,
                        trigger="trig",
                    )
                    .returning(runbooks.c.id)
                )
            ).scalar_one()
            await session.execute(
                dream_promotions.insert().values(
                    source_learning_id=learning_id,
                    target_type="runbook",
                    target_runbook_id=runbook_id,
                )
            )
    return learning_id, runbook_id


async def _seed_dream_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    dream_runs.insert()
                    .values(run_date=dt.date.today(), phase="promote", status="ok")
                    .returning(dream_runs.c.id)
                )
            ).scalar_one()


@pytest.mark.asyncio
async def test_validate_adr_wet_backfills_dream_run_id(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """WET materialization: repo writes dream_promotions with dream_run_id=NULL
    (agent has no knowledge of the run id). Validator knows it — must backfill
    the FK so metrics/audit queries can join dream_promotions → dream_runs.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    run_id = await _seed_dream_run(session_factory)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "dry_run": False,
    }
    await validate(
        report, candidates, session_factory, dream_run_id=run_id, project_key=isolated_pk
    )

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(dream_promotions.c.target_adr_id == adr_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["dream_run_id"] == run_id


@pytest.mark.asyncio
async def test_validate_runbook_wet_backfills_dream_run_id(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    learning_id, runbook_id = await _seed_learning_and_runbook(session_factory, isolated_pk)
    run_id = await _seed_dream_run(session_factory)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "runbook",
        "candidate_id": str(learning_id),
        "target_id": str(runbook_id),
        "dry_run": False,
    }
    await validate(
        report, candidates, session_factory, dream_run_id=run_id, project_key=isolated_pk
    )

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(
                        dream_promotions.c.target_runbook_id == runbook_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["dream_run_id"] == run_id


@pytest.mark.asyncio
async def test_validate_adr_wet_with_none_run_id_leaves_dream_run_id_null(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """When DREAM_RUN_ID helper fails (returns None), validator must not
    clobber an existing dream_run_id nor raise — just no-op the backfill.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "dry_run": False,
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(dream_promotions.c.target_adr_id == adr_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["dream_run_id"] is None


# ────────── validate — fail paths ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_candidate_mismatch_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    candidates = [{"id": str(uuid.uuid4()), "topic": "right"}]
    report = {
        "target_type": "adr",
        "candidate_id": str(uuid.uuid4()),
        "target_id": str(uuid.uuid4()),
    }
    with pytest.raises(ValidationFailure, match="does not match"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key="pk-unused"
        )


@pytest.mark.asyncio
async def test_validate_hallucinated_adr_raises(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="t",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(uuid.uuid4()),  # random UUID, no matching ADR
    }
    with pytest.raises(ValidationFailure, match="not found or not accepted"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


# ────────── _mark_dream_run_partial ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_dream_run_partial_updates_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            run_id = (
                await session.execute(
                    dream_runs.insert()
                    .values(
                        run_date=dt.date.today(),
                        phase="promote",
                        status="ok",
                    )
                    .returning(dream_runs.c.id)
                )
            ).scalar_one()

    await _mark_dream_run_partial(session_factory, run_id, "something went wrong")

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(dream_runs.c.status, dream_runs.c.error_message).where(
                        dream_runs.c.id == run_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "partial"
    assert row["error_message"] == "something went wrong"


@pytest.mark.asyncio
async def test_mark_dream_run_partial_with_none_run_id_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _mark_dream_run_partial(session_factory, None, "n/a")


# ---------------------------------------------------------------------------
# Project-scope enforcement (lot 1 of the v2 delivery order, spec §8)
#
# Until now validate() never looked at project_key. At one project that is
# invisible; at 55 it means PROMOTE can write an ADR into the wrong project and
# nothing downstream notices. The check fails the run — a mislabelled promotion
# is a referential-integrity violation like the others in this file, and
# dream_runs is marked partial by main()'s existing handler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_rejects_adr_created_in_another_project(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """The ADR exists and its audit row is correct — only the project is wrong."""
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
    }
    with pytest.raises(ValidationFailure, match="project"):
        await validate(
            report,
            candidates,
            session_factory,
            dream_run_id=None,
            project_key=f"{isolated_pk}-other",
        )


@pytest.mark.asyncio
async def test_validate_rejects_runbook_created_in_another_project(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    learning_id, rb_id = await _seed_learning_and_runbook(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "runbook",
        "candidate_id": str(learning_id),
        "target_id": str(rb_id),
    }
    with pytest.raises(ValidationFailure, match="project"):
        await validate(
            report,
            candidates,
            session_factory,
            dream_run_id=None,
            project_key=f"{isolated_pk}-other",
        )


@pytest.mark.asyncio
async def test_validate_accepts_adr_in_the_expected_project(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """The negative twin of the rejection: same shape, matching project, passes.

    Without this, a check that rejected *everything* would look correct.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)


@pytest.mark.asyncio
async def test_validate_accepts_runbook_in_the_expected_project(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    learning_id, rb_id = await _seed_learning_and_runbook(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "runbook",
        "candidate_id": str(learning_id),
        "target_id": str(rb_id),
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)
