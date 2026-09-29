"""Selection of stale-or-never-verified claims for the nightly verification step.

Spec §4: active claims, all projects, oldest-first per-project round robin,
capped. No view (`knowledge_claim_current` is a whole-table `DISTINCT ON`) and
no new index -- both LATERAL subqueries below walk the existing
`ix_knowledge_claim_verdicts_claim_seq (claim_id, seq DESC)`.

NOTE for reviewers: this SQL (LATERAL joins + a `row_number()` window) was
written and reasoned through without a reachable PostgreSQL to execute it
against (ADR 27 lot C, T1.3, implemented from a container with no database).
`tests/integration/db/test_claim_nightly_selection.py` exercises every
predicate and the ranking, but it SKIPPED here for lack of a database -- it
must be run for real before this is trusted.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

import sqlalchemy as sa
from asyncpg import InternalClientError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from brain_v42.db.tables import dream_runs, knowledge_claim_verdicts, knowledge_claims
from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

#: `dream_runs` phase name for the nightly claim-verification step (spec §5.1).
_VERIFY_PHASE: Final = "verify"

#: The D1 initial status (plan §1): a row left in this state by a dead process
#: reports as FAILED the next morning, never as `other` (`pg_dream_runs.py`,
#: `post_run_alert.FAILED_STATUSES`, `DreamRunService._FAILURE_STATUSES` all
#: already treat `fail` that way; no vocabulary change needed).
_INITIAL_STATUS: Final = "fail"
_INITIAL_ERROR_MESSAGE: Final = "verify started; no terminal status recorded"

# LIMIT 1 prevents PostgreSQL from pulling up the lateral lookup into a
# ledger-wide hash join. The unique claim/issuer/key constraint makes it lossless.
LAST_WET_VERIFY_RUN_SQL = sa.text("""
WITH r AS (
  SELECT id, run_date, status FROM dream_runs
   WHERE phase = 'verify' AND project_key = '*' AND phase_dry_run = false
   ORDER BY run_date DESC, id ASC LIMIT 1)
SELECT r.id, r.run_date, r.status,
       count(v.verdict) FILTER (WHERE v.verdict = 'holds') AS holds,
       count(v.verdict) FILTER (WHERE v.verdict = 'falsified') AS falsified,
       count(v.verdict) FILTER (WHERE v.verdict = 'unreadable') AS unreadable
  FROM r
  LEFT JOIN knowledge_claims c ON true
  LEFT JOIN LATERAL (
       SELECT v.verdict FROM knowledge_claim_verdicts v
        WHERE v.claim_id = c.id
          AND v.issuer_identity = CAST(:issuer_prefix AS text) || r.id
          AND v.idempotency_key = CAST(:key_prefix AS text) || to_char(r.run_date, 'YYYY-MM-DD')
        LIMIT 1) v ON true
 GROUP BY r.id, r.run_date, r.status
""")


@dataclass(frozen=True, slots=True)
class LastWetVerifyRun:
    """One measured wet run and the current uncapped eligibility count."""

    run_id: int
    run_date: date
    status: str
    holds: int
    falsified: int
    unreadable: int
    eligible_now: int


async def read_last_wet_verify_run(
    session: AsyncSession, issuer_prefix: str, key_prefix: str, now: datetime
) -> LastWetVerifyRun | None:
    """Read run evidence and eligibility inside the caller's source snapshot."""
    row = (
        (
            await session.execute(
                LAST_WET_VERIFY_RUN_SQL,
                {"issuer_prefix": issuer_prefix, "key_prefix": key_prefix},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    eligible = _eligible_query(now).subquery("eligible")
    eligible_now = await session.scalar(sa.select(sa.func.count()).select_from(eligible))
    return LastWetVerifyRun(
        run_id=row["id"],
        run_date=row["run_date"],
        status=row["status"],
        holds=row["holds"],
        falsified=row["falsified"],
        unreadable=row["unreadable"],
        eligible_now=int(eligible_now or 0),
    )


@dataclass(frozen=True, slots=True)
class NightlyClaim:
    """One claim selected for tonight's verification, with what `verify_outcome` needs."""

    id: UUID
    project_key: str
    fact_name: str
    target: str
    definition_version: int
    validity_seconds: int
    seq: int
    age_key: datetime


def _eligible_query(now: datetime) -> sa.Select:
    """The eligibility predicate of spec §4.1, before ranking or the cap."""
    claims = knowledge_claims
    verdicts = knowledge_claim_verdicts

    latest = (
        sa.select(
            verdicts.c.seq.label("seq"),
            verdicts.c.verdict.label("verdict"),
            verdicts.c.emitted_at.label("emitted_at"),
        )
        .where(verdicts.c.claim_id == claims.c.id)
        .order_by(verdicts.c.seq.desc())
        .limit(1)
        .correlate(claims)
        .lateral("latest")
    )
    conclusive = (
        sa.select(
            verdicts.c.seq.label("seq"),
            verdicts.c.emitted_at.label("emitted_at"),
        )
        .where(verdicts.c.claim_id == claims.c.id, verdicts.c.verdict.in_(("holds", "falsified")))
        .order_by(verdicts.c.seq.desc())
        .limit(1)
        .correlate(claims)
        .lateral("conclusive")
    )

    one_second: sa.ColumnElement[Any] = sa.literal_column("interval '1 second'")
    validity_expiry = conclusive.c.emitted_at + claims.c.validity_seconds * one_second
    age_key = sa.func.coalesce(latest.c.emitted_at, claims.c.recorded_at).label("age_key")

    return (
        sa.select(
            claims.c.id,
            claims.c.project_key,
            claims.c.fact_name,
            claims.c.target,
            claims.c.definition_version,
            claims.c.validity_seconds,
            claims.c.seq,
            age_key,
        )
        .select_from(claims.outerjoin(latest, sa.true()).outerjoin(conclusive, sa.true()))
        .where(
            claims.c.retired_at.is_(None),
            sa.or_(
                conclusive.c.seq.is_(None),
                validity_expiry <= now,
                sa.and_(latest.c.verdict == "unreadable", latest.c.seq > conclusive.c.seq),
            ),
        )
    )


def _ranked_query(
    now: datetime, max_claims: int, *, eligible_fact_names: Sequence[str]
) -> sa.Select:
    """Wrap the eligibility predicate in the spec §4.3 per-project round robin."""
    eligible = (
        _eligible_query(now)
        .where(
            knowledge_claims.c.fact_name.in_(eligible_fact_names),
            knowledge_claims.c.fact_name != "dream_last_night",
        )
        .subquery("eligible")
    )
    rank = (
        sa.func.row_number()
        .over(partition_by=eligible.c.project_key, order_by=(eligible.c.age_key, eligible.c.seq))
        .label("rank")
    )
    ranked = sa.select(eligible, rank).subquery("ranked")
    return (
        sa.select(ranked).order_by(ranked.c.rank, ranked.c.age_key, ranked.c.seq).limit(max_claims)
    )


def _self_referential_count_query(
    now: datetime, *, eligible_fact_names: Sequence[str]
) -> sa.Select:
    """Count self-referential claims that the uncapped selector would have admitted."""
    eligible = (
        _eligible_query(now)
        .where(
            knowledge_claims.c.fact_name.in_(eligible_fact_names),
            knowledge_claims.c.fact_name == "dream_last_night",
        )
        .subquery("eligible")
    )
    return sa.select(sa.func.count()).select_from(eligible)


def _nightly_claim(row: sa.RowMapping) -> NightlyClaim:
    return NightlyClaim(
        id=row["id"],
        project_key=row["project_key"],
        fact_name=row["fact_name"],
        target=row["target"],
        definition_version=row["definition_version"],
        validity_seconds=row["validity_seconds"],
        seq=row["seq"],
        age_key=row["age_key"],
    )


async def select_nightly_claims(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    max_claims: int,
    eligible_fact_names: Sequence[str],
) -> tuple[NightlyClaim, ...]:
    """One short read-only transaction, separate from the verifications (spec §4.4)."""
    async with session_factory() as session, session.begin():
        await session.execute(sa.text("SET TRANSACTION READ ONLY"))
        rows = (
            (
                await session.execute(
                    _ranked_query(now, max_claims, eligible_fact_names=eligible_fact_names)
                )
            )
            .mappings()
            .all()
        )
    return tuple(_nightly_claim(row) for row in rows)


async def count_self_referential_claims(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    eligible_fact_names: Sequence[str],
) -> int:
    """Read the uncapped skipped count without locking claims or writing verdicts."""
    async with session_factory() as session, session.begin():
        await session.execute(sa.text("SET TRANSACTION READ ONLY"))
        count = await session.scalar(
            _self_referential_count_query(now, eligible_fact_names=eligible_fact_names)
        )
    return int(count or 0)


async def eligible_fact_names(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> tuple[str, ...]:
    """The distinct `fact_name` of every eligible claim, ignoring the cap (spec §6.1 step 3)."""
    query = sa.select(_eligible_query(now).subquery("eligible").c.fact_name).distinct()
    async with session_factory() as session, session.begin():
        await session.execute(sa.text("SET TRANSACTION READ ONLY"))
        names = (await session.execute(query)).scalars().all()
    return tuple(sorted(set(names)))


#: Above 2**32 (a `pg_locks` classid/objid pair, not an int4 `hashtext()` key)
#: and in its own `7_311_042_xxx` family, distinct from the delivery observer's
#: `7_311_041_xxx` one (plan D5). A collision with a transaction-scoped
#: `hashtextextended` key (migration 033, full int8 range) is possible only by
#: hash chance and costs at most one spurious "busy" (rc 6), never a wrong
#: write. `tests/unit/repositories/test_claim_verify_lock_key.py` re-scans
#: `src/` so a newly added constant cannot collide with this one in silence.
CLAIM_VERIFY_RUN_LOCK: Final[int] = 7_311_042_001


class VerifyRunOwnershipLost(RuntimeError):
    """Stable, redacted fatal error: this process must not write the run row again."""

    def __init__(self) -> None:
        super().__init__("claim_verify_ownership_lost")


class VerifyRunOwnership:
    """One dedicated connection; every run-row write and every dispatch check share it.

    Modelled on `delivery_observer.ownership.ObserverOwnership` (spec §5.1,
    plan §2) but independent of it: no import from `delivery_observer`, and a
    distinct lock key and error message. A session advisory lock survives the
    short acquisition transaction; a lost backend is permanently fenced, and
    only a new process may acquire again.
    """

    LOCK_KEY: Final[int] = CLAIM_VERIFY_RUN_LOCK

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: AsyncConnection | None = None
        self._backend_pid: int | None = None
        self._state: Literal["new", "owned", "lost", "released"] = "new"
        self._mutex = asyncio.Lock()
        self.lost = asyncio.Event()

    @property
    def backend_pid(self) -> int | None:
        return self._backend_pid

    @property
    def owned(self) -> bool:
        return self._state == "owned"

    def _lose(self) -> None:
        self._state = "lost"
        self.lost.set()

    @staticmethod
    async def _dispose(connection: AsyncConnection) -> None:
        with suppress(SQLAlchemyError, InternalClientError):
            await connection.invalidate()
        with suppress(SQLAlchemyError, InternalClientError):
            await connection.close()

    async def acquire(self) -> bool:
        async with self._mutex:
            if self._state != "new":
                raise VerifyRunOwnershipLost()
            connection: AsyncConnection | None = None
            try:
                connection = await self._engine.connect()
                connection = await connection.execution_options(isolation_level="READ COMMITTED")
                async with connection.begin():
                    backend_pid = await connection.scalar(sa.text("SELECT pg_backend_pid()"))
                    acquired = await connection.scalar(
                        sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": self.LOCK_KEY}
                    )
                if not acquired:
                    self._state = "released"
                    await self._dispose(connection)
                    return False
                if not isinstance(backend_pid, int):
                    raise VerifyRunOwnershipLost()
                self._connection, self._backend_pid = connection, backend_pid
                self._state = "owned"
                return True
            except BaseException as error:
                self._lose()
                if connection is not None:
                    await self._dispose(connection)
                if isinstance(error, (SQLAlchemyError, InternalClientError)):
                    raise VerifyRunOwnershipLost() from None
                raise

    async def _verify_locked(self, connection: AsyncConnection) -> None:
        """The ownership proof itself; the caller must already hold an open transaction."""
        if not self.owned or connection.invalidated or connection.closed:
            self._lose()
            raise VerifyRunOwnershipLost()
        try:
            async with asyncio.timeout(5):
                row = (
                    await connection.execute(
                        sa.text(
                            "SELECT pg_backend_pid() AS backend_pid, EXISTS ("
                            "SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted "
                            "AND pid=pg_backend_pid() AND objsubid=1 "
                            "AND classid=((CAST(:key AS bigint) >> 32) & 4294967295)::oid "
                            "AND objid=(CAST(:key AS bigint) & 4294967295)::oid) AS locked"
                        ),
                        {"key": self.LOCK_KEY},
                    )
                ).one()
            if row.backend_pid != self._backend_pid or not row.locked:
                raise VerifyRunOwnershipLost()
        except (SQLAlchemyError, InternalClientError, TimeoutError, VerifyRunOwnershipLost):
            self._lose()
            raise VerifyRunOwnershipLost() from None

    async def check(self) -> None:
        """Standalone ownership check, in its own short transaction (spec §5.1, §6.2).

        Used before each verification dispatch and anywhere else ownership must
        be re-proven without also writing the run row.
        """
        async with self._mutex:
            if not self.owned or self._connection is None:
                self._lose()
                raise VerifyRunOwnershipLost()
            connection = self._connection
            if connection.invalidated or connection.closed:
                self._lose()
                raise VerifyRunOwnershipLost()
            try:
                async with connection.begin():
                    await self._verify_locked(connection)
            except (SQLAlchemyError, InternalClientError):
                self._lose()
                raise VerifyRunOwnershipLost() from None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield the sole writer session; a run-row write commits only with retained ownership."""
        async with self._mutex:
            if not self.owned or self._connection is None:
                raise VerifyRunOwnershipLost()
            connection = self._connection
            try:
                if connection.invalidated or connection.closed:
                    self._lose()
                    raise VerifyRunOwnershipLost()
                async with connection.begin():
                    await self._verify_locked(connection)
                    async with AsyncSession(
                        bind=connection,
                        expire_on_commit=False,
                        join_transaction_mode="rollback_only",
                    ) as session:
                        async with session.begin():
                            yield session
                        await self._verify_locked(connection)
            except (SQLAlchemyError, InternalClientError):
                self._lose()
                raise VerifyRunOwnershipLost() from None
            except BaseException:
                if connection.invalidated or connection.closed:
                    self._lose()
                raise

    async def release(self) -> None:
        """Idempotently close the dedicated backend; a released owner cannot restart."""
        async with self._mutex:
            connection, self._connection = self._connection, None
            try:
                if connection is not None and self.owned:
                    with suppress(
                        SQLAlchemyError, InternalClientError, VerifyRunOwnershipLost, TimeoutError
                    ):
                        async with connection.begin():
                            await self._verify_locked(connection)
                            unlocked = await connection.scalar(
                                sa.text("SELECT pg_advisory_unlock(:key)"), {"key": self.LOCK_KEY}
                            )
                            if not unlocked:
                                self._lose()
            finally:
                if not self.lost.is_set():
                    self._state = "released"
                if connection is not None:
                    cleanup = asyncio.create_task(self._dispose(connection))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise


def _existing_wet_run_query(run_date: date) -> sa.Select:
    return (
        sa.select(dream_runs.c.id)
        .where(
            dream_runs.c.run_date == run_date,
            dream_runs.c.phase == _VERIFY_PHASE,
            dream_runs.c.project_key == GLOBAL_PHASE_PROJECT_KEY,
            dream_runs.c.phase_dry_run.is_(False),
        )
        .order_by(dream_runs.c.id)
        .limit(1)
    )


async def get_or_create_wet_run(ownership: VerifyRunOwnership, run_date: date) -> int:
    """Fenced get-or-create: a rerun for the same date gets the SAME run id (spec §5.1)."""
    async with ownership.transaction() as session:
        existing = (await session.execute(_existing_wet_run_query(run_date))).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        return int(
            (
                await session.execute(
                    sa.insert(dream_runs)
                    .values(
                        run_date=run_date,
                        phase=_VERIFY_PHASE,
                        project_key=GLOBAL_PHASE_PROJECT_KEY,
                        phase_dry_run=False,
                        model=None,
                        status=_INITIAL_STATUS,
                        error_message=_INITIAL_ERROR_MESSAGE,
                    )
                    .returning(dream_runs.c.id)
                )
            ).scalar_one()
        )


async def finish_run(
    ownership: VerifyRunOwnership,
    run_id: int,
    *,
    status: str,
    duration_s: float,
    error_message: str | None,
) -> None:
    """Fenced final status update (spec §6.6); rolled back if ownership was lost mid-write."""
    async with ownership.transaction() as session:
        await session.execute(
            sa.update(dream_runs)
            .where(dream_runs.c.id == run_id)
            .values(status=status, duration_s=duration_s, error_message=error_message)
        )


async def insert_dry_run(session_factory: async_sessionmaker[AsyncSession], run_date: date) -> int:
    """Unfenced (D2): a dry run takes no lock and always inserts a NEW row."""
    async with session_factory() as session, session.begin():
        return int(
            (
                await session.execute(
                    sa.insert(dream_runs)
                    .values(
                        run_date=run_date,
                        phase=_VERIFY_PHASE,
                        project_key=GLOBAL_PHASE_PROJECT_KEY,
                        phase_dry_run=True,
                        model=None,
                        status=_INITIAL_STATUS,
                        error_message=_INITIAL_ERROR_MESSAGE,
                    )
                    .returning(dream_runs.c.id)
                )
            ).scalar_one()
        )


async def finish_dry_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: int,
    *,
    status: str,
    duration_s: float,
    error_message: str | None,
) -> None:
    """Unfenced (D2): no lock, no ownership fence -- symmetric with `insert_dry_run`."""
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.update(dream_runs)
            .where(dream_runs.c.id == run_id)
            .values(status=status, duration_s=duration_s, error_message=error_message)
        )
