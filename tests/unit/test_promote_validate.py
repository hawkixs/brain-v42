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
    _as_float_or_fail,
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


# ────────── dedup shadow-verdict fixtures (W25 lot 1) ──────────────────────
#
# `candidates[0]["dedup"]` is the block `promote_prepare.attach_dedup_verdict`
# injects (see test_promote_prepare_dedup_bands.py for the real SQL). These
# tests exercise `validate()` in isolation, so a synthetic block of the same
# SHAPE is enough — the two are pinned to agree by
# test_human_access_count_survives_all_the_way_into_the_promote_prompt-style
# end-to-end coverage on the promote_prepare side, not duplicated here.


def _dedup_family_block(
    band: str = "clear",
    *,
    nearest_id: str | None = None,
    nearest_title: str | None = None,
    nearest_raw_cosine: float | None = None,
) -> dict:
    return {
        "band": band,
        "nearest_id": nearest_id,
        "nearest_title": nearest_title,
        "nearest_raw_cosine": nearest_raw_cosine,
        "top3": [],
        "null_embedding_excluded": 0,
    }


def _dedup_block(adr: dict | None = None, runbook: dict | None = None) -> dict:
    """Both families default to "clear" with nothing to compare against —
    the shape a validator call unrelated to dedup behaviour should see.
    """
    return {
        "score_kind": "raw_cosine_pgvector",
        "adr": adr or _dedup_family_block(),
        "runbook": runbook or _dedup_family_block(),
    }


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
    candidates = [{"id": str(learning_id), "topic": "t", "dedup": _dedup_block()}]
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
async def test_validate_none_with_pool_but_no_reported_candidate_is_still_noop(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Discriminator is the REPORT's candidate_id, not whether the pool was
    non-empty. A model that reports bare ``{"target_type": "none"}`` while a
    pool existed said nothing identifiable — still nothing to audit.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]
    await validate(
        {"target_type": "none"},
        candidates,
        session_factory,
        dream_run_id=None,
        project_key=isolated_pk,
    )

    async with session_factory() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(dream_promotions)
                .where(dream_promotions.c.source_learning_id == learning_id)
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_validate_none_with_candidate_is_audited_as_refused(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Night 2026-09-06/07 regression: auto-discord and watchk-claude both
    reported ``target_type="none"`` carrying a real ``candidate_id``,
    ``candidate_topic`` and ``draft_title`` because the promote tool itself
    was unavailable — a REFUSAL, not "nothing happened". The old early
    ``return`` at the top of validate() swallowed both: dream_promotions held
    4 rows for a night that examined 6 candidates. A refusal that names a
    candidate is an audited outcome and MUST produce a row.

    dream_promotions_target_shape (migration 017) does not admit
    target_type='none' — confirmed live against production
    (INSERT ... target_type='none' raises CheckViolation) — so this can't be
    stored verbatim without a migration, which this validator-only lot does
    not write. The row is filed under 'dedup_unavailable' (no migration
    needed, and unlike 'classification_uncertain' it carries no terminal
    "don't re-offer" exclusion in promote_prepare.py's candidate query — a
    transient tool outage must not permanently blacklist a good candidate),
    with the literal reported value preserved in skipped_reason.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "none",
        "candidate_id": str(learning_id),
        "candidate_topic": "t",
        "draft_title": "Some draft title",
        "reason": "promote tool unavailable",
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
    assert row["target_type"] == "dedup_unavailable"
    assert row["target_adr_id"] is None
    assert row["target_runbook_id"] is None
    assert row["cosine_observed"] is None
    assert "promote tool unavailable" in row["skipped_reason"]
    assert "none" in row["skipped_reason"]


@pytest.mark.asyncio
async def test_validate_none_with_candidate_and_no_reason_still_records_row(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """The prompt never requires a `reason` field for target_type='none' (it
    is only listed in the output shape's enum, not tied to a step). A missing
    reason must not crash the audit write or silently drop the row.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {"target_type": "none", "candidate_id": str(learning_id)}
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
    assert row["target_type"] == "dedup_unavailable"
    assert row["skipped_reason"] is not None
    assert "none" in row["skipped_reason"]


@pytest.mark.asyncio
async def test_validate_none_candidate_mismatch_raises(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """A 'none' report naming a candidate is still subject to the same
    referential-integrity check as every other target_type: the reported
    candidate_id must match candidates[0].id.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {"target_type": "none", "candidate_id": str(uuid.uuid4())}
    with pytest.raises(ValidationFailure, match="does not match candidates\\[0\\].id"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )

    async with session_factory() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(dream_promotions)
                .where(dream_promotions.c.source_learning_id == learning_id)
            )
        ).scalar_one()
    assert count == 0


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

    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(
                adr=_dedup_family_block(
                    "block",
                    nearest_id=str(uuid.uuid4()),
                    nearest_title="ADR-3",
                    nearest_raw_cosine=0.9153,
                )
            ),
        }
    ]
    # Asymmetric on purpose (review finding, round 3): the injected
    # 0.9153 and the reported 0.92 differ by 0.0047, within
    # _DEDUP_COSINE_TOLERANCE (6e-3) so the cross-check still passes, but
    # far enough apart that persisting the WRONG one is visible below. What
    # must land in the row is the SERVER-injected 0.9153, never the
    # model's 0.92 transcription.
    report = {
        "target_type": "skipped_dedup",
        "candidate_id": str(learning_id),
        "dedup_family": "adr",
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
    assert row["cosine_observed"] == pytest.approx(0.9153)
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
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("clear", nearest_raw_cosine=0.5)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "dry_run": False,
        "cosine_observed": 0.5,
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
    # W25 lot 1: the model-reported cosine, cross-checked against the
    # server-injected one, is now persisted on the MATERIALIZED row too —
    # not just on skip paths.
    assert row["cosine_observed"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_validate_runbook_wet_backfills_dream_run_id(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    learning_id, runbook_id = await _seed_learning_and_runbook(session_factory, isolated_pk)
    run_id = await _seed_dream_run(session_factory)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(runbook=_dedup_family_block("clear", nearest_raw_cosine=0.4)),
        }
    ]
    report = {
        "target_type": "runbook",
        "candidate_id": str(learning_id),
        "target_id": str(runbook_id),
        "dry_run": False,
        "cosine_observed": 0.4,
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
    assert row["cosine_observed"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_validate_adr_wet_with_none_run_id_leaves_dream_run_id_null(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """When DREAM_RUN_ID helper fails (returns None), validator must not
    clobber an existing dream_run_id nor raise — just no-op the backfill.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t", "dedup": _dedup_block()}]
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


# ────────── dedup shadow-verdict cross-check (W25 lot 1) ───────────────────────


@pytest.mark.asyncio
async def test_validate_rejects_missing_dedup_block(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """A candidates[0] with no `dedup` key at all is a bug (promote_prepare
    silently not deployed / stale pool), never a silent pass. Fail closed.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t"}]  # no "dedup" key
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
    }
    with pytest.raises(ValidationFailure, match="missing dedup band"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_missing_dedup_block_runbook(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Runbook twin of test_validate_rejects_missing_dedup_block. Mutation
    verified (review finding, fix round): deleting the
    `_check_dedup_against_pool(...)` call on the runbook materialization
    branch leaves every other test in this module green — only the ADR
    branch had a witness for this guard.
    """
    learning_id, rb_id = await _seed_learning_and_runbook(session_factory, isolated_pk)
    candidates = [{"id": str(learning_id), "topic": "t"}]  # no "dedup" key
    report = {
        "target_type": "runbook",
        "candidate_id": str(learning_id),
        "target_id": str(rb_id),
    }
    with pytest.raises(ValidationFailure, match="missing dedup band"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_cosine_diverging_from_injected_pool(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """The model's reported `cosine_observed` must match the server-injected
    `nearest_raw_cosine` for the SAME family. A divergence is treated as
    fabrication (`dream.sh:1100` already hands the validator the pool that
    was injected into the prompt — this is a free cross-check).
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("clear", nearest_raw_cosine=0.3)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": 0.95,  # fabricated — server injected 0.3
    }
    with pytest.raises(ValidationFailure, match="diverges from the server-injected"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_cosine_diverging_from_injected_pool_runbook(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Runbook twin of test_validate_rejects_cosine_diverging_from_injected_pool.
    Same fabrication cross-check, same mutation witness, runbook family."""
    learning_id, rb_id = await _seed_learning_and_runbook(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(runbook=_dedup_family_block("clear", nearest_raw_cosine=0.3)),
        }
    ]
    report = {
        "target_type": "runbook",
        "candidate_id": str(learning_id),
        "target_id": str(rb_id),
        "cosine_observed": 0.95,  # fabricated — server injected 0.3
    }
    with pytest.raises(ValidationFailure, match="diverges from the server-injected"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


def test_as_float_or_fail_rejects_non_numeric_string() -> None:
    """`cosine_observed` arrives straight from `json.loads` on the model's
    report -- a stray shape must fail closed as ValidationFailure, not an
    uncaught ValueError (review finding, fix round: `_amain` only catches
    ValidationFailure, so anything else dies with a traceback and never
    marks the run 'partial').
    """
    with pytest.raises(ValidationFailure, match="is not a number"):
        _as_float_or_fail("0.85 (approx)", "cosine_observed")


def test_as_float_or_fail_rejects_list() -> None:
    with pytest.raises(ValidationFailure, match="is not a number"):
        _as_float_or_fail([0.85], "cosine_observed")


def test_as_float_or_fail_accepts_a_numeric_string() -> None:
    """Negative twin: JSON round-tripping a number AS a string (rare but
    not itself malformed) must still convert, not fail.
    """
    assert _as_float_or_fail("0.85", "cosine_observed") == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_validate_rejects_non_numeric_cosine_string_as_validation_failure(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Production-path witness for `_as_float_or_fail`: a non-numeric
    `cosine_observed` reaching the divergence cross-check used to raise a
    bare, uncaught `ValueError` out of `float()` -- `_amain` catches only
    `ValidationFailure`, so the process died with a Python traceback,
    printed no "PROMOTE VALIDATION FAILED" line, and never called
    `_mark_dream_run_partial`. Verified before the fix: `float("0.85
    (approx)")` raises `ValueError: could not convert string to float`.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("clear", nearest_raw_cosine=0.3)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": "0.85 (approx)",
    }
    with pytest.raises(ValidationFailure, match="is not a number"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_list_shaped_cosine_as_validation_failure(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Same production-path witness, list shape (`TypeError` out of a bare
    `float()`, not `ValueError` -- both must fail the same way).
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("clear", nearest_raw_cosine=0.3)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": [0.3],
    }
    with pytest.raises(ValidationFailure, match="is not a number"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_fabricated_cosine_when_server_has_no_candidate(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """The server injected `nearest_raw_cosine: null` (no same-family row
    exists yet) but the model reported a number anyway — fabrication.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(),  # both families: nearest_raw_cosine=None
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": 0.5,
    }
    with pytest.raises(ValidationFailure, match="fabrication"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_accepts_matching_cosine_within_tolerance(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Negative twin: a cosine that matches (within float tolerance) passes."""
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("clear", nearest_raw_cosine=0.30000001)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": 0.3,
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)


@pytest.mark.asyncio
async def test_validate_accepts_two_decimal_transcription_of_injected_cosine(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Blocker regression (fix round, W25 lot 1): all 15 historical
    dream_promotions.cosine_observed values the model has ever reported
    carry 2 decimals (0.82, 0.98, 0.87, ...), and phase_promote.md's own
    dry-run example teaches ``"cosine_observed": 0.42`` -- a 2-decimal
    style. `candidates[0].dedup.<family>.nearest_raw_cosine` is what
    promote_prepare.py now rounds to (at most) 4 decimals before injecting
    it into the pool JSON (see test_promote_prepare_dedup_bands.py), so the
    validator's tolerance must accept a faithful 2-decimal transcription of
    THAT number -- not demand the model reproduce a 16-significant-digit
    float it was never asked to compute. 0.65 is exactly what a model
    transcribing 0.645 to 2 decimals would write.

    What lands in `dream_promotions.cosine_observed` is the SERVER-injected
    0.645, not the model's 0.65 transcription (review finding, fix round):
    the reported value is used only as a cross-check, and as a fallback
    when the server injects nothing at all. Reading the row back is what
    makes this distinction a witness rather than a claim in a docstring.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("clear", nearest_raw_cosine=0.645)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": 0.65,
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(dream_promotions.c.cosine_observed).where(
                        dream_promotions.c.target_adr_id == adr_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["cosine_observed"] == pytest.approx(0.645)


@pytest.mark.asyncio
async def test_validate_accepts_two_decimal_transcription_of_injected_cosine_runbook(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Runbook twin of test_validate_accepts_two_decimal_transcription_of_injected_cosine
    (review finding, round 3): the ADR write site (:380) had a reader for
    "the SERVER-injected value is what gets persisted", but the runbook
    write site (:430, now the second `if injected_cosine is not None`
    branch) did not. Every other runbook cosine assertion in this module
    uses reported == injected, so none of them could catch a regression
    that persisted the model's transcription instead of the server's
    number on THIS site specifically. Reading the row back is what makes
    this a witness rather than a claim.
    """
    learning_id, rb_id = await _seed_learning_and_runbook(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(runbook=_dedup_family_block("clear", nearest_raw_cosine=0.645)),
        }
    ]
    report = {
        "target_type": "runbook",
        "candidate_id": str(learning_id),
        "target_id": str(rb_id),
        "cosine_observed": 0.65,
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(dream_promotions.c.cosine_observed).where(
                        dream_promotions.c.target_runbook_id == rb_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["cosine_observed"] == pytest.approx(0.645)


@pytest.mark.asyncio
async def test_validate_still_rejects_fabricated_cosine_with_widened_tolerance(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Negative twin of the widened-tolerance regression above: a genuinely
    fabricated value (0.95 vs the server-injected 0.30) must still fail by
    a wide margin -- widening the tolerance to absorb 2-decimal rounding
    must not also swallow real divergence.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("clear", nearest_raw_cosine=0.30)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": 0.95,
    }
    with pytest.raises(ValidationFailure, match="diverges from the server-injected"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_borderline_without_dedup_examined(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """band="borderline" MUST come with a non-empty `dedup_examined` — this
    is what makes a near-duplicate pass-through observable instead of mute.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("borderline", nearest_raw_cosine=0.79)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": 0.79,
        # dedup_examined intentionally omitted
    }
    with pytest.raises(ValidationFailure, match="requires a non-empty dedup_examined"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_accepts_borderline_with_dedup_examined(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Negative twin: the same borderline band WITH `dedup_examined` populated
    passes — proving the check is on presence, not on the band itself.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("borderline", nearest_raw_cosine=0.79)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": 0.79,
        "dedup_examined": [{"id": str(uuid.uuid4()), "raw_cosine": 0.79, "verdict": "distinct"}],
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)


@pytest.mark.asyncio
async def test_validate_does_not_enforce_the_block_band_lot1_is_shadow(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Characterization test (review finding, fix round): SHADOW means
    `promote_validate.validate` never enforces the band -- it only
    cross-checks the reported `cosine_observed` against the server-injected
    number. This name is deliberately unmistakable: a materialization
    landing in band="block" is ACCEPTED today, on purpose, because the
    model -- not the server -- still decides who gets promoted (W25 lot 1,
    W25-promote-nearest-tool-design.md §5). Verified by direct call before
    this test existed: `_check_dedup_against_pool` accepted
    `{"target_type": "adr", "cosine_observed": 0.95}` against `band ==
    "block"` with zero test in this module ever materializing while the
    band said "block" -- every "block" fixture elsewhere in this module is
    a `skipped_dedup` report, never a WET materialization.

    If lot 2 makes the validator enforce the band, THIS test must be
    deleted (not adjusted) as part of that change -- its own name says why
    it existed. Until then, it is the mechanical proof that the authority
    transfer has NOT silently happened.
    """
    learning_id, adr_id = await _seed_learning_and_adr(session_factory, isolated_pk)
    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(adr=_dedup_family_block("block", nearest_raw_cosine=0.95)),
        }
    ]
    report = {
        "target_type": "adr",
        "candidate_id": str(learning_id),
        "target_id": str(adr_id),
        "cosine_observed": 0.95,
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
    assert row["cosine_observed"] == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_validate_skipped_dedup_uses_dedup_family_field_to_locate_the_band(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """`skipped_dedup` no longer carries the classification in `target_type`
    (it was overwritten) — the report's `dedup_family` field is what the
    validator uses to pick which family's block to cross-check against.
    A mismatched `dedup_family` must diverge against the WRONG family's
    number and fail.
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

    candidates = [
        {
            "id": str(learning_id),
            "topic": "t",
            "dedup": _dedup_block(
                adr=_dedup_family_block("block", nearest_raw_cosine=0.9),
                runbook=_dedup_family_block("clear", nearest_raw_cosine=0.3),
            ),
        }
    ]
    report = {
        "target_type": "skipped_dedup",
        "candidate_id": str(learning_id),
        "dedup_family": "runbook",  # wrong family for a cosine of 0.9
        "cosine_observed": 0.9,
        "reason": "near-duplicate",
    }
    with pytest.raises(ValidationFailure, match="diverges from the server-injected"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


# ────────── dedup_family mandatory + fabrication on never-reached-step-3 ───
# (major finding, fix round: `_report_dedup_family` used to return None
# — a silent no-op — whenever a `skipped_dedup` report omitted
# `dedup_family` or set it to anything other than "adr"/"runbook", and the
# same None was returned for every target_type that never reaches Step 3
# at all. Either way the fabricated `cosine_observed` still got INSERTed.
# One test per bypass route named in the finding.


@pytest.mark.asyncio
async def test_validate_rejects_skipped_dedup_missing_dedup_family(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """`dedup_family` is mandatory for `skipped_dedup` — the prompt already
    declares it required. Omitting it must not silently bypass the
    fabrication cross-check.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "skipped_dedup",
        "candidate_id": str(learning_id),
        "cosine_observed": 0.99,  # fabricated — no dedup_family to cross-check against
    }
    with pytest.raises(ValidationFailure, match="dedup_family"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )

    async with session_factory() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(dream_promotions)
                .where(dream_promotions.c.source_learning_id == learning_id)
            )
        ).scalar_one()
    assert count == 0, "fabricated report must not be persisted"


@pytest.mark.asyncio
async def test_validate_rejects_skipped_dedup_invalid_dedup_family(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """`dedup_family` set to anything other than "adr"/"runbook" is the same
    bypass as omitting it entirely.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "skipped_dedup",
        "candidate_id": str(learning_id),
        "dedup_family": "learning",  # not "adr" or "runbook"
        "cosine_observed": 0.99,
    }
    with pytest.raises(ValidationFailure, match="dedup_family"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_cosine_on_dedup_unavailable(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """`dedup_unavailable` means the source learning has no embedding — Step
    3 never ran, so there is no server number to have copied. A reported
    `cosine_observed` here can only be fabricated.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]  # no "dedup" key at all
    report = {
        "target_type": "dedup_unavailable",
        "candidate_id": str(learning_id),
        "cosine_observed": 0.99,
        "reason": "embedding missing on source learning",
    }
    with pytest.raises(ValidationFailure, match="fabrication"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_cosine_on_dry_run_target_type(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """`target_type="dry_run"` (the literal value, distinct from the `dry_run`
    boolean flag carried by an "adr"/"runbook" report) never reaches Step 3
    either — same fabrication hole.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "dry_run",
        "candidate_id": str(learning_id),
        "cosine_observed": 0.99,
    }
    with pytest.raises(ValidationFailure, match="fabrication"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )


@pytest.mark.asyncio
async def test_validate_rejects_cosine_on_target_type_none(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """`target_type="none"` returns immediately in `validate()` — the
    fabrication guard must apply BEFORE that early return, not only inside
    `_check_dedup_against_pool`, which this path never reaches.
    """
    report = {"target_type": "none", "cosine_observed": 0.99}
    with pytest.raises(ValidationFailure, match="fabrication"):
        await validate(report, [], session_factory, dream_run_id=None, project_key=isolated_pk)


@pytest.mark.asyncio
async def test_validate_rejects_cosine_on_none_that_names_a_candidate(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Rebase-seam regression (review finding, round 3): a 'none' report
    that NAMES a candidate falls through past the early no-candidate guard
    (test_validate_rejects_cosine_on_target_type_none, above) into the
    skip-path branch, where `_check_dedup_against_pool` runs BEFORE the
    `target_type == "none"` split. Moving that call into the `else` branch
    -- a natural-looking simplification, since the `none` branch hard-codes
    `cosine = None` anyway -- would let this exact shape through silently:
    the run stays `ok` and a fabricated cosine_observed is audited under
    _NONE_REFUSAL_TARGET_TYPE with no fabrication check ever having run.
    This pins the call at its current, unconditional position.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]
    report = {
        "target_type": "none",
        "candidate_id": str(learning_id),
        "cosine_observed": 0.99,
    }
    with pytest.raises(ValidationFailure, match="fabrication"):
        await validate(
            report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk
        )

    async with session_factory() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(dream_promotions)
                .where(dream_promotions.c.source_learning_id == learning_id)
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_validate_classification_uncertain_skips_dedup_check_entirely(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """`classification_uncertain` never reaches Step 3 — no `dedup` key is
    required on candidates[0], and no cosine cross-check applies.
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

    candidates = [{"id": str(learning_id), "topic": "t"}]  # no "dedup" key
    report = {
        "target_type": "classification_uncertain",
        "candidate_id": str(learning_id),
        "reason": "no clear alternatives or steps",
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)


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
    candidates = [{"id": str(learning_id), "topic": "t", "dedup": _dedup_block()}]
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


@pytest.mark.asyncio
async def test_amain_none_refusal_with_candidate_is_audited_and_run_stays_ok(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """End-to-end regression for the 2026-09-06/07 vanished refusals: a
    well-formed report naming a candidate but classifying target_type='none'
    must (a) exit 0 — this is a graceful report, not a contract violation —
    (b) leave dream_runs.status untouched (a refusal is not a validation
    failure), and (c) still land a dream_promotions row for the candidate.
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
    raw = (
        "=== PROMOTE REPORT ===\n"
        "{"
        f'"target_type": "none", "candidate_id": "{learning_id}", '
        '"candidate_topic": "t", "draft_title": "Some draft", '
        '"reason": "promote tool unavailable"'
        "}\n"
        "=== END ==="
    )

    args = argparse.Namespace(dream_run_id=run_id, project_key=isolated_pk)
    exit_code = await _amain(raw, candidates, session_factory, args)

    assert exit_code == 0
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
        run_status = (
            await session.execute(sa.select(dream_runs.c.status).where(dream_runs.c.id == run_id))
        ).scalar_one()
    assert row["dream_run_id"] == run_id
    assert row["target_type"] == "dedup_unavailable"
    assert "promote tool unavailable" in row["skipped_reason"]
    assert run_status == "ok"


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
    candidates = [{"id": str(learning_id), "topic": "t", "dedup": _dedup_block()}]
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
    candidates = [{"id": str(learning_id), "topic": "t", "dedup": _dedup_block()}]
    report = {
        "target_type": "runbook",
        "candidate_id": str(learning_id),
        "target_id": str(rb_id),
    }
    await validate(report, candidates, session_factory, dream_run_id=None, project_key=isolated_pk)
