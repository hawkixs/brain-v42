"""Unit tests for scripts.dream.promote_prepare (T8 of Dream v3 plan).

Covers the fetch_candidates filter (maturity, confidence, provenance, dedup)
plus the CLI's JSON-to-stdout behaviour. The tombstone-reconsideration test
from the plan is parked until migration 017 resolves the CHECK-vs-SET-NULL
conflict (CHECK rejects `target_type='adr' AND target_adr_id IS NULL`, and
PostgreSQL CHECKs are not deferrable).

Migration 041 split provenance from content, and the fixtures here follow that
split rather than papering over it:

  - the maturity gate reads `access_count_human` (>= 3), NOT `access_count`.
    Seeding only `access_count` leaves the human counter at its 041 default of
    0, so the row is rejected before it ever reaches the clause a test names —
    which is how the `filters_*` tests turned into false witnesses.
  - the ORDER BY reads `access_count_human`, but the low-confidence guard
    (`confidence='low' AND access_count < 5`) still reads the TOTAL counter.
    Fixtures therefore drive the two counters to DIFFERENT values, so a failure
    names which gate closed.
  - the terminal-uncertain cache compares against `content_updated_at`, NOT
    `updated_at`. Fixtures set it explicitly; the 041 trigger is BEFORE UPDATE
    only, so an INSERT-time value survives.

Each `filters_*` fixture is built to be rejected by EXACTLY ONE clause, so
deleting that clause from the query — and only that clause — makes the test
fall.

Coverage is verified by MUTATION, not by naming: every one of the eight WHERE
clauses and the ORDER BY has been deleted in isolation and shown to make at
least one named test fall. Three consequences of that audit are worth keeping
in mind before editing a fixture here:

  - a test that asserts a PRESENCE can never witness the REMOVAL of a filter.
    Guards therefore need a negative twin, or they have no witness at all —
    which is how the low-confidence guard went uncovered.
  - asserting the SQL text proves a clause is written, not that it runs. The
    EXCLUDE_FROM_PROMOTE insulation has both a text check and a behavioural one.
  - a fixture must use a row shape production actually has. `content_updated_at`
    is NULL on every live row (041 shipped without a backfill), so the uncertain
    cache is covered twice: once with the column set, once with it NULL.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream import promote_prepare
from scripts.dream._render_prompt import render
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import adrs, dream_promotions, learnings
from tests.conftest import require_test_db_url
from tests.unit.keys import make_unit_project_key

# Resolved from the module under test so a moved prompt breaks the import,
# not a silently-skipped assertion.
_PROMOTE_PROMPT = Path(promote_prepare.__file__).with_name("phase_promote.md")


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
    """Per-test project key so seeded rows don't collide across tests."""
    return make_unit_project_key("t8")


@pytest.mark.asyncio
async def test_fetch_candidates_happy_path(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Mature, human-read, unpromoted learning is returned.

    `access_count_human` sits EXACTLY on the maturity threshold, which pins the
    constant itself: raise the gate to 4 and this test falls. This is the one
    place that boundary can be pinned unambiguously — confidence is high, so
    the low-confidence guard is dormant and cannot compete for the blame.

    The two counters are deliberately unequal (total 5, human 3) so the
    asserted payload value also pins which of the two lands under
    `access_count` — the TOTAL. The human counter's own key is pinned by
    `test_fetch_candidates_exposes_the_human_access_count`.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="candidate",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent"],
                    access_count=5,
                    access_count_human=3,
                    created_at=now - dt.timedelta(days=10),
                )
            )

    candidates = await promote_prepare.fetch_candidates(
        session_factory,
        project_key=isolated_pk,
        limit=10,
    )
    assert len(candidates) == 1
    assert candidates[0]["topic"] == "candidate"
    assert candidates[0]["access_count"] == 5


@pytest.mark.asyncio
async def test_fetch_candidates_filters_too_young(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """A learning younger than 7 days is filtered out.

    Age is the ONLY disqualifier: the row clears maturity (human 4 >= 3), the
    low-confidence guard (confidence high), the ADR #4 provenance gate (not
    dream:generated) and both dedup clauses. Drop the INTERVAL '7 days' clause
    and this row is admitted.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="young",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent"],
                    access_count=5,
                    access_count_human=4,
                    created_at=now - dt.timedelta(days=3),
                )
            )
    assert (
        await promote_prepare.fetch_candidates(
            session_factory,
            project_key=isolated_pk,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_fetch_candidates_filters_low_human_access(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """A learning read by a human fewer than 3 times is filtered out.

    Since migration 041 the maturity gate reads `access_count_human`, not the
    total. The fixture pushes the two counters apart on purpose — total 10,
    human 2 — so the exclusion is attributable to the HUMAN counter alone: a
    total of 10 clears every gate that reads the total, including the
    low-confidence guard's `access_count < 5`.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="unpopular",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent"],
                    access_count=10,
                    access_count_human=2,
                    created_at=now - dt.timedelta(days=20),
                )
            )
    assert (
        await promote_prepare.fetch_candidates(
            session_factory,
            project_key=isolated_pk,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_fetch_candidates_filters_other_project(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """A fully-qualifying learning from ANOTHER project never enters this pool.

    The project scope was previously witnessed only by collateral damage —
    dropping `l.project_key = :pk` made nine tests fall because the shared test
    database leaks rows across project keys, and not one of them named the
    clause. This is the named witness: the seeded row clears every other gate,
    so the empty pool is attributable to the project scope and nothing else.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="foreign-project",
                    insight="i",
                    project_key=f"{isolated_pk}-other",
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent"],
                    access_count=10,
                    access_count_human=6,
                    created_at=now - dt.timedelta(days=10),
                )
            )
    assert (
        await promote_prepare.fetch_candidates(
            session_factory,
            project_key=isolated_pk,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_fetch_candidates_tombstone_after_target_delete_allows_reconsideration(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """If the promoted ADR is hard-deleted, the source re-qualifies.

    Covers the migration-017 tombstone flow: ON DELETE SET NULL on
    target_adr_id leaves a row shaped (target_type='adr', target_adr_id=NULL)
    which the widened CHECK admits, and the candidate SQL treats as "target
    no longer alive".

    The learning must clear the 041 maturity gate (human 6 >= 3), otherwise it
    would be absent for a reason that has nothing to do with the tombstone.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="resurrect",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                        access_count=10,
                        access_count_human=6,
                        created_at=now - dt.timedelta(days=30),
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()
            adr_id = (
                await session.execute(
                    adrs.insert()
                    .values(
                        number=1,
                        title=f"Tombstone-{uuid.uuid4().hex[:8]}",
                        context="c",
                        decision="d",
                        consequences="q",
                        alternatives_considered=[],
                        project_key=isolated_pk,
                        tags=[],
                        status="accepted",
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

    # Hard-delete the ADR — ON DELETE SET NULL cascades to target_adr_id.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(adrs.delete().where(adrs.c.id == adr_id))

    candidates = await promote_prepare.fetch_candidates(
        session_factory,
        project_key=isolated_pk,
        limit=10,
    )
    assert any(c["topic"] == "resurrect" for c in candidates), (
        "learning should re-qualify after its promoted target is deleted"
    )


@pytest.mark.asyncio
async def test_fetch_candidates_filters_already_materialized(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """A learning with a live ADR promotion is excluded.

    The live promotion is the ONLY disqualifier: the row is 30 days old, clears
    maturity (human 6 >= 3), is high-confidence, untagged and carries no
    classification_uncertain verdict. Drop the materialized-promotion NOT
    EXISTS and this row is admitted.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="already",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                        access_count=10,
                        access_count_human=6,
                        created_at=now - dt.timedelta(days=30),
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()
            adr_id = (
                await session.execute(
                    adrs.insert()
                    .values(
                        number=1,
                        title=f"Tombstone-test-{uuid.uuid4().hex[:8]}",
                        context="c",
                        decision="d",
                        consequences="q",
                        alternatives_considered=[],
                        project_key=isolated_pk,
                        tags=[],
                        status="accepted",
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

    assert (
        await promote_prepare.fetch_candidates(
            session_factory,
            project_key=isolated_pk,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_fetch_candidates_filters_uncertain_for_current_version(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """A learning already judged classification_uncertain on its current
    version is excluded — no point re-judging unchanged content every night.

    `updated_at` is deliberately set to NOW while `content_updated_at` stays 30
    days old: this is the exact shape 041 exists to distinguish — a counter
    write today on content untouched for a month. The verdict predates the
    counter write but postdates the content, so the row is excluded only if the
    query compares against `content_updated_at`. Pointing the clause back at
    `updated_at` re-admits the row and this test falls.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="terminally-uncertain",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                        access_count=10,
                        access_count_human=6,
                        created_at=now - dt.timedelta(days=30),
                        content_updated_at=now - dt.timedelta(days=30),
                        updated_at=now,  # counter write today, content untouched
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()
            # Judged uncertain AFTER the last CONTENT change, before the counter write.
            await session.execute(
                dream_promotions.insert().values(
                    source_learning_id=learning_id,
                    target_type="classification_uncertain",
                    skipped_reason="exploration survey, no decision/procedure shape",
                    created_at=now - dt.timedelta(days=1),
                )
            )

    assert (
        await promote_prepare.fetch_candidates(
            session_factory,
            project_key=isolated_pk,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_fetch_candidates_filters_uncertain_when_content_never_stamped(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """The uncertain cache still holds when `content_updated_at` is NULL.

    This is the shape PRODUCTION actually has: 041 added the column with no
    backfill and its trigger is BEFORE UPDATE only, so every learning written
    before the migration — and every one written since without a content edit —
    carries NULL. The twin test above sets the column explicitly and therefore
    exercises a row shape the live database does not yet contain; without this
    test the COALESCE fallback can be deleted outright and nothing notices,
    which would silently re-admit every NULL-stamped learning to be re-judged
    each night. That is the exact loop the clause exists to break.

    Comparing a timestamp against NULL yields NULL, so the NOT EXISTS would
    admit the row rather than exclude it: this test falls the moment the
    fallback goes.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="uncertain-never-stamped",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                        access_count=10,
                        access_count_human=6,
                        created_at=now - dt.timedelta(days=30),
                        # content_updated_at deliberately left unset -> NULL.
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()
            await session.execute(
                dream_promotions.insert().values(
                    source_learning_id=learning_id,
                    target_type="classification_uncertain",
                    skipped_reason="judged the only version there has ever been",
                    created_at=now - dt.timedelta(days=1),
                )
            )

    assert (
        await promote_prepare.fetch_candidates(
            session_factory,
            project_key=isolated_pk,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_fetch_candidates_readmits_uncertain_after_edit(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """A learning whose CONTENT changed after an uncertain verdict re-enters
    the pool — the prior verdict judged a now-stale version.

    `content_updated_at` is what re-opens the door, and it is the only thing
    that can: falling back to `created_at` (30 days old) would leave the
    10-day-old verdict looking current and keep the row out, so this test also
    guards the COALESCE fallback from being mistaken for the real signal.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            learning_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="edited-after-uncertain",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                        access_count=10,
                        access_count_human=6,
                        created_at=now - dt.timedelta(days=30),
                        content_updated_at=now,  # content edited today
                        updated_at=now,
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()
            # Uncertain verdict predates the latest edit → must not exclude.
            await session.execute(
                dream_promotions.insert().values(
                    source_learning_id=learning_id,
                    target_type="classification_uncertain",
                    skipped_reason="judged the pre-edit version",
                    created_at=now - dt.timedelta(days=10),
                )
            )

    candidates = await promote_prepare.fetch_candidates(
        session_factory,
        project_key=isolated_pk,
        limit=10,
    )
    assert [c["id"] for c in candidates] == [str(learning_id)]


@pytest.mark.asyncio
async def test_fetch_candidates_filters_dream_generated_low_unvalidated(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """ADR #4 gate — dream:generated + low confidence + no validated_at → excluded.

    Closes the cite-ratio echo-drift loop: dream-synthesized insights cannot
    auto-graduate without explicit human validation or raised confidence.

    Provenance is the ONLY disqualifier, and the counters are chosen to keep it
    that way: total 10 clears the low-confidence guard's `access_count < 5`
    (which would otherwise reject this same low-confidence row for a different
    reason), and human 6 clears maturity. Drop the dream:generated clause and
    this row is admitted.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="dream-low-unvalidated",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="low",
                    tags=["dream:agent", "dream:insight", "dream:generated"],
                    access_count=10,
                    access_count_human=6,
                    created_at=now - dt.timedelta(days=10),
                )
            )

    candidates = await promote_prepare.fetch_candidates(
        session_factory, project_key=isolated_pk, limit=10
    )
    assert candidates == [], "dream:generated low-confidence without validated_at must be gated"


@pytest.mark.asyncio
async def test_fetch_candidates_admits_dream_generated_low_after_validation(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """ADR #4 gate — dream:generated + low confidence + validated_at SET → included.

    Human validation via brain_validate_learning re-qualifies the insight.
    Counters mirror the gated twin above (total 10, human 6) so that
    `validated_at` is the single difference between the two outcomes.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="dream-low-validated",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="low",
                    tags=["dream:agent", "dream:insight", "dream:generated"],
                    access_count=10,
                    access_count_human=6,
                    validated_at=now - dt.timedelta(days=1),
                    created_at=now - dt.timedelta(days=10),
                )
            )

    candidates = await promote_prepare.fetch_candidates(
        session_factory, project_key=isolated_pk, limit=10
    )
    assert any(c["topic"] == "dream-low-validated" for c in candidates), (
        "human-validated dream:generated insight must re-qualify"
    )


@pytest.mark.asyncio
async def test_fetch_candidates_admits_dream_generated_with_raised_confidence(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """ADR #4 gate — dream:generated + non-low confidence → included.

    Curator-raised confidence (medium/high) is the alternate gate path
    when validated_at hasn't been set. Counters mirror the gated twin
    (total 10, human 6) so that `confidence` is the single difference.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="dream-high-confidence",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent", "dream:insight", "dream:generated"],
                    access_count=10,
                    access_count_human=6,
                    created_at=now - dt.timedelta(days=10),
                )
            )

    candidates = await promote_prepare.fetch_candidates(
        session_factory, project_key=isolated_pk, limit=10
    )
    assert any(c["topic"] == "dream-high-confidence" for c in candidates), (
        "raised-confidence dream:generated insight must qualify"
    )


@pytest.mark.asyncio
async def test_fetch_candidates_non_dream_low_confidence_unaffected_by_adr4_gate(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Regression guard — ADR #4 gate is tag-scoped to dream:generated.

    A non-dream low-confidence learning that passes the existing sub-gate must
    still qualify. That sub-gate reads the TOTAL counter, so the fixture sits
    exactly on its boundary (`access_count=5`) while holding the human counter
    clear of maturity (4 >= 3): admission is therefore attributable to the
    total-counter boundary being inclusive, not to the human counter.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="non-dream-low-with-access",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="low",
                    tags=["topic-foo"],
                    access_count=5,
                    access_count_human=4,
                    created_at=now - dt.timedelta(days=10),
                )
            )

    candidates = await promote_prepare.fetch_candidates(
        session_factory, project_key=isolated_pk, limit=10
    )
    assert any(c["topic"] == "non-dream-low-with-access" for c in candidates), (
        "non-dream low-confidence with access_count>=5 must remain admissible"
    )


@pytest.mark.asyncio
async def test_fetch_candidates_filters_low_confidence_under_total_access(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Low-confidence guard — `confidence='low' AND access_count < 5` → excluded.

    Negative twin of the test above, and the only witness the guard has: its
    positive twin asserts a PRESENCE, and removing a filter can never make a
    presence assertion fall. Deleting the guard outright therefore used to kill
    no test at all.

    The counters straddle the two gates on purpose — total 4 (below the guard's
    5), human 3 (on the maturity threshold). Maturity is cleared, so the empty
    pool is attributable to the guard alone, and the total counter sitting one
    short of the boundary also pins the constant downward: relax `< 5` to `< 4`
    and this row is admitted.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="low-confidence-thinly-read",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="low",
                    tags=["topic-foo"],
                    access_count=4,
                    access_count_human=3,
                    created_at=now - dt.timedelta(days=10),
                )
            )
    assert (
        await promote_prepare.fetch_candidates(
            session_factory,
            project_key=isolated_pk,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_fetch_candidates_filters_exclude_from_promote_tag(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Spec C insulation, measured — an EXCLUDE_FROM_PROMOTE learning is rejected.

    The class below asserts the clause's TEXT, which proves it is written, not
    that it runs: rename the tag it matches and the query still parses, still
    promotes resonance learnings, and only the string comparison notices. This
    is the behavioural witness. The row clears every other gate, so the empty
    pool is attributable to the tag alone.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="cross-project-resonance",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent", "EXCLUDE_FROM_PROMOTE"],
                    access_count=10,
                    access_count_human=6,
                    created_at=now - dt.timedelta(days=10),
                )
            )
    assert (
        await promote_prepare.fetch_candidates(
            session_factory,
            project_key=isolated_pk,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_fetch_candidates_ranks_by_human_access_then_recency(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """Candidates come back ordered by HUMAN access DESC, then created_at DESC.

    Ranking is what decides `candidates[0]`, and the prompt orders the judge to
    evaluate that slot and no other. Ordering on the total counter therefore
    hands the one evaluated slot to whichever learning the dream itself read
    most — the phase picks its own winner. Since 041 the maturity gate reads
    `access_count_human`; the ranking now reads the same counter, so the pool is
    ordered on the evidence it is admitted on.

    The fixture separates the four failure modes:
      - `access_count` and `access_count_human` are driven in OPPOSITE
        directions, so ranking on the total counter fully reverses the result;
      - two rows tie on the human counter and differ in age, which pins the
        `created_at DESC` tie-break;
      - inside that tie the OLDER row carries the HIGHER total, so restoring
        `access_count` as the secondary key — re-contaminating the tie-break —
        swaps the top two;
      - rows are inserted in an order that is not the expected one, so dropping
        the ORDER BY surfaces the physical order instead.

    Every row sits clear of both counter thresholds, so this test answers for
    the ordering and nothing else: moving either gate leaves it green.
    """
    now = dt.datetime.now(dt.UTC)
    seed = [
        # (topic, access_count, access_count_human, age_days) — insertion order
        # deliberately differs from the expected output order.
        ("rank-c-low-human", 40, 3, 10),
        ("rank-b-mid-human", 30, 5, 10),
        ("rank-d-tied-older", 20, 8, 20),
        ("rank-a-tied-newer", 10, 8, 10),
    ]
    async with session_factory() as session:
        async with session.begin():
            for topic, total, human, age_days in seed:
                await session.execute(
                    learnings.insert().values(
                        topic=topic,
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=["dream:agent"],
                        access_count=total,
                        access_count_human=human,
                        created_at=now - dt.timedelta(days=age_days),
                    )
                )

    candidates = await promote_prepare.fetch_candidates(
        session_factory,
        project_key=isolated_pk,
        limit=10,
    )
    assert [c["topic"] for c in candidates] == [
        "rank-a-tied-newer",  # human 8, newer of the tie (total 10 — LOWEST of the four)
        "rank-d-tied-older",  # human 8, older of the tie (total 20 — would lead on totals)
        "rank-b-mid-human",  # human 5 (total 30)
        "rank-c-low-human",  # human 3 (total 40 — would lead outright on the total counter)
    ]


@pytest.mark.asyncio
async def test_fetch_candidates_exposes_the_human_access_count(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """The payload carries BOTH counters, under distinct keys.

    The judge is told to weigh the candidate's maturity, and until now the only
    number it could see was the total — the counter the dream's own reads
    inflate. Exposing `access_count_human` is what makes the maturity claim
    auditable from inside the prompt. The two counters are driven apart (total
    9, human 4) so the assertion pins which value lands under which key: swap
    them, or drop the human key, and this test falls.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="human-counter-exposed",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent"],
                    access_count=9,
                    access_count_human=4,
                    created_at=now - dt.timedelta(days=10),
                )
            )

    candidates = await promote_prepare.fetch_candidates(
        session_factory,
        project_key=isolated_pk,
        limit=10,
    )
    assert len(candidates) == 1
    assert candidates[0]["access_count"] == 9
    assert candidates[0]["access_count_human"] == 4


@pytest.mark.asyncio
async def test_human_access_count_survives_all_the_way_into_the_promote_prompt(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str
) -> None:
    """End-to-end witness: the new key reaches the text the judge actually reads.

    A value nobody reads is a false witness. `fetch_candidates` is three hops
    from the model — dream.sh dumps its stdout to `$CANDIDATES_JSON`, re-reads
    it into `PROMOTE_CANDIDATE_POOL_JSON`, and hands that string to
    `_render_prompt` for the `{{CANDIDATE_POOL_JSON}}` marker in
    `phase_promote.md`. This test replays those hops on the real template, so a
    future schema, `jq` projection or formatter inserted anywhere on that path
    that drops unknown keys makes it fall instead of silently starving the
    judge.
    """
    now = dt.datetime.now(dt.UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                learnings.insert().values(
                    topic="reaches-the-judge",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent"],
                    access_count=9,
                    access_count_human=4,
                    created_at=now - dt.timedelta(days=10),
                )
            )

    candidates = await promote_prepare.fetch_candidates(
        session_factory,
        project_key=isolated_pk,
        limit=10,
    )
    # dream.sh:463 → stdout to $CANDIDATES_JSON; dream.sh:505 → back into the env var.
    pool_json = json.dumps(candidates)
    template = _PROMOTE_PROMPT.read_text(encoding="utf-8")
    # dream.sh:196 → the env var becomes the {{CANDIDATE_POOL_JSON}} argument.
    prompt = render(
        template,
        {
            "PROJECT_KEY": isolated_pk,
            "DATE": "2026-08-07",
            "DRY_RUN": "false",
            "CANDIDATE_POOL_JSON": pool_json,
            "RECENT_PROMOTIONS_JSON": "[]",
        },
    )

    assert '"access_count_human": 4' in prompt
    assert '"topic": "reaches-the-judge"' in prompt


class TestCandidateSqlInsulation:
    """Pure SQL-text check — runs without a DB, unlike the rest of this file."""

    def test_candidate_sql_excludes_cross_project_resonance_learnings(self):
        """Spec C insulation: resonance learnings must never enter the PROMOTE pool."""
        sql = str(promote_prepare._CANDIDATE_SQL)
        assert "'EXCLUDE_FROM_PROMOTE' != ALL(l.tags)" in sql


class TestPromotePromptDescribesTheRanking:
    """The prompt states the ordering contract; drift there misleads the judge.

    `phase_promote.md` tells the model how the pool was ranked and orders it to
    evaluate `candidates[0]`. A sentence that names the wrong counter is not
    cosmetic: it is the only explanation the judge gets for why the top slot is
    the top slot, and it invites it to re-rank on the number the sentence names.
    """

    def test_prompt_names_the_human_counter_as_the_ranking_key(self):
        prompt = _PROMOTE_PROMPT.read_text(encoding="utf-8")
        assert "Ranked by access_count_human DESC, then created_at DESC." in prompt

    def test_prompt_tells_the_judge_what_each_counter_measures(self):
        """Two numeric counters with no legend is an invitation to pick either."""
        prompt = _PROMOTE_PROMPT.read_text(encoding="utf-8")
        assert "EXCLUDES this dream's own automated reads" in prompt
        assert "`access_count` is the TOTAL" in prompt

    def test_prompt_does_not_sell_the_human_counter_as_an_attestation(self):
        """The legend must not claim more than ``is_human_actor`` delivers.

        That predicate returns True for ANY actor that is not ``unknown``,
        ``_unexpanded`` or ``dream-`` prefixed — so another project's bot
        declaring ``X-Brain-Agent`` is counted. Telling the judge the counter
        proves a person read the entry would hand it a false premise on
        exactly the evidence it is asked to weigh.

        Le préfixe est passé de ``dream-codex-`` à la famille ``dream-`` : deux
        rails sur trois (``claude``, ``agy``) étaient comptés HUMAINS. Le
        caveat reste néanmoins nécessaire — la garde couvre la famille dream,
        pas toute machine concevable, et ce test le protège.
        """
        prompt = _PROMOTE_PROMPT.read_text(encoding="utf-8")
        assert "as proof that a person read it" in prompt
        assert "including another project's bot" in prompt


def test_cli_outputs_json(capsys, monkeypatch) -> None:
    """CLI emits the candidate list as a JSON array on stdout."""
    # Settings() is instantiated inside main(); inject POSTGRES_URL so it
    # validates in a CI container that has no .env file.
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:brain@localhost:5433/brain")
    fake_rows = [{"id": "abc", "topic": "x", "content": "y", "access_count": 5}]

    async def fake_fetch(*_a, **_k):
        return fake_rows

    monkeypatch.setattr(promote_prepare, "fetch_candidates", fake_fetch)
    monkeypatch.setattr(
        promote_prepare,
        "_build_factory",
        lambda _url: None,  # unused
    )
    promote_prepare.main(["--project-key", "brain-v42", "--limit", "10"])
    captured = capsys.readouterr()
    assert json.loads(captured.out) == fake_rows
