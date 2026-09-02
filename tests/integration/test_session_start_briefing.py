"""End-to-end golden snapshot for brain_session_start (SQLite-mirror harness)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.mcp.tools.session_tools import _format_session_briefing
from brain_v42.services.dream_run_service import DreamRunService
from brain_v42.services.feature_service import FeatureService

FIXTURES = Path(__file__).parent.parent / "fixtures"


# Local SQLite-friendly METADATA. Mirrors prod columns for the 5 tables we need,
# stripped of pgvector / JSONB / vector-index types. Drift risk acknowledged.
_TEST_METADATA = MetaData()


_project_contexts = Table(
    "project_contexts",
    _TEST_METADATA,
    Column("id", String(36), primary_key=True),
    Column("project_key", String(50), unique=True, nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("current_focus", String, nullable=True),
    Column("blockers", JSON, nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

_dream_runs = Table(
    "dream_runs",
    _TEST_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_date", Date, nullable=False),
    Column("phase", String(10), nullable=False),
    Column("model", String(120)),
    Column("status", String(10), nullable=False),
    Column("duration_s", Float),
    Column("error_message", Text, nullable=True),
    Column("phase_dry_run", Boolean, nullable=False, server_default=sa.text("0")),
    Column("created_at", DateTime, server_default=sa.func.now()),
)

_features = Table(
    "features",
    _TEST_METADATA,
    Column("id", String(36), primary_key=True),
    Column("project_key", String(50), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("status", String(20), nullable=False, server_default=sa.text("'planned'")),
    Column("status_updated_at", DateTime, nullable=False),
    Column("pinned", Boolean, server_default=sa.text("0")),
    Column("merged_into", String(36), nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

# SQLite mirror of feature_artifacts — needed by roadmap_alive SQL (LEFT JOIN).
# No artifacts are seeded so all features render with artifact_count=0.
_feature_artifacts = Table(
    "feature_artifacts",
    _TEST_METADATA,
    Column("artifact_id", String(36), primary_key=True),
    Column("feature_id", String(36), nullable=False),
    Column("created_at", DateTime, nullable=False),
)

_decisions = Table(
    "decisions",
    _TEST_METADATA,
    Column("id", String(36), primary_key=True),
    Column("project_key", String(50), nullable=False),
    Column("title", String(300), nullable=False),
    Column("rationale", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

_learnings = Table(
    "learnings",
    _TEST_METADATA,
    Column("id", String(36), primary_key=True),
    Column("project_key", String(50), nullable=False),
    Column("topic", String(200), nullable=False),
    Column("insight", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(_TEST_METADATA.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _normalize(s: str) -> str:
    """Strip relative dates and last_run_date to make snapshots stable."""
    s = re.sub(r"\d+d ago", "Nd ago", s)
    s = re.sub(r"as of \d{4}-\d{2}-\d{2}", "as of YYYY-MM-DD", s)
    s = re.sub(r"on \d{4}-\d{2}-\d{2}", "on YYYY-MM-DD", s)
    return s


def _with_aware_updated_at(features: list) -> list:
    """Return Feature objects with timezone-aware updated_at.

    SQLite strips tzinfo from DateTime columns, producing naive datetimes.
    _ds_relative() in session_tools compares updated_at against
    datetime.now(tz=UTC) and raises TypeError for naive datetimes. We
    re-attach UTC on the way out so the e2e pipeline behaves identically
    to production (PostgreSQL always returns tz-aware datetimes).
    """
    result = []
    for f in features:
        if f.updated_at is not None and f.updated_at.tzinfo is None:
            # Re-attach UTC to naive datetime from SQLite
            f_aware = f.model_copy(update={"updated_at": f.updated_at.replace(tzinfo=UTC)})
            result.append(f_aware)
        else:
            result.append(f)
    return result


async def _seed_full(session_factory):
    """Shared full-seed helper. Seeds 5+5 to exercise §3.2 recap cap."""
    now = datetime.now(tz=UTC)
    today = date.today()
    async with session_factory() as session:
        await session.execute(
            sa.insert(_project_contexts).values(
                id=str(uuid4()),
                project_key="brain-v42",
                name="brain-v42",
                description="Second Cerveau",
                current_focus="ship the briefing",
                blockers=["calibration pending", "vmem drift"],
                created_at=now,
                updated_at=now,
            )
        )
        await session.execute(
            sa.insert(_dream_runs).values(
                run_date=today,
                phase="promote",
                model="sonnet",
                status="done",
                duration_s=10.0,
                phase_dry_run=False,
                created_at=now,
            )
        )
        await session.execute(
            sa.insert(_dream_runs).values(
                run_date=today,
                phase="reorg",
                model="sonnet",
                status="done",
                duration_s=10.0,
                phase_dry_run=True,
                created_at=now,
            )
        )
        await session.execute(
            sa.insert(_features).values(
                id=str(uuid4()),
                project_key="brain-v42",
                name="enrich session start",
                description="",
                status="in_progress",
                status_updated_at=now,
                pinned=False,
                created_at=now,
                updated_at=now,
            )
        )
        await session.execute(
            sa.insert(_features).values(
                id=str(uuid4()),
                project_key="brain-v42",
                name="dream v3 spec b",
                description="",
                status="planned",
                status_updated_at=now,
                pinned=True,
                created_at=now - timedelta(days=60),
                updated_at=now - timedelta(days=60),
            )
        )
        for i in range(5):
            await session.execute(
                sa.insert(_decisions).values(
                    id=str(uuid4()),
                    project_key="brain-v42",
                    title=f"decision {i}",
                    rationale=f"because {i}",
                    created_at=now - timedelta(hours=i),
                    updated_at=now - timedelta(hours=i),
                )
            )
            await session.execute(
                sa.insert(_learnings).values(
                    id=str(uuid4()),
                    project_key="brain-v42",
                    topic=f"topic {i}",
                    insight=f"insight {i} payload",
                    created_at=now - timedelta(hours=i),
                    updated_at=now - timedelta(hours=i),
                )
            )
        await session.commit()


async def _compose_full(session_factory, graph_enabled: bool = False):
    """Run the full briefing pipeline against the seeded SQLite DB."""
    from types import SimpleNamespace

    dream_svc = DreamRunService(session_factory, table=_dream_runs)
    feature_svc = FeatureService(session_factory, table=_features)
    async with session_factory() as session:
        ctx_row = (
            (
                await session.execute(
                    sa.select(_project_contexts).where(
                        _project_contexts.c.project_key == "brain-v42"
                    )
                )
            )
            .mappings()
            .one()
        )
        decision_rows = (
            (
                await session.execute(
                    sa.select(_decisions)
                    .where(_decisions.c.project_key == "brain-v42")
                    .order_by(_decisions.c.created_at.desc())
                    .limit(5)
                )
            )
            .mappings()
            .all()
        )
        learning_rows = (
            (
                await session.execute(
                    sa.select(_learnings)
                    .where(_learnings.c.project_key == "brain-v42")
                    .order_by(_learnings.c.created_at.desc())
                    .limit(5)
                )
            )
            .mappings()
            .all()
        )

    ctx_data = dict(ctx_row)
    ctx_data["blockers"] = ctx_data.get("blockers") or []
    ctx = SimpleNamespace(**ctx_data)
    decisions_list = [SimpleNamespace(**dict(r)) for r in decision_rows]
    learnings_list = [SimpleNamespace(**dict(r)) for r in learning_rows]
    # Non-existent path → fallback on dream_runs: a deterministic golden (host-free).
    killswitches = await dream_svc.killswitch_state(
        killswitches_path=Path("/nonexistent/killswitches.conf")
    )
    last_failure = await dream_svc.last_failure()
    # roadmap_alive runs on the SQLite mirror too (NULLS LAST supported since
    # SQLite 3.30): the seeded features return with artifact_count=0
    # (feature_artifacts table is empty in this harness). The golden snapshot
    # reflects the 2 features + their artifact counts.
    roadmap_items = await feature_svc.roadmap_alive(project_key="brain-v42")
    stale_pinned = _with_aware_updated_at(await feature_svc.stale_pinned(project_key="brain-v42"))
    return _format_session_briefing(
        ctx,
        decisions_list,
        learnings_list,
        killswitches,
        last_failure,
        roadmap_items,
        stale_pinned,
        graph_enabled=graph_enabled,
    )


@pytest.mark.asyncio
async def test_full_seed_matches_golden(session_factory):
    """Briefing with every section populated matches briefing_full.md."""
    # graph_enabled pinned True so the golden snapshot is deterministic and
    # independent of ambient config (the GRAPH row is threaded, not env-read).
    await _seed_full(session_factory)
    out = await _compose_full(session_factory, graph_enabled=True)
    golden_path = FIXTURES / "briefing_full.md"
    if not golden_path.exists():
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(_normalize(out))
        pytest.skip("Golden fixture written — re-run to assert")
    golden = golden_path.read_text()
    assert _normalize(out).strip() == _normalize(golden).strip()


@pytest.mark.asyncio
async def test_empty_seed_matches_golden(session_factory):
    """Empty DB produces the no-context degraded briefing."""
    dream_svc = DreamRunService(session_factory, table=_dream_runs)
    feature_svc = FeatureService(session_factory, table=_features)
    killswitches = await dream_svc.killswitch_state(
        killswitches_path=Path("/nonexistent/killswitches.conf")
    )
    last_failure = await dream_svc.last_failure()
    roadmap_items = await feature_svc.roadmap_alive(project_key="missing")
    stale_pinned = _with_aware_updated_at(await feature_svc.stale_pinned(project_key="missing"))

    out = _format_session_briefing(
        None,
        [],
        [],
        killswitches,
        last_failure,
        roadmap_items,
        stale_pinned,
    )
    golden_path = FIXTURES / "briefing_empty.md"
    if not golden_path.exists():
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(_normalize(out))
        pytest.skip("Golden fixture written — re-run to assert")
    golden = golden_path.read_text()
    assert _normalize(out).strip() == _normalize(golden).strip()


@pytest.mark.asyncio
async def test_token_budget(session_factory):
    """Full briefing stays under 4000 chars (~800 tokens).

    4000 chars is a coarse proxy for "≲800 tokens" — English text averages
    ~4-5 chars/token for Anthropic tokenisers, so 4000 chars sits under the
    800-token guidance from spec §3. We assert chars (not tokens) to avoid
    pulling a tokenizer into the test path. Runs against the FULL-seed
    briefing so the assertion is meaningful.
    """
    await _seed_full(session_factory)
    out = await _compose_full(session_factory)
    assert len(out) < 4000
