"""Unit tests for the PROMOTE dedup shadow verdict (W25 lot 1).

Covers `classify_band` (the pure three-way decision, exact bounds pinned in
Python — a pgvector round trip stores embeddings as float4, so exact-boundary
assertions belong at the function level, not chased through the database)
and `compute_dedup`/`attach_dedup_verdict` (the SQL wiring, exercised against
a real Postgres with engineered embeddings that carry a KNOWN raw cosine).

Engineered embeddings: `_base_embedding()` is the unit vector `[1, 0, 0, ...]`
and `_embedding_at_cosine(x)` is `[x, sqrt(1-x**2), 0, ...]` — both unit
length, so `1 - (a <=> b) == x` up to float4 rounding. This buys an exact,
reproducible raw cosine without depending on the embedding service.
"""

from __future__ import annotations

import math
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream import promote_prepare
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.config import Settings
from brain_v42.db.tables import _EMBEDDING_DIM, adrs, learnings, runbooks
from tests.conftest import require_test_db_url
from tests.unit.keys import make_unit_project_key


def _base_embedding() -> list[float]:
    return [1.0] + [0.0] * (_EMBEDDING_DIM - 1)


def _embedding_at_cosine(cosine: float) -> list[float]:
    """Unit vector whose dot product with `_base_embedding()` is `cosine`."""
    return [cosine, math.sqrt(1.0 - cosine**2)] + [0.0] * (_EMBEDDING_DIM - 2)


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
    return make_unit_project_key("dedup")


@pytest.fixture
def settings() -> Settings:
    return Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")


class TestClassifyBandExactBounds:
    """Pure function, no DB. Bounds per family (W25 §1.3, ADR: 0.760/0.820)."""

    def test_below_low_bound_is_clear(self) -> None:
        assert promote_prepare.classify_band(0.759, clear_below=0.760, block_above=0.820) == "clear"

    def test_exactly_at_low_bound_is_borderline(self) -> None:
        """The low bound is itself an OBSERVED duplicate-proxy value (ADR
        min 0.760) — it belongs to the "we don't know" zone, not "clear".
        """
        assert (
            promote_prepare.classify_band(0.760, clear_below=0.760, block_above=0.820)
            == "borderline"
        )

    def test_just_inside_the_band_is_borderline(self) -> None:
        assert (
            promote_prepare.classify_band(0.790, clear_below=0.760, block_above=0.820)
            == "borderline"
        )

    def test_exactly_at_high_bound_is_borderline(self) -> None:
        """The high bound is itself an OBSERVED non-duplicate value (ADR max
        0.820) — same reasoning as the low bound, the other direction.
        """
        assert (
            promote_prepare.classify_band(0.820, clear_below=0.760, block_above=0.820)
            == "borderline"
        )

    def test_above_high_bound_is_block(self) -> None:
        assert promote_prepare.classify_band(0.821, clear_below=0.760, block_above=0.820) == "block"

    def test_no_candidate_in_family_is_clear(self) -> None:
        """`None` means nothing exists to compare against — nothing to be a
        duplicate of, distinct from `"unavailable"` (source has no embedding
        at all, see TestSourceWithoutEmbedding below).
        """
        assert promote_prepare.classify_band(None, clear_below=0.760, block_above=0.820) == "clear"

    def test_adr_and_runbook_thresholds_are_independent(self) -> None:
        """The SAME raw cosine can land in different bands per family —
        this is the entire reason thresholds are per-family in Settings.
        """
        raw_cosine = 0.715
        adr_band = promote_prepare.classify_band(raw_cosine, clear_below=0.760, block_above=0.820)
        runbook_band = promote_prepare.classify_band(
            raw_cosine, clear_below=0.712, block_above=0.818
        )
        assert adr_band == "clear"
        assert runbook_band == "borderline"


async def _seed_learning(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    *,
    embedding: list[float] | None,
) -> uuid.UUID:
    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="dedup-candidate",
                        insight="i",
                        project_key=project_key,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                        embedding=embedding,
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()


async def _seed_adr(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    *,
    number: int,
    title: str,
    embedding: list[float] | None,
) -> uuid.UUID:
    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    adrs.insert()
                    .values(
                        number=number,
                        title=title,
                        context="c",
                        decision="d",
                        consequences="e",
                        project_key=project_key,
                        embedding=embedding,
                    )
                    .returning(adrs.c.id)
                )
            ).scalar_one()


async def _seed_runbook(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    *,
    title: str,
    embedding: list[float] | None,
) -> uuid.UUID:
    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    runbooks.insert()
                    .values(
                        title=title,
                        description="d",
                        project_key=project_key,
                        trigger="t",
                        embedding=embedding,
                    )
                    .returning(runbooks.c.id)
                )
            ).scalar_one()


@pytest.mark.asyncio
async def test_dedup_key_absent_on_empty_pool(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """Empty pool stays `[]` — the dream.sh:949 `jq 'length'` contract."""
    result = await promote_prepare.attach_dedup_verdict(session_factory, [], isolated_pk, settings)
    assert result == []


@pytest.mark.asyncio
async def test_dedup_only_attached_to_candidates_zero(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """Cap = 1 (spec): only the evaluated slot pays the SQL cost."""
    candidates = [
        {"id": str(uuid.uuid4()), "topic": "a"},
        {"id": str(uuid.uuid4()), "topic": "b"},
    ]
    # candidates[0]["id"] must resolve to a real learning for the query.
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    candidates[0]["id"] = str(learning_id)

    result = await promote_prepare.attach_dedup_verdict(
        session_factory, candidates, isolated_pk, settings
    )

    assert "dedup" in result[0]
    assert "dedup" not in result[1]


@pytest.mark.asyncio
async def test_top_level_stays_a_list_after_attach(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    candidates = [{"id": str(learning_id), "topic": "a"}]

    result = await promote_prepare.attach_dedup_verdict(
        session_factory, candidates, isolated_pk, settings
    )

    assert isinstance(result, list)
    assert len(result) == 1


class TestSourceWithoutEmbedding:
    @pytest.mark.asyncio
    async def test_source_without_embedding_is_unavailable_not_absent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        isolated_pk: str,
        settings: Settings,
    ) -> None:
        """A learning admitted to the pool without an embedding (15/3552
        measured live) must still carry a `dedup` block — `verdict:
        "unavailable"`, never a missing key the validator would call a bug.
        """
        learning_id = await _seed_learning(session_factory, isolated_pk, embedding=None)

        async with session_factory() as session:
            dedup = await promote_prepare.compute_dedup(
                session,
                str(learning_id),
                isolated_pk,
                settings,
            )
        assert dedup["adr"]["band"] == "unavailable"
        assert dedup["runbook"]["band"] == "unavailable"
        assert dedup["adr"]["nearest_id"] is None
        assert dedup["adr"]["top3"] == []


@pytest.mark.asyncio
async def test_dedup_computes_realistic_block_band_via_real_pgvector(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """End-to-end smoke test: a near-identical ADR pushes the ADR family
    into "block" via ACTUAL pgvector arithmetic (not the pure function).
    """
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    await _seed_adr(
        session_factory,
        isolated_pk,
        number=1,
        title="Near-duplicate ADR",
        embedding=_embedding_at_cosine(0.95),
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    assert dedup["score_kind"] == "raw_cosine_pgvector"
    assert dedup["adr"]["band"] == "block"
    assert dedup["adr"]["nearest_title"] == "Near-duplicate ADR"
    assert dedup["adr"]["nearest_raw_cosine"] == pytest.approx(0.95, abs=1e-4)
    # No runbooks seeded at all for this project -> nothing to compare -> clear.
    assert dedup["runbook"]["band"] == "clear"
    assert dedup["runbook"]["nearest_id"] is None


@pytest.mark.asyncio
async def test_dedup_computes_realistic_clear_band(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    await _seed_adr(
        session_factory,
        isolated_pk,
        number=1,
        title="Unrelated ADR",
        embedding=_embedding_at_cosine(0.4),
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    assert dedup["adr"]["band"] == "clear"
    assert dedup["adr"]["nearest_raw_cosine"] == pytest.approx(0.4, abs=1e-4)


@pytest.mark.asyncio
async def test_family_thresholds_are_wired_to_the_matching_settings_field(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """Per-family threshold wiring (review finding, fix round): the design's
    central requirement is that runbook bounds are NEVER the ADR bounds.
    `TestClassifyBandExactBounds` above proves `classify_band` honours
    whatever bounds it is handed; the `settings`-parametrized tests above
    only exercise cosines (0.95, 0.4) that land the SAME way under EITHER
    family's bounds, so a `compute_dedup` that swapped the runbook call's
    `clear_below`/`block_above` for the ADR ones would still pass every one
    of them. Mutation verified: that exact swap left the entire unit suite
    green.

    Raw cosine ~0.73 sits in the interval that only the RUNBOOK bounds
    (0.712 clear_below / 0.818 block_above) call "borderline" — the ADR
    bounds (0.760 / 0.820) call the same value "clear". A SINGLE learning
    compared against an ADR and a runbook at the SAME raw cosine, in the
    SAME `compute_dedup` call, is what a swapped wire cannot survive: it
    would report "clear" (or "borderline") on BOTH families instead of one
    of each.
    """
    raw_cosine = 0.730
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    await _seed_adr(
        session_factory,
        isolated_pk,
        number=1,
        title="Borderline-for-runbook-only ADR",
        embedding=_embedding_at_cosine(raw_cosine),
    )
    await _seed_runbook(
        session_factory,
        isolated_pk,
        title="Borderline-for-runbook-only runbook",
        embedding=_embedding_at_cosine(raw_cosine),
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    assert dedup["adr"]["band"] == "clear"
    assert dedup["runbook"]["band"] == "borderline"


@pytest.mark.asyncio
async def test_adr_and_runbook_rows_without_embedding_are_excluded_and_counted(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """An ADR/runbook row with a NULL embedding must never be ranked as the
    nearest match (garbage-in) AND its exclusion must be visible in the
    payload — otherwise a silent gap looks identical to "genuinely clear".
    """
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    await _seed_adr(
        session_factory, isolated_pk, number=1, title="No embedding yet", embedding=None
    )
    await _seed_runbook(session_factory, isolated_pk, title="No embedding yet", embedding=None)

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    assert dedup["adr"]["top3"] == []
    assert dedup["adr"]["null_embedding_excluded"] == 1
    assert dedup["runbook"]["top3"] == []
    assert dedup["runbook"]["null_embedding_excluded"] == 1


@pytest.mark.asyncio
async def test_dedup_is_scoped_to_the_project(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """An ADR in a DIFFERENT project must never be treated as a duplicate."""
    other_pk = make_unit_project_key("dedup-other")
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    await _seed_adr(
        session_factory,
        other_pk,
        number=1,
        title="Other project's near-identical ADR",
        embedding=_embedding_at_cosine(0.99),
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    assert dedup["adr"]["band"] == "clear"
    assert dedup["adr"]["nearest_id"] is None


# ─── injected-cosine rounding (blocker, fix round W25 lot 1) ───────────────
#
# asyncpg returns `1 - (a <=> b)` as a Python double: measured live,
# 0.6450062431625778 (16 significant digits) even though the underlying
# columns are stored float4. promote_validate's cross-check asks the model
# to copy this number VERBATIM, but every one of the 15 historical
# cosine_observed values the model has ever reported carries 2 decimals.
# Rounding the number we hand the model to a transcribable precision (here:
# 4 decimals) is the fix -- and classify_band must still run on the
# UNROUNDED value, since 3-decimal-precision band bounds are themselves
# observed values and rounding the input to classify_band could flip a
# result that sits right on a bound.


@pytest.mark.asyncio
async def test_nearest_raw_cosine_is_rounded_to_a_transcribable_precision(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    await _seed_adr(
        session_factory,
        isolated_pk,
        number=1,
        title="Near-duplicate ADR",
        embedding=_embedding_at_cosine(0.65),
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    nearest = dedup["adr"]["nearest_raw_cosine"]
    assert nearest == pytest.approx(0.65, abs=1e-4)
    assert nearest == round(nearest, 4), (
        f"nearest_raw_cosine={nearest!r} is not rounded to <=4 decimals -- "
        "the model cannot transcribe this verbatim"
    )
    for entry in dedup["adr"]["top3"]:
        assert entry["raw_cosine"] == round(entry["raw_cosine"], 4)


@pytest.mark.asyncio
async def test_band_is_classified_on_the_unrounded_cosine_not_the_rounded_one(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """Regression: an ADR engineered so the TRUE raw cosine (0.82003...) is
    strictly above `promote_dedup_block_adr` (0.820, "block"), but whose
    value ROUNDED to 4 decimals lands EXACTLY on the bound (0.8200) --
    which `classify_band` treats as "borderline" (bounds are closed, per
    TestClassifyBandExactBounds). If band were computed from the rounded
    display value instead of the true one, this ADR would wrongly demote
    from "block" to "borderline", silently weakening the dedup gate.
    """
    learning_id = await _seed_learning(session_factory, isolated_pk, embedding=_base_embedding())
    await _seed_adr(
        session_factory,
        isolated_pk,
        number=1,
        title="Just-over-the-line ADR",
        embedding=_embedding_at_cosine(0.82003),
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    assert round(dedup["adr"]["nearest_raw_cosine"], 4) == pytest.approx(0.82)
    assert dedup["adr"]["band"] == "block"
