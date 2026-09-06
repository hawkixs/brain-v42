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

import argparse
import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream.promote_validate import (
    ValidationFailure,
    _amain,
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


# Fixture: SYNTHETIC log reproducing only the shape of the night 2026-09-06
# regression — the model's marker line reads '=== PROMOTE REPORT === Bettina',
# a stray trailing word the strict marker regex rejected outright even though
# the JSON body that followed was well-formed. This is not a copy of any real
# dream run: the candidate/target UUIDs, topic and draft are placeholder text
# generated for this test, carrying no project content (this repo publishes
# to a public GitHub remote and logs/ is gitignored, so a real log line must
# never end up here).
_TRAILING_WORD_FIXTURE = (
    Path(__file__).parent / "data" / "promote_report_marker_line_trailing_word.log"
)


def test_parse_report_tolerates_trailing_word_on_marker_line() -> None:
    """Regression — night 2026-09-06: a stray word after the marker
    ('=== PROMOTE REPORT === Bettina') must not fail a promotion whose JSON
    body is otherwise well-formed. The marker is anchored on the LINE, not
    on an exact '=== PROMOTE REPORT ===\\s*{' sequence.
    """
    raw = _TRAILING_WORD_FIXTURE.read_text()
    parsed = parse_report(raw)
    assert parsed["target_type"] == "adr"
    assert parsed["candidate_id"] == "f18ddc01-1493-4fdd-99a8-eeca3cb55512"
    assert parsed["target_id"] == "d778d077-0fd5-4e80-92e1-147845fa1959"
    assert parsed["dry_run"] is False


def test_parse_report_trailing_word_with_malformed_json_still_raises() -> None:
    """Negative witness: tolerating marker-line junk must not turn into
    tolerating bad JSON — a malformed body still fails strictly.
    """
    raw = "=== PROMOTE REPORT === Bettina\n{not: valid, json}\n=== END ==="
    with pytest.raises(ValidationFailure, match="malformed JSON"):
        parse_report(raw)


def test_parse_report_tolerates_prose_prefix_on_marker_line() -> None:
    """Regression: the codex rail writes the model's last message verbatim
    (``--output-last-message``), so the marker is not guaranteed to sit at
    the start of a line — a closing sentence can share the line with it
    ('Voici le rapport. === PROMOTE REPORT ===').  Anchoring the marker
    regex with ``^...$`` (MULTILINE) rejects this even though the JSON body
    that follows is well-formed; the marker must be located anywhere in the
    text, not only at line start.
    """
    raw = (
        "Voici le rapport. === PROMOTE REPORT ===\n"
        '{"target_type": "adr", "candidate_id": "abc"}\n'
        "=== END ==="
    )
    parsed = parse_report(raw)
    assert parsed == {"target_type": "adr", "candidate_id": "abc"}


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
# dream_run_id backfill decoupled from report parsing (night 2026-09-06)
#
# create_with_promotion (T2/T3) writes the adr/runbook audit row with
# dream_run_id=NULL at tool-call time — before dream.sh ever hands this
# script the LLM's stdout. The 2026-09-06 trailing-word marker made
# parse_report raise BEFORE the backfill in validate() ever ran, so the ADR
# that WAS accepted in production kept an orphaned dream_promotions row. The
# backfill must not depend on the report parsing at all: it only needs
# candidates[0]["id"] (known from the candidates file alone) and
# --dream-run-id.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amain_backfills_dream_run_id_even_when_report_parsing_fails(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    run_id = await _seed_dream_run(session_factory)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    raw = "no markers at all, the model rambled"  # parse_report raises

    args = argparse.Namespace(dream_run_id=run_id, project_key=isolated_pk)
    exit_code = await _amain(raw, candidates, session_factory, args)

    assert exit_code == 1

    async with session_factory() as session:
        promo_row = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(dream_promotions.c.target_adr_id == adr_id)
                )
            )
            .mappings()
            .one()
        )
        run_row = (
            (await session.execute(sa.select(dream_runs.c.status).where(dream_runs.c.id == run_id)))
            .mappings()
            .one()
        )
    assert promo_row["dream_run_id"] == run_id
    assert run_row["status"] == "partial"


@pytest.mark.asyncio
async def test_amain_backfill_runs_even_when_marker_line_has_trailing_junk(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Same fixture as the parse_report regression, driven end-to-end through
    _amain: with the marker-line fix this now succeeds outright (exit 0) and
    the promotion row is backfilled with dream_run_id.

    This is the happy-path witness only. Decoupling the backfill from report
    parsing — the actual claim of the section header above — is proven by
    ``test_amain_backfills_dream_run_id_even_when_report_parsing_fails``
    just above: with an unparseable report, ``validate()`` never runs its own
    inline backfill for the adr success path, so only the standalone call can
    have set dream_run_id there. This test's happy path cannot tell the two
    backfill sites apart (either one alone yields the same asserted row), so
    it does not itself witness decoupling.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    run_id = await _seed_dream_run(session_factory)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    raw = json.dumps(
        {
            "dry_run": False,
            "candidate_id": str(learning_id),
            "target_type": "adr",
            "target_id": str(adr_id),
        }
    )
    raw = f"=== PROMOTE REPORT === Bettina\n{raw}\n=== END ==="

    args = argparse.Namespace(dream_run_id=run_id, project_key=isolated_pk)
    exit_code = await _amain(raw, candidates, session_factory, args)

    assert exit_code == 0
    async with session_factory() as session:
        promo_row = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(dream_promotions.c.target_adr_id == adr_id)
                )
            )
            .mappings()
            .one()
        )
    assert promo_row["dream_run_id"] == run_id


@pytest.mark.asyncio
async def test_amain_backfill_is_noop_without_matching_promotion_row(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Negative witness: a candidate with no dream_promotions row yet (e.g.
    target_type='none', nothing materialized) must not blow up the backfill
    or fabricate a row.
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
    run_id = await _seed_dream_run(session_factory)
    candidates = [{"id": str(learning_id), "topic": "t"}]
    raw = '=== PROMOTE REPORT ===\n{"target_type": "none"}\n=== END ==='

    args = argparse.Namespace(dream_run_id=run_id, project_key=isolated_pk)
    exit_code = await _amain(raw, candidates, session_factory, args)

    assert exit_code == 0
    async with session_factory() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(dream_promotions)
                .where(dream_promotions.c.source_learning_id == learning_id)
            )
        ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# Standalone backfill call must be best-effort (fix round, night 2026-09-06
# follow-up): it used to sit OUTSIDE _amain's try/except. A DB failure there
# (or a non-UUID candidates[0]["id"]) crashed the whole process uncaught —
# no "PROMOTE VALIDATION FAILED" line, no dream_runs row ever marked
# 'partial'. It must degrade like every other repo backfill
# ("Best-effort — never raises"): warn on stderr and let validation run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amain_survives_backfill_db_failure_and_still_marks_partial(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_pk: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A DB failure inside the standalone backfill (simulated here; a real
    outage looks identical to the caller) must not crash _amain: validation
    still runs against the real DB and still marks the run 'partial'.
    """
    run_id = await _seed_dream_run(session_factory)
    candidates = [{"id": str(uuid.uuid4()), "topic": "t"}]
    raw = "no markers at all, the model rambled"  # parse_report raises

    async def _boom(*args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError("simulated DB outage")

    monkeypatch.setattr(
        "scripts.dream.promote_validate._backfill_dream_run_id_from_candidate",
        _boom,
    )

    args = argparse.Namespace(dream_run_id=run_id, project_key=isolated_pk)
    exit_code = await _amain(raw, candidates, session_factory, args)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "WARN backfill dream_run_id skipped" in captured.err
    assert "PROMOTE VALIDATION FAILED" in captured.err

    async with session_factory() as session:
        run_row = (
            (await session.execute(sa.select(dream_runs.c.status).where(dream_runs.c.id == run_id)))
            .mappings()
            .one()
        )
    assert run_row["status"] == "partial"


@pytest.mark.asyncio
async def test_amain_survives_non_uuid_candidate_id_in_backfill(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_pk: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same guarantee, real trigger: candidates[0]["id"] that is not a UUID
    (a malformed candidates file, not a DB problem) must not crash _amain
    either — ``UUID(candidate_id)`` raises ValueError inside the backfill.
    """
    run_id = await _seed_dream_run(session_factory)
    candidates = [{"id": "not-a-uuid", "topic": "t"}]
    raw = "no markers at all, the model rambled"  # parse_report raises

    args = argparse.Namespace(dream_run_id=run_id, project_key=isolated_pk)
    exit_code = await _amain(raw, candidates, session_factory, args)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "WARN backfill dream_run_id skipped" in captured.err
    assert "PROMOTE VALIDATION FAILED" in captured.err

    async with session_factory() as session:
        run_row = (
            (await session.execute(sa.select(dream_runs.c.status).where(dream_runs.c.id == run_id)))
            .mappings()
            .one()
        )
    assert run_row["status"] == "partial"


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
