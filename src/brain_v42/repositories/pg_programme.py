"""Programme progress in PostgreSQL: authority, caps, transitions and the published shape.

Contract `docs/contracts/programmes_v1.md` §5 and §6 (tickets 8a063256, 24c1f172).

- **Authority.** The project that creates a programme coordinates it: only it
  changes the programme and the structure of its gates. A gate's owner and the
  coordinator report the gate and raise or lift its request. `caller` is the
  project the credential layer resolved; it reaches this module from the service,
  never from a tool argument, and it signs `updated_by` and `last_change.by`.
- **One writer at a time.** Every write takes one transaction-scoped advisory lock.
  Programmes change a few times an hour; serialising them makes every cap (live
  programmes, gates, bytes) a plain read inside the same transaction, with no race.
- **Refuse, never cut.** A write that would break a cap fails before COMMIT, so
  nothing of it is stored; the byte cap is measured on the programme exactly as
  the sidecar would publish it.
- **Server time.** `updated_at`, `waiting_since` and `last_change_at` are the
  transaction's `now()`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from brain_v42.db.tables import programme_gates, programmes, project_contexts
from brain_v42.models.programme import (
    DEPENDS_ON_MAX,
    DONE_PROGRAMMES_PUBLISHED,
    DONE_PUBLISHED_DAYS,
    GATES_MAX,
    LIVE_PER_COORDINATOR_MAX,
    LIVE_PROGRAMMES_MAX,
    POSITION_MAX,
    PROGRAMME_BYTES_MAX,
    PROGRAMMES_PER_COORDINATOR_MAX,
    ProgrammeError,
    acyclic_or_refuse,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

VERSION: Final = "programmes/v1"
#: One lock for every programme write (see the module docstring).
_WRITE_LOCK: Final = sa.text("SELECT pg_advisory_xact_lock(hashtext('brain_v42.programmes'))")
_LIVE: Final = ("active", "paused")
#: A result row read by column name (`RowMapping` and plain mappings alike).
Row = Mapping[Any, Any]


def _instant(value: datetime) -> str:
    """RFC 3339 in UTC, offset `+00:00` (contract §4)."""
    return value.astimezone(UTC).isoformat()


def published_gate(row: Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "title": row["title"],
        "owner": row["owner_project"],
        "state": row["state"],
        "depends_on": list(row["depends_on"]),
        "weight": row["weight"],
        "waiting_on_operator": (
            None
            if row["waiting_ask"] is None
            else {"ask": row["waiting_ask"], "since": _instant(row["waiting_since"])}
        ),
        "progress": (
            None
            if row["progress_total"] is None
            else {
                "done": row["progress_done"],
                "total": row["progress_total"],
                "unit": row["progress_unit"],
            }
        ),
        "note": row["note"],
        "updated_at": _instant(row["updated_at"]),
        "updated_by": row["updated_by"],
    }


def published_programme(row: Row, gates: Sequence[Row]) -> dict[str, Any]:
    """One programme exactly as contract §2 publishes it, every key always present."""
    return {
        "key": row["key"],
        "title": row["title"],
        "coordinator": row["coordinator_project"],
        "state": row["state"],
        "last_change": (
            None
            if row["last_change_gate"] is None
            else {
                "gate": row["last_change_gate"],
                "from": row["last_change_from"],
                "to": row["last_change_to"],
                "at": _instant(row["last_change_at"]),
                "by": row["last_change_by"],
            }
        ),
        "gates": [published_gate(gate) for gate in gates],
    }


_PROGRAMME_ORDER: Final = (programmes.c.position, programmes.c.created_at, programmes.c.key)
_GATE_ORDER: Final = (
    programme_gates.c.position,
    programme_gates.c.created_at,
    programme_gates.c.key,
)


async def _one_snapshot(session: AsyncSession) -> None:
    """Read a programme and its gates from one snapshot (review round 2 of #377).

    Under READ COMMITTED each statement sees its own snapshot: a report committed
    between the programme read and the gate read would publish a gate already moved
    beside the programme's previous `last_change`. This must be the transaction's
    first statement.
    """
    await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


class PgProgrammeRepo:
    """Every programme read and write; the limits default to the contract's."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_live: int = LIVE_PROGRAMMES_MAX,
        max_gates: int = GATES_MAX,
        max_bytes: int = PROGRAMME_BYTES_MAX,
        max_live_per_coordinator: int = LIVE_PER_COORDINATOR_MAX,
        max_programmes_per_coordinator: int = PROGRAMMES_PER_COORDINATOR_MAX,
    ) -> None:
        self._session_factory = session_factory
        self._max_live = max_live
        self._max_live_per_coordinator = max_live_per_coordinator
        self._max_programmes_per_coordinator = max_programmes_per_coordinator
        self._max_gates = max_gates
        self._max_bytes = max_bytes

    # -- reads -------------------------------------------------------------------

    async def list_programmes(
        self, *, include_done: bool = False, key: str | None = None
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            await _one_snapshot(session)
            query = sa.select(programmes).order_by(*_PROGRAMME_ORDER)
            if key is not None:
                query = query.where(programmes.c.key == key)
            elif not include_done:
                query = query.where(programmes.c.state.in_(_LIVE))
            rows = (await session.execute(query)).mappings().all()
            return [await self._published(session, row) for row in rows]

    async def published_block(self) -> dict[str, Any]:
        """The sidecar block without `generated_at` (the memo stamps it, contract §4).

        Every live programme, plus the done ones updated in the last seven days, the
        four most recent at most; the whole selection in brain's order (contract §5:
        position, then creation, then key, whatever the state).
        """
        async with self._session_factory() as session:
            await _one_snapshot(session)
            # The window is the database's clock, like every timestamp it compares to.
            now = await session.scalar(sa.select(sa.func.now()))
            assert isinstance(now, datetime)
            recent_done = (
                sa.select(programmes.c.id)
                .where(
                    programmes.c.state == "done",
                    programmes.c.updated_at > now - timedelta(days=DONE_PUBLISHED_DAYS),
                )
                .order_by(programmes.c.updated_at.desc(), programmes.c.key)
                .limit(DONE_PROGRAMMES_PUBLISHED)
            )
            rows = (
                (
                    await session.execute(
                        sa.select(programmes)
                        .where(
                            sa.or_(programmes.c.state.in_(_LIVE), programmes.c.id.in_(recent_done))
                        )
                        .order_by(*_PROGRAMME_ORDER)
                    )
                )
                .mappings()
                .all()
            )
            return {
                "version": VERSION,
                "programmes": [await self._published(session, row) for row in rows],
            }

    # -- writes ------------------------------------------------------------------

    async def upsert_programme(
        self,
        *,
        caller: str,
        key: str,
        title: str | None = None,
        state: str | None = None,
        position: int | None = None,
    ) -> dict[str, Any]:
        async with self._session_factory.begin() as session:
            await session.execute(_WRITE_LOCK)
            row = await self._programme(session, key, lock=True)
            if row is None:
                if title is None:
                    raise ProgrammeError("programme_incomplete", "a new programme needs a title")
                new_state = state or "active"
                await self._coordinator_total_cap(session, caller)
                if new_state in _LIVE:
                    await self._live_cap(session, caller)
                await session.execute(
                    sa.insert(programmes).values(
                        key=key,
                        title=title,
                        coordinator_project=caller,
                        state=new_state,
                        position=(
                            position
                            if position is not None
                            else await self._next_position(session, None)
                        ),
                        updated_by=caller,
                    )
                )
            else:
                self._coordinator(row, caller)
                if state in _LIVE and row["state"] not in _LIVE:
                    await self._live_cap(session, row["coordinator_project"])
                values: dict[str, Any] = {"updated_at": sa.func.now(), "updated_by": caller}
                for column, value in (("title", title), ("state", state), ("position", position)):
                    if value is not None:
                        values[column] = value
                await session.execute(
                    sa.update(programmes).where(programmes.c.id == row["id"]).values(**values)
                )
            return await self._checked(session, key)

    async def upsert_gate(
        self,
        *,
        caller: str,
        programme: str,
        gate: str,
        title: str | None = None,
        owner: str | None = None,
        depends_on: Sequence[str] | None = None,
        weight: int | None = None,
        position: int | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Create a gate or change its structure; the coordinator's alone (contract §6)."""
        async with self._session_factory.begin() as session:
            await session.execute(_WRITE_LOCK)
            row = await self._existing_programme(session, programme)
            self._coordinator(row, caller)
            existing = await self._gate(session, row["id"], gate)
            if owner is not None:
                await self._known_project(session, owner)
            if depends_on is not None:
                await self._dependencies(session, row["id"], gate, list(depends_on))
            if existing is None:
                if title is None or owner is None:
                    raise ProgrammeError("gate_incomplete", "a new gate needs a title and an owner")
                count = await session.scalar(
                    sa.select(sa.func.count()).where(programme_gates.c.programme_id == row["id"])
                )
                if (count or 0) >= self._max_gates:
                    raise ProgrammeError(
                        "gate_limit_reached",
                        f"a programme holds at most {self._max_gates} gates",
                    )
                await session.execute(
                    sa.insert(programme_gates).values(
                        programme_id=row["id"],
                        key=gate,
                        title=title,
                        owner_project=owner,
                        state=state or "todo",
                        depends_on=list(depends_on or []),
                        weight=weight if weight is not None else 1,
                        position=(
                            position
                            if position is not None
                            else await self._next_position(session, row["id"])
                        ),
                        updated_by=caller,
                    )
                )
            else:
                values: dict[str, Any] = {}
                for column, value in (
                    ("title", title),
                    ("owner_project", owner),
                    ("weight", weight),
                    ("position", position),
                ):
                    if value is not None:
                        values[column] = value
                if depends_on is not None:
                    values["depends_on"] = list(depends_on)
                await self._write_gate(session, row, existing, caller, state, values)
            await self._touch(session, row["id"], caller)
            return await self._checked(session, programme)

    async def report_gate(
        self,
        *,
        caller: str,
        programme: str,
        gate: str,
        state: str | None = None,
        progress: Mapping[str, Any] | None = None,
        clear_progress: bool = False,
        note: str | None = None,
        clear_note: bool = False,
    ) -> dict[str, Any]:
        """Declare a gate's state, progress or note: its owner or the coordinator."""
        async with self._session_factory.begin() as session:
            await session.execute(_WRITE_LOCK)
            row = await self._existing_programme(session, programme)
            existing = await self._existing_gate(session, row["id"], gate)
            self._owner_or_coordinator(row, existing, caller)
            values: dict[str, Any] = {}
            if clear_progress:
                values.update(progress_done=None, progress_total=None, progress_unit=None)
            elif progress is not None:
                values.update(
                    progress_done=progress["done"],
                    progress_total=progress["total"],
                    progress_unit=progress["unit"],
                )
            if clear_note:
                values["note"] = None
            elif note is not None:
                values["note"] = note
            await self._write_gate(session, row, existing, caller, state, values)
            await self._touch(session, row["id"], caller)
            return await self._checked(session, programme)

    async def request(
        self, *, caller: str, programme: str, gate: str, ask: str | None
    ) -> dict[str, Any]:
        """Raise (`ask`) or lift (`None`) a gate's request to the operator.

        The same ask raised again keeps its `since`; a different ask is a new request.
        """
        async with self._session_factory.begin() as session:
            await session.execute(_WRITE_LOCK)
            row = await self._existing_programme(session, programme)
            existing = await self._existing_gate(session, row["id"], gate)
            self._owner_or_coordinator(row, existing, caller)
            if ask is None:
                values: dict[str, Any] = {"waiting_ask": None, "waiting_since": None}
            elif ask == existing["waiting_ask"]:
                values = {}
            else:
                values = {"waiting_ask": ask, "waiting_since": sa.func.now()}
            await self._write_gate(session, row, existing, caller, None, values)
            await self._touch(session, row["id"], caller)
            return await self._checked(session, programme)

    # -- helpers -----------------------------------------------------------------

    async def _write_gate(
        self,
        session: AsyncSession,
        programme_row: Row,
        gate_row: Row,
        caller: str,
        state: str | None,
        values: dict[str, Any],
    ) -> None:
        """Every accepted gate write refreshes its signature; a state change is recorded."""
        if state is not None:
            values["state"] = state
        values.update(updated_at=sa.func.now(), updated_by=caller)
        await session.execute(
            sa.update(programme_gates)
            .where(programme_gates.c.id == gate_row["id"])
            .values(**values)
        )
        if state is not None and state != gate_row["state"]:
            await session.execute(
                sa.update(programmes)
                .where(programmes.c.id == programme_row["id"])
                .values(
                    last_change_gate=gate_row["key"],
                    last_change_from=gate_row["state"],
                    last_change_to=state,
                    last_change_at=sa.func.now(),
                    last_change_by=caller,
                )
            )

    async def _touch(self, session: AsyncSession, programme_id: Any, caller: str) -> None:
        await session.execute(
            sa.update(programmes)
            .where(programmes.c.id == programme_id)
            .values(updated_at=sa.func.now(), updated_by=caller)
        )

    async def _checked(self, session: AsyncSession, key: str) -> dict[str, Any]:
        """The programme as published, refused past the byte cap before COMMIT."""
        row = await self._programme(session, key, lock=False)
        assert row is not None  # written in this very transaction
        published = await self._published(session, row)
        size = len(json.dumps(published).encode())
        if size > self._max_bytes:
            raise ProgrammeError(
                "programme_too_large",
                f"the programme would publish {size} bytes; the cap is {self._max_bytes}",
            )
        return published

    async def _published(self, session: AsyncSession, row: Row) -> dict[str, Any]:
        gates = (
            (
                await session.execute(
                    sa.select(programme_gates)
                    .where(programme_gates.c.programme_id == row["id"])
                    .order_by(*_GATE_ORDER)
                )
            )
            .mappings()
            .all()
        )
        return published_programme(row, gates)

    async def _programme(self, session: AsyncSession, key: str, *, lock: bool) -> Row | None:
        query = sa.select(programmes).where(programmes.c.key == key)
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).mappings().one_or_none()

    async def _existing_programme(self, session: AsyncSession, key: str) -> Row:
        row = await self._programme(session, key, lock=True)
        if row is None:
            raise ProgrammeError("programme_not_found", f"no programme has the key {key}")
        return row

    async def _gate(self, session: AsyncSession, programme_id: Any, key: str) -> Row | None:
        return (
            (
                await session.execute(
                    sa.select(programme_gates).where(
                        programme_gates.c.programme_id == programme_id,
                        programme_gates.c.key == key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _existing_gate(self, session: AsyncSession, programme_id: Any, key: str) -> Row:
        row = await self._gate(session, programme_id, key)
        if row is None:
            raise ProgrammeError("gate_not_found", f"the programme has no gate {key}")
        return row

    @staticmethod
    def _coordinator(row: Row, caller: str) -> None:
        if row["coordinator_project"] != caller:
            raise ProgrammeError(
                "not_coordinator",
                f"only the coordinating project ({row['coordinator_project']}) may do this",
            )

    @staticmethod
    def _owner_or_coordinator(programme_row: Row, gate_row: Row, caller: str) -> None:
        if caller not in (gate_row["owner_project"], programme_row["coordinator_project"]):
            raise ProgrammeError(
                "not_gate_owner",
                f"only the gate's owner ({gate_row['owner_project']}) or the coordinating "
                f"project ({programme_row['coordinator_project']}) may do this",
            )

    async def _live_cap(self, session: AsyncSession, coordinator: str) -> None:
        live = await session.scalar(sa.select(sa.func.count()).where(programmes.c.state.in_(_LIVE)))
        if (live or 0) >= self._max_live:
            raise ProgrammeError(
                "programme_limit_reached",
                f"at most {self._max_live} programmes may be active or paused; mark one done",
            )
        mine = await session.scalar(
            sa.select(sa.func.count()).where(
                programmes.c.state.in_(_LIVE), programmes.c.coordinator_project == coordinator
            )
        )
        if (mine or 0) >= self._max_live_per_coordinator:
            raise ProgrammeError(
                "coordinator_limit_reached",
                f"one project coordinates at most {self._max_live_per_coordinator} active or "
                "paused programmes; mark one done",
            )

    async def _coordinator_total_cap(self, session: AsyncSession, coordinator: str) -> None:
        total = await session.scalar(
            sa.select(sa.func.count()).where(programmes.c.coordinator_project == coordinator)
        )
        if (total or 0) >= self._max_programmes_per_coordinator:
            raise ProgrammeError(
                "coordinator_limit_reached",
                f"one project coordinates at most {self._max_programmes_per_coordinator} "
                "programmes in any state",
            )

    async def _known_project(self, session: AsyncSession, project: str) -> None:
        found = await session.scalar(
            sa.select(sa.literal(True)).where(project_contexts.c.project_key == project)
        )
        if not found:
            raise ProgrammeError("owner_unknown", f"no project has the key {project}")

    async def _dependencies(
        self, session: AsyncSession, programme_id: Any, gate: str, depends_on: list[str]
    ) -> None:
        if len(depends_on) > DEPENDS_ON_MAX or len(set(depends_on)) != len(depends_on):
            raise ProgrammeError(
                "invalid_dependencies",
                f"depends_on holds at most {DEPENDS_ON_MAX} distinct gate keys",
            )
        rows = (
            await session.execute(
                sa.select(programme_gates.c.key, programme_gates.c.depends_on).where(
                    programme_gates.c.programme_id == programme_id
                )
            )
        ).all()
        graph = {key: list(dependencies) for key, dependencies in rows}
        unknown = [key for key in depends_on if key not in graph and key != gate]
        if unknown:
            raise ProgrammeError(
                "unknown_dependency", f"depends_on names gates the programme lacks: {unknown}"
            )
        graph[gate] = depends_on
        acyclic_or_refuse(graph)

    async def _next_position(self, session: AsyncSession, programme_id: Any | None) -> int:
        if programme_id is None:
            current = await session.scalar(sa.select(sa.func.max(programmes.c.position)))
        else:
            current = await session.scalar(
                sa.select(sa.func.max(programme_gates.c.position)).where(
                    programme_gates.c.programme_id == programme_id
                )
            )
        return 0 if current is None else min(int(current) + 1, POSITION_MAX)
