"""Real-PostgreSQL end to end: wet, rerun, dry, failure paths (ADR 27 lot C, T1.9).

NOTE for reviewers: this module SKIPPED in the container that wrote it (no
`BRAIN_V42_TEST_DB_URL`, no reachable PostgreSQL) -- every assertion below is
untested against a real server and must be run for real before PR 1 merges.
This task writes tests only, per the plan; no production code changes here.

Ordering matters and is easy to get backwards: `register_fact_definitions`
must run BEFORE a claim referencing that (fact_name, definition_version) is
inserted, so it INSERTS the definition fresh with the correctly computed
digest (`knowledge_fact_definitions` is append-only: migration 055's
`knowledge_fact_definitions_append_only` trigger refuses UPDATE and DELETE).
The one exception is the drift test, which pre-seeds a WRONG digest before
registration runs, so the drift is detected instead of a fresh insert.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import (
    brain_entities,
    dream_runs,
    knowledge_claim_verdicts,
    knowledge_claims,
    knowledge_fact_definitions,
    project_contexts,
)
from brain_v42.facts.definitions_startup import register_fact_definitions
from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.nightly import (
    NightlyVerifier,
    ReleaseCheck,
    check_preconditions,
    issuer_for,
    key_for,
)
from brain_v42.facts.registry import FactRegistry, RefreshBudget
from brain_v42.facts.sources import PostgresSourceFactory, PostgresSourceSession
from brain_v42.facts.verification import ClaimVerificationService
from brain_v42.repositories.pg_claim_nightly import (
    VerifyRunOwnership,
    eligible_fact_names,
    finish_dry_run,
    finish_run,
    get_or_create_wet_run,
    insert_dry_run,
    select_nightly_claims,
)
from brain_v42.repositories.pg_knowledge_claims import insert_claim
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration

_RUN_DATE_SEQUENCE = count()
_NONEXISTENT_RELEASE_PATH = Path("/nonexistent")


@pytest.fixture
def run_date() -> date:
    """Each test owns its run row, while a rerun within one test keeps that date."""
    return date(2030, 1, 1) + timedelta(days=next(_RUN_DATE_SEQUENCE))


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    with fresh_head_database(_get_integration_db_url_or_skip(), prefix="brain_claimnight") as url:
        yield url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migration_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _retire_claims_left_by_earlier_tests(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Retire every not-yet-retired claim before each test (module isolation).

    This file shares ONE disposable database across the whole session
    (`knowledge_claims` is append-only, see the module docstring). A claim that
    stays perpetually eligible for `select_nightly_claims` -- an `unreadable`
    latest verdict (never conclusive), a claim whose fact a later registry does
    not know, or one skipped by a refresh budget -- otherwise gets re-selected
    by a later test and skews its counts and report fields (`retired_mid_run`,
    `stopped_facts`, the plain verdict-count assertions several tests make).
    Retiring is an UPDATE, which the append-only trigger allows (it refuses
    only DELETE).
    """
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.update(knowledge_claims)
            .where(knowledge_claims.c.retired_at.is_(None))
            .values(retired_at=datetime.now(UTC))
        )


class _Source:
    async def identity(self) -> SourceIdentity:
        return SourceIdentity("1", "brain_test", "127.0.0.1", 5432)


class _RealHeadProbe:
    """Mirrors the real `alembic_head` probe's shape for a claim expecting a revision."""

    definition_version = 1
    target = FactTarget.PRODUCTION
    ttl = timedelta(seconds=60)
    timeout = timedelta(seconds=3)
    briefing = False
    policies: Mapping[str, int] = {}
    value_schema = {"revision": "string"}

    def __init__(self, name: str, revision: str, *, ttl_seconds: int | None = None) -> None:
        self.name = name
        self.revision = revision
        self.runs = 0
        if ttl_seconds is not None:
            # Per-instance override (the class attribute stays the shared
            # default): `FactRegistry.measure` only spends the refresh budget
            # on a FORCED refresh, i.e. when the caller's `max_age` (here,
            # the claim's own `validity_seconds`) is SMALLER than the probe's
            # own TTL. A budget-exhaustion scenario across several claims of
            # one fact therefore needs a TTL larger than those claims'
            # `validity_seconds`, not the reverse.
            self.ttl = timedelta(seconds=ttl_seconds)

    async def measure(self, source: _Source) -> Mapping[str, object]:
        self.runs += 1
        return {"revision": self.revision}


class _TimeoutProbe(_RealHeadProbe):
    timeout = timedelta(seconds=1)

    async def measure(self, source: _Source) -> Mapping[str, object]:
        self.runs += 1
        await asyncio.sleep(10)
        return {"revision": self.revision}


def _registry(*probes: object, refresh_budget: RefreshBudget | None = None) -> FactRegistry:
    @asynccontextmanager
    async def source() -> AsyncIterator[_Source]:
        yield _Source()

    identity = SourceIdentity("1", "brain_test", "127.0.0.1", 5432)
    kwargs: dict[str, object] = {}
    if refresh_budget is not None:
        kwargs["refresh_budget"] = refresh_budget
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source},
        expected={FactTarget.PRODUCTION: identity},
        **kwargs,
    )
    for probe in probes:
        registry.register(probe)
    registry.freeze()
    return registry


async def _insert_claim(
    session: AsyncSession,
    *,
    fact_name: str,
    expected_revision: str,
    validity_seconds: int = 600,
    project_key: str | None = None,
) -> UUID:
    """Insert a project, an anchor and a claim -- the DEFINITION must already exist."""
    project_key = project_key or f"nightly-run-{uuid4().hex[:12]}"
    await session.execute(
        sa.insert(project_contexts).values(
            project_key=project_key, name=project_key, description="T1.9 fixture"
        )
    )
    entity_id = (
        await session.execute(
            sa.insert(brain_entities)
            .values(
                entity_type="learning",
                entity_key=f"nightly-run-entity-{uuid4()}",
                project_key=project_key,
                scope_kind="project",
                lifecycle="active",
            )
            .returning(brain_entities.c.id)
        )
    ).scalar_one()
    row = await insert_claim(
        session,
        entity_ref_id=entity_id,
        entity_type="learning",
        project_key=project_key,
        claim_key="b" * 64,
        statement="The repository alembic head matches the expected revision.",
        fact_name=fact_name,
        definition_version=1,
        target="production",
        expected={"path": "/revision", "op": "eq", "value": expected_revision},
        expected_resolved={"path": "/revision", "op": "eq", "value": expected_revision},
        validity_seconds=validity_seconds,
        provenance="declared",
        declared_by="integration-test",
        declared_at=datetime.now(UTC),
    )
    return row.id


async def _seed_wrong_digest_definition(session: AsyncSession, *, fact_name: str) -> None:
    """Pre-seed a definition row registration will find already there, with a WRONG digest."""
    await session.execute(
        sa.insert(knowledge_fact_definitions).values(
            fact_name=fact_name,
            definition_version=1,
            target="production",
            ttl_seconds=60,
            timeout_seconds=3,
            policies={},
            value_schema={"revision": "string"},
            digest="f" * 64,
        )
    )


async def _run_id_row(
    session_factory: async_sessionmaker[AsyncSession], run_id: int
) -> sa.RowMapping:
    async with session_factory() as session:
        return (
            (await session.execute(sa.select(dream_runs).where(dream_runs.c.id == run_id)))
            .mappings()
            .one()
        )


async def _wet_pass(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    registry: FactRegistry,
    run_date: date,
    *,
    max_concurrency: int = 4,
    service: object = None,
) -> tuple[int, object]:
    ownership = VerifyRunOwnership(engine)
    assert await ownership.acquire()
    try:
        run_id = await get_or_create_wet_run(ownership, run_date)
        claims = await select_nightly_claims(session_factory, now=datetime.now(UTC), max_claims=200)
        release_check = ReleaseCheck(cli_release_path=_NONEXISTENT_RELEASE_PATH)
        verifier = NightlyVerifier(
            service=service or ClaimVerificationService(registry, session_factory),
            release_check=release_check,
            ownership=ownership,
            max_concurrency=max_concurrency,
        )
        report = await verifier.run(claims, run_id=run_id, run_date=run_date, wet=True)
        await finish_run(
            ownership, run_id, status=report.status, duration_s=0.0, error_message=None
        )
        return run_id, report
    finally:
        await ownership.release()


async def test_a_wet_run_records_holds_and_falsified(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, run_date: date
) -> None:
    probe = _RealHeadProbe(f"nightly_head_{uuid4().hex}", revision="057")
    registry = _registry(probe)
    assert await register_fact_definitions(registry, session_factory)

    async with session_factory() as session, session.begin():
        holds_id = await _insert_claim(session, fact_name=probe.name, expected_revision="057")
        falsified_id = await _insert_claim(session, fact_name=probe.name, expected_revision="055")

    run_id, report = await _wet_pass(engine, session_factory, registry, run_date)

    assert report.status == "done"
    async with session_factory() as session:
        holds_row = (
            (
                await session.execute(
                    sa.select(knowledge_claim_verdicts).where(
                        knowledge_claim_verdicts.c.claim_id == holds_id
                    )
                )
            )
            .mappings()
            .one()
        )
        falsified_row = (
            (
                await session.execute(
                    sa.select(knowledge_claim_verdicts).where(
                        knowledge_claim_verdicts.c.claim_id == falsified_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert holds_row["verdict"] == "holds"
    assert falsified_row["verdict"] == "falsified"
    assert holds_row["issuer_identity"] == issuer_for(run_id)
    assert holds_row["issuer_kind"] == "robot"
    assert holds_row["idempotency_key"] == key_for(run_date)
    run_row = await _run_id_row(session_factory, run_id)
    assert run_row["status"] == "done"


async def test_a_rerun_of_the_same_run_date_adds_zero_verdicts_and_reuses_the_run_id(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, run_date: date
) -> None:
    probe = _RealHeadProbe(f"nightly_rerun_{uuid4().hex}", revision="057")
    registry = _registry(probe)
    assert await register_fact_definitions(registry, session_factory)
    async with session_factory() as session, session.begin():
        claim_id = await _insert_claim(session, fact_name=probe.name, expected_revision="057")

    first_run_id, _ = await _wet_pass(engine, session_factory, registry, run_date)
    async with session_factory() as session:
        first_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(knowledge_claim_verdicts)
            .where(knowledge_claim_verdicts.c.claim_id == claim_id)
        )

    second_run_id, _ = await _wet_pass(engine, session_factory, registry, run_date)
    async with session_factory() as session:
        second_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(knowledge_claim_verdicts)
            .where(knowledge_claim_verdicts.c.claim_id == claim_id)
        )

    assert second_run_id == first_run_id
    assert second_count == first_count == 1


async def test_a_timed_out_probe_gives_a_durable_unreadable_row(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, run_date: date
) -> None:
    probe = _TimeoutProbe(f"nightly_timeout_{uuid4().hex}", revision="057")
    registry = _registry(probe)
    assert await register_fact_definitions(registry, session_factory)
    async with session_factory() as session, session.begin():
        claim_id = await _insert_claim(session, fact_name=probe.name, expected_revision="057")

    await _wet_pass(engine, session_factory, registry, run_date)

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(knowledge_claim_verdicts).where(
                        knowledge_claim_verdicts.c.claim_id == claim_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["verdict"] == "unreadable"
    assert row["measurement"]["error_code"] == "timeout"


async def test_a_disabled_drifted_fact_named_by_an_eligible_claim_fails_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fact_name = f"nightly_drift_{uuid4().hex}"
    probe = _RealHeadProbe(fact_name, revision="057")
    registry = _registry(probe)

    async with session_factory() as session, session.begin():
        await _seed_wrong_digest_definition(session, fact_name=fact_name)
        claim_id = await _insert_claim(session, fact_name=fact_name, expected_revision="057")

    registered_ok = await register_fact_definitions(registry, session_factory)
    eligible_facts = await eligible_fact_names(session_factory, now=datetime.now(UTC))
    failure = check_preconditions(registry, eligible_facts, definitions_registered=registered_ok)

    assert failure is not None
    assert failure.reason == f"definition_drift: {fact_name}"
    async with session_factory() as session:
        # Scoped to this test's own claim: `knowledge_claim_verdicts` is
        # append-only and accumulates across the whole module (shared
        # database), so an unscoped count would include earlier tests' rows.
        verdict_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(knowledge_claim_verdicts)
            .where(knowledge_claim_verdicts.c.claim_id == claim_id)
        )
    assert verdict_count == 0


async def test_refresh_budget_exhaustion_skips_the_rest_of_one_fact_and_verifies_others(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, run_date: date
) -> None:
    """Spec §6.4: an exhausted TARGET bucket stops each OTHER fact on it in turn.

    `FactRegistry.measure` only spends a refresh-budget token on a FORCED
    charge (`max_age < ttl`) that is also a genuine cache MISS. Three claims
    sharing ONE fact name cannot demonstrate exhaustion in a fast, real-clock
    test: the first claim's successful measurement is cached, and every claim
    validity is at least 60s (`knowledge_claims_validity_seconds_valid`), so a
    second or third claim of the SAME fact always finds that cache still
    fresh -- `_charge_refresh` is never even reached for it, whatever the
    budget's size (confirmed empirically: with one shared fact, all three
    claims came back `holds`, `skipped_budget == 0`). Spec §4.5 makes the same
    point about the per-FACT bucket specifically ("not reachable within one
    invocation"). Three DIFFERENT facts sharing one TARGET have no such
    shared cache: each is a genuine, independent cache miss, so the shared
    per_target_per_minute=1 bucket is what exhausts, per §6.4 -- the first
    fact's claim spends the only token, and the other two facts each hit the
    empty bucket once and are stopped.
    """
    probe_a = _RealHeadProbe(f"nightly_budget_a_{uuid4().hex}", revision="057", ttl_seconds=3600)
    probe_b = _RealHeadProbe(f"nightly_budget_b_{uuid4().hex}", revision="057", ttl_seconds=3600)
    probe_c = _RealHeadProbe(f"nightly_budget_c_{uuid4().hex}", revision="057", ttl_seconds=3600)
    other_probe = _RealHeadProbe(f"nightly_other_{uuid4().hex}", revision="057")
    registry = _registry(probe_a, probe_b, probe_c, other_probe, refresh_budget=RefreshBudget(1, 1))
    assert await register_fact_definitions(registry, session_factory)

    async with session_factory() as session, session.begin():
        # Distinct (default, random) project keys put each claim at rank 1 of
        # its own project; the tie-break on equal `age_key` (one shared
        # transaction) is `seq`, so dispatch follows this insertion order
        # under `max_concurrency=1`: a, b, c, then other.
        claim_a = await _insert_claim(
            session, fact_name=probe_a.name, expected_revision="057", validity_seconds=60
        )
        claim_b = await _insert_claim(
            session, fact_name=probe_b.name, expected_revision="057", validity_seconds=60
        )
        claim_c = await _insert_claim(
            session, fact_name=probe_c.name, expected_revision="057", validity_seconds=60
        )
        other_id = await _insert_claim(session, fact_name=other_probe.name, expected_revision="057")

    _, report = await _wet_pass(engine, session_factory, registry, run_date, max_concurrency=1)

    assert report.stopped_facts == sorted([probe_b.name, probe_c.name])
    assert report.skipped_budget == 2
    assert report.holds == 2  # claim_a and the untouched other_id
    async with session_factory() as session:
        verdicts_by_claim = {
            row["claim_id"]: row["verdict"]
            for row in (
                await session.execute(
                    sa.select(knowledge_claim_verdicts).where(
                        knowledge_claim_verdicts.c.claim_id.in_(
                            [claim_a, claim_b, claim_c, other_id]
                        )
                    )
                )
            )
            .mappings()
            .all()
        }
    assert verdicts_by_claim == {claim_a: "holds", other_id: "holds"}


async def test_a_mid_run_retirement_is_counted_and_writes_no_row(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, run_date: date
) -> None:
    probe = _RealHeadProbe(f"nightly_retire_{uuid4().hex}", revision="057")
    registry = _registry(probe)
    assert await register_fact_definitions(registry, session_factory)
    async with session_factory() as session, session.begin():
        claim_id = await _insert_claim(session, fact_name=probe.name, expected_revision="057")

    real_service = ClaimVerificationService(registry, session_factory)

    class _RetireThenVerify:
        async def verify_outcome(self, *args: object, **kwargs: object) -> object:
            async with session_factory() as session, session.begin():
                await session.execute(
                    sa.update(knowledge_claims)
                    .where(knowledge_claims.c.id == claim_id)
                    .values(retired_at=datetime.now(UTC))
                )
            return await real_service.verify_outcome(*args, **kwargs)  # type: ignore[arg-type]

    _, report = await _wet_pass(
        engine, session_factory, registry, run_date, service=_RetireThenVerify()
    )

    assert report.retired_mid_run == 1
    async with session_factory() as session:
        verdict_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(knowledge_claim_verdicts)
            .where(knowledge_claim_verdicts.c.claim_id == claim_id)
        )
    assert verdict_count == 0


async def test_dry_mode_writes_no_verdict_takes_no_lock_and_marks_the_row_dry(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, run_date: date
) -> None:
    probe = _RealHeadProbe(f"nightly_dry_{uuid4().hex}", revision="057")
    registry = _registry(probe)
    assert await register_fact_definitions(registry, session_factory)
    async with session_factory() as session, session.begin():
        await _insert_claim(session, fact_name=probe.name, expected_revision="057")

    # `knowledge_claim_verdicts` is append-only and accumulates across the
    # whole module (shared database): the dry pass must add zero rows to
    # whatever earlier tests already committed, so the proof is a before/after
    # delta rather than an absolute count.
    async with session_factory() as session:
        verdict_count_before = await session.scalar(
            sa.select(sa.func.count()).select_from(knowledge_claim_verdicts)
        )

    statements: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        run_id = await insert_dry_run(session_factory, run_date)
        claims = await select_nightly_claims(session_factory, now=datetime.now(UTC), max_claims=200)
        release_check = ReleaseCheck(cli_release_path=_NONEXISTENT_RELEASE_PATH)
        verifier = NightlyVerifier(service=None, release_check=release_check)  # type: ignore[arg-type]
        report = await verifier.run_dry(claims, registry=registry, run_id=run_id, run_date=run_date)
        await finish_dry_run(
            session_factory, run_id, status=report.status, duration_s=0.0, error_message=None
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert not any("FOR UPDATE" in statement.upper() for statement in statements)
    assert not any("pg_advisory_lock" in statement for statement in statements)
    async with session_factory() as session:
        verdict_count_after = await session.scalar(
            sa.select(sa.func.count()).select_from(knowledge_claim_verdicts)
        )
    row = await _run_id_row(session_factory, run_id)
    assert verdict_count_after == verdict_count_before
    assert row["phase_dry_run"] is True


async def test_dream_last_night_measured_during_a_wet_run_includes_its_own_row(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, run_date: date
) -> None:
    """H1(a), pinned deliberately: this is the accepted, documented behaviour.

    `dream_last_night` aggregates `max(run_date)` over ALL `dream_runs` rows
    (`pg_dream_runs.py`), so a wet verify row written moments ago is what it
    measures -- not the previous complete night. No active claim names
    `dream_last_night` today (spec §15); switching this is a human decision
    (plan H1), not part of this PR.
    """
    from brain_v42.facts.probes.dream_last_night import DreamLastNightProbe

    # Unlike `_RealHeadProbe` (which never touches `source`), `DreamLastNightProbe`
    # queries `dream_runs` through a REAL `PostgresSourceSession` -- the bare
    # `_Source` fixture used elsewhere in this module has no `.session` and
    # turns every call into an `Unreadable` `probe_error`. The expected
    # identity must be the disposable database's own, measured the same way
    # the registry measures it (plan T1.9: "the identity measured from that DB").
    async with session_factory() as identity_session, identity_session.begin():
        real_identity = await PostgresSourceSession(identity_session).identity()

    dream_last_night_registry = FactRegistry(
        sources={FactTarget.PRODUCTION: PostgresSourceFactory(session_factory)},
        expected={FactTarget.PRODUCTION: real_identity},
    )
    dream_last_night_registry.register(DreamLastNightProbe())
    dream_last_night_registry.freeze()

    ownership = VerifyRunOwnership(engine)
    assert await ownership.acquire()
    try:
        run_id = await get_or_create_wet_run(ownership, run_date)
        await finish_run(ownership, run_id, status="done", duration_s=0.0, error_message=None)
    finally:
        await ownership.release()

    measurement = await dream_last_night_registry.measure("dream_last_night", max_age=timedelta(0))

    assert measurement.value["run_date"] == run_date.isoformat()
