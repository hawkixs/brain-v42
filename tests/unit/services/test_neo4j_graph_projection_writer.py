"""RED contracts for the fenced Neo4j graph projection writer.

The writer is intentionally specified independently from the PostgreSQL lease
repository.  Projection claims carry the event plus the leadership and claim
tokens that Neo4j must enforce at commit time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from brain_v42.services.neo4j_graph_projection_writer import (
    Neo4jGraphProjectionWriter,
    ProjectionActivation,
    ProjectionOutcome,
)


@dataclass(frozen=True, slots=True)
class _ClaimedEvent:
    event_id: UUID
    operation: str
    aggregate_revision: int
    entity_id: UUID | None = None
    entity_type: str | None = None
    entity_key: str | None = None
    source_uuid: UUID | None = None
    project_key: str | None = None
    display_label: str | None = None
    lifecycle: str | None = None
    relation_id: UUID | None = None
    source_type: str | None = None
    source_key: str | None = None
    target_type: str | None = None
    target_key: str | None = None
    relation_type: str | None = None
    relation_lifecycle: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ProjectionClaim:
    event: _ClaimedEvent
    owner_id: str
    lease_generation: int
    claim_version: int
    leased_until: datetime


@dataclass(frozen=True, slots=True)
class _Cursor:
    revision: int
    claim_version: int
    event_id: UUID
    operation: str


@dataclass(slots=True)
class _Neo4jState:
    fence_generation: int = 0
    fence_owner_id: str | None = None
    auto_seed_predecessor: bool = True
    activation_attempts: int = 0
    cursors: dict[str, _Cursor] = field(default_factory=dict)
    entity_keys: set[str] = field(default_factory=set)
    relation_keys: set[str] = field(default_factory=set)
    domain_keys: set[str] = field(default_factory=set)
    transactions: list[_ExplicitTransaction] = field(default_factory=list)
    implicit_runs: int = 0
    missing_anchor_keys: set[str] = field(default_factory=set)
    cancel_on_run: bool = False
    run_error: BaseException | None = None
    cancel_on_rollback: bool = False
    commit_error: BaseException | None = None
    missing_mutation_record: bool = False
    missing_mutation_anchors: bool = False


def _query_text(query: Any) -> str:
    return str(getattr(query, "text", query))


class _Result:
    def __init__(self, record: dict[str, Any] | None) -> None:
        self._record = record

    async def single(self, strict: bool = False) -> dict[str, Any] | None:  # noqa: ARG002
        return None if self._record is None else dict(self._record)

    async def consume(self) -> Any:
        return SimpleNamespace(counters=SimpleNamespace())

    def __aiter__(self):  # noqa: ANN204
        async def iterate():
            if self._record:
                yield dict(self._record)

        return iterate()


class _ExplicitTransaction:
    """Small stateful double for one Neo4j explicit write transaction."""

    def __init__(self, state: _Neo4jState) -> None:
        self._state = state
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.parameters: dict[str, Any] = {}
        self.committed = False
        self.rolled_back = False
        self.cancelled = False
        self._is_closed = False
        self._observed_outcome: str | None = None

    async def run(
        self,
        query: Any,
        parameters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _Result:
        if self._state.cancel_on_run:
            raise asyncio.CancelledError
        if self._state.run_error is not None:
            raise self._state.run_error
        params = dict(parameters or {})
        params.update(kwargs)
        text = _query_text(query)
        self.queries.append((text, params))
        self.parameters.update(params)
        record = self._record_for_current_state(text)
        system_query = "BrainProjectionFence" in text or "BrainProjectionCursor" in text
        if not system_query and self._state.missing_mutation_record:
            return _Result(None)
        if not system_query and self._state.missing_mutation_anchors:
            record.pop("anchors", None)
        return _Result(record)

    def _record_for_current_state(self, query: str) -> dict[str, Any]:
        generation = self.parameters.get(
            "lease_generation",
            self.parameters.get("generation"),
        )
        aggregate_key = self.parameters.get("aggregate_key")
        if aggregate_key is None:
            owner_id = self.parameters.get("owner_id")
            requested_generation = int(generation or 0)
            if (
                self._state.auto_seed_predecessor
                and self._state.activation_attempts == 0
                and self._state.fence_generation == 0
                and requested_generation > 1
            ):
                # Most projection tests start at an arbitrary generation. Seed
                # the immediately preceding durable fence as fixture setup.
                self._state.fence_generation = requested_generation - 1
            self._state.activation_attempts += 1
            allow_advance = bool(self.parameters.get("allow_advance"))
            accepted = generation is not None and (
                (allow_advance and requested_generation == self._state.fence_generation + 1)
                or (
                    requested_generation == self._state.fence_generation
                    and self._state.fence_owner_id in {None, owner_id}
                )
            )
            current = requested_generation if accepted else self._state.fence_generation
            self._observed_outcome = "activated" if accepted else "stale_generation"
            return {
                "accepted": accepted,
                "activated": accepted,
                "generation": current,
                "current_generation": current,
                "status": self._observed_outcome,
            }

        if (
            generation != self._state.fence_generation
            or self.parameters.get("owner_id") != self._state.fence_owner_id
        ):
            self._observed_outcome = "stale_generation"
            return self._projection_record("stale_generation")

        revision = int(self.parameters["aggregate_revision"])
        claim_version = int(self.parameters["claim_version"])
        event_id = UUID(str(self.parameters["event_id"]))
        cursor = self._state.cursors.get(str(aggregate_key))
        if cursor is None or revision > cursor.revision:
            self._observed_outcome = "applied"
        elif revision < cursor.revision:
            self._observed_outcome = "superseded"
        elif event_id != cursor.event_id:
            self._observed_outcome = "conflict"
        elif claim_version < cursor.claim_version:
            self._observed_outcome = "superseded"
        else:
            self._observed_outcome = "already_current"
        record = self._projection_record(self._observed_outcome)
        system_query = "BrainProjectionFence" in query or "BrainProjectionCursor" in query
        if not system_query and str(aggregate_key) in self._state.missing_anchor_keys:
            record["anchors"] = 0
        return record

    def _projection_record(self, status: str) -> dict[str, Any]:
        return {
            "accepted": status != "stale_generation",
            "status": status,
            "outcome": status,
            "anchors": 1,
            "current_generation": self._state.fence_generation,
        }

    async def commit(self) -> None:
        if self._state.commit_error is not None:
            self._is_closed = True
            raise self._state.commit_error
        if self._observed_outcome == "stale_generation":
            raise AssertionError("a stale generation transaction must be rolled back")
        aggregate_key = self.parameters.get("aggregate_key")
        if aggregate_key is None:
            raw_generation = self.parameters.get("lease_generation")
            if raw_generation is None:
                raw_generation = self.parameters["generation"]
            generation = int(raw_generation)
            self._state.fence_generation = generation
            self._state.fence_owner_id = str(self.parameters["owner_id"])
            self.committed = True
            self._is_closed = True
            return

        if self._observed_outcome in {"applied", "already_current"}:
            key = str(aggregate_key)
            operation = str(self.parameters["operation"])
            if self._observed_outcome == "applied":
                if operation == "upsert_entity":
                    self._state.entity_keys.add(key)
                elif operation == "delete_entity":
                    self._state.entity_keys.discard(key)
                elif operation == "upsert_relation":
                    self._state.relation_keys.add(key)
                elif operation == "delete_relation":
                    self._state.relation_keys.discard(key)
                text = _transaction_text(self)
                if "MERGE (node:Domain {name: $entity_key})" in text:
                    self._state.domain_keys.add(str(self.parameters["entity_key"]))
                if "MERGE (target:Domain {name: $target_key})" in text:
                    self._state.domain_keys.add(str(self.parameters["target_key"]))
            self._state.cursors[key] = _Cursor(
                revision=int(self.parameters["aggregate_revision"]),
                claim_version=int(self.parameters["claim_version"]),
                event_id=UUID(str(self.parameters["event_id"])),
                operation=operation,
            )
        self.committed = True
        self._is_closed = True

    async def rollback(self) -> None:
        if self._state.cancel_on_rollback:
            raise asyncio.CancelledError
        if self.closed():
            raise AssertionError("a closed transaction must not be rolled back")
        self.rolled_back = True
        self._is_closed = True

    def cancel(self) -> None:
        self.cancelled = True
        self._is_closed = True

    def closed(self) -> bool:
        return self._is_closed

    async def __aenter__(self) -> _ExplicitTransaction:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None or not self.committed:
            await self.rollback()
        return False


class _Session:
    def __init__(self, state: _Neo4jState) -> None:
        self._state = state

    async def begin_transaction(self, **kwargs: Any) -> _ExplicitTransaction:  # noqa: ARG002
        transaction = _ExplicitTransaction(self._state)
        self._state.transactions.append(transaction)
        return transaction

    async def run(self, *_args: Any, **_kwargs: Any) -> None:
        self._state.implicit_runs += 1
        raise AssertionError("fenced projection must use an explicit transaction")


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False


class _Driver:
    def __init__(self) -> None:
        self.state = _Neo4jState()

    def session(self, **kwargs: Any) -> _SessionContext:  # noqa: ARG002
        return _SessionContext(_Session(self.state))


def _leadership(
    generation: int,
    owner: str = "worker-a",
    *,
    armed: bool = False,
) -> Any:
    return SimpleNamespace(
        owner_id=owner,
        generation=generation,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
        armed=armed,
    )


def _entity_claim(
    *,
    generation: int,
    revision: int,
    operation: str = "upsert_entity",
    claim_version: int = 1,
    event_id: UUID | None = None,
    entity_id: UUID | None = None,
    entity_type: str = "decision",
    entity_key: str | None = None,
    source_uuid: UUID | None = None,
    lifecycle: str | None = None,
    display_label: str = "Fenced projection",
    project_key: str | None = "brain-v42",
    owner_id: str = "worker-a",
) -> _ProjectionClaim:
    entity_id = entity_id or uuid4()
    entity_key = entity_key or str(entity_id)
    source_uuid = entity_id if source_uuid is None else source_uuid
    lifecycle = lifecycle or ("deleted" if operation == "delete_entity" else "active")
    return _ProjectionClaim(
        event=_ClaimedEvent(
            event_id=event_id or uuid4(),
            operation=operation,
            aggregate_revision=revision,
            entity_id=entity_id,
            entity_type=entity_type,
            entity_key=entity_key,
            source_uuid=source_uuid,
            project_key=project_key,
            display_label=display_label,
            lifecycle=lifecycle,
        ),
        owner_id=owner_id,
        lease_generation=generation,
        claim_version=claim_version,
        leased_until=datetime.now(UTC) + timedelta(seconds=30),
    )


def _relation_claim(
    *,
    generation: int,
    revision: int,
    operation: str = "upsert_relation",
    claim_version: int = 1,
    event_id: UUID | None = None,
    relation_id: UUID | None = None,
    source_type: str = "decision",
    source_key: str | None = None,
    target_type: str = "learning",
    target_key: str | None = None,
    relation_type: str = "RELATED_TO",
    relation_lifecycle: str | None = None,
    properties: dict[str, Any] | None = None,
) -> _ProjectionClaim:
    source_key = source_key or str(uuid4())
    target_key = target_key or str(uuid4())
    relation_lifecycle = relation_lifecycle or (
        "deleted" if operation == "delete_relation" else "active"
    )
    return _ProjectionClaim(
        event=_ClaimedEvent(
            event_id=event_id or uuid4(),
            operation=operation,
            aggregate_revision=revision,
            relation_id=relation_id or uuid4(),
            source_type=source_type,
            source_key=source_key,
            target_type=target_type,
            target_key=target_key,
            relation_type=relation_type,
            relation_lifecycle=relation_lifecycle,
            properties=properties or {},
        ),
        owner_id="worker-a",
        lease_generation=generation,
        claim_version=claim_version,
        leased_until=datetime.now(UTC) + timedelta(seconds=30),
    )


def _transaction_text(transaction: _ExplicitTransaction) -> str:
    return "\n".join(query for query, _params in transaction.queries)


@pytest.mark.asyncio
async def test_activation_barrier_is_monotone_and_idempotent() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)

    assert await writer.activate_generation(_leadership(1)) == ProjectionActivation(True, 1)
    assert await writer.activate_generation(_leadership(1)) == ProjectionActivation(True, 1)
    assert await writer.activate_generation(
        _leadership(0, owner="worker-stale")
    ) == ProjectionActivation(False, 1)

    assert driver.state.fence_generation == 1
    assert driver.state.implicit_runs == 0
    assert [tx.committed for tx in driver.state.transactions] == [True, True, False]
    assert driver.state.transactions[-1].rolled_back is True


@pytest.mark.asyncio
async def test_activation_query_only_advances_one_unarmed_generation() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)

    await writer.activate_generation(_leadership(1, armed=True))

    transaction = driver.state.transactions[-1]
    query, params = transaction.queries[0]
    assert params["allow_advance"] is False
    assert "OPTIONAL MATCH (fence:BrainProjectionFence" in query
    assert "MERGE (fence:BrainProjectionFence" not in query
    assert "fence.recovery_id IS NULL" in query
    assert "fence.generation = $generation - 1" in query
    assert "fence.generation < $generation" not in query


@pytest.mark.asyncio
async def test_activation_double_rejects_gap_and_armed_predecessor() -> None:
    gap_driver = _Driver()
    gap_driver.state.auto_seed_predecessor = False
    gap_writer = Neo4jGraphProjectionWriter(gap_driver, timeout=0.2)

    assert await gap_writer.activate_generation(_leadership(2)) == ProjectionActivation(False, 0)

    armed_driver = _Driver()
    armed_writer = Neo4jGraphProjectionWriter(armed_driver, timeout=0.2)
    assert await armed_writer.activate_generation(
        _leadership(1, armed=True)
    ) == ProjectionActivation(False, 0)


@pytest.mark.asyncio
async def test_activation_cancellation_rolls_back_explicit_transaction() -> None:
    driver = _Driver()
    driver.state.cancel_on_run = True
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)

    with pytest.raises(asyncio.CancelledError):
        await writer.activate_generation(_leadership(7))

    assert driver.state.transactions[-1].committed is False
    assert driver.state.transactions[-1].cancelled is True
    assert driver.state.transactions[-1].rolled_back is False


@pytest.mark.asyncio
async def test_apply_cancellation_rolls_back_explicit_transaction() -> None:
    driver = _Driver()
    driver.state.cancel_on_run = True
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)

    with pytest.raises(asyncio.CancelledError):
        await writer.apply(_entity_claim(generation=7, revision=1))

    assert driver.state.transactions[-1].committed is False
    assert driver.state.transactions[-1].cancelled is True
    assert driver.state.transactions[-1].rolled_back is False


@pytest.mark.asyncio
async def test_activation_commit_failure_preserves_original_exception() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    driver.state.commit_error = RuntimeError("activation commit uncertain")

    with pytest.raises(RuntimeError, match="activation commit uncertain"):
        await writer.activate_generation(_leadership(1))

    transaction = driver.state.transactions[-1]
    assert transaction.closed() is True
    assert transaction.rolled_back is False


@pytest.mark.asyncio
async def test_apply_commit_failure_preserves_original_exception() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(1))
    driver.state.commit_error = RuntimeError("apply commit uncertain")

    with pytest.raises(RuntimeError, match="apply commit uncertain"):
        await writer.apply(_entity_claim(generation=1, revision=1))

    transaction = driver.state.transactions[-1]
    assert transaction.closed() is True
    assert transaction.rolled_back is False


@pytest.mark.asyncio
async def test_rollback_cancellation_supersedes_the_primary_error() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    driver.state.run_error = RuntimeError("primary Neo4j error")
    driver.state.cancel_on_rollback = True

    with pytest.raises(asyncio.CancelledError):
        await writer.activate_generation(_leadership(1))

    transaction = driver.state.transactions[-1]
    assert transaction.cancelled is True
    assert transaction.rolled_back is False


@pytest.mark.asyncio
async def test_same_generation_activation_cannot_replace_the_fence_owner() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)

    assert await writer.activate_generation(
        _leadership(8, owner="worker-a")
    ) == ProjectionActivation(True, 8)
    assert await writer.activate_generation(
        _leadership(8, owner="worker-b")
    ) == ProjectionActivation(False, 8)

    assert driver.state.fence_generation == 8
    assert driver.state.fence_owner_id == "worker-a"
    assert driver.state.transactions[-1].committed is False
    assert driver.state.transactions[-1].rolled_back is True


@pytest.mark.asyncio
async def test_same_generation_claim_from_another_owner_is_rejected() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(9, owner="worker-a"))
    claim = _entity_claim(
        generation=9,
        revision=1,
        owner_id="worker-b",
    )

    assert await writer.apply(claim) is ProjectionOutcome.STALE_GENERATION

    assert driver.state.entity_keys == set()
    assert driver.state.cursors == {}
    assert driver.state.transactions[-1].committed is False
    assert driver.state.transactions[-1].rolled_back is True


@pytest.mark.asyncio
async def test_apply_locks_and_checks_the_same_fence_in_one_explicit_transaction() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(11))
    claim = _entity_claim(generation=11, revision=1)

    outcome = await writer.apply(claim)

    assert outcome is ProjectionOutcome.APPLIED
    projection_tx = driver.state.transactions[-1]
    text = _transaction_text(projection_tx)
    params = projection_tx.parameters
    assert projection_tx.committed is True
    assert driver.state.implicit_runs == 0
    assert "BrainProjectionFence" in text
    assert "BrainProjectionCursor" in text
    assert "fence.recovery_id IS NULL" in text
    assert "_lock" in text or "_dummy" in text
    assert text.index("BrainProjectionFence") < text.index("BrainProjectionCursor")
    assert params["lease_generation"] == 11
    assert params["aggregate_revision"] == 1
    assert params["claim_version"] == 1
    assert params["aggregate_key"] == f"entity:{claim.event.entity_id}"
    assert params["event_id"] == str(claim.event.event_id)


@pytest.mark.asyncio
async def test_stale_generation_cannot_mutate_or_commit() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(4, owner="worker-b"))
    stale = _entity_claim(generation=3, revision=1)

    outcome = await writer.apply(stale)

    assert outcome is ProjectionOutcome.STALE_GENERATION
    assert driver.state.entity_keys == set()
    assert driver.state.cursors == {}
    stale_tx = driver.state.transactions[-1]
    assert stale_tx.committed is False
    assert stale_tx.rolled_back is True


@pytest.mark.asyncio
async def test_entity_cursor_tombstone_blocks_stale_resurrection() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(20))
    entity_id = uuid4()
    created = _entity_claim(generation=20, revision=1, entity_id=entity_id)
    deleted = _entity_claim(
        generation=20,
        revision=2,
        operation="delete_entity",
        entity_id=entity_id,
    )
    stale_recreate = _entity_claim(
        generation=20,
        revision=1,
        claim_version=99,
        entity_id=entity_id,
    )

    assert await writer.apply(created) is ProjectionOutcome.APPLIED
    assert await writer.apply(deleted) is ProjectionOutcome.APPLIED
    assert await writer.apply(stale_recreate) is ProjectionOutcome.SUPERSEDED

    aggregate_key = f"entity:{entity_id}"
    assert aggregate_key not in driver.state.entity_keys
    assert driver.state.cursors[aggregate_key].revision == 2
    assert driver.state.cursors[aggregate_key].operation == "delete_entity"


@pytest.mark.asyncio
async def test_relation_cursor_tombstone_blocks_stale_recreation() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(21))
    relation_id = uuid4()
    created = _relation_claim(generation=21, revision=1, relation_id=relation_id)
    deleted = _relation_claim(
        generation=21,
        revision=2,
        operation="delete_relation",
        relation_id=relation_id,
    )
    stale_recreate = _relation_claim(
        generation=21,
        revision=1,
        claim_version=99,
        relation_id=relation_id,
    )

    assert await writer.apply(created) is ProjectionOutcome.APPLIED
    assert await writer.apply(deleted) is ProjectionOutcome.APPLIED
    assert await writer.apply(stale_recreate) is ProjectionOutcome.SUPERSEDED

    aggregate_key = f"relation:{relation_id}"
    assert aggregate_key not in driver.state.relation_keys
    assert driver.state.cursors[aggregate_key].revision == 2
    assert driver.state.cursors[aggregate_key].operation == "delete_relation"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["entity", "relation"])
async def test_exact_event_replay_is_idempotent(kind: str) -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(30))
    claim = (
        _entity_claim(generation=30, revision=3)
        if kind == "entity"
        else _relation_claim(generation=30, revision=3)
    )

    first = await writer.apply(claim)
    replay = await writer.apply(claim)

    assert first is ProjectionOutcome.APPLIED
    assert replay is ProjectionOutcome.ALREADY_CURRENT
    aggregate_key = (
        f"entity:{claim.event.entity_id}"
        if kind == "entity"
        else f"relation:{claim.event.relation_id}"
    )
    assert driver.state.cursors[aggregate_key] == _Cursor(
        revision=3,
        claim_version=1,
        event_id=claim.event.event_id,
        operation=claim.event.operation,
    )
    projected = driver.state.entity_keys if kind == "entity" else driver.state.relation_keys
    assert projected == {aggregate_key}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_type", "label", "label_property"),
    [
        ("decision", "Decision", "title"),
        ("feature", "Feature", "name"),
        ("plan", "Plan", "title"),
    ],
)
async def test_knowledge_entity_upsert_and_delete_route_by_type(
    entity_type: str,
    label: str,
    label_property: str,
) -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(40))
    entity_id = uuid4()
    upsert = _entity_claim(
        generation=40,
        revision=1,
        entity_id=entity_id,
        entity_type=entity_type,
    )

    assert await writer.apply(upsert) is ProjectionOutcome.APPLIED

    upsert_text = _transaction_text(driver.state.transactions[-1])
    assert f"MERGE (node:{label} {{id: $graph_id}})" in upsert_text
    assert f"node.{label_property} = $display_label" in upsert_text
    delete = _entity_claim(
        generation=40,
        revision=2,
        operation="delete_entity",
        entity_id=entity_id,
        entity_type=entity_type,
    )

    assert await writer.apply(delete) is ProjectionOutcome.APPLIED

    delete_text = _transaction_text(driver.state.transactions[-1])
    assert f"OPTIONAL MATCH (node:{label} {{id: $graph_id}})" in delete_text
    assert "DETACH DELETE node" in delete_text


@pytest.mark.asyncio
async def test_project_entity_upsert_and_delete_use_the_stable_project_key() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(41))
    project_id = uuid4()
    upsert = _entity_claim(
        generation=41,
        revision=1,
        entity_id=project_id,
        entity_type="project",
        entity_key="brain-v42",
        display_label="Brain v42",
    )

    assert await writer.apply(upsert) is ProjectionOutcome.APPLIED

    upsert_tx = driver.state.transactions[-1]
    assert "MERGE (node:Project {project_key: $entity_key})" in _transaction_text(upsert_tx)
    assert upsert_tx.parameters["entity_key"] == "brain-v42"
    assert upsert_tx.parameters["graph_id"] == str(project_id)
    delete = _entity_claim(
        generation=41,
        revision=2,
        operation="delete_entity",
        entity_id=project_id,
        entity_type="project",
        entity_key="brain-v42",
    )

    assert await writer.apply(delete) is ProjectionOutcome.APPLIED

    delete_text = _transaction_text(driver.state.transactions[-1])
    assert "OPTIONAL MATCH (node:Project {project_key: $entity_key})" in delete_text
    assert "DETACH DELETE node" in delete_text


@pytest.mark.asyncio
async def test_domain_entity_upserts_but_delete_is_an_invalid_canonical_event() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(42))
    domain_id = uuid4()
    upsert = _entity_claim(
        generation=42,
        revision=1,
        entity_id=domain_id,
        entity_type="domain",
        entity_key="data",
        source_uuid=None,
        project_key=None,
    )

    assert await writer.apply(upsert) is ProjectionOutcome.APPLIED
    assert "MERGE (node:Domain {name: $entity_key})" in _transaction_text(
        driver.state.transactions[-1]
    )
    before_delete = len(driver.state.transactions)
    delete = _entity_claim(
        generation=42,
        revision=2,
        operation="delete_entity",
        entity_id=domain_id,
        entity_type="domain",
        entity_key="data",
        source_uuid=None,
        project_key=None,
    )

    assert await writer.apply(delete) is ProjectionOutcome.INVALID_EVENT
    assert len(driver.state.transactions) == before_delete


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relation_type", "expected_pattern"),
    [
        ("SUPERSEDES", "-[relation:SUPERSEDES]->"),
        ("RELATED_TO", "-[relation:RELATED_TO]-"),
    ],
)
async def test_generic_relation_routing_preserves_direction_semantics(
    relation_type: str,
    expected_pattern: str,
) -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(43))
    claim = _relation_claim(
        generation=43,
        revision=1,
        source_type="decision",
        target_type="decision" if relation_type == "SUPERSEDES" else "learning",
        relation_type=relation_type,
    )

    assert await writer.apply(claim) is ProjectionOutcome.APPLIED

    text = _transaction_text(driver.state.transactions[-1])
    assert "MATCH (source {id: $source_key})" in text
    assert "MATCH (target {id: $target_key})" in text
    assert expected_pattern in text


@pytest.mark.asyncio
async def test_project_hierarchy_upsert_and_delete_route_by_business_key() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(44))
    relation_id = uuid4()
    upsert = _relation_claim(
        generation=44,
        revision=1,
        relation_id=relation_id,
        source_type="project",
        source_key="brain-v42",
        target_type="project",
        target_key="red-data",
        relation_type="CONTAINS",
    )

    assert await writer.apply(upsert) is ProjectionOutcome.APPLIED

    text = _transaction_text(driver.state.transactions[-1])
    assert "MATCH (source:Project {project_key: $source_key})" in text
    assert "MATCH (target:Project {project_key: $target_key})" in text
    assert "MERGE (source)-[relation:CONTAINS]->(target)" in text
    delete = _relation_claim(
        generation=44,
        revision=2,
        operation="delete_relation",
        relation_id=relation_id,
        source_type="project",
        source_key="brain-v42",
        target_type="project",
        target_key="red-data",
        relation_type="CONTAINS",
    )

    assert await writer.apply(delete) is ProjectionOutcome.APPLIED

    delete_text = _transaction_text(driver.state.transactions[-1])
    assert "OPTIONAL MATCH (source:Project {project_key: $source_key})" in delete_text
    assert "DELETE relation" in delete_text


@pytest.mark.asyncio
async def test_project_membership_upsert_and_delete_use_belongs_to() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(45))
    relation_id = uuid4()
    entity_id = str(uuid4())
    upsert = _relation_claim(
        generation=45,
        revision=1,
        relation_id=relation_id,
        source_type="feature",
        source_key=entity_id,
        target_type="project",
        target_key="brain-v42",
        relation_type="BELONGS_TO",
    )

    assert await writer.apply(upsert) is ProjectionOutcome.APPLIED
    text = _transaction_text(driver.state.transactions[-1])
    assert "MATCH (source {id: $source_key})" in text
    assert "MATCH (target:Project {project_key: $target_key})" in text
    assert "MERGE (source)-[relation:BELONGS_TO]->(target)" in text
    delete = _relation_claim(
        generation=45,
        revision=2,
        operation="delete_relation",
        relation_id=relation_id,
        source_type="feature",
        source_key=entity_id,
        target_type="project",
        target_key="brain-v42",
        relation_type="BELONGS_TO",
    )

    assert await writer.apply(delete) is ProjectionOutcome.APPLIED
    assert "DELETE relation" in _transaction_text(driver.state.transactions[-1])


@pytest.mark.asyncio
async def test_domain_membership_upsert_is_atomic_and_delete_is_keyed() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(46))
    relation_id = uuid4()
    entity_id = str(uuid4())
    upsert = _relation_claim(
        generation=46,
        revision=1,
        relation_id=relation_id,
        source_type="decision",
        source_key=entity_id,
        target_type="domain",
        target_key="infra",
        relation_type="BELONGS_TO_DOMAIN",
    )
    before_upsert = len(driver.state.transactions)

    assert await writer.apply(upsert) is ProjectionOutcome.APPLIED

    assert len(driver.state.transactions) == before_upsert + 1
    upsert_tx = driver.state.transactions[-1]
    text = _transaction_text(upsert_tx)
    assert "BrainProjectionFence" in text
    assert "MERGE (target:Domain {name: $target_key})" in text
    assert "MATCH (source {id: $source_key})" in text
    assert "MERGE (source)-[relation:BELONGS_TO_DOMAIN]->(target)" in text
    assert "BrainProjectionCursor" in text
    assert upsert_tx.committed is True
    assert driver.state.domain_keys == {"infra"}
    delete = _relation_claim(
        generation=46,
        revision=2,
        operation="delete_relation",
        relation_id=relation_id,
        source_type="decision",
        source_key=entity_id,
        target_type="domain",
        target_key="infra",
        relation_type="BELONGS_TO_DOMAIN",
    )

    assert await writer.apply(delete) is ProjectionOutcome.APPLIED

    delete_text = _transaction_text(driver.state.transactions[-1])
    assert "MATCH (target:Domain {name: $target_key})" in delete_text
    assert "DELETE relation" in delete_text


@pytest.mark.asyncio
async def test_domain_membership_missing_source_rolls_back_the_atomic_mutation() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(53))
    claim = _relation_claim(
        generation=53,
        revision=1,
        source_type="decision",
        target_type="domain",
        target_key="infra",
        relation_type="BELONGS_TO_DOMAIN",
    )
    aggregate_key = f"relation:{claim.event.relation_id}"
    driver.state.missing_anchor_keys.add(aggregate_key)
    before_apply = len(driver.state.transactions)

    assert await writer.apply(claim) is ProjectionOutcome.MISSING_NODE

    assert len(driver.state.transactions) == before_apply + 1
    transaction = driver.state.transactions[-1]
    text = _transaction_text(transaction)
    assert "MERGE (target:Domain {name: $target_key})" in text
    assert "MERGE (source)-[relation:BELONGS_TO_DOMAIN]->(target)" in text
    assert len(transaction.queries) == 2
    assert transaction.committed is False
    assert transaction.rolled_back is True
    assert driver.state.domain_keys == set()
    assert aggregate_key not in driver.state.relation_keys
    assert aggregate_key not in driver.state.cursors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "stale_operation", "current_lifecycle", "expected_operation"),
    [
        ("entity", "delete_entity", "active", "upsert_entity"),
        ("entity", "upsert_entity", "deleted", "delete_entity"),
        ("relation", "delete_relation", "active", "upsert_relation"),
        ("relation", "upsert_relation", "deleted", "delete_relation"),
    ],
)
async def test_current_lifecycle_overrides_stale_outbox_operation(
    kind: str,
    stale_operation: str,
    current_lifecycle: str,
    expected_operation: str,
) -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(47))
    if kind == "entity":
        claim = _entity_claim(
            generation=47,
            revision=1,
            operation=stale_operation,
            lifecycle=current_lifecycle,
        )
    else:
        claim = _relation_claim(
            generation=47,
            revision=1,
            operation=stale_operation,
            relation_lifecycle=current_lifecycle,
        )

    assert await writer.apply(claim) is ProjectionOutcome.APPLIED
    assert driver.state.transactions[-1].parameters["operation"] == expected_operation


@pytest.mark.asyncio
async def test_missing_anchor_rolls_back_without_advancing_cursor() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(48))
    claim = _relation_claim(
        generation=48,
        revision=1,
        source_type="decision",
        target_type="decision",
        relation_type="SUPERSEDES",
    )
    aggregate_key = f"relation:{claim.event.relation_id}"
    driver.state.missing_anchor_keys.add(aggregate_key)

    assert await writer.apply(claim) is ProjectionOutcome.MISSING_NODE

    transaction = driver.state.transactions[-1]
    assert transaction.committed is False
    assert transaction.rolled_back is True
    assert aggregate_key not in driver.state.cursors
    assert aggregate_key not in driver.state.relation_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["missing_record", "missing_anchors"])
async def test_missing_mutation_evidence_fails_closed(failure_mode: str) -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(54))
    if failure_mode == "missing_record":
        driver.state.missing_mutation_record = True
    else:
        driver.state.missing_mutation_anchors = True
    claim = _entity_claim(generation=54, revision=1)

    assert await writer.apply(claim) is ProjectionOutcome.ERROR

    aggregate_key = f"entity:{claim.event.entity_id}"
    assert aggregate_key not in driver.state.entity_keys
    assert aggregate_key not in driver.state.cursors
    assert driver.state.transactions[-1].committed is False
    assert driver.state.transactions[-1].rolled_back is True


@pytest.mark.asyncio
async def test_same_revision_with_another_event_id_is_a_conflict() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(49))
    entity_id = uuid4()
    first = _entity_claim(generation=49, revision=1, entity_id=entity_id)
    conflict = _entity_claim(generation=49, revision=1, entity_id=entity_id)

    assert await writer.apply(first) is ProjectionOutcome.APPLIED
    assert await writer.apply(conflict) is ProjectionOutcome.CONFLICT

    aggregate_key = f"entity:{entity_id}"
    assert driver.state.cursors[aggregate_key].event_id == first.event.event_id
    assert driver.state.transactions[-1].rolled_back is True


@pytest.mark.asyncio
async def test_higher_claim_version_replay_advances_only_the_cursor_claim() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(50))
    entity_id = uuid4()
    event_id = uuid4()
    first = _entity_claim(
        generation=50,
        revision=1,
        claim_version=1,
        event_id=event_id,
        entity_id=entity_id,
    )
    reclaimed = _entity_claim(
        generation=50,
        revision=1,
        claim_version=2,
        event_id=event_id,
        entity_id=entity_id,
    )

    assert await writer.apply(first) is ProjectionOutcome.APPLIED
    assert await writer.apply(reclaimed) is ProjectionOutcome.ALREADY_CURRENT

    cursor = driver.state.cursors[f"entity:{entity_id}"]
    assert cursor.revision == 1
    assert cursor.claim_version == 2
    assert cursor.event_id == event_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim",
    [
        _entity_claim(generation=51, revision=1, entity_type="unsupported"),
        _entity_claim(
            generation=51,
            revision=1,
            operation="delete_entity",
            entity_type="domain",
            entity_key="data",
            project_key=None,
        ),
        _relation_claim(generation=51, revision=1, relation_type="NOT_CANONICAL"),
        _relation_claim(
            generation=51,
            revision=1,
            source_type="decision",
            target_type="project",
            target_key="brain-v42",
            relation_type="RELATED_TO",
        ),
        _relation_claim(
            generation=51,
            revision=1,
            source_type="decision",
            target_type="learning",
            relation_type="SUPERSEDES",
        ),
        _relation_claim(
            generation=51,
            revision=1,
            source_type="mystery",
            target_type="learning",
            relation_type="RELATED_TO",
        ),
    ],
    ids=[
        "unsupported-entity-type",
        "domain-delete",
        "unsupported-relation-type",
        "invalid-project-membership",
        "supersedes-mixed-types",
        "unknown-source-type",
    ],
)
async def test_invalid_entity_and_relation_shapes_are_rejected_before_neo4j(
    claim: _ProjectionClaim,
) -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(51))
    before_apply = len(driver.state.transactions)

    assert await writer.apply(claim) is ProjectionOutcome.INVALID_EVENT
    assert len(driver.state.transactions) == before_apply


@pytest.mark.asyncio
async def test_relation_projection_parameters_are_secret_safe_and_allowlisted() -> None:
    driver = _Driver()
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)
    await writer.activate_generation(_leadership(52))
    safe_properties = {
        "similarity": 0.82,
        "score": 0.76,
        "threshold": 0.7,
        "model": "embedding-v2",
        "model_version": "2026-07",
        "method": "cosine",
    }
    claim = _relation_claim(
        generation=52,
        revision=1,
        source_type="decision",
        target_type="learning",
        relation_type="RELATED_TO",
        properties={
            **safe_properties,
            "secret": "must-not-cross-neo4j-boundary",
            "content": "full private payload",
            "weight": 0.99,
        },
    )

    assert await writer.apply(claim) is ProjectionOutcome.APPLIED

    params = driver.state.transactions[-1].parameters
    assert params["properties"] == safe_properties
    assert set(params) == {
        "aggregate_key",
        "aggregate_revision",
        "claim_version",
        "event_id",
        "lease_generation",
        "owner_id",
        "operation",
        "source_key",
        "target_key",
        "properties",
    }
    assert "must-not-cross-neo4j-boundary" not in repr(params)
    assert "full private payload" not in repr(params)
