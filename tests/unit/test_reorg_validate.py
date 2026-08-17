"""Unit tests for scripts.dream.reorg_validate.

Covers:
  1. parse_report: extracts update/archive IDs from the machine-readable
     === REORG REPORT === JSON trailer (the only supported format since the
     first agent revision; prose-section extraction was never implemented).
  2. parse_report: fail-closed behaviour on missing trailer.
  3. parse_report: malformed JSON inside the marker raises ValidationFailure.
  4. validate() dry-run: skips DB checks, logs a skip message.
  5. validate() missing-marker + wet-run: raises ValidationFailure (fail-closed).
  6. validate() missing-marker + dry-run flag: skips gracefully (no raise).
  7. validate() wet-run success: entities claimed as updated/archived have the
     expected freshness_status / updated_at in PG (happy path with mocked DB).
  8. validate() wet-run failure: entity NOT updated in PG → ValidationFailure.
  9. _mark_dream_run_partial: flips dream_runs row to status='partial'.
  10. main() smoke: parse_report + validate succeed without DB (dry-run).
  11. Real-log fixture tests: the three historical reorg logs (2026-06-26,
      2026-06-29, 2026-06-30) carry NO trailer → fail-closed in wet, skip in dry.

DB-backed tests are skipped unless BRAIN_V42_TEST_DB_URL is set (same guard
as test_promote_validate.py).
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream.reorg_validate import (
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

from tests.conftest import require_test_db_url
from tests.unit.keys import make_unit_project_key

# ────────── helpers ───────────────────────────────────────────────────────────

_LOGS_DIR = pathlib.Path(__file__).parent.parent.parent / "logs" / "dream"


def _make_trailer(
    *,
    dry_run: bool = False,
    updated: list[str] | None = None,
    archived: list[str] | None = None,
) -> str:
    """Return a minimal log fragment containing the REORG REPORT trailer."""
    u = updated or []
    a = archived or []
    updated_str = ", ".join(f'"{x}"' for x in u)
    archived_str = ", ".join(f'"{x}"' for x in a)
    return (
        "Prose report here.\n\n"
        "=== REORG REPORT ===\n"
        f'{{"dry_run": {str(dry_run).lower()}, "updated": [{updated_str}], "archived": [{archived_str}]}}\n'
        "=== END ===\n"
    )


def test_main_logs_positive_wet_validation_evidence(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful wet validator run must leave explicit operator evidence."""
    from scripts.dream import reorg_validate

    report_log = tmp_path / "reorg.log"
    report_log.write_text(_make_trailer())
    monkeypatch.setattr(
        reorg_validate,
        "Settings",
        lambda: MagicMock(postgres_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(reorg_validate, "_build_factory", lambda _url: MagicMock())

    assert reorg_validate.main(["--report-log", str(report_log)]) == 0
    assert "REORG VALIDATE: OK" in capsys.readouterr().err


# ────────── parse_report: trailer-based extraction ───────────────────────────


def test_parse_report_extracts_updated_ids() -> None:
    """Full UUIDs in the 'updated' field of the JSON trailer must be extracted."""
    eid = str(uuid.uuid4())
    raw = _make_trailer(updated=[eid])
    result = parse_report(raw)
    assert eid in result["updated_ids"]
    assert result["archived_ids"] == []
    assert result["found_marker"] is True


def test_parse_report_extracts_archived_ids() -> None:
    eid = str(uuid.uuid4())
    raw = _make_trailer(archived=[eid])
    result = parse_report(raw)
    assert eid in result["archived_ids"]
    assert result["updated_ids"] == []
    assert result["found_marker"] is True


def test_parse_report_extracts_both_sections() -> None:
    uid = str(uuid.uuid4())
    aid = str(uuid.uuid4())
    raw = _make_trailer(updated=[uid], archived=[aid])
    result = parse_report(raw)
    assert uid in result["updated_ids"]
    assert aid in result["archived_ids"]
    assert result["found_marker"] is True


def test_parse_report_dry_run_flag_detected() -> None:
    raw = _make_trailer(dry_run=True, updated=[str(uuid.uuid4())])
    result = parse_report(raw)
    assert result["dry_run"] is True
    assert result["found_marker"] is True


def test_parse_report_no_ids_returns_empty() -> None:
    raw = _make_trailer()
    result = parse_report(raw)
    assert result["updated_ids"] == []
    assert result["archived_ids"] == []
    assert result["dry_run"] is False
    assert result["found_marker"] is True


def test_parse_report_deduplicates_ids() -> None:
    """Duplicate UUIDs in the JSON array should be deduplicated."""
    eid = str(uuid.uuid4())
    # Build a trailer with the same UUID twice in updated.
    raw = (
        "=== REORG REPORT ===\n"
        f'{{"dry_run": false, "updated": ["{eid}", "{eid}"], "archived": []}}\n'
        "=== END ===\n"
    )
    result = parse_report(raw)
    # Same ID must appear only once.
    assert result["updated_ids"].count(eid) == 1


def test_parse_report_missing_marker_returns_found_marker_false() -> None:
    """When the REORG REPORT block is absent, found_marker must be False."""
    raw = "## Metadata normalization\n\nSome prose.\n\n## Pollution archived\n\n(none)\n"
    result = parse_report(raw)
    assert result["found_marker"] is False
    assert result["updated_ids"] == []
    assert result["archived_ids"] == []
    assert result["dry_run"] is False


def test_parse_report_malformed_json_raises() -> None:
    """If the REORG REPORT block is present but contains invalid JSON, raise."""
    raw = "=== REORG REPORT ===\n{not valid json}\n=== END ===\n"
    with pytest.raises(ValidationFailure, match="malformed"):
        parse_report(raw)


# ────────── validate — dry-run skip ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_dry_run_skips_db_check(capsys: pytest.CaptureFixture) -> None:
    """In dry-run mode the validator must not touch the DB and must log a
    clear skip message to stderr."""
    session_factory = MagicMock()  # never called
    report = {
        "dry_run": True,
        "updated_ids": ["00000000-0000-0000-0000-000000000001"],
        "archived_ids": ["00000000-0000-0000-0000-000000000002"],
        "found_marker": True,
    }
    # Must not raise
    await validate(report, session_factory, dream_run_id=None)
    # session_factory must not have been invoked
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_validate_empty_report_is_noop() -> None:
    """Empty report (no IDs, marker present, not dry-run) must succeed without
    hitting DB."""
    session_factory = MagicMock()
    report = {"dry_run": False, "updated_ids": [], "archived_ids": [], "found_marker": True}
    await validate(report, session_factory, dream_run_id=None)
    session_factory.assert_not_called()


# ────────── validate — fail-closed on missing marker ─────────────────────────


@pytest.mark.asyncio
async def test_validate_missing_marker_wet_raises() -> None:
    """Missing REORG REPORT block + wet run → ValidationFailure (fail-closed)."""
    session_factory = MagicMock()
    report = parse_report("Some prose, no trailer.")
    assert report["found_marker"] is False
    with pytest.raises(ValidationFailure, match="missing REORG REPORT"):
        await validate(report, session_factory, dream_run_id=None)
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_validate_missing_marker_dry_run_skips(capsys: pytest.CaptureFixture) -> None:
    """Missing REORG REPORT block + dry-run flag → no raise, just log skip.

    This is the belt+suspenders path: the CLI --dry-run flag overrides the
    JSON trailer detection (which is absent), so the validator skips DB checks
    rather than fail-closing. Dry runs never write, so there's nothing to verify.
    """
    session_factory = MagicMock()
    report = parse_report("Some prose, no trailer.")
    # Simulate what main() does when --dry-run is passed.
    report = {**report, "dry_run": True}
    # Must not raise
    await validate(report, session_factory, dream_run_id=None)
    session_factory.assert_not_called()


# ────────── validate — cap enforcement ───────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_cap_exceeded_updated_raises() -> None:
    """More than 20 updated_ids violates the phase_reorg.md guardrail."""
    session_factory = MagicMock()
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [str(uuid.uuid4()) for _ in range(21)],
        "archived_ids": [],
    }
    with pytest.raises(ValidationFailure, match="exceeds cap"):
        await validate(report, session_factory, dream_run_id=None)


@pytest.mark.asyncio
async def test_validate_cap_exceeded_archived_raises() -> None:
    """More than 20 archived_ids violates the phase_reorg.md guardrail."""
    session_factory = MagicMock()
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [],
        "archived_ids": [str(uuid.uuid4()) for _ in range(21)],
    }
    with pytest.raises(ValidationFailure, match="exceeds cap"):
        await validate(report, session_factory, dream_run_id=None)


# ────────── Real-log fixture tests ───────────────────────────────────────────
# These three historical reorg logs predate the REORG REPORT trailer
# requirement. They must fail-closed in wet mode and skip in dry mode.


@pytest.mark.parametrize(
    "log_filename",
    [
        "2026-06-26_reorg.log",
        "2026-06-29_reorg.log",
        "2026-06-30_reorg.log",
    ],
)
def test_historical_log_no_trailer(log_filename: str) -> None:
    """Historical reorg logs must be parsed as found_marker=False.

    The 2026-06-26/29 runs were dry-run soak runs; 2026-06-30 was a wet run
    with 5 updates + 2 archives. None contain the REORG REPORT trailer because
    the trailer requirement was introduced after these runs. This test verifies
    that the parser correctly reports found_marker=False for all three, which
    triggers fail-closed behaviour in wet mode (the masked failure BLOCKER 2
    this validator was designed to close).
    """
    log_path = _LOGS_DIR / log_filename
    if not log_path.exists():
        pytest.skip(f"log fixture not found: {log_path}")
    raw = log_path.read_text()
    result = parse_report(raw)
    assert result["found_marker"] is False, (
        f"{log_filename} should have found_marker=False (no trailer present)"
    )
    assert result["updated_ids"] == []
    assert result["archived_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "log_filename",
    [
        "2026-06-26_reorg.log",
        "2026-06-29_reorg.log",
        "2026-06-30_reorg.log",
    ],
)
async def test_historical_log_fails_closed_in_wet(log_filename: str) -> None:
    """Wet validation of a log without a trailer → ValidationFailure.

    This is the masked-failure gate: a future REORG run that somehow produces
    output without the trailer must be flagged as partial, not silently accepted.
    """
    log_path = _LOGS_DIR / log_filename
    if not log_path.exists():
        pytest.skip(f"log fixture not found: {log_path}")
    raw = log_path.read_text()
    report = parse_report(raw)
    # Wet: found_marker=False → fail-closed
    with pytest.raises(ValidationFailure, match="missing REORG REPORT"):
        await validate(report, MagicMock(), dream_run_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "log_filename",
    [
        "2026-06-26_reorg.log",
        "2026-06-29_reorg.log",
        "2026-06-30_reorg.log",
    ],
)
async def test_historical_log_skips_in_dry_run(log_filename: str) -> None:
    """Dry-run validation of a log without a trailer → no failure.

    The --dry-run CLI flag overrides the missing-marker check (nothing to
    verify — dry runs never write).
    """
    log_path = _LOGS_DIR / log_filename
    if not log_path.exists():
        pytest.skip(f"log fixture not found: {log_path}")
    raw = log_path.read_text()
    report = {**parse_report(raw), "dry_run": True}
    session_factory = MagicMock()
    # Must not raise
    await validate(report, session_factory, dream_run_id=None)
    session_factory.assert_not_called()


# ────────── validate — wet-run with real DB ───────────────────────────────────


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
    return make_unit_project_key("rv")


async def _seed_learning(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    *,
    freshness_status: str = "fresh",
) -> uuid.UUID:
    from brain_v42.db.tables import learnings

    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic=f"t-{uuid.uuid4().hex[:6]}",
                        insight="i",
                        project_key=project_key,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                        freshness_status=freshness_status,
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()
    return learning_id


async def _seed_decision(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    *,
    freshness_status: str = "fresh",
) -> uuid.UUID:
    from brain_v42.db.tables import decisions

    async with session_factory() as session:
        async with session.begin():
            decision_id = (
                await session.execute(
                    decisions.insert()
                    .values(
                        title=f"D-{uuid.uuid4().hex[:6]}",
                        description="d",
                        reasoning="r",
                        alternatives=[],
                        project_key=project_key,
                        tags=[],
                        freshness_status=freshness_status,
                    )
                    .returning(decisions.c.id)
                )
            ).scalar_one()
    return decision_id


@pytest.mark.asyncio
async def test_validate_wet_archived_entity_passes(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Entity that was actually archived in PG → validator must pass."""
    lid = await _seed_learning(session_factory, isolated_pk, freshness_status="archived")
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [],
        "archived_ids": [str(lid)],
    }
    # Must not raise
    await validate(report, session_factory, dream_run_id=None)


@pytest.mark.asyncio
async def test_validate_wet_updated_entity_passes(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Entity with fresh status (updated but not archived) → passes as an
    updated entity."""
    lid = await _seed_learning(session_factory, isolated_pk, freshness_status="fresh")
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [str(lid)],
        "archived_ids": [],
    }
    await validate(report, session_factory, dream_run_id=None)


@pytest.mark.asyncio
async def test_validate_wet_updated_entity_stale_date_raises(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Entity that exists but was last updated BEFORE run_date → masked failure.

    This is the MAJOR 1 fix: existence-only check cannot detect Part 1 masked
    failures where the agent claims updates but performed none. The updated_at
    recency check closes this gap.
    """
    lid = await _seed_learning(session_factory, isolated_pk, freshness_status="fresh")
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [str(lid)],
        "archived_ids": [],
    }
    # Run date in the far future → entity's updated_at will be before it.
    future_run_date = dt.date(2099, 1, 1)
    with pytest.raises(ValidationFailure, match="before run_date"):
        await validate(report, session_factory, dream_run_id=None, run_date=future_run_date)


@pytest.mark.asyncio
async def test_validate_wet_archived_entity_still_fresh_raises(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Entity the agent CLAIMS to have archived but is still fresh in PG
    → ValidationFailure (masked failure)."""
    lid = await _seed_learning(session_factory, isolated_pk, freshness_status="fresh")
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [],
        "archived_ids": [str(lid)],
    }
    with pytest.raises(ValidationFailure, match="claimed archived but freshness_status"):
        await validate(report, session_factory, dream_run_id=None)


@pytest.mark.asyncio
async def test_validate_wet_hallucinated_entity_raises(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Entity the agent claims to have updated but does not exist in PG
    → ValidationFailure."""
    fake_id = str(uuid.uuid4())
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [fake_id],
        "archived_ids": [],
    }
    with pytest.raises(ValidationFailure, match="not found"):
        await validate(report, session_factory, dream_run_id=None)


@pytest.mark.asyncio
async def test_validate_wet_decision_archived_passes(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Validator must check both learnings AND decisions tables."""
    did = await _seed_decision(session_factory, isolated_pk, freshness_status="archived")
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [],
        "archived_ids": [str(did)],
    }
    await validate(report, session_factory, dream_run_id=None)


@pytest.mark.asyncio
async def test_validate_wet_marks_partial_on_failure(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Failing validation must have marked the dream_runs row partial (tested
    via _mark_dream_run_partial, same as promote_validate)."""
    from brain_v42.db.tables import dream_runs

    async with session_factory() as session:
        async with session.begin():
            run_id = (
                await session.execute(
                    dream_runs.insert()
                    .values(run_date=dt.date.today(), phase="reorg", status="done")
                    .returning(dream_runs.c.id)
                )
            ).scalar_one()

    await _mark_dream_run_partial(session_factory, run_id, "reorg integrity failure")

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
    assert row["error_message"] == "reorg integrity failure"


@pytest.mark.asyncio
async def test_mark_dream_run_partial_with_none_run_id_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _mark_dream_run_partial(session_factory, None, "n/a")


# ────────── Garde de périmètre projet (défense en profondeur) ────────────────


@pytest.mark.asyncio
async def test_validate_rejects_an_entity_belonging_to_another_project(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Une entité hors du projet du run doit faire échouer la validation.

    Le serveur borne déjà REORG au projet, deux fois : le middleware injecte
    `project_key` dans les arguments de `brain_list` et refuse tout project_key
    divergent, et les cinq repos portent `AND project_key = :scope` dans le WHERE
    de l'UPDATE. Cette garde-ci est donc de la DÉFENSE EN PROFONDEUR, et sa
    justification est mesurée : `brain_list` est le SEUL outil CRUD qui n'appelle
    jamais `get_dream_project_scope()` lui-même — sa borne vit entièrement dans le
    middleware — et `brain_dream_capability_enforcement` vaut `False` par défaut
    dans le code. Si l'enforcement retombe (rollback, transport stdio, killswitch),
    REORG repagine le corpus entier en silence et plus rien en aval ne le verrait.
    Le validateur est le dernier endroit qui peut encore le dire.

    C'est aussi la parité avec `promote_validate`, qui refuse depuis toujours un
    ADR ou un runbook créé hors du périmètre du run.
    """
    foreign_pk = make_unit_project_key("rv-foreign")
    lid = await _seed_learning(session_factory, foreign_pk, freshness_status="archived")
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [],
        "archived_ids": [str(lid)],
    }

    with pytest.raises(ValidationFailure, match="project"):
        await validate(report, session_factory, dream_run_id=None, project_key=isolated_pk)


@pytest.mark.asyncio
async def test_validate_accepts_an_entity_of_the_run_project(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Contre-épreuve : la garde ne rejette pas le cas nominal."""
    lid = await _seed_learning(session_factory, isolated_pk, freshness_status="archived")
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [],
        "archived_ids": [str(lid)],
    }

    await validate(report, session_factory, dream_run_id=None, project_key=isolated_pk)


@pytest.mark.asyncio
async def test_validate_without_a_project_key_keeps_the_legacy_behaviour(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Sans périmètre fourni, aucune vérification de projet — pas de faux échec."""
    lid = await _seed_learning(session_factory, isolated_pk, freshness_status="archived")
    report = {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": [],
        "archived_ids": [str(lid)],
    }

    await validate(report, session_factory, dream_run_id=None)


def test_dream_sh_passes_the_project_key_to_the_reorg_validator() -> None:
    """Le drapeau doit être CÂBLÉ, pas seulement disponible.

    promote_validate et connect_validate reçoivent `--project-key "$PROJECT_KEY"`
    depuis dream.sh. Un validateur qui sait vérifier un périmètre qu'on ne lui
    passe jamais est une garde qui n'existe pas.
    """
    repo_root = pathlib.Path(__file__).parent.parent.parent
    dream_sh = (repo_root / "scripts" / "dream.sh").read_text(encoding="utf-8")
    start = dream_sh.index("reorg_validator_flags=()")
    end = dream_sh.index("scripts.dream.reorg_validate", start)
    block = dream_sh[start:end]

    assert "--project-key" in block, (
        "dream.sh ne passe aucun périmètre projet à reorg_validate; la garde "
        f"resterait morte. Bloc lu:\n{block}"
    )
